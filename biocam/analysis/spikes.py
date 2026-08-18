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

from collections import deque
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

# How much of the waveform around a crossing to keep, for sorting. A spike is
# roughly a millisecond wide with a short rising edge before the trough, so
# these capture the shape without carrying much noise on either side.
DEFAULT_WAVEFORM_PRE_MS = 0.5
DEFAULT_WAVEFORM_POST_MS = 1.5

# Completed waveforms held before a caller takes them. Bounded by the detector
# itself, because nothing outside it is guaranteed to drain: the UI does, a
# headless script written the obvious way does not, and an unbounded list on
# the acquisition thread is not a caller's mistake to make.
DEFAULT_MAX_READY = 4096

# Returned instead of a fresh list when nothing is waiting, which is most
# packets. Immutable, so a caller cannot append to it and be surprised.
_NOTHING_READY = ()


@dataclass(frozen=True, slots=True)
class Spike:
    """One threshold crossing.

    `frame` is absolute within the recording, counted from the first frame the
    detector ever saw, so a spike is locatable in the `.raw` file and against
    the stimulus log without any further bookkeeping - as long as nothing
    skipped frames without saying so. See `SpikeDetector.skip_frames`.

    `slots=True` is not a micro-optimisation. Thousands of these are retained
    for sorting, and without slots each carries a `__dict__` that every full
    garbage collection has to walk. This repo has already measured a
    `gc.collect(1)` at 31.8 ms stalling an unrelated thread - and a collection
    holds the GIL for its whole traversal, so the acquisition callback cannot
    run a single bytecode while it happens.
    """

    frame: int
    channel: int
    amplitude: float          # filtered value at the crossing
    threshold: float          # what it had to beat, for provenance
    # The filtered waveform around the crossing, when one was collected. None
    # on the detection path, which cannot have it: the samples after the
    # crossing have not arrived yet. See SpikeDetector.take_waveforms.
    waveform: object = None

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

    def reset(self) -> None:
        """Forget everything. Only correct before a recording starts."""
        self.sigma[:] = 0.0
        self.blocks = 0
        self.frames = 0

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
                 noise_tau_seconds: float = DEFAULT_NOISE_TAU_SECONDS,
                 collect_waveforms: bool = False,
                 waveform_pre_ms: float = DEFAULT_WAVEFORM_PRE_MS,
                 waveform_post_ms: float = DEFAULT_WAVEFORM_POST_MS,
                 max_ready: int = DEFAULT_MAX_READY):
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

        # Waveform collection, for sorting. Off by default: it costs a
        # copy of the recent filtered signal per block, and the closed loop
        # has no use for it - a waveform cannot be completed until the
        # samples *after* the crossing arrive, so it is inherently later than
        # the decision the loop has to make.
        self.collect_waveforms = collect_waveforms
        self.waveform_pre = max(1, int(round(waveform_pre_ms * 1e-3 * frame_rate_hz)))
        self.waveform_post = max(1, int(round(waveform_post_ms * 1e-3 * frame_rate_hz)))
        self.waveform_length = self.waveform_pre + self.waveform_post + 1
        self._tail = None            # recent filtered frames
        self._tail_start = 0         # absolute frame index of _tail[0]
        self._pending = []           # spikes waiting for their post-samples
        self.frames_skipped = 0      # recorded, but never detected on
        self.max_ready = max(1, int(max_ready))
        # A deque, so eviction is the append rather than a pop(0). The
        # bound only bites when nobody is draining - and in that case it
        # bites on every spike, which is precisely when an O(n) shift of
        # 4096 pointers per spike would be the wrong thing to be doing on
        # the acquisition thread.
        self._ready = deque(maxlen=self.max_ready)
        self.waveforms_dropped = 0

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

    def skip_frames(self, n_frames: int) -> None:
        """Account for frames that were recorded but never detected on.

        `Spike.frame` promises to locate a spike in the `.raw` file, and that
        promise is only kept if this counter tracks what the writer wrote.
        Whenever a block reaches the writer but not the detector - a decode
        failure, a suspended loop - the frames still happened, and silently
        not counting them would offset every later spike from the recording by
        that much, permanently, with nothing in the output to say so.

        The filter state and the tail are both stale across a gap, and both
        are discarded. Pending waveforms go with them: a waveform stitched
        from either side of missing data is not a waveform.

        Clearing the filter matters more than it looks. Left primed on the
        pre-gap baseline, the biquad rings on the first block after the gap,
        and if the DC baseline moved across it that transient is a run of
        threshold crossings - false spikes, which on a closed loop are
        stimuli. Dropping `_warmed` makes the next `detect()` re-settle the
        filter on the post-gap baseline, which is exactly what the
        start-of-recording path already does for the same reason.

        `noise.frames` is deliberately *not* advanced: a gap must not grant
        the estimator readiness for time it never observed.
        """
        if n_frames <= 0:
            return
        self._frames_seen += int(n_frames)
        self.frames_skipped += int(n_frames)
        self.waveforms_dropped += len(self._pending)
        self._pending.clear()
        self._tail = None
        self._tail_start = self._frames_seen
        self.filter.reset()
        self._warmed = False
        self._previous[:] = 0.0

    def reset(self) -> None:
        """Return to the state of a detector that has seen nothing.

        Exists so that a warm-up pass - running blocks through the whole code
        path to pay its allocation and first-touch costs before a recording
        starts - can be undone. Without the reset a warm-up would leave the
        noise estimate and the filter carrying synthetic data, which is worse
        than the cost it was avoiding.
        """
        self.filter.reset()
        self.noise.reset()
        self._frames_seen = 0
        self._warmed = False
        self._previous[:] = 0.0
        self._last_spike[:] = -10 ** 15
        self.spikes_detected = 0
        self._tail = None
        self._tail_start = 0
        self._pending.clear()
        self._ready.clear()
        self.waveforms_dropped = 0
        self.frames_skipped = 0

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
            self._previous[:] = filtered[-1]
            self._frames_seen += block.shape[0]
            return []

        thresholds = self.noise.thresholds(self.threshold_sigmas)
        spikes = self._crossings(filtered, thresholds)
        self._previous[:] = filtered[-1]
        if self.collect_waveforms:
            self._keep_tail(filtered, block.shape[0])
        self._frames_seen += block.shape[0]
        self.spikes_detected += len(spikes)
        if self.collect_waveforms:
            self._pending.extend(spikes)
            self._complete_waveforms()
        return spikes

    # -- waveforms, for sorting ------------------------------------------

    def _keep_tail(self, filtered, block_frames: int) -> None:
        """Retain just enough recent filtered signal to cut waveforms from.

        Sized to the window plus the largest block seen, so it holds whatever
        a spike at the very start of a block needs and nothing more. It does
        not grow with the recording.
        """
        keep = self.waveform_length + block_frames + 1
        if self._tail is None:
            self._tail = filtered.copy()
            self._tail_start = self._frames_seen
        else:
            self._tail = np.concatenate((self._tail, filtered), axis=0)
        if self._tail.shape[0] > keep:
            trimmed = self._tail.shape[0] - keep
            self._tail = self._tail[trimmed:]
            self._tail_start += trimmed

    def _complete_waveforms(self) -> None:
        """Move spikes whose window has fully arrived onto the ready list.

        A spike is reported the moment it crosses, but its waveform cannot be
        cut until the samples after it exist. So the two are separate outputs:
        `detect` returns crossings now, for the closed loop; `take_waveforms`
        returns shapes about a millisecond later, for sorting.
        """
        if self._tail is None:
            return
        tail_end = self._tail_start + self._tail.shape[0]
        still_pending = []
        for spike in self._pending:
            start = spike.frame - self.waveform_pre
            stop = spike.frame + self.waveform_post + 1
            if stop > tail_end:
                still_pending.append(spike)
                continue
            if start < self._tail_start:
                # The window opens before anything still retained - only
                # possible for a spike in the first moments of a recording.
                # Counted rather than silently skipped: a sorter fed fewer
                # waveforms than there were spikes should know why.
                self.waveforms_dropped += 1
                continue
            window = self._tail[start - self._tail_start:stop - self._tail_start,
                                spike.channel]
            if window.shape[0] != self.waveform_length:
                # numpy clips an out-of-range slice rather than raising, so a
                # bookkeeping drift here would ship a short waveform and
                # surface much later and much further away, as "waveforms have
                # different lengths" inside a sorter. Refuse it at the source.
                self.waveforms_dropped += 1
                continue
            if len(self._ready) == self.max_ready:
                # Bounded here rather than trusting a caller to drain. Nothing
                # drains this in a headless closed-loop script, and an
                # unbounded queue on the acquisition thread is how a recording
                # ends in swap. The append below evicts the oldest itself;
                # this only has to count it.
                self.waveforms_dropped += 1
            # Constructed rather than `replace`d: replace() calls fields(),
            # builds a kwargs dict and re-runs __init__, and this is per
            # spike, not per packet. A burst completes tens of them on a
            # single packet inside a 1 ms budget.
            self._ready.append(Spike(
                spike.frame, spike.channel, spike.amplitude, spike.threshold,
                window.copy(),
            ))
        self._pending = still_pending

    def take_waveforms(self) -> list:
        """Every spike whose waveform is now complete. Empties the queue.

        Returns `Spike` objects with `waveform` filled in - the same spikes
        `detect` already returned, arriving again once their shape is known.
        Callers that want both must not count them twice.
        """
        if not self._ready:
            return _NOTHING_READY     # the common case: no allocation for it
        # Two operations where the old list form was one atomic swap. Safe
        # because `detect` and `take_waveforms` both run on the consumer
        # thread and never overlap - safe by convention now, where it used to
        # be safe by construction. If this is ever called from another thread,
        # this is the line to revisit.
        ready = list(self._ready)
        self._ready.clear()
        return ready

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

    @property
    def frames_analysed(self) -> int:
        """Frames actually detected on - the denominator for any rate.

        Not `_frames_seen`, which counts skipped frames too so that
        `Spike.frame` stays aligned with the recording. Dividing by that would
        make a suspended loop's spike rate decay smoothly towards zero and be
        displayed as a measurement rather than as an absence - which is the
        exact confusion `RatePolicy` warns about, since a detector that has
        gone deaf looks like a culture that has gone quiet.
        """
        return max(0, self._frames_seen - self.frames_skipped)

    def rates_hz(self, elapsed_frames: int = None) -> float:
        """Mean detection rate across the recording so far, in Hz."""
        frames = (elapsed_frames if elapsed_frames is not None
                  else self.frames_analysed)
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
