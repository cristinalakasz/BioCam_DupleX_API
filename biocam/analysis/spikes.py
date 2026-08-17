"""Layer 3 - finding spikes in a stream, without seeing the future.

Threshold detection on a high-passed signal, which is the standard approach
for extracellular arrays and the one everything else builds on. Three parts,
each of which has a way of being quietly wrong:

**The threshold has to come from the noise, not from a number someone typed.**
Electrode impedances differ by an order of magnitude across an array, so a
fixed microvolt threshold detects everything on one electrode and nothing on
its neighbour. The estimator here is Quiroga's: `sigma = median(|x|) /
0.6745`, which is a median rather than a standard deviation precisely so that
the spikes themselves do not inflate the estimate of the noise they are
supposed to stand out from.

**It has to be causal.** A detector that needs the whole recording is fine
afterwards and useless for a closed loop. Nothing here looks ahead, and the
state carries across blocks, so feeding a recording in one piece or in a
thousand packets finds the same spikes - including one that straddles a
packet boundary, which is the case a naive per-block detector silently
misses nine thousand times a second.

Precisely: the same spikes, with frames agreeing to within a sample or two.
Not bit-identical, and the reason is worth knowing rather than filing as
noise - the threshold comes from a median over whatever window the caller
supplies, so a 37-frame packet and a 4096-frame chunk produce slightly
different noise estimates and therefore cross the line a sample apart. The
filter itself *is* bit-identical across any blocking; it is the estimator
that is not, by construction.

**A spike is one event, not a run of samples below a line.** A refractory
period per channel collapses each crossing into a single detection. Without
it a 1 ms spike at 18.5 kHz reports about eighteen.

What this is not: it is not spike *sorting* - it says something crossed, not
which neuron it came from - and its amplitude is the value at the crossing,
not the trough. See `SpikeDetector.detect`.
"""

from dataclasses import dataclass

import numpy as np

# median(|x|) = 0.6745 * sigma for zero-mean Gaussian noise. Dividing by it
# turns a robust median into a standard-deviation estimate.
MAD_TO_SIGMA = 1.0 / 0.6745

# Spikes are predominantly negative-going in extracellular recordings, so the
# default threshold is below the baseline. Five sigma is conservative; four is
# common in the literature and finds more, with more false positives.
DEFAULT_THRESHOLD_SIGMA = 5.0

# A neuron cannot fire twice within its own refractory period, and one spike
# is about a millisecond wide, so anything closer is the same event.
DEFAULT_REFRACTORY_MS = 1.0

# Noise to observe before any detection is allowed, in SECONDS. Until the
# estimator has settled, a threshold derived from it is arbitrary - and a
# detector that reports arbitrary spikes for its first second is worse than
# one that reports none, because the first second looks like data.
#
# Seconds, not blocks. A block is however many frames a packet happened to
# carry, so counting blocks made the warm-up 40 ms at a 2 ms acquisition
# period and 4.4 s when the same data was analysed in 4096-frame chunks - and
# in the second case a 2 s recording produced zero spikes and looked like a
# quiet preparation. Anything a caller can change by choosing a chunk size is
# not a property of the signal.
DEFAULT_WARMUP_SECONDS = 0.05

# Time constant of the noise estimate. Also in seconds, and for the same
# reason: a per-block smoothing factor makes the estimator's memory depend on
# the packet size.
DEFAULT_NOISE_TAU_SECONDS = 0.5


@dataclass(frozen=True)
class Spike:
    """One threshold crossing.

    `frame` is absolute within the recording, counted from the first frame the
    detector ever saw, so a spike is locatable in the `.raw` file and against
    the stimulus log without any further bookkeeping.
    """

    frame: int
    channel: int
    amplitude: float          # filtered value at the crossing
    threshold: float          # what it had to beat, for provenance

    @property
    def sigma(self) -> float:
        """How many noise sigmas the crossing was, for comparing channels."""
        if self.threshold == 0:
            return 0.0
        return abs(self.amplitude / self.threshold) * DEFAULT_THRESHOLD_SIGMA


class NoiseEstimator:
    """Per-channel noise level, updated as blocks arrive.

    Robust by construction: the median of |x| barely moves when a few samples
    in a block are spikes, where a standard deviation would rise and quietly
    raise the threshold until the spikes stopped being detected - a failure
    that looks exactly like a preparation going quiet.
    """

    def __init__(self, n_channels: int, frame_rate_hz: float, *,
                 tau_seconds: float = DEFAULT_NOISE_TAU_SECONDS,
                 warmup_seconds: float = DEFAULT_WARMUP_SECONDS):
        if frame_rate_hz <= 0:
            raise ValueError(f"frame_rate_hz must be positive, got {frame_rate_hz}")
        if tau_seconds <= 0:
            raise ValueError(f"tau_seconds must be positive, got {tau_seconds}")
        self.n_channels = n_channels
        self.frame_rate_hz = frame_rate_hz
        self._tau_frames = tau_seconds * frame_rate_hz
        self._warmup_frames = max(1, int(round(warmup_seconds * frame_rate_hz)))
        self.sigma = np.zeros(n_channels, dtype=np.float64)
        self.blocks = 0
        self.frames = 0

    @property
    def ready(self) -> bool:
        return self.frames >= self._warmup_frames

    @property
    def warmup_frames(self) -> int:
        return self._warmup_frames

    def update(self, block) -> None:
        """Fold one (frames, channels) block into the estimate."""
        block = np.asarray(block, dtype=np.float64)
        if block.size == 0:
            return
        estimate = np.median(np.abs(block), axis=0) * MAD_TO_SIGMA
        if self.blocks == 0:
            self.sigma[:] = estimate
        else:
            # An exponential moving average whose time constant is in
            # seconds: the weight of a block is set by how many frames it
            # carried, so the estimator has the same memory whether it is fed
            # 37-frame packets or 4096-frame chunks. A fixed per-block weight
            # would make its memory a hundred times longer in the second case.
            alpha = 1.0 - np.exp(-block.shape[0] / self._tau_frames)
            self.sigma *= (1.0 - alpha)
            self.sigma += alpha * estimate
        self.blocks += 1
        self.frames += block.shape[0]

    def thresholds(self, sigmas: float) -> np.ndarray:
        """The negative-going threshold per channel."""
        return -abs(sigmas) * self.sigma


class SpikeDetector:
    """Streaming threshold detection over a fixed set of channels.

    Deliberately over a *subset*. Detecting on all 4096 channels means
    filtering 4096 channels for every frame, which is not affordable on the
    thread that drains the packet queue - see `biocam/control.py` for the same
    argument about stimulation. Choose the channels the experiment is about.
    """

    def __init__(self, n_channels: int, frame_rate_hz: float, *,
                 cutoff_hz: float = 300.0,
                 threshold_sigmas: float = DEFAULT_THRESHOLD_SIGMA,
                 refractory_ms: float = DEFAULT_REFRACTORY_MS,
                 warmup_seconds: float = DEFAULT_WARMUP_SECONDS,
                 noise_tau_seconds: float = DEFAULT_NOISE_TAU_SECONDS):
        from biocam.analysis.filters import HighPass

        if n_channels < 1:
            raise ValueError(f"n_channels must be at least 1, got {n_channels}")
        if threshold_sigmas <= 0:
            raise ValueError(
                f"threshold_sigmas must be positive, got {threshold_sigmas}"
            )
        self.n_channels = n_channels
        self.frame_rate_hz = frame_rate_hz
        self.threshold_sigmas = threshold_sigmas
        self.refractory_frames = max(
            1, int(round(refractory_ms * 1e-3 * frame_rate_hz))
        )
        self.filter = HighPass(n_channels, frame_rate_hz, cutoff_hz=cutoff_hz)
        self.noise = NoiseEstimator(
            n_channels, frame_rate_hz,
            tau_seconds=noise_tau_seconds, warmup_seconds=warmup_seconds,
        )

        self._frames_seen = 0
        self._warmed = False
        # The last filtered sample of the previous block, per channel. Without
        # it a spike whose crossing falls exactly on a block boundary is
        # invisible - and at ~500 packets a second, boundaries are where a
        # meaningful fraction of everything happens.
        self._previous = np.zeros(n_channels, dtype=np.float64)
        # Frame index of the last detection per channel, so the refractory
        # period spans block boundaries too.
        self._last_spike = np.full(n_channels, -10 ** 15, dtype=np.int64)
        self.spikes_detected = 0

    @property
    def ready(self) -> bool:
        """Whether the noise estimate has settled enough to detect."""
        return self.noise.ready

    def detect(self, block) -> list:
        """Filter a block, update the noise estimate, and return its spikes.

        A spike is reported at the sample where the signal first falls below
        the threshold, with the amplitude *at that sample*. That is a
        deliberate choice for latency: waiting for the trough would mean
        holding each detection for the length of the refractory period, which
        is the whole latency budget a closed loop has. The trough is deeper
        than the reported amplitude, typically by a few tens of percent.

        Returns an empty list until the noise estimator has warmed up.
        """
        block = np.asarray(block, dtype=np.float64)
        if block.ndim != 2 or block.shape[1] != self.n_channels:
            raise ValueError(
                f"expected a (frames, {self.n_channels}) block, got shape "
                f"{block.shape}"
            )
        if block.shape[0] == 0:
            return []

        if not self._warmed:
            # Settle the filter on the first sample rather than letting it
            # ring down from zero. A recording sitting at 2048 counts would
            # otherwise begin with a step response tens of milliseconds long,
            # every sample of which is a large deflection this would report.
            self.filter.warm_up(block[0])
            self._warmed = True

        filtered = self.filter.process(block)
        self.noise.update(filtered)
        if not self.noise.ready:
            self._previous = filtered[-1].copy()
            self._frames_seen += block.shape[0]
            return []

        thresholds = self.noise.thresholds(self.threshold_sigmas)
        spikes = self._crossings(filtered, thresholds)
        self._previous = filtered[-1].copy()
        self._frames_seen += block.shape[0]
        self.spikes_detected += len(spikes)
        return spikes

    def _crossings(self, filtered, thresholds) -> list:
        """Falling crossings, one per event, respecting the refractory."""
        n_frames = filtered.shape[0]
        # A crossing is a sample below the threshold whose predecessor was
        # not. Comparing against the carried-over previous sample is what
        # makes a boundary-straddling spike visible.
        below = filtered <= thresholds
        previous_below = np.empty_like(below)
        previous_below[0] = self._previous <= thresholds
        previous_below[1:] = below[:-1]
        crossings = below & ~previous_below
        if not crossings.any():
            return []

        spikes = []
        frames, channels = np.nonzero(crossings)
        base = self._frames_seen
        for frame_offset, channel in zip(frames.tolist(), channels.tolist()):
            absolute = base + frame_offset
            if absolute - self._last_spike[channel] < self.refractory_frames:
                # Same event, still ringing. Not a second spike.
                continue
            self._last_spike[channel] = absolute
            spikes.append(Spike(
                frame=absolute,
                channel=channel,
                amplitude=float(filtered[frame_offset, channel]),
                threshold=float(thresholds[channel]),
            ))
        # np.nonzero walks row-major, so this is already in frame order, and
        # in channel order within a frame. Callers that write a spike train
        # to disk depend on that.
        return spikes

    def rates_hz(self, elapsed_frames: int = None) -> float:
        """Mean detection rate across the recording so far, in Hz."""
        frames = elapsed_frames if elapsed_frames is not None else self._frames_seen
        if frames <= 0:
            return 0.0
        return self.spikes_detected / (frames / self.frame_rate_hz)


def detect_all(data, frame_rate_hz: float, *, block_frames: int = 512, **kwargs):
    """Run a whole recording through the streaming detector.

    For analysis after the fact. Chunked rather than done in one pass so that
    the offline path exercises the same block-boundary handling the online one
    uses - if the two ever disagree, it is this that is wrong, and it should
    be caught here rather than on the instrument.
    """
    data = np.asarray(data)
    if data.ndim != 2:
        raise ValueError(f"expected (frames, channels), got shape {data.shape}")
    detector = SpikeDetector(data.shape[1], frame_rate_hz, **kwargs)
    spikes = []
    for start in range(0, data.shape[0], block_frames):
        spikes.extend(detector.detect(data[start:start + block_frames]))
    return spikes, detector
