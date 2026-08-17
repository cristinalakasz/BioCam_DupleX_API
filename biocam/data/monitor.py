"""Layer 2 - per-electrode activity, cheap enough to compute while recording.

A window that only shows counters cannot tell you whether the preparation is
alive, whether an electrode is dead, or whether the stimulus you just sent did
anything. For a 4096-electrode array the natural answer is a picture of the
array itself, updating as the recording runs.

The difficulty is that the only thread with the data is the one that must
never be slowed down. Decoding every frame of every packet is out of the
question: at 4096 channels and a 2 ms acquisition period that is ~150,000
samples per packet, several times a second's worth of budget spent on
something nobody is looking at closely.

So this is **decimated on purpose, twice over**:

- **In time.** At most one packet per refresh interval is examined; the rest
  are counted and skipped. At the default 10 Hz that is one packet in fifty.
- **Within the packet.** Only the first `frames_per_sample` frames are
  decoded, not all of them.

What comes out is peak-to-peak per electrode over a short window - enough to
see which electrodes carry signal, which are flat, and where a stimulus
artefact landed. It is a display aid and is documented as one: it is not a
measurement, it never touches the recording, and the recording on disk is
unaffected by anything here.

Every observation is timed, like `biocam.control` times its dispatches,
because "cheap enough" is a claim that has to be checkable rather than
asserted.
"""

import time

import numpy as np

from biocam.data.frames import DTYPE_BY_BYTE_SIZE

# How often the picture is allowed to be recomputed. Ten times a second is
# faster than a person can read and far slower than packets arrive.
DEFAULT_REFRESH_HZ = 10.0

# Frames decoded from the one packet that is examined. 64 frames at ~18.5 kHz
# is about 3.5 ms of signal - long enough for a spike or a stimulus artefact
# to show up in a peak-to-peak, short enough to stay negligible.
DEFAULT_FRAMES_PER_SAMPLE = 64

# Above this, an observation is reported as having put the drain at risk.
# Same reasoning as biocam.control.SLOW_DISPATCH_US, and the same caveat: it
# is a guess until a lab run measures it.
SLOW_OBSERVE_US = 500.0


class MonitorSnapshot:
    """One picture of the array, safe to hand to the UI thread."""

    __slots__ = ("activity", "n_rows", "n_cols", "samples", "skipped",
                 "max_observe_us", "unit")

    def __init__(self, activity, n_rows, n_cols, samples, skipped,
                 max_observe_us, unit):
        self.activity = activity          # 1-D array, one value per channel
        self.n_rows = n_rows
        self.n_cols = n_cols
        self.samples = samples
        self.skipped = skipped
        self.max_observe_us = max_observe_us
        self.unit = unit

    @property
    def has_data(self) -> bool:
        return self.samples > 0

    def as_grid(self):
        """The activity reshaped to (rows, cols), row-major.

        Row-major is an assumption: it is how `biocam.convert` and
        `FrameDecoder` lay a frame out, and it matches ChCoord's (row, col)
        reading order. Nothing in the XML states the channel ordering
        explicitly, so a picture that looks transposed on the instrument is
        this assumption being wrong rather than the data being odd - worth
        reporting rather than working around.
        """
        needed = self.n_rows * self.n_cols
        if self.activity is None or self.activity.size < needed:
            return None
        return self.activity[:needed].reshape(self.n_rows, self.n_cols)

    def range(self):
        """(low, high) over the electrodes, for scaling a colour map."""
        if not self.has_data or self.activity is None or not self.activity.size:
            return 0.0, 1.0
        low = float(np.min(self.activity))
        high = float(np.max(self.activity))
        if high <= low:
            high = low + 1.0
        return low, high


class LiveMonitor:
    """Per-electrode activity, sampled from the packets a recording writes.

    `observe()` is called on the consumer thread and must stay cheap;
    `snapshot()` is called from the UI thread and only copies.
    """

    def __init__(self, params, n_rows: int = 64, n_cols: int = 64, *,
                 refresh_hz: float = DEFAULT_REFRESH_HZ,
                 frames_per_sample: int = DEFAULT_FRAMES_PER_SAMPLE,
                 slow_observe_us: float = SLOW_OBSERVE_US):
        if refresh_hz <= 0:
            raise ValueError(f"refresh_hz must be positive, got {refresh_hz}")
        if frames_per_sample < 2:
            raise ValueError(
                f"frames_per_sample must be at least 2 to have a peak-to-peak, "
                f"got {frames_per_sample}"
            )
        self._params = params
        self._channels = params.total_channels
        self._dtype = DTYPE_BY_BYTE_SIZE[params.ch_sample_byte_size]
        self._bytes_per_frame = params.bytes_per_frame
        self._frames_per_sample = frames_per_sample
        self._interval = 1.0 / refresh_hz
        self._slow_observe_us = slow_observe_us

        self.n_rows = n_rows
        self.n_cols = n_cols
        # Pre-allocated, and reused: this is the only array `observe` writes,
        # so a long recording allocates nothing after the first packet.
        self._activity = np.zeros(self._channels, dtype=np.float32)
        self._last_at = None
        self.samples = 0
        self.skipped = 0
        self.errors = 0
        self.max_observe_us = 0.0
        self.slow_observations = 0

    # -- consumer thread --------------------------------------------------

    def observe(self, packet) -> bool:
        """Maybe sample this packet. Returns whether it was used.

        Never raises: this runs inside the packet loop, where an escaping
        exception costs the buffered backlog and stamps the recording failed.
        A picture is not worth that, so a failure is counted and the recording
        carries on.
        """
        now = time.perf_counter()
        if self._last_at is not None and (now - self._last_at) < self._interval:
            self.skipped += 1
            return False
        self._last_at = now

        try:
            frames = min(
                self._frames_per_sample,
                len(packet.payload) // self._bytes_per_frame,
            )
            if frames < 2:
                self.skipped += 1
                return False
            count = frames * self._channels
            block = np.frombuffer(
                packet.payload, dtype=self._dtype, count=count
            ).reshape(frames, self._channels)
            # ptp in counts, then scaled to the analogue unit. The offset
            # cancels in a difference, so only the gain is applied.
            np.subtract(
                block.max(axis=0), block.min(axis=0),
                out=self._activity, dtype=np.float32, casting="unsafe",
            )
            self._activity *= abs(self._params.adc_counts_to_value)
            self.samples += 1
            return True
        except Exception:  # noqa: BLE001 - a picture never costs a recording
            self.errors += 1
            return False
        finally:
            elapsed_us = (time.perf_counter() - now) * 1e6
            if elapsed_us > self.max_observe_us:
                self.max_observe_us = elapsed_us
            if elapsed_us > self._slow_observe_us:
                self.slow_observations += 1

    # -- UI thread --------------------------------------------------------

    def snapshot(self) -> MonitorSnapshot:
        """A copy of the current picture. Cheap, and safe to call at any rate.

        A copy rather than the live array: the consumer thread writes into
        `_activity` in place, and handing that out would let the UI render a
        half-updated frame.
        """
        return MonitorSnapshot(
            activity=self._activity.copy() if self.samples else None,
            n_rows=self.n_rows,
            n_cols=self.n_cols,
            samples=self.samples,
            skipped=self.skipped,
            max_observe_us=self.max_observe_us,
            unit="uV",
        )

    def warnings(self) -> list:
        problems = []
        if self.errors:
            problems.append(
                f"the activity display failed to decode {self.errors} "
                "packet(s). The recording itself is unaffected - nothing here "
                "touches what is written to disk."
            )
        if self.slow_observations:
            problems.append(
                f"{self.slow_observations} activity sample(s) took longer "
                f"than {self._slow_observe_us:g} us on the acquisition thread "
                f"(slowest {self.max_observe_us:.0f} us). That time comes out "
                "of the packet queue's drain."
            )
        return problems
