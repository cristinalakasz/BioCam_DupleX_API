"""Layer 2 - pulse trains and arbitrary timed sequences.

A stimulation protocol is *(endpoints) + (pulses) + (timestamps)*. Everything
Phase 2 was asked for falls out of that one structure:

- a single pulse on demand - one timestamp, one pulse;
- a regular train - N evenly spaced timestamps, one pulse reused;
- an arbitrary timed sequence - N timestamps, N pulses, one per timestamp.

Timestamps are generated here and handed to the instrument, which executes
them. They are deliberately **not** driven from a Python timer: the stimulator
resolves 10 us, while a Python loop on Windows drifts and can be preempted for
milliseconds. 3Brain's own documentation notes that Windows is not a real-time
OS and that latency varies with load. A schedule the hardware holds is both
more accurate and immune to this process stalling.

**Timestamps are absolute, not relative.** The XML for
`Send(pulse, positive, negative, Double[])` states they are "in microsecond
relative to the beginning of the acquisition" - not relative to the moment of
sending. A train planned as if `delay_us` were "start in half a second" will,
ten minutes into a recording, have every timestamp in the past. What the
instrument does then is untested; the plausible outcomes are that the whole
train fires at once or that it is discarded, and neither is what was meant.

So `delay_us` here is an offset from the start of acquisition, and scheduling
relative to *now* means adding the current acquisition time to it. Whoever
sends the plan is responsible for that conversion, because only they know how
far into the recording they are - see `biocam.interop.stimulator`.

The overlap check is the one that matters most here. Nothing in the driver
stops a train whose period is shorter than its own pulse, and the result is
not a faster train - it is stimuli running into each other.
"""

from dataclasses import dataclass, replace

from biocam.stim.constraints import StimConstraints
from biocam.stim.pulse import PulsePlan, PulseSpec, PulseValidationError
from biocam.stim.pulse import plan as plan_pulse

# Measured from _3Brain.Common.StimulusBaseTrainConfigurationOptionsConstants
# (instantiable without a device):
#   MinCount = 1, MaxCount = 1000
#   MinDistance = 1000, MaxDistance = 3600000   (microseconds)
#
# The three sources do not quite agree on the timestamp limit. The XML for
# Send(pulse, pos, neg, Double[]) says "Time-stamps Up to 1024 values"; the
# MaxCount constant above says 1000; the API introduction PDF says 1000 queued
# future stimuli. 1000 is the intersection, so that is what is enforced - the
# 24 extra slots are not worth the risk of finding out which source is stale.
MAX_TRAIN_PULSES = 1000
MAX_TIMESTAMPS_PER_SEND_XML = 1024  # recorded for the discrepancy, not enforced
MIN_PERIOD_US = 1000
MAX_PERIOD_US = 3_600_000


class TrainValidationError(ValueError):
    """A train the instrument would reject, or execute as something else."""

    def __init__(self, problems):
        self.problems = list(problems)
        super().__init__(
            "train cannot be built as specified:\n  - " + "\n  - ".join(self.problems)
        )


@dataclass(frozen=True)
class TrainSpec:
    """A regular train: one pulse repeated at a fixed period.

    `delay_us` is measured from the **beginning of the acquisition**, matching
    the driver's timestamp convention - not from the moment the train is sent.
    To schedule relative to now, plan with `delay_us=0` and call
    `TrainPlan.shifted_by(current_acquisition_time_us)`.
    """

    pulse: PulseSpec
    count: int
    period_us: float
    delay_us: float = 0.0
    name: str = "train"

    @property
    def rate_hz(self) -> float:
        return 1_000_000.0 / self.period_us

    @property
    def duration_us(self) -> float:
        """First pulse start to last pulse end."""
        if self.count <= 0:
            return 0.0
        return self.delay_us + (self.count - 1) * self.period_us + self.pulse.total_us

    @classmethod
    def at_rate(
        cls,
        pulse: PulseSpec,
        count: int,
        rate_hz: float,
        delay_us: float = 0.0,
        name: str = "train",
    ) -> "TrainSpec":
        """Build from a rate rather than a period.

        The period must still land on the stimulator's tick grid, so a rate
        that does not divide evenly will be refused by `plan_train` rather
        than rounded here. 100 Hz at a 10 us resolution is fine; 3 Hz is
        333333.33 us and is not.
        """
        if rate_hz <= 0:
            raise ValueError(f"rate_hz must be positive, got {rate_hz}")
        return cls(
            pulse=pulse,
            count=count,
            period_us=1_000_000.0 / rate_hz,
            delay_us=delay_us,
            name=name,
        )


@dataclass(frozen=True)
class TrainPlan:
    """A validated train, with the timestamps the instrument will execute."""

    spec: TrainSpec
    pulse_plan: PulsePlan
    timestamps_us: tuple

    @property
    def count(self) -> int:
        return len(self.timestamps_us)

    @property
    def duration_us(self) -> float:
        if not self.timestamps_us:
            return 0.0
        return self.timestamps_us[-1] + self.pulse_plan.total_us

    @property
    def net_charge_pc(self) -> float:
        """Charge across the whole train.

        A per-pulse imbalance too small to worry about becomes a large DC
        offset over a thousand pulses, which is the case worth seeing.
        """
        return self.pulse_plan.net_charge_pc * self.count

    def shifted_by(self, offset_us: float) -> "TrainPlan":
        """Move every timestamp later by `offset_us`.

        Timestamps are measured from the beginning of the acquisition, so
        scheduling a train relative to *now* means shifting it by the current
        acquisition time. Returns a new plan; the original is unchanged.
        """
        if offset_us < 0:
            raise ValueError(f"offset_us must not be negative, got {offset_us}")
        return replace(
            self,
            timestamps_us=tuple(t + offset_us for t in self.timestamps_us),
        )

    def describe(self) -> str:
        return (
            f"{self.spec.name}: {self.count} x [{self.pulse_plan.describe()}] "
            f"every {self.spec.period_us:g} us "
            f"({self.spec.rate_hz:g} Hz), starting at "
            f"{self.spec.delay_us:g} us, lasting {self.duration_us:g} us"
        )


@dataclass(frozen=True)
class SequencePlan:
    """An arbitrary timed sequence: one pulse per timestamp.

    This is the driver's *dynamic* protocol - `IStimProtocol.IsDynamic` is
    true when `Pulses.Length == TimestampsMicroSec.Length`. Use it when the
    pulses differ from one another; a train reusing a single pulse is the
    static case and `TrainPlan` covers it.
    """

    pulse_plans: tuple
    timestamps_us: tuple
    name: str = "sequence"

    @property
    def count(self) -> int:
        return len(self.timestamps_us)

    @property
    def duration_us(self) -> float:
        if not self.timestamps_us:
            return 0.0
        return self.timestamps_us[-1] + self.pulse_plans[-1].total_us

    @property
    def net_charge_pc(self) -> float:
        return sum(p.net_charge_pc for p in self.pulse_plans)

    def shifted_by(self, offset_us: float) -> "SequencePlan":
        """Move every timestamp later by `offset_us`. See `TrainPlan.shifted_by`."""
        if offset_us < 0:
            raise ValueError(f"offset_us must not be negative, got {offset_us}")
        return replace(
            self,
            timestamps_us=tuple(t + offset_us for t in self.timestamps_us),
        )


def plan_train(
    spec: TrainSpec,
    constraints: StimConstraints,
    *,
    require_charge_balance: bool = True,
    allow_short_period: bool = False,
) -> TrainPlan:
    """Validate a train and compute its timestamps.

    Raises `TrainValidationError` listing every problem, or
    `PulseValidationError` if the pulse itself cannot be built.

    `allow_short_period=True` permits a period below the driver's own
    `MinDistance` of 1000 us. That constant governs the driver's train
    builder; whether it also binds timestamps passed straight to `Send` is
    unverified, so the override exists rather than a hard block. The
    pulse-overlap check is never waived - that one is arithmetic, not policy.
    """
    # Build the pulse first: a train of an invalid pulse is not worth checking.
    pulse_plan = plan_pulse(
        spec.pulse, constraints, require_charge_balance=require_charge_balance
    )

    problems = []

    if spec.count < 1:
        problems.append(f"count must be at least 1, got {spec.count}")
    elif spec.count > MAX_TRAIN_PULSES:
        problems.append(
            f"count {spec.count} exceeds the maximum of {MAX_TRAIN_PULSES} "
            "queued stimuli (StimulusBaseTrainConfigurationOptionsConstants."
            "MaxCount, and the API introduction PDF independently). Split the "
            "train, or drive the repeats from the host."
        )

    if spec.period_us <= 0:
        problems.append(f"period_us must be positive, got {spec.period_us}")
    else:
        # The check that catches real mistakes: a period shorter than the
        # pulse means the next stimulus starts before this one has finished.
        if spec.count > 1 and spec.period_us < pulse_plan.total_us:
            problems.append(
                f"period {spec.period_us:g} us is shorter than the pulse "
                f"itself ({pulse_plan.total_us} us), so stimuli would overlap. "
                f"The fastest this pulse can repeat is "
                f"{1_000_000.0 / pulse_plan.total_us:.1f} Hz."
            )
        if not allow_short_period and spec.period_us < MIN_PERIOD_US:
            problems.append(
                f"period {spec.period_us:g} us is below the driver's minimum "
                f"distance of {MIN_PERIOD_US} us ({1_000_000.0 / MIN_PERIOD_US:g} "
                "Hz). That constant governs the driver's own train builder; "
                "whether it binds timestamps passed straight to Send is "
                "unverified. Pass allow_short_period=True to try it anyway."
            )
        if spec.period_us > MAX_PERIOD_US:
            problems.append(
                f"period {spec.period_us:g} us exceeds the driver's maximum "
                f"distance of {MAX_PERIOD_US} us ({MAX_PERIOD_US / 1e6:g} s)"
            )
        problems.extend(_grid_problem("period_us", spec.period_us, constraints))

    if spec.delay_us < 0:
        problems.append(f"delay_us must not be negative, got {spec.delay_us}")
    else:
        problems.extend(_grid_problem("delay_us", spec.delay_us, constraints))

    if problems:
        raise TrainValidationError(problems)

    timestamps = tuple(
        spec.delay_us + i * spec.period_us for i in range(spec.count)
    )
    return TrainPlan(spec=spec, pulse_plan=pulse_plan, timestamps_us=timestamps)


def plan_sequence(
    pulses,
    timestamps_us,
    constraints: StimConstraints,
    *,
    name: str = "sequence",
    require_charge_balance: bool = True,
) -> SequencePlan:
    """Validate an arbitrary timed sequence of differing pulses.

    `pulses` and `timestamps_us` must be the same length - that equality is
    what makes the driver treat the protocol as dynamic. Timestamps must be
    strictly increasing and far enough apart that consecutive pulses do not
    overlap.
    """
    pulses = list(pulses)
    timestamps = [float(t) for t in timestamps_us]
    problems = []

    if len(pulses) != len(timestamps):
        raise TrainValidationError(
            [
                f"{len(pulses)} pulses against {len(timestamps)} timestamps. A "
                "dynamic protocol pairs exactly one pulse with each timestamp; "
                "unequal lengths are what makes the driver read it as static "
                "instead."
            ]
        )
    if not pulses:
        raise TrainValidationError(["a sequence needs at least one pulse"])
    if len(pulses) > MAX_TRAIN_PULSES:
        problems.append(
            f"{len(pulses)} pulses exceeds the maximum of {MAX_TRAIN_PULSES} "
            "queued stimuli"
        )

    plans = []
    for index, pulse in enumerate(pulses):
        try:
            plans.append(
                plan_pulse(
                    pulse,
                    constraints,
                    require_charge_balance=require_charge_balance,
                )
            )
        except PulseValidationError as exc:
            for problem in exc.problems:
                problems.append(f"pulse {index} ({pulse.name}): {problem}")

    for index, timestamp in enumerate(timestamps):
        if timestamp < 0:
            problems.append(f"timestamp {index} is negative ({timestamp} us)")
        problems.extend(
            _grid_problem(f"timestamp {index}", timestamp, constraints)
        )

    for index in range(1, len(timestamps)):
        if timestamps[index] <= timestamps[index - 1]:
            problems.append(
                f"timestamp {index} ({timestamps[index]:g} us) does not come "
                f"after timestamp {index - 1} ({timestamps[index - 1]:g} us); "
                "timestamps must strictly increase"
            )
        elif len(plans) == len(pulses):
            gap = timestamps[index] - timestamps[index - 1]
            previous = plans[index - 1].total_us
            if gap < previous:
                problems.append(
                    f"pulse {index - 1} lasts {previous} us but pulse {index} "
                    f"starts {gap:g} us later, so they would overlap"
                )

    if problems:
        raise TrainValidationError(problems)

    return SequencePlan(
        pulse_plans=tuple(plans), timestamps_us=tuple(timestamps), name=name
    )


def ramp(
    base: PulseSpec,
    count: int,
    from_amplitude: float,
    to_amplitude: float,
) -> list:
    """A sequence of pulses whose amplitude sweeps linearly.

    Both phases are scaled by the same factor, so a charge-balanced base pulse
    stays balanced at every step. Mirrors the driver's own
    `GetDynamicPulseProtocol(fromAmplitude, toAmplitude)`, but produces plain
    specs that `plan_sequence` can check.
    """
    if count < 1:
        raise ValueError(f"count must be at least 1, got {count}")
    if base.amplitude1 == 0:
        raise ValueError("base pulse has zero amplitude; nothing to scale")

    pulses = []
    for index in range(count):
        fraction = 0.0 if count == 1 else index / (count - 1)
        amplitude = from_amplitude + (to_amplitude - from_amplitude) * fraction
        scale = amplitude / base.amplitude1
        pulses.append(
            replace(
                base,
                amplitude1=amplitude,
                amplitude2=base.amplitude2 * scale,
                name=f"{base.name}[{index}]",
            )
        )
    return pulses


def _grid_problem(label, value, constraints):
    """Yield a problem if a duration is not a whole number of ticks."""
    try:
        constraints.ticks_for(value)
    except ValueError as exc:
        return [f"{label}: {exc}"]
    return []
