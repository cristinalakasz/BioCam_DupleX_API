import pytest

from biocam.stim import (
    Electrode,
    ElectrodeGrid,
    PatternValidationError,
    PulseSpec,
    PulseValidationError,
    StimConstraints,
    StimPattern,
    TrainSpec,
    TrainValidationError,
    bipolar_pair,
    plan_sequence,
    plan_train,
    ramp,
    validate_pattern,
)
from biocam.stim.electrodes import MAX_ENDPOINT_VALUES_PER_SEND
from biocam.stim.train import MAX_TRAIN_PULSES

DUPLEX = StimConstraints(
    time_resolution_us=10,
    amplitude_resolution=1.0,
    min_amplitude=-1000.0,
    max_amplitude=1000.0,
    max_total_ticks=1000,
)

# 200 us + 100 us gap + 200 us = 500 us total, charge balanced.
PULSE = PulseSpec(100.0, 200.0, 100.0, -100.0, 200.0, name="p")


# --------------------------------------------------------------------------
# electrodes
# --------------------------------------------------------------------------

def test_grid_is_one_based():
    grid = ElectrodeGrid(64, 64)
    assert grid.contains(Electrode(1, 1))
    assert grid.contains(Electrode(64, 64))
    assert not grid.contains(Electrode(0, 0))
    assert not grid.contains(Electrode(65, 65))


def test_square_grid_from_channel_count():
    assert ElectrodeGrid.square_from_channel_count(4096) == ElectrodeGrid(64, 64)


def test_square_grid_refuses_a_non_square_count():
    with pytest.raises(ValueError, match="not a square array"):
        ElectrodeGrid.square_from_channel_count(4000)


def test_grid_must_be_positive():
    with pytest.raises(ValueError, match="positive in both dimensions"):
        ElectrodeGrid(0, 64)


def test_a_simple_bipolar_pair_validates():
    validate_pattern(bipolar_pair(Electrode(10, 10), Electrode(20, 20)))


def test_out_of_range_electrode_is_refused():
    # ChCoord(65, 65) reports IsValid == True, so this is the only place the
    # mistake can be caught.
    pattern = bipolar_pair(Electrode(65, 65), Electrode(20, 20))
    with pytest.raises(PatternValidationError) as exc:
        validate_pattern(pattern)
    assert "outside the 64x64 array" in str(exc.value)


def test_zero_indexed_electrode_is_refused():
    with pytest.raises(PatternValidationError, match="outside the 64x64 array"):
        validate_pattern(bipolar_pair(Electrode(0, 0), Electrode(20, 20)))


def test_empty_endpoint_sets_are_refused():
    with pytest.raises(PatternValidationError) as exc:
        validate_pattern(StimPattern(positive=(), negative=()))
    assert "no positive endpoints" in str(exc.value)
    assert "no negative endpoints" in str(exc.value)


def test_an_electrode_cannot_be_both_polarities():
    pattern = StimPattern(
        positive=(Electrode(10, 10),), negative=(Electrode(10, 10),)
    )
    with pytest.raises(PatternValidationError) as exc:
        validate_pattern(pattern)
    assert "both positive and negative" in str(exc.value)


def test_shared_column_is_refused():
    # Same column, different rows: legal coordinates, but the PDF forbids it.
    pattern = bipolar_pair(Electrode(10, 5), Electrode(20, 5))
    with pytest.raises(PatternValidationError) as exc:
        validate_pattern(pattern)
    assert "share column(s) [5]" in str(exc.value)


def test_shared_column_check_can_be_overridden():
    pattern = bipolar_pair(Electrode(10, 5), Electrode(20, 5))
    validate_pattern(pattern, enforce_column_rule=False)


def test_repeated_endpoint_is_reported():
    pattern = StimPattern(
        positive=(Electrode(1, 1), Electrode(1, 1)),
        negative=(Electrode(2, 2),),
    )
    with pytest.raises(PatternValidationError, match="positive endpoints repeat"):
        validate_pattern(pattern)


def test_too_many_endpoints_for_one_send_is_refused():
    positive = tuple(
        Electrode(r, c) for r in range(1, 11) for c in range(1, 16)
    )  # 150, all in columns 1-15
    negative = tuple(
        Electrode(r, c) for r in range(1, 11) for c in range(16, 31)
    )  # 150, columns 16-30 - no column overlap
    pattern = StimPattern(positive=positive, negative=negative)
    with pytest.raises(PatternValidationError) as exc:
        validate_pattern(pattern)
    assert str(MAX_ENDPOINT_VALUES_PER_SEND) in str(exc.value)


def test_pattern_stores_tuples_so_it_cannot_change_after_validation():
    electrodes = [Electrode(1, 1)]
    pattern = StimPattern(positive=electrodes, negative=[Electrode(2, 2)])
    electrodes.append(Electrode(99, 99))
    assert pattern.positive == (Electrode(1, 1),)


# --------------------------------------------------------------------------
# trains
# --------------------------------------------------------------------------

def test_a_regular_train_produces_evenly_spaced_timestamps():
    p = plan_train(TrainSpec(PULSE, count=5, period_us=100_000.0), DUPLEX)
    assert p.timestamps_us == (0.0, 100_000.0, 200_000.0, 300_000.0, 400_000.0)
    assert p.count == 5


def test_a_delay_shifts_every_timestamp():
    p = plan_train(
        TrainSpec(PULSE, count=3, period_us=100_000.0, delay_us=50_000.0), DUPLEX
    )
    assert p.timestamps_us == (50_000.0, 150_000.0, 250_000.0)


def test_at_rate_matches_the_equivalent_period():
    by_rate = plan_train(TrainSpec.at_rate(PULSE, count=4, rate_hz=10.0), DUPLEX)
    by_period = plan_train(TrainSpec(PULSE, count=4, period_us=100_000.0), DUPLEX)
    assert by_rate.timestamps_us == by_period.timestamps_us


def test_at_rate_refuses_a_non_positive_rate():
    with pytest.raises(ValueError, match="rate_hz must be positive"):
        TrainSpec.at_rate(PULSE, count=4, rate_hz=0.0)


def test_train_duration_covers_the_final_pulse():
    p = plan_train(TrainSpec(PULSE, count=3, period_us=100_000.0), DUPLEX)
    # last pulse starts at 200000 us and lasts 500 us
    assert p.duration_us == 200_500.0


def test_train_charge_is_per_pulse_charge_times_count():
    unbalanced = PulseSpec(100.0, 200.0, 0.0, -100.0, 100.0)
    p = plan_train(
        TrainSpec(unbalanced, count=100, period_us=100_000.0),
        DUPLEX,
        require_charge_balance=False,
    )
    # 100 uA x 200 us - 100 uA x 100 us = 10000 pC per pulse
    assert p.pulse_plan.net_charge_pc == 10_000.0
    assert p.net_charge_pc == 1_000_000.0


def test_a_period_shorter_than_the_pulse_is_refused():
    # The check that catches real mistakes: 500 us pulse, 300 us period.
    with pytest.raises(TrainValidationError) as exc:
        plan_train(
            TrainSpec(PULSE, count=5, period_us=300.0),
            DUPLEX,
            allow_short_period=True,
        )
    assert "stimuli would overlap" in str(exc.value)
    assert "2000.0 Hz" in str(exc.value)


def test_overlap_is_not_checked_for_a_single_pulse():
    # One pulse cannot overlap itself, so a short period is irrelevant.
    p = plan_train(
        TrainSpec(PULSE, count=1, period_us=100.0),
        DUPLEX,
        allow_short_period=True,
    )
    assert p.timestamps_us == (0.0,)


def test_a_period_below_the_driver_minimum_is_refused_by_default():
    with pytest.raises(TrainValidationError) as exc:
        plan_train(TrainSpec(PULSE, count=5, period_us=900.0), DUPLEX)
    assert "below the driver's minimum distance" in str(exc.value)


def test_short_period_can_be_allowed_explicitly():
    p = plan_train(
        TrainSpec(PULSE, count=5, period_us=600.0),
        DUPLEX,
        allow_short_period=True,
    )
    assert p.timestamps_us[1] == 600.0


def test_too_many_pulses_is_refused():
    spec = TrainSpec(PULSE, count=MAX_TRAIN_PULSES + 1, period_us=100_000.0)
    with pytest.raises(TrainValidationError, match="exceeds the maximum"):
        plan_train(spec, DUPLEX)


def test_zero_count_is_refused():
    with pytest.raises(TrainValidationError, match="count must be at least 1"):
        plan_train(TrainSpec(PULSE, count=0, period_us=100_000.0), DUPLEX)


def test_period_off_the_tick_grid_is_refused():
    with pytest.raises(TrainValidationError, match="period_us"):
        plan_train(TrainSpec(PULSE, count=5, period_us=100_005.0), DUPLEX)


def test_negative_delay_is_refused():
    spec = TrainSpec(PULSE, count=5, period_us=100_000.0, delay_us=-10.0)
    with pytest.raises(TrainValidationError, match="delay_us must not be negative"):
        plan_train(spec, DUPLEX)


def test_a_bad_pulse_fails_before_the_train_is_considered():
    bad = PulseSpec(5000.0, 200.0, 0.0, -5000.0, 200.0)
    with pytest.raises(PulseValidationError, match="outside the stimulator"):
        plan_train(TrainSpec(bad, count=5, period_us=100_000.0), DUPLEX)


def test_shifted_by_moves_every_timestamp():
    # Timestamps are measured from the beginning of the acquisition, not from
    # the moment of sending, so scheduling "now plus a bit" means shifting.
    p = plan_train(TrainSpec(PULSE, count=3, period_us=100_000.0), DUPLEX)
    shifted = p.shifted_by(600_000_000.0)  # ten minutes into a recording
    assert shifted.timestamps_us == (
        600_000_000.0, 600_100_000.0, 600_200_000.0,
    )


def test_shifted_by_does_not_mutate_the_original():
    p = plan_train(TrainSpec(PULSE, count=3, period_us=100_000.0), DUPLEX)
    p.shifted_by(1_000.0)
    assert p.timestamps_us[0] == 0.0


def test_shifted_by_refuses_a_negative_offset():
    p = plan_train(TrainSpec(PULSE, count=3, period_us=100_000.0), DUPLEX)
    with pytest.raises(ValueError, match="must not be negative"):
        p.shifted_by(-1.0)


def test_sequence_can_be_shifted_too():
    p = plan_sequence([PULSE, PULSE], (0.0, 100_000.0), DUPLEX)
    assert p.shifted_by(5_000.0).timestamps_us == (5_000.0, 105_000.0)


def test_describe_mentions_rate_and_duration():
    text = plan_train(TrainSpec(PULSE, count=5, period_us=100_000.0), DUPLEX).describe()
    assert "10 Hz" in text
    assert "5 x" in text


# --------------------------------------------------------------------------
# arbitrary sequences
# --------------------------------------------------------------------------

def test_a_sequence_pairs_one_pulse_with_each_timestamp():
    pulses = ramp(PULSE, count=3, from_amplitude=50.0, to_amplitude=150.0)
    p = plan_sequence(pulses, (0.0, 100_000.0, 200_000.0), DUPLEX)
    assert p.count == 3
    assert len(p.pulse_plans) == 3


def test_sequence_lengths_must_match():
    with pytest.raises(TrainValidationError) as exc:
        plan_sequence([PULSE, PULSE], (0.0,), DUPLEX)
    assert "2 pulses against 1 timestamps" in str(exc.value)


def test_sequence_timestamps_must_increase():
    with pytest.raises(TrainValidationError) as exc:
        plan_sequence([PULSE, PULSE], (100_000.0, 100_000.0), DUPLEX)
    assert "must strictly increase" in str(exc.value)


def test_sequence_refuses_overlapping_pulses():
    # PULSE lasts 500 us; 300 us apart means the second starts too early.
    with pytest.raises(TrainValidationError) as exc:
        plan_sequence([PULSE, PULSE], (0.0, 300.0), DUPLEX)
    assert "would overlap" in str(exc.value)


def test_sequence_names_the_offending_pulse():
    bad = PulseSpec(5000.0, 200.0, 0.0, -5000.0, 200.0, name="too-big")
    with pytest.raises(TrainValidationError) as exc:
        plan_sequence([PULSE, bad], (0.0, 100_000.0), DUPLEX)
    assert "pulse 1 (too-big)" in str(exc.value)


def test_empty_sequence_is_refused():
    with pytest.raises(TrainValidationError, match="at least one pulse"):
        plan_sequence([], (), DUPLEX)


def test_sequence_duration_covers_the_last_pulse():
    p = plan_sequence([PULSE, PULSE], (0.0, 100_000.0), DUPLEX)
    assert p.duration_us == 100_500.0


# --------------------------------------------------------------------------
# amplitude ramps
# --------------------------------------------------------------------------

def test_ramp_sweeps_amplitude_linearly():
    pulses = ramp(PULSE, count=5, from_amplitude=0.0, to_amplitude=100.0)
    assert [p.amplitude1 for p in pulses] == [0.0, 25.0, 50.0, 75.0, 100.0]


def test_ramp_keeps_a_balanced_pulse_balanced():
    for pulse in ramp(PULSE, count=5, from_amplitude=20.0, to_amplitude=100.0):
        assert pulse.net_charge_pc == 0.0


def test_ramp_scales_both_phases_of_an_asymmetric_pulse():
    base = PulseSpec(100.0, 100.0, 0.0, -50.0, 200.0)
    pulses = ramp(base, count=2, from_amplitude=50.0, to_amplitude=100.0)
    assert pulses[0].amplitude1 == 50.0
    assert pulses[0].amplitude2 == -25.0
    assert pulses[0].net_charge_pc == 0.0


def test_ramp_of_one_uses_the_start_amplitude():
    assert ramp(PULSE, 1, 30.0, 90.0)[0].amplitude1 == 30.0


def test_ramp_refuses_a_zero_amplitude_base():
    with pytest.raises(ValueError, match="zero amplitude"):
        ramp(PulseSpec(0.0, 200.0), count=3, from_amplitude=0.0, to_amplitude=1.0)


def test_ramp_pulses_are_named_so_errors_point_somewhere():
    assert ramp(PULSE, 3, 10.0, 30.0)[1].name == "p[1]"
