"""Layer 2 - a rolling window of signal for a handful of electrodes.

The activity display answers "which electrodes are alive". It cannot answer
"what does this electrode actually look like", and that is the question an
operator asks when a channel behaves oddly: is it spiking, is it saturated, is
it 50 Hz hum, did the stimulus artefact swamp it. For that you have to see the
trace.

The difficulty is the same one the activity display has: the only thread with
the data is the one that must never be slowed down. This is why the design is
deliberately narrow.

- **Only the selected electrodes.** A trace is for looking at closely, and
  nobody looks closely at 4096 of them. The cost is proportional to how many
  are chosen, and choosing none costs nothing at all.
- **Peak-preserving decimation.** A screen is ~600 pixels wide and a second of
  recording is 18,558 samples per channel, so something has to be thrown away.
  Taking every Nth sample is the obvious choice and the wrong one: a spike is
  ~20 samples long, so subsampling by 30 makes most spikes vanish and the rest
  change height at random. That would be a display that lies about the thing
  it exists to show. Instead each output column keeps the **minimum and
  maximum** over its bin, so a spike always leaves a mark of the right height,
  and the trace looks like the envelope a hardware oscilloscope would draw.

What comes out is a display aid and is documented as one: it never touches the
recording, and the recording on disk is unaffected by anything here.
"""

import time

import numpy as np

from biocam.data.frames import DTYPE_BY_BYTE_SIZE

# Columns kept per channel. Roughly a screen's width - more is invisible.
DEFAULT_COLUMNS = 600

# Seconds of signal the window spans. Long enough to see a burst, short enough
# that the newest data is still obviously the newest.
DEFAULT_SPAN_SEC = 2.0

# Above this many channels the whole idea stops making sense: the traces are
# too thin to read and the per-packet cost stops being negligible. Refused
# rather than silently truncated - a display that quietly drops half the
# channels asked for is worse than one that says no.
MAX_TRACE_CHANNELS = 8

# A single observation taking longer than this is worth reporting. Same
# reasoning as the closed loop's own budget: this runs between packets.
SLOW_OBSERVATION_US = 200.0


class TraceSnapshot:
    """What the UI draws. A copy, safe to read on another thread."""

    __slots__ = ("channels", "minima", "maxima", "filled", "columns",
                 "seconds_per_column", "value_unit")

    def __init__(self, channels, minima, maxima, filled, seconds_per_column,
                 value_unit="uV"):
        self.channels = tuple(channels)
        self.minima = minima
        self.maxima = maxima
        self.filled = filled
        self.columns = minima.shape[1] if minima.size else 0
        self.seconds_per_column = seconds_per_column
        self.value_unit = value_unit

    @property
    def has_data(self) -> bool:
        return self.filled > 0

    def range_of(self, index: int):
        """(low, high) across the filled part of one channel's trace.

        Returns (0.0, 0.0) when nothing has arrived, so a caller scaling an
        axis by this does not have to special-case an empty window.
        """
        if not self.filled:
            return 0.0, 0.0
        lo = float(self.minima[index, :self.filled].min())
        hi = float(self.maxima[index, :self.filled].max())
        return lo, hi


class TraceRecorder:
    """Keeps a rolling, peak-preserving window for a few channels.

    Fed one packet at a time from the consumer thread. Everything it does is
    bounded by the number of watched channels and the packet size; nothing
    grows with the length of the recording.
    """

    def __init__(self, params, channels, *, columns: int = DEFAULT_COLUMNS,
                 span_sec: float = DEFAULT_SPAN_SEC,
                 as_microvolts: bool = True):
        channels = [int(c) for c in channels]
        if len(channels) > MAX_TRACE_CHANNELS:
            raise ValueError(
                f"traces are for looking at closely, so at most "
                f"{MAX_TRACE_CHANNELS} channels can be watched at once; "
                f"{len(channels)} were given"
            )
        total = params.total_channels
        for channel in channels:
            if not 0 <= channel < total:
                raise ValueError(
                    f"channel {channel} is outside the {total}-channel array"
                )
        if columns < 1:
            raise ValueError("a trace needs at least one column")

        self.params = params
        self.channels = channels
        self.columns = int(columns)
        self.as_microvolts = as_microvolts

        self._np = np
        self._indices = np.asarray(channels, dtype=np.intp)
        self._dtype = DTYPE_BY_BYTE_SIZE[params.ch_sample_byte_size]
        self._total_channels = total
        self._bytes_per_frame = params.bytes_per_frame
        self._offset = float(params.offset)
        self._scale = float(params.adc_counts_to_value)

        # Frames per output column. At least one, or a short span with many
        # columns would divide to zero and drop every packet on the floor.
        self._frames_per_column = max(
            1, int(round(span_sec * params.frame_rate_hz / self.columns)))
        self.seconds_per_column = (
            self._frames_per_column / params.frame_rate_hz)

        n = len(channels)
        # Ring buffers. Written at `_write`, which wraps; `filled` says how
        # much of the ring has ever been written, so a partly-filled window
        # draws correctly instead of showing zeros as signal.
        self._min = np.zeros((n, self.columns), dtype=np.float64)
        self._max = np.zeros((n, self.columns), dtype=np.float64)
        self._write = 0
        self.filled = 0

        # Frames carried over from the previous packet, so a column spans the
        # right number of frames even when packets do not divide evenly into
        # columns. Bounded by `_frames_per_column`.
        self._carry = np.zeros((0, n), dtype=np.float64)

        self.packets_seen = 0
        self.decode_errors = 0
        self.max_observation_us = 0.0
        self.slow_observations = 0

    # -- the consumer thread's entry point --------------------------------

    def observe(self, packet) -> bool:
        """Fold one packet into the window. Never raises.

        Returns True if anything was added. Like the activity display and the
        closed loop, a failure here disconnects the traces rather than the
        recording: nobody loses a session because a picture stopped drawing.
        """
        if not self.channels:
            return False
        started = time.perf_counter()
        try:
            np_ = self._np
            frames = len(packet.payload) // self._bytes_per_frame
            if frames < 1:
                return False
            block = np_.frombuffer(
                packet.payload, dtype=self._dtype,
                count=frames * self._total_channels,
            ).reshape(frames, self._total_channels)[:, self._indices]
            values = block.astype(np.float64)
            if self.as_microvolts:
                # In place: `values` is already a private copy from astype, so
                # this saves two whole-block temporaries per packet on the
                # thread that drains the queue.
                values *= self._scale
                values += self._offset
            self._fold(values)
            self.packets_seen += 1
            return True
        except Exception:  # noqa: BLE001 - the recording outranks the picture
            self.decode_errors += 1
            return False
        finally:
            elapsed_us = (time.perf_counter() - started) * 1e6
            if elapsed_us > self.max_observation_us:
                self.max_observation_us = elapsed_us
            if elapsed_us > SLOW_OBSERVATION_US:
                self.slow_observations += 1

    def _fold(self, values) -> None:
        """Reduce (frames, channels) into whole columns, carrying the rest."""
        if self._carry.shape[0]:
            values = np.concatenate([self._carry, values])
        per = self._frames_per_column
        n_columns = values.shape[0] // per
        if n_columns:
            used = values[:n_columns * per]
            # (columns, frames_per_column, channels) -> reduce the middle axis.
            shaped = used.reshape(n_columns, per, used.shape[1])
            lows = shaped.min(axis=1).T      # (channels, columns)
            highs = shaped.max(axis=1).T
            self._append(lows, highs)
        # Whatever did not fill a column waits for the next packet. Bounded by
        # `per - 1` rows, so this cannot grow with the recording.
        self._carry = values[n_columns * per:].copy()

    def _append(self, lows, highs) -> None:
        n_new = lows.shape[1]
        if n_new >= self.columns:
            # A single packet longer than the whole window: keep its tail.
            self._min[:] = lows[:, -self.columns:]
            self._max[:] = highs[:, -self.columns:]
            self._write = 0
            self.filled = self.columns
            return
        end = self._write + n_new
        if end <= self.columns:
            self._min[:, self._write:end] = lows
            self._max[:, self._write:end] = highs
        else:
            split = self.columns - self._write
            self._min[:, self._write:] = lows[:, :split]
            self._max[:, self._write:] = highs[:, :split]
            self._min[:, :end - self.columns] = lows[:, split:]
            self._max[:, :end - self.columns] = highs[:, split:]
        self._write = end % self.columns
        self.filled = min(self.columns, self.filled + n_new)

    # -- the UI thread's entry point --------------------------------------

    def snapshot(self) -> TraceSnapshot:
        """A copy, oldest column first. Safe to call from another thread."""
        # Both scalars are read ONCE, into locals, before anything uses them.
        # The consumer thread advances them while this runs on the UI thread,
        # and reading `_write` twice let `_append` land between the two rolls -
        # pairing a minimum from one instant with a maximum from another, in a
        # single drawn column. That is a display lying about the thing it
        # exists to show, which is the exact failure this module rejects
        # subsampling to avoid.
        filled, write = self.filled, self._write
        unit = "uV" if self.as_microvolts else "counts"
        if not filled:
            return TraceSnapshot(self.channels,
                                 np.zeros((len(self.channels), 0)),
                                 np.zeros((len(self.channels), 0)),
                                 0, self.seconds_per_column, unit)
        if filled < self.columns:
            lows = self._min[:, :filled].copy()
            highs = self._max[:, :filled].copy()
        else:
            # Unwrap the ring so column 0 is the oldest sample, which is what
            # anyone drawing left-to-right expects. np.roll already returns a
            # new array, so no further copy is needed.
            lows = np.roll(self._min, -write, axis=1)
            highs = np.roll(self._max, -write, axis=1)
        return TraceSnapshot(self.channels, lows, highs, filled,
                             self.seconds_per_column, unit)

    def warnings(self) -> list:
        problems = []
        if self.decode_errors:
            problems.append(
                f"the trace display failed to decode {self.decode_errors} "
                "packet(s). The recording itself is unaffected."
            )
        if self.slow_observations:
            problems.append(
                f"{self.slow_observations} trace updates took longer than "
                f"{SLOW_OBSERVATION_US:g} us on the acquisition thread "
                f"(slowest {self.max_observation_us:.0f} us). That time comes "
                "out of the packet queue's drain - watch fewer channels."
            )
        return problems

    def summary(self) -> dict:
        return {
            "trace_channels": list(self.channels),
            "trace_packets": self.packets_seen,
            "trace_decode_errors": self.decode_errors,
            "trace_max_observation_us": round(self.max_observation_us, 1),
            "trace_slow_observations": self.slow_observations,
        }
