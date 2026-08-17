"""Layer 1 - checking `biocam.stim`'s model against the real assembly.

`biocam.stim.plan()` refuses pulses on the grounds that the driver would
otherwise alter them silently. That claim is only worth anything if it is
true of the actual `RectangularStimPulse`, so this module checks it both ways:

- every pulse `plan()` **accepts** must come back from the driver unchanged;
- every pulse `plan()` **refuses** must be one the driver would have altered
  without saying so.

The second direction is the one that keeps the validation honest. A rule that
refuses pulses the driver would have built correctly is not caution, it is a
bug that blocks legitimate experiments.

Needs the 3Brain DLLs and pythonnet. Does **not** need the instrument: nothing
here claims a BioCAM or activates the pool - `RectangularStimPulse` and
`StimProperties` are ordinary managed objects.

    python -m biocam.interop.verify_stim_model

Exits non-zero if the model and the assembly disagree.

Note this checks the model against `StimProperties` objects *we* construct.
The DupleX's own constraints can only be read from the instrument, so the
numbers - not the rules - still need confirming in the lab.
"""

import io
import sys

from biocam.stim import (
    PulseSpec,
    PulseValidationError,
    StimConstraints,
    plan,
    verify_built_pulse,
)

# (label, spec, constraints, expect_plan_to_accept)
#
# Each refused case names the silent adjustment it is protecting against.
_CONSTRAINTS_1US = StimConstraints(
    time_resolution_us=1, amplitude_resolution=1.0,
    min_amplitude=-1000.0, max_amplitude=1000.0, max_total_ticks=10000,
)
_CONSTRAINTS_COARSE_AMP = StimConstraints(
    time_resolution_us=1, amplitude_resolution=5.0,
    min_amplitude=-1000.0, max_amplitude=1000.0, max_total_ticks=10000,
)

CASES = [
    (
        "balanced, well inside every limit",
        PulseSpec(100.0, 100.0, 50.0, -100.0, 100.0),
        _CONSTRAINTS_1US,
        True,
    ),
    (
        "balanced, exactly at the duration limit",
        PulseSpec(100.0, 5000.0, 0.0, -100.0, 5000.0),
        _CONSTRAINTS_1US,
        True,
    ),
    (
        "amplitude exactly at the range limit",
        PulseSpec(1000.0, 100.0, 0.0, -1000.0, 100.0),
        _CONSTRAINTS_1US,
        True,
    ),
    (
        "asymmetric but charge-balanced",
        PulseSpec(100.0, 100.0, 0.0, -50.0, 200.0),
        _CONSTRAINTS_1US,
        True,
    ),
    (
        "one tick over the duration limit -> driver shortens phase 2",
        PulseSpec(100.0, 5000.0, 1.0, -100.0, 5000.0),
        _CONSTRAINTS_1US,
        False,
    ),
    (
        "far over the duration limit -> balanced request becomes net DC",
        PulseSpec(100.0, 8000.0, 0.0, -100.0, 8000.0),
        _CONSTRAINTS_1US,
        False,
    ),
    (
        "inter-phase gap pushes it over -> driver zeroes phase 2",
        PulseSpec(100.0, 10000.0, 1.0, -100.0, 1.0),
        _CONSTRAINTS_1US,
        False,
    ),
    (
        "amplitude above the range -> driver clamps it",
        PulseSpec(2000.0, 100.0, 0.0, -2000.0, 100.0),
        _CONSTRAINTS_1US,
        False,
    ),
    (
        "amplitude below the range -> driver clamps it",
        PulseSpec(-2000.0, 100.0, 0.0, 2000.0, 100.0),
        _CONSTRAINTS_1US,
        False,
    ),
    (
        "amplitude off the resolution grid -> driver rounds it",
        PulseSpec(7.0, 100.0, 0.0, -7.0, 100.0),
        _CONSTRAINTS_COARSE_AMP,
        False,
    ),
]


def _stim_properties(constraints):
    """Build a .NET StimProperties matching a StimConstraints."""
    from _3Brain.Common import StimProperties

    properties = StimProperties(
        int(constraints.time_resolution_us),
        float(constraints.amplitude_resolution),
        int(constraints.min_amplitude),
        int(constraints.max_amplitude),
    )
    properties.MaxPulseDuration = int(constraints.max_total_ticks)
    return properties


def _build(spec, constraints):
    """Construct the driver's pulse from a spec, bypassing validation.

    Durations are converted with plain integer division rather than
    `constraints.ticks_for`, because several cases deliberately ask for values
    `ticks_for` would refuse - the point is to see what the driver does with
    them.
    """
    from _3Brain.Common import RectangularStimPulse

    resolution = constraints.time_resolution_us
    return RectangularStimPulse(
        spec.name,
        _stim_properties(constraints),
        float(spec.amplitude1),
        int(spec.phase1_us // resolution),
        int(spec.inter_us // resolution),
        float(spec.amplitude2),
        int(spec.phase2_us // resolution),
    )


def _readback(built):
    return (
        built.Amplitude1,
        built.Width1,
        built.InterWidth,
        built.Amplitude2,
        built.Width2,
    )


def _requested(spec, constraints):
    resolution = constraints.time_resolution_us
    return (
        float(spec.amplitude1),
        int(spec.phase1_us // resolution),
        int(spec.inter_us // resolution),
        float(spec.amplitude2),
        int(spec.phase2_us // resolution),
    )


def main(argv=None) -> int:
    # Obfuscated stack traces in these assemblies carry private-use codepoints
    # that a cp1252 console cannot encode; printing one would raise
    # UnicodeEncodeError and hide the real result.
    if hasattr(sys.stdout, "buffer"):
        sys.stdout = io.TextIOWrapper(
            sys.stdout.buffer, encoding="utf-8", errors="backslashreplace"
        )

    from biocam.interop.device import load_assemblies

    load_assemblies()

    failures = []
    print(f"Checking {len(CASES)} cases against the real RectangularStimPulse\n")

    for label, spec, constraints, expect_accept in CASES:
        try:
            planned = plan(spec, constraints)
            accepted = True
        except PulseValidationError:
            planned = None
            accepted = False

        if accepted != expect_accept:
            failures.append(
                f"{label}: plan() {'accepted' if accepted else 'refused'} it, "
                f"but the case expects it to be "
                f"{'accepted' if expect_accept else 'refused'}"
            )
            print(f"  [MODEL] {label}")
            continue

        try:
            built = _build(spec, constraints)
        except Exception as exc:  # noqa: BLE001 - a raising ctor is a valid outcome
            if accepted:
                failures.append(
                    f"{label}: plan() accepted it but the driver raised "
                    f"{type(exc).__name__}"
                )
                print(f"  [RAISED] {label}: {type(exc).__name__}")
            else:
                print(f"  [ok] {label}\n         driver raised "
                      f"{type(exc).__name__} - refusing it was correct")
            continue

        got = _readback(built)
        want = _requested(spec, constraints)

        if accepted:
            # The driver must have built exactly what was asked for.
            try:
                verify_built_pulse(planned, built)
                print(f"  [ok] {label}\n         driver built it unchanged: {got}")
            except PulseValidationError as exc:
                failures.append(
                    f"{label}: plan() accepted it but the driver altered it - "
                    f"asked {want}, got {got}"
                )
                print(f"  [ALTERED] {label}\n         {exc}")
        else:
            # The refusal is only justified if the driver would have altered it.
            if got == want:
                failures.append(
                    f"{label}: plan() refused it, but the driver would have "
                    f"built it correctly ({got}). The rule is too strict and "
                    "blocks a legitimate pulse."
                )
                print(f"  [TOO STRICT] {label}\n         driver built it "
                      f"unchanged: {got}")
            else:
                print(f"  [ok] {label}\n         asked {want}\n"
                      f"         got   {got}  <- silent adjustment, refusing "
                      "it was correct")

    print()
    if failures:
        print(f"{len(failures)} DISAGREEMENT(S) between biocam.stim and the "
              "assembly:")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print(f"All {len(CASES)} cases agree: biocam.stim.plan() accepts exactly "
          "the pulses the driver builds faithfully.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
