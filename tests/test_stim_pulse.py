import pytest

from biocam.stim import (
    PulseSpec,
    PulseValidationError,
    StimConstraints,
    plan,
    verify_built_pulse,
)

# The DupleX constraints as this repository currently understands them. The
# stimulator's real StimProperties must be read from the device - issue
# "confirm the DupleX StimProperties" covers checking these numbers. They are
# used here because the *rules* under test do not depend on the exact values.
DUPLEX = StimConstraints(
    time_resolution_us=10,
    amplitude_resolution=1.0,
    min_amplitude=-1000.0,
    max_amplitude=1000.0,
    max_total_ticks=1000,
)

# The placeholder shipped as StimProperties.Default, measured by reflection.
DEFAULT_PROPERTIES = StimConstraints(
    time_resolution_us=1,
    amplitude_resolution=1.0,
    min_amplitude=-1000.0,
    max_amplitude=1000.0,
    max_total_ticks=10000,
)

BALANCED = PulseSpec(
    amplitude1=100.0, phase1_us=200.0, inter_us=100.0,
    amplitude2=-100.0, phase2_us=200.0, name="balanced",
)


# --------------------------------------------------------------------------
# constraints
# --------------------------------------------------------------------------

def test_ticks_for_exact_multiple():
    assert DUPLEX.ticks_for(200.0) == 20


def test_ticks_for_zero():
    assert DUPLEX.ticks_for(0) == 0


def test_ticks_for_refuses_a_partial_tick():
    # 205 us is 20.5 ticks. The driver would snap it; we refuse instead.
    with pytest.raises(ValueError, match="not a whole number"):
        DUPLEX.ticks_for(205.0)


def test_ticks_for_is_exact_on_values_that_float_division_gets_wrong():
    # 0.1-style values are why this uses Decimal rather than %.
    fine = StimConstraints(
        time_resolution_us=1, amplitude_resolution=0.1,
        min_amplitude=-10.0, max_amplitude=10.0, max_total_ticks=100,
    )
    assert fine.ticks_for(3) == 3


def test_max_total_us_converts_ticks_to_time():
    assert DUPLEX.max_total_us == 10000


def test_amplitude_grid_accepts_multiples():
    assert DUPLEX.is_on_amplitude_grid(7.0)


def test_amplitude_grid_rejects_non_multiples():
    assert not DUPLEX.is_on_amplitude_grid(7.3)


def test_snap_rounds_halves_away_from_zero_like_dotnet():
    # Measured: at a resolution of 1.0 the driver turns 7.5 into 8.0.
    # Python's round() would give 8 here but 2 for 2.5 - banker's rounding
    # disagrees with .NET on exact halves, so snap_amplitude must not use it.
    assert DUPLEX.snap_amplitude(7.5) == 8.0
    assert DUPLEX.snap_amplitude(2.5) == 3.0
    assert round(2.5) == 2  # the disagreement this guards against


def test_snap_rounds_negative_halves_away_from_zero():
    assert DUPLEX.snap_amplitude(-2.5) == -3.0


def test_snap_to_a_coarse_grid():
    coarse = StimConstraints(
        time_resolution_us=10, amplitude_resolution=5.0,
        min_amplitude=-1000.0, max_amplitude=1000.0, max_total_ticks=1000,
    )
    # Measured against the driver: 7.0 -> 5.0, 7.5 -> 10.0, 12.4 -> 10.0.
    assert coarse.snap_amplitude(7.0) == 5.0
    assert coarse.snap_amplitude(7.5) == 10.0
    assert coarse.snap_amplitude(12.4) == 10.0


def test_constraints_reject_nonsense():
    with pytest.raises(ValueError, match="time_resolution_us"):
        StimConstraints(0, 1.0, -1.0, 1.0, 10)
    with pytest.raises(ValueError, match="amplitude_resolution"):
        StimConstraints(1, 0.0, -1.0, 1.0, 10)
    with pytest.raises(ValueError, match="max_total_ticks"):
        StimConstraints(1, 1.0, -1.0, 1.0, 0)
    with pytest.raises(ValueError, match="exceeds max_amplitude"):
        StimConstraints(1, 1.0, 5.0, -5.0, 10)


def test_matches_numerically_ignores_the_unit_string():
    # The device reports 'µA' (U+00B5); a hand-built StimConstraints defaults
    # to ASCII 'uA'. Comparing whole dataclasses would reject a plan that is
    # numerically identical to what the instrument would accept.
    from dataclasses import replace

    device = replace(DUPLEX, unit="µA")
    assert device != DUPLEX
    assert DUPLEX.matches_numerically(device)


def test_matches_numerically_catches_a_different_time_resolution():
    from dataclasses import replace

    # The StimProperties.Default trap: 1 us where the device is coarser.
    assert not DUPLEX.matches_numerically(replace(DUPLEX, time_resolution_us=1))


def test_matches_numerically_catches_every_numeric_field():
    from dataclasses import replace

    for field, value in (
        ("time_resolution_us", 5),
        ("amplitude_resolution", 2.0),
        ("min_amplitude", -500.0),
        ("max_amplitude", 500.0),
        ("max_total_ticks", 999),
    ):
        assert not DUPLEX.matches_numerically(replace(DUPLEX, **{field: value})), (
            f"a difference in {field} was not caught"
        )


def test_from_stim_properties_reads_a_live_object():
    class FakeProperties:
        TimeResolutionMicroSec = 10
        AmplitudeResolution = 2.5
        MinAmplitude = -500.0
        MaxAmplitude = 500.0
        MaxPulseDuration = 4000
        UnitMeasureString = "µA"
        IsCurrentStimulator = True

    got = StimConstraints.from_stim_properties(FakeProperties())
    assert got == StimConstraints(
        time_resolution_us=10, amplitude_resolution=2.5,
        min_amplitude=-500.0, max_amplitude=500.0, max_total_ticks=4000,
        unit="µA", is_current=True,
    )


# --------------------------------------------------------------------------
# planning: the happy path
# --------------------------------------------------------------------------

def test_a_balanced_pulse_plans_cleanly():
    p = plan(BALANCED, DUPLEX)
    assert (p.width1, p.inter_width, p.width2) == (20, 10, 20)
    assert p.total_ticks == 50
    assert p.total_us == 500
    assert p.is_charge_balanced


def test_constructor_args_are_in_the_order_the_driver_takes():
    # RectangularStimPulse(name, constraints, amplitude1, width1,
    #                      interWidth, amplitude2, width2)
    assert plan(BALANCED, DUPLEX).constructor_args() == (
        100.0, 20, 10, -100.0, 20,
    )


def test_asymmetric_but_balanced_pulse_is_accepted():
    # Half the amplitude for twice as long carries the same charge.
    spec = PulseSpec(
        amplitude1=100.0, phase1_us=100.0, inter_us=0.0,
        amplitude2=-50.0, phase2_us=200.0,
    )
    assert plan(spec, DUPLEX).is_charge_balanced


def test_zero_inter_phase_gap_is_fine():
    spec = PulseSpec(100.0, 100.0, 0.0, -100.0, 100.0)
    assert plan(spec, DUPLEX).inter_width == 0


def test_describe_mentions_the_balance():
    assert "balanced" in plan(BALANCED, DUPLEX).describe()


# --------------------------------------------------------------------------
# planning: each silent adjustment the driver would have made
# --------------------------------------------------------------------------

def test_over_long_pulse_is_refused_rather_than_truncated():
    # Measured: asking for 8000/0/8000 ticks against a 10000-tick maximum
    # returns 8000/0/2000 - a balanced request becoming a net-DC pulse, with
    # IsBiphasic still True. This is the defect the whole module exists for.
    spec = PulseSpec(100.0, 8000.0, 0.0, -100.0, 8000.0)
    with pytest.raises(PulseValidationError) as exc:
        plan(spec, DEFAULT_PROPERTIES)
    assert "exceeds the maximum" in str(exc.value)
    assert "charge balance" in str(exc.value)


def test_over_long_by_a_single_tick_is_still_refused():
    # 5000/1/5000 comes back as 5000/1/4999. One tick, but it unbalances
    # 100 uA x 1 us of charge every pulse, every pulse of a long train.
    spec = PulseSpec(100.0, 5000.0, 1.0, -100.0, 5000.0)
    with pytest.raises(PulseValidationError, match="exceeds the maximum"):
        plan(spec, DEFAULT_PROPERTIES)


def test_amplitude_above_the_range_is_refused_rather_than_clamped():
    spec = PulseSpec(2000.0, 200.0, 0.0, -2000.0, 200.0)
    with pytest.raises(PulseValidationError) as exc:
        plan(spec, DUPLEX)
    assert "outside the stimulator's range" in str(exc.value)
    assert "clamp it to 1000" in str(exc.value)


def test_amplitude_below_the_range_is_refused():
    spec = PulseSpec(-2000.0, 200.0, 0.0, 2000.0, 200.0)
    with pytest.raises(PulseValidationError, match="outside the stimulator"):
        plan(spec, DUPLEX)


def test_off_grid_amplitude_is_refused_rather_than_rounded():
    coarse = StimConstraints(
        time_resolution_us=10, amplitude_resolution=5.0,
        min_amplitude=-1000.0, max_amplitude=1000.0, max_total_ticks=1000,
    )
    spec = PulseSpec(7.0, 200.0, 0.0, -7.0, 200.0)
    with pytest.raises(PulseValidationError) as exc:
        plan(spec, coarse)
    assert "not a multiple of the amplitude resolution" in str(exc.value)
    assert "round it to 5" in str(exc.value)


def test_partial_tick_duration_is_refused_rather_than_snapped():
    spec = PulseSpec(100.0, 205.0, 0.0, -100.0, 205.0)
    with pytest.raises(PulseValidationError, match="not a whole number"):
        plan(spec, DUPLEX)


def test_unbalanced_pulse_is_refused_by_default():
    spec = PulseSpec(100.0, 200.0, 0.0, -50.0, 200.0)
    with pytest.raises(PulseValidationError) as exc:
        plan(spec, DUPLEX)
    assert "net charge is +10000 pC" in str(exc.value)


def test_monophasic_pulse_is_refused_by_default():
    with pytest.raises(PulseValidationError, match="net charge"):
        plan(PulseSpec(100.0, 200.0), DUPLEX)


def test_unbalanced_pulse_is_allowed_when_asked_for_explicitly():
    p = plan(PulseSpec(100.0, 200.0), DUPLEX, require_charge_balance=False)
    assert not p.is_charge_balanced
    assert p.net_charge_pc == 20000.0
    assert "UNBALANCED" in p.describe()


def test_negative_duration_is_refused():
    spec = PulseSpec(100.0, 200.0, -10.0, -100.0, 200.0)
    with pytest.raises(PulseValidationError, match="inter_us is negative"):
        plan(spec, DUPLEX)


def test_zero_first_phase_is_refused():
    spec = PulseSpec(100.0, 0.0, 0.0, 0.0, 0.0)
    with pytest.raises(PulseValidationError, match="phase1_us must be positive"):
        plan(spec, DUPLEX)


def test_every_problem_is_reported_not_just_the_first():
    # One turnaround to the lab is a day. Report all four faults at once.
    spec = PulseSpec(
        amplitude1=5000.0,   # out of range
        phase1_us=205.0,     # not a whole tick
        inter_us=-1.0,       # negative
        amplitude2=-3.0,     # in range, on grid - fine
        phase2_us=200.0,     # leaves the pulse unbalanced too
    )
    with pytest.raises(PulseValidationError) as exc:
        plan(spec, DUPLEX)
    problems = exc.value.problems
    assert len(problems) >= 4
    joined = "\n".join(problems)
    assert "amplitude1" in joined
    assert "phase1_us" in joined
    assert "inter_us is negative" in joined
    assert "net charge" in joined


def test_snapped_moves_amplitudes_onto_the_grid_without_mutating():
    coarse = StimConstraints(
        time_resolution_us=10, amplitude_resolution=5.0,
        min_amplitude=-1000.0, max_amplitude=1000.0, max_total_ticks=1000,
    )
    spec = PulseSpec(7.0, 200.0, 0.0, -7.0, 200.0)
    snapped = spec.snapped(coarse)
    assert (snapped.amplitude1, snapped.amplitude2) == (5.0, -5.0)
    assert (spec.amplitude1, spec.amplitude2) == (7.0, -7.0)
    assert plan(snapped, coarse).is_charge_balanced


# --------------------------------------------------------------------------
# verifying what the driver actually built
# --------------------------------------------------------------------------

class FakePulse:
    """Stands in for a constructed RectangularStimPulse."""

    def __init__(self, a1, w1, iw, a2, w2):
        self.Amplitude1 = a1
        self.Width1 = w1
        self.InterWidth = iw
        self.Amplitude2 = a2
        self.Width2 = w2


def test_verify_passes_when_the_driver_built_what_was_asked():
    p = plan(BALANCED, DUPLEX)
    verify_built_pulse(p, FakePulse(100.0, 20, 10, -100.0, 20))


def test_verify_catches_a_truncated_second_phase():
    # The exact failure measured on the real driver, reproduced against the
    # fake: the object comes back with a shorter Width2 and says nothing.
    p = plan(BALANCED, DUPLEX)
    with pytest.raises(PulseValidationError) as exc:
        verify_built_pulse(p, FakePulse(100.0, 20, 10, -100.0, 5))
    assert "Width2: asked for 20, the driver built 5" in str(exc.value)


def test_verify_catches_a_clamped_amplitude():
    p = plan(BALANCED, DUPLEX)
    with pytest.raises(PulseValidationError, match="Amplitude1"):
        verify_built_pulse(p, FakePulse(50.0, 20, 10, -100.0, 20))


def test_verify_reports_every_field_that_differs():
    p = plan(BALANCED, DUPLEX)
    with pytest.raises(PulseValidationError) as exc:
        verify_built_pulse(p, FakePulse(50.0, 1, 2, -50.0, 3))
    # five fields wrong, plus the planned-pulse description
    assert len(exc.value.problems) == 6
