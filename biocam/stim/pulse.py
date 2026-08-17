"""Layer 2 - describing a stimulus pulse and checking it before it is built.

A `PulseSpec` is what the experimenter means: amplitudes in the stimulator's
unit, durations in microseconds. `plan()` turns one into a `PulsePlan`, the
tick-domain form the driver's constructor takes - or refuses it.

Refusing is the point. `RectangularStimPulse` accepts almost anything and
quietly adjusts it:

- an amplitude beyond the range is clamped to the range;
- an amplitude off the resolution grid is rounded onto it;
- a pulse longer than the maximum has its **later phases shortened**, so a
  charge-balanced request can become one that injects net charge.

None of these raise. The last is the dangerous one: 8000/0/8000 us at +-100 uA
comes back as 8000/0/2000, a net 600 nC where zero was asked for, with
`IsBiphasic` still reporting True. Net DC through a microelectrode drives
electrolysis and corrodes the electrode. There is no in-band signal for it.

So every limit is checked here first, and `verify_built_pulse()` compares the
object .NET actually returned against what was asked for. Two independent
chances to catch a silent adjustment, neither of which relies on the driver
reporting one.
"""

from dataclasses import dataclass

from biocam.stim.constraints import StimConstraints


class PulseValidationError(ValueError):
    """A pulse the stimulator would silently alter, or reject outright.

    Carries every problem found, not just the first. A colleague running this
    on the instrument gets one attempt per turnaround; a list of three faults
    beats three round trips.
    """

    def __init__(self, problems):
        self.problems = list(problems)
        super().__init__(
            "pulse cannot be built as specified:\n  - "
            + "\n  - ".join(self.problems)
        )


@dataclass(frozen=True)
class PulseSpec:
    """A biphasic rectangular pulse in physical units.

    Durations are microseconds; amplitudes are in the stimulator's unit (uA on
    a current stimulator, which the DupleX is). The second phase normally has
    the opposite sign to the first, so that the charge injected by one is
    withdrawn by the other.

    A monophasic pulse is expressed as `amplitude2 = 0, phase2_us = 0`. It is
    representable, and `plan()` will report it as charge-unbalanced.
    """

    amplitude1: float
    phase1_us: float
    inter_us: float = 0.0
    amplitude2: float = 0.0
    phase2_us: float = 0.0
    name: str = "pulse"

    @property
    def total_us(self) -> float:
        return self.phase1_us + self.inter_us + self.phase2_us

    @property
    def net_charge_pc(self) -> float:
        """Net injected charge in picocoulombs.

        uA x us is pC exactly (1e-6 A x 1e-6 s = 1e-12 C), so no scaling
        factor is needed - and none is applied, which is why this is safe to
        compare against zero.
        """
        return self.amplitude1 * self.phase1_us + self.amplitude2 * self.phase2_us

    def snapped(self, constraints: StimConstraints) -> "PulseSpec":
        """Return this spec with amplitudes moved onto the resolution grid.

        Opt-in, and it returns a new spec rather than mutating, so the caller
        can see exactly what changed. `plan()` refuses off-grid amplitudes
        instead of doing this silently.
        """
        from dataclasses import replace

        return replace(
            self,
            amplitude1=constraints.snap_amplitude(self.amplitude1),
            amplitude2=constraints.snap_amplitude(self.amplitude2),
        )


@dataclass(frozen=True)
class PulsePlan:
    """A validated pulse in the driver's own terms, ready to construct.

    `width1`, `inter_width` and `width2` are tick counts. Passing anything
    else to `RectangularStimPulse` means passing a duration the driver will
    reinterpret.
    """

    spec: PulseSpec
    constraints: StimConstraints
    width1: int
    inter_width: int
    width2: int

    @property
    def total_ticks(self) -> int:
        return self.width1 + self.inter_width + self.width2

    @property
    def total_us(self) -> int:
        return self.total_ticks * self.constraints.time_resolution_us

    @property
    def net_charge_pc(self) -> float:
        return self.spec.net_charge_pc

    @property
    def is_charge_balanced(self) -> bool:
        return self.net_charge_pc == 0.0

    def constructor_args(self):
        """The positional arguments `RectangularStimPulse` takes, after name
        and constraints:

            RectangularStimPulse(friendlyName, constraints,
                                 amplitude1, width1, interWidth,
                                 amplitude2, width2)

        Verified by reflection over 3Brain.Common - see
        `python -m biocam.interop.reflect RectangularStimPulse`.
        """
        return (
            self.spec.amplitude1,
            self.width1,
            self.inter_width,
            self.spec.amplitude2,
            self.width2,
        )

    def describe(self) -> str:
        unit = self.constraints.unit
        balance = (
            "balanced"
            if self.is_charge_balanced
            else f"UNBALANCED, net {self.net_charge_pc:+.1f} pC"
        )
        return (
            f"{self.spec.name}: {self.spec.amplitude1:+g} {unit} for "
            f"{self.spec.phase1_us:g} us, gap {self.spec.inter_us:g} us, "
            f"{self.spec.amplitude2:+g} {unit} for {self.spec.phase2_us:g} us "
            f"({self.total_us} us total, {self.total_ticks} ticks; {balance})"
        )


def plan(
    spec: PulseSpec,
    constraints: StimConstraints,
    *,
    require_charge_balance: bool = True,
) -> PulsePlan:
    """Validate a pulse against the stimulator's limits and convert to ticks.

    Raises `PulseValidationError` listing every problem. Nothing is adjusted:
    a spec either passes unchanged or is refused, so that what reaches the
    instrument is what was written down.

    `require_charge_balance=False` permits a deliberately unbalanced pulse -
    a monophasic one, say. It is not the default because an unbalanced pulse
    is more often an oversight than a decision, and the cost lands on the
    electrode and the culture rather than on the run.
    """
    problems = []

    # --- durations ------------------------------------------------------
    widths = {}
    for field, value in (
        ("phase1_us", spec.phase1_us),
        ("inter_us", spec.inter_us),
        ("phase2_us", spec.phase2_us),
    ):
        if value < 0:
            problems.append(f"{field} is negative ({value} us)")
            continue
        try:
            widths[field] = constraints.ticks_for(value)
        except ValueError as exc:
            problems.append(f"{field}: {exc}")

    if spec.phase1_us <= 0:
        problems.append(
            f"phase1_us must be positive ({spec.phase1_us} us); a pulse with "
            "no first phase delivers nothing"
        )

    if len(widths) == 3:
        total_ticks = sum(widths.values())
        if total_ticks > constraints.max_total_ticks:
            problems.append(
                f"total duration {spec.total_us:g} us "
                f"({total_ticks} ticks) exceeds the maximum "
                f"{constraints.max_total_us} us "
                f"({constraints.max_total_ticks} ticks). The driver would not "
                "reject this - it shortens the later phases to fit, which "
                "changes the charge balance without reporting it."
            )

    # --- amplitudes -----------------------------------------------------
    for field, value in (
        ("amplitude1", spec.amplitude1),
        ("amplitude2", spec.amplitude2),
    ):
        if not (constraints.min_amplitude <= value <= constraints.max_amplitude):
            problems.append(
                f"{field} {value:g} {constraints.unit} is outside the "
                f"stimulator's range [{constraints.min_amplitude:g}, "
                f"{constraints.max_amplitude:g}] {constraints.unit}. The "
                f"driver would clamp it to "
                f"{max(constraints.min_amplitude, min(constraints.max_amplitude, value)):g} "
                "without reporting it."
            )
        elif not constraints.is_on_amplitude_grid(value):
            problems.append(
                f"{field} {value:g} {constraints.unit} is not a multiple of "
                f"the amplitude resolution {constraints.amplitude_resolution:g} "
                f"{constraints.unit}. The driver would round it to "
                f"{constraints.snap_amplitude(value):g} without reporting it. "
                "Call PulseSpec.snapped() to accept that explicitly."
            )

    # --- charge ---------------------------------------------------------
    if require_charge_balance and spec.net_charge_pc != 0:
        problems.append(
            f"net charge is {spec.net_charge_pc:+g} pC, not zero "
            f"(phase 1 injects {spec.amplitude1 * spec.phase1_us:+g} pC, "
            f"phase 2 {spec.amplitude2 * spec.phase2_us:+g} pC). Sustained net "
            "charge drives electrolysis at the electrode. Pass "
            "require_charge_balance=False if this is deliberate."
        )

    if problems:
        raise PulseValidationError(problems)

    return PulsePlan(
        spec=spec,
        constraints=constraints,
        width1=widths["phase1_us"],
        inter_width=widths["inter_us"],
        width2=widths["phase2_us"],
    )


def verify_built_pulse(plan_: PulsePlan, built) -> None:
    """Compare a constructed `RectangularStimPulse` against its plan.

    `plan()` checks the limits this repository knows about. This checks the
    object the driver actually returned, which also covers limits it does not
    know about - a firmware revision with a shorter maximum, a constraints
    object read from a different device, an assumption that was wrong.

    Raises `PulseValidationError` if any field differs. Call it every time a
    pulse is built; it is five attribute reads against a stimulus that may run
    for hours.
    """
    expected = {
        "Amplitude1": plan_.spec.amplitude1,
        "Width1": plan_.width1,
        "InterWidth": plan_.inter_width,
        "Amplitude2": plan_.spec.amplitude2,
        "Width2": plan_.width2,
    }
    problems = []
    for name, want in expected.items():
        got = getattr(built, name)
        if got != want:
            problems.append(
                f"{name}: asked for {want!r}, the driver built {got!r} - it "
                "adjusted the pulse silently"
            )
    if problems:
        problems.append(
            f"planned {plan_.describe()}"
        )
        raise PulseValidationError(problems)
