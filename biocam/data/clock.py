"""Layer 2 - where a recording currently is, in acquisition time.

Phase 2 left one question unanswered, and it blocks scheduled stimulation
entirely: `Send(pulse, positive, negative, timestamps)` takes microseconds
**relative to the beginning of the acquisition**, and nothing in this
repository could say how far into an acquisition it was. A train scheduled
without that number is a train scheduled at an unknown time.

The answer is already flowing past. `DataPacketHeader.Timestamp` is documented
as "the timestamp of the data packet in number of BioCAM's clock cycles **or 0
when the timestamp is not available**", and `biocam/interop/source.py` has
been carrying it on every `Packet` since Phase 1. This module turns that into
a usable clock.

Three things make it more than a division.

**Zero is not a time.** A timestamp of 0 means "not available", not "the
acquisition just started". Treating one as the other would place a stimulus at
the beginning of the recording rather than at the present moment - a silent
error of the whole elapsed duration.

**Loss must not slow the clock down.** Counting the frames that arrived
undercounts elapsed time whenever packets are dropped. Frames known to be lost
are counted in, so a recording that loses data does not also lose time.

**Two estimates, cross-checked - but only when the check is real.** The
instrument's timestamps and our frame count should advance at the same rate,
and divergence means an assumption is wrong. That comparison is only evidence
when the cycles-per-microsecond factor comes from outside: calibrating it from
these same packets makes the device estimate reduce algebraically to the frame
estimate, and the difference is then identically zero no matter how wrong the
clock is. Measured, on the configuration the CLI actually builds: a device
clock running 30% fast produced a disagreement of 5e-10 us.

`cross_check_is_meaningful` says which case you are in, and `warnings()` says
so out loud. A check that cannot fail reads exactly like a check that passed,
which is the more dangerous of the two.

Nothing here talks to the driver, so all of it is testable.
"""

from dataclasses import dataclass

# DataPacketHeader.Timestamp uses 0 as a sentinel for "not available" (XML:
# "or 0 when the timestamp is not available"). It is not a valid time.
TIMESTAMP_UNAVAILABLE = 0

# How far the two estimates may drift apart before `disagreement` is reported.
# Generous on purpose: this is a wrong-assumption detector, not a precision
# measurement, and a false alarm mid-recording costs more than it saves.
DEFAULT_TOLERANCE_US = 50_000.0

# Calibration needs a long enough baseline that the quantisation of a single
# packet's timestamp does not dominate the ratio. At the CLI's default 2 ms
# packet period this is about two seconds of acquisition.
MIN_CALIBRATION_FRAMES = 20_000


class ClockUnavailable(RuntimeError):
    """The acquisition time cannot be established yet.

    Raised rather than returning a plausible-looking zero, because a stimulus
    scheduled at "time zero" of a recording that has been running for ten
    minutes is not a smaller error than a crash - it is a much larger one.
    """


@dataclass(frozen=True)
class ClockReading:
    """An acquisition time, and how it was arrived at."""

    acquisition_us: float
    source: str  # "device" or "frames"
    frames_seen: int
    frames_lost: int
    disagreement_us: float = None

    @property
    def is_from_device(self) -> bool:
        return self.source in ("device", "device-calibrated")

    def describe(self) -> str:
        text = (
            f"{self.acquisition_us / 1e6:.3f} s into the acquisition "
            f"(from {self.source}; {self.frames_seen} frames seen"
        )
        if self.frames_lost:
            text += f", {self.frames_lost} lost"
        text += ")"
        if self.disagreement_us is not None:
            text += f", estimates differ by {self.disagreement_us / 1000:.1f} ms"
        return text


class AcquisitionClock:
    """Tracks acquisition time from the packets a recording is already reading.

    Fed from the consumer thread - `observe()` does arithmetic and nothing
    else, but it is not on the callback path and does not need to be.

    `now_us()` returns the acquisition time **of the most recent packet
    observed**, which is necessarily in the past by however long that packet
    took to reach this code. It is a lower bound on the present, never an
    upper one. Anything scheduling against it must add a lead time; see
    `schedule_after()`.
    """

    def __init__(
        self,
        frame_rate: float,
        *,
        cycles_per_us: float = None,
        tolerance_us: float = DEFAULT_TOLERANCE_US,
    ):
        if frame_rate <= 0:
            raise ValueError(f"frame_rate must be positive, got {frame_rate}")
        self._frame_rate = float(frame_rate)
        self._cycles_per_us = cycles_per_us
        self._tolerance_us = tolerance_us

        self._first_timestamp = None
        self._last_timestamp = None
        self._frames_seen = 0
        self._frames_lost = 0
        self._frames_at_first_timestamp = None
        self.timestamp_anomalies = 0
        self.unavailable_timestamps = 0

    # -- feeding ---------------------------------------------------------

    def observe(self, packet, frames_in_packet: int, frames_lost: int = 0) -> None:
        """Record one packet's arrival.

        `frames_lost` is the number of frames missing *before* this packet,
        as established by the gap tracker. Counting them keeps elapsed time
        honest across a lossy stretch.
        """
        if frames_in_packet < 0:
            raise ValueError(
                f"frames_in_packet must not be negative, got {frames_in_packet}"
            )
        if frames_lost < 0:
            raise ValueError(
                f"frames_lost must not be negative, got {frames_lost}"
            )

        self._frames_lost += frames_lost
        self._frames_seen += frames_in_packet

        timestamp = getattr(packet, "timestamp", TIMESTAMP_UNAVAILABLE)
        if timestamp == TIMESTAMP_UNAVAILABLE:
            # Documented sentinel, not a time. Counted so that a recording
            # whose device clock never works is visibly different from one
            # where it does.
            self.unavailable_timestamps += 1
            return

        if self._first_timestamp is None:
            self._first_timestamp = timestamp
            self._frames_at_first_timestamp = self._total_frames
            self._last_timestamp = timestamp
            return

        if timestamp < self._last_timestamp:
            # The instrument's clock does not run backwards, so this is a
            # device reset, a corrupted header, or packets arriving out of
            # order. Counted and ignored: adopting it would make the clock
            # jump backwards and could schedule a stimulus into the past.
            self.timestamp_anomalies += 1
            return

        self._last_timestamp = timestamp

    def observe_totals(
        self, packet, frames_written_total: int, frames_missing_total: int
    ) -> None:
        """Record one packet from a writer's running totals.

        `RecordingWriter` already tracks both numbers, and derives its frame
        count from bytes actually written rather than from per-packet
        arithmetic. Taking the totals and differencing them here keeps one
        source of truth instead of counting frames a second, subtly different
        way alongside it.
        """
        frames_in_packet = frames_written_total - self._frames_seen
        frames_lost = frames_missing_total - self._frames_lost
        if frames_in_packet < 0 or frames_lost < 0:
            raise ValueError(
                f"running totals went backwards (frames written "
                f"{frames_written_total} against {self._frames_seen} already "
                f"seen, missing {frames_missing_total} against "
                f"{self._frames_lost}). These totals only ever increase, so "
                "this clock is being fed from a different recording."
            )
        self.observe(packet, frames_in_packet, frames_lost)

    # -- state -----------------------------------------------------------

    @property
    def _total_frames(self) -> int:
        return self._frames_seen + self._frames_lost

    @property
    def frames_seen(self) -> int:
        return self._frames_seen

    @property
    def frames_lost(self) -> int:
        return self._frames_lost

    @property
    def device_timestamps_available(self) -> bool:
        """Whether the instrument has reported a usable timestamp at all."""
        return self._first_timestamp is not None

    @property
    def cycles_per_us(self) -> float:
        """Clock cycles per microsecond, given or calibrated.

        The driver's own `IBioCam.ClockCyclesToMilliseconds` is authoritative
        and should be preferred when a device is present - pass the factor
        derived from it as `cycles_per_us`. This calibration exists so that a
        replayed recording, which has timestamps but no driver, still has a
        clock, and so that a device-supplied factor can be checked against the
        data rather than taken on faith.
        """
        if self._cycles_per_us is not None:
            return self._cycles_per_us
        return self.calibrated_cycles_per_us

    @property
    def calibrated_cycles_per_us(self) -> float:
        """Cycles per microsecond derived from the packets themselves.

        Over a long enough baseline the elapsed time is known independently -
        frames divided by the frame rate - so the ratio to the elapsed clock
        cycles gives the conversion. Returns None until the baseline is long
        enough to be meaningful.
        """
        if self._first_timestamp is None:
            return None
        elapsed_frames = self._total_frames - self._frames_at_first_timestamp
        if elapsed_frames < MIN_CALIBRATION_FRAMES:
            return None
        elapsed_cycles = self._last_timestamp - self._first_timestamp
        if elapsed_cycles <= 0:
            return None
        elapsed_us = elapsed_frames / self._frame_rate * 1e6
        return elapsed_cycles / elapsed_us

    # -- reading ---------------------------------------------------------

    @property
    def cross_check_is_meaningful(self) -> bool:
        """Whether comparing the two estimates can detect anything.

        It cannot when the conversion factor was calibrated from these same
        packets. Calibration defines the factor as elapsed-cycles divided by
        frame-derived elapsed-time, so feeding it back in makes the device
        estimate reduce algebraically to the frame estimate and the difference
        is identically zero. Measured: a device clock running 30% fast
        produced a disagreement of 5e-10 us.

        So the comparison is only evidence when `cycles_per_us` came from
        somewhere else - `IBioCam.ClockCyclesToMilliseconds` on the device.
        Reported rather than hidden, because a check that cannot fail reads
        exactly like a check that passed.
        """
        return self._cycles_per_us is not None and self.device_timestamps_available

    def elapsed_us_from_frames(self) -> float:
        """Acquisition time implied by the frames, lost ones included."""
        return self._total_frames / self._frame_rate * 1e6

    def elapsed_us_from_device(self) -> float:
        """Acquisition time from the instrument's own clock, in microseconds.

        The timestamp is treated as **absolute** - measured from the same
        origin the stimulator schedules against - rather than as an offset
        from the first packet this recording happened to see.

        That is what 3Brain's own sample does. `MainForm.cs:508` keeps
        `e.Header.Timestamp` from the data callback, and `MainForm.cs:383-385`
        subtracts it directly from the `out` latency of `Send`, which the XML
        documents as "clock cycles relative to the beginning of the
        acquisition". For that subtraction to mean anything the two must share
        units and origin.

        It is still an inference - the sample only ever takes a difference, so
        it establishes a *shared* origin, not where that origin is. Issue #24.

        Returns None when no usable timestamp has been seen, or when the
        cycles-per-microsecond factor is neither supplied nor yet calibrated.
        """
        if self._last_timestamp is None:
            return None
        factor = self.cycles_per_us
        if not factor:
            return None
        return self._last_timestamp / factor

    def drift_us(self) -> float:
        """How far the two clocks have diverged since the first timestamp.

        Compares *rates of advance*, not absolute times: the instrument may
        have been acquiring before this recording started, so the absolute
        values legitimately differ while the rates should not.

        Returns None when there is nothing to compare, including when the
        comparison would be vacuous - see `cross_check_is_meaningful`.
        """
        if not self.cross_check_is_meaningful:
            return None
        elapsed_frames = self._total_frames - self._frames_at_first_timestamp
        device_us = (self._last_timestamp - self._first_timestamp) / self._cycles_per_us
        frames_us = elapsed_frames / self._frame_rate * 1e6
        return abs(device_us - frames_us)

    # Retained under its old name because `warnings()` and `schedule_after`
    # read better with it, but it is the drift, not a difference of absolutes.
    disagreement_us = drift_us

    def read(self) -> ClockReading:
        """The current acquisition time, and where it came from.

        Prefers the instrument's clock: it is the same clock the stimulator
        schedules against, so agreeing with it matters more than agreeing
        with our own arithmetic.

        Raises `ClockUnavailable` when neither estimate can be formed, rather
        than returning zero.
        """
        if self._total_frames == 0 and self._first_timestamp is None:
            raise ClockUnavailable(
                "no packets observed yet, so the acquisition time is unknown. "
                "Scheduling against it would place stimuli at the start of "
                "the recording rather than at the present moment."
            )

        device = self.elapsed_us_from_device()
        drift = self.drift_us()
        if device is not None:
            return ClockReading(
                acquisition_us=device,
                # "device" when the instrument supplied both the timestamps
                # and the factor; "device-calibrated" when we derived the
                # factor ourselves, which is a weaker claim and says so.
                source="device" if self._cycles_per_us else "device-calibrated",
                frames_seen=self._frames_seen,
                frames_lost=self._frames_lost,
                disagreement_us=drift,
            )
        return ClockReading(
            acquisition_us=self.elapsed_us_from_frames(),
            source="frames",
            frames_seen=self._frames_seen,
            frames_lost=self._frames_lost,
            disagreement_us=drift,
        )

    def now_us(self) -> float:
        """The acquisition time of the most recent packet, in microseconds.

        A lower bound on the present, never an upper one.
        """
        return self.read().acquisition_us

    def estimates_agree(self) -> bool:
        """Whether the two clocks have stayed within tolerance of each other.

        True when there is nothing to compare - including when the comparison
        would be vacuous. Do not read a True from this as "the clock was
        checked"; ask `cross_check_is_meaningful` for that.
        """
        drift = self.drift_us()
        if drift is None:
            return True
        return drift <= self._tolerance_us

    def warnings(self) -> list:
        """Everything about this clock that a person should be told."""
        problems = []
        if not self.device_timestamps_available and self._frames_seen:
            problems.append(
                f"the instrument reported no usable timestamp in "
                f"{self.unavailable_timestamps} packets (DataPacketHeader."
                "Timestamp was 0, its documented 'not available' value), so "
                "acquisition time is derived from the frame count and the "
                "nominal frame rate. Any drift between that rate and the "
                "instrument's real one accumulates."
            )
        if self.timestamp_anomalies:
            problems.append(
                f"{self.timestamp_anomalies} packet timestamp(s) went "
                "backwards. The instrument's clock does not run backwards, so "
                "this is a device reset, a corrupted header, or out-of-order "
                "packets. They were ignored rather than adopted."
            )
        if self.device_timestamps_available and not self.cross_check_is_meaningful:
            problems.append(
                "the clock's conversion factor was calibrated from these same "
                "packets, so comparing the instrument's clock against the "
                "frame count cannot detect anything - the comparison reduces "
                "to an identity. Supply cycles_per_us from "
                "IBioCam.ClockCyclesToMilliseconds to make it real. Until "
                "then the acquisition time is a single unchecked estimate."
            )
        if not self.estimates_agree():
            problems.append(
                f"the instrument's clock and the frame count have drifted "
                f"{self.drift_us() / 1000:.1f} ms apart. One of the frame "
                "rate, the clock calibration, or the loss accounting is "
                "wrong. Do not schedule stimulation against this."
            )
        return problems


def schedule_after(plan, clock: AcquisitionClock, lead_us: float):
    """Place a plan `lead_us` microseconds after the present moment.

    `plan` is a `TrainPlan` or `SequencePlan` built with timestamps starting
    at zero; the result has them shifted onto the acquisition's timeline.

    **`lead_us` is not padding.** `clock.now_us()` is the acquisition time of
    the last packet *processed*, so the real present is already later than it
    by the queue depth plus the time to drain and write. Schedule inside that
    margin and the timestamps are in the past before the driver sees them -
    and what the instrument does with past timestamps is untested (issue #24).

    A lead comfortably larger than the queue's buffering is the safe choice.
    At the CLI's defaults that buffer is about two seconds.
    """
    if lead_us <= 0:
        raise ValueError(
            f"lead_us must be positive, got {lead_us}. Scheduling at or "
            "before the last observed packet places the stimulus in the past."
        )
    reading = clock.read()
    if not clock.estimates_agree():
        raise ClockUnavailable(
            "refusing to schedule: " + "; ".join(clock.warnings())
        )
    return plan.shifted_by(reading.acquisition_us + lead_us)
