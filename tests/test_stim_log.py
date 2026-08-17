import json

import pytest

from biocam.data.clock import AcquisitionClock
from biocam.data.replay import Packet
from biocam.stim import (
    Electrode,
    PulseSpec,
    StimConstraints,
    TrainSpec,
    bipolar_pair,
    plan,
    plan_train,
)
from biocam.stim.log import REFUSED, REJECTED, SENT, StimulusLog

DUPLEX = StimConstraints(
    time_resolution_us=10, amplitude_resolution=1.0,
    min_amplitude=-1000.0, max_amplitude=1000.0, max_total_ticks=1000,
)
PULSE = PulseSpec(100.0, 200.0, 100.0, -100.0, 200.0, name="p")
PATTERN = bipolar_pair(Electrode(10, 10), Electrode(20, 30))


def a_pulse():
    return plan(PULSE, DUPLEX)


def a_train(count=3):
    return plan_train(TrainSpec(PULSE, count=count, period_us=100_000.0), DUPLEX)


def a_reading():
    clock = AcquisitionClock(1000.0)
    clock.observe(Packet(timestamp=0, counter=0, payload=b""), 5000)
    return clock.read()


# --------------------------------------------------------------------------
# recording
# --------------------------------------------------------------------------

def test_an_empty_log_says_so():
    assert StimulusLog().describe() == "no stimuli attempted"
    assert len(StimulusLog()) == 0


def test_an_immediate_stimulus_is_recorded():
    log = StimulusLog()
    record = log.immediate(
        a_pulse(), PATTERN, clock_reading=a_reading(), latency_cycles=12345)
    assert record.kind == "immediate"
    assert record.outcome == SENT
    assert record.delivered
    assert record.latency_cycles == 12345
    assert record.positive == ("(10,10)",)
    assert record.negative == ("(20,30)",)
    assert "uA" in record.pulse


def test_a_scheduled_stimulus_keeps_the_requested_timestamps():
    log = StimulusLog()
    train = a_train()
    record = log.scheduled(train, PATTERN, clock_reading=a_reading())
    assert record.kind == "scheduled"
    assert record.requested_timestamps_us == train.timestamps_us


def test_indices_are_sequential():
    log = StimulusLog()
    for _ in range(3):
        log.immediate(a_pulse(), PATTERN)
    assert [r.index for r in log] == [0, 1, 2]


def test_the_clock_reading_is_carried_through():
    log = StimulusLog()
    reading = a_reading()
    record = log.immediate(a_pulse(), PATTERN, clock_reading=reading)
    assert record.clock_us == reading.acquisition_us
    assert record.clock_source == "frames"


def test_a_missing_clock_reading_leaves_none_rather_than_zero():
    # Zero would read as "at the very start of the recording", which is a
    # specific and wrong claim rather than an absent one.
    record = StimulusLog().immediate(a_pulse(), PATTERN)
    assert record.clock_us is None


# --------------------------------------------------------------------------
# failures are part of the record
# --------------------------------------------------------------------------

def test_a_refusal_is_recorded_alongside_the_successes():
    # A hole in a stimulus train looks, in the signal, exactly like a stimulus
    # that evoked nothing. It has to be written down.
    log = StimulusLog()
    log.immediate(a_pulse(), PATTERN)
    log.failure("immediate", "net charge is +20000 pC", plan=a_pulse(),
                pattern=PATTERN)
    assert len(log) == 2
    assert len(log.delivered) == 1
    assert len(log.failed) == 1
    assert log.failed[0].outcome == REFUSED
    assert "net charge" in log.failed[0].detail


def test_a_driver_rejection_is_distinguished_from_our_refusal():
    log = StimulusLog()
    log.failure("immediate", "Send returned false", rejected_by_driver=True)
    assert log.records[0].outcome == REJECTED


def test_a_failure_without_a_plan_still_records():
    log = StimulusLog()
    record = log.failure("scheduled", "the BioCAM is not streaming")
    assert record.outcome == REFUSED
    assert record.pulse is None
    assert not record.delivered


# --------------------------------------------------------------------------
# which time to believe
# --------------------------------------------------------------------------

def test_the_driver_latency_is_preferred_over_the_clock():
    # The clock is a lower bound; the driver's latency is a measurement.
    log = StimulusLog()
    record = log.immediate(
        a_pulse(), PATTERN, clock_reading=a_reading(), latency_cycles=9_000_000)
    assert record.best_time_us(cycles_per_us=2.0) == 4_500_000.0
    assert record.time_is_measured(cycles_per_us=2.0)


def test_the_clock_is_used_when_no_latency_was_reported():
    log = StimulusLog()
    reading = a_reading()
    record = log.immediate(a_pulse(), PATTERN, clock_reading=reading)
    assert record.best_time_us(cycles_per_us=2.0) == reading.acquisition_us
    assert not record.time_is_measured(cycles_per_us=2.0)


def test_latency_is_unusable_without_a_conversion_factor():
    log = StimulusLog()
    reading = a_reading()
    record = log.immediate(
        a_pulse(), PATTERN, clock_reading=reading, latency_cycles=9_000_000)
    assert record.best_time_us() == reading.acquisition_us
    assert not record.time_is_measured()


def test_a_scheduled_stimulus_reports_its_first_requested_timestamp():
    log = StimulusLog()
    train = a_train().shifted_by(5_000_000.0)
    record = log.scheduled(train, PATTERN)
    assert record.best_time_us() == 5_000_000.0


def test_best_time_is_none_when_nothing_is_known():
    record = StimulusLog().failure("immediate", "no")
    assert record.best_time_us() is None


# --------------------------------------------------------------------------
# accumulated charge
# --------------------------------------------------------------------------

def test_only_delivered_stimuli_count_towards_charge():
    unbalanced = plan(
        PulseSpec(100.0, 200.0, 0.0, -50.0, 200.0),
        DUPLEX, require_charge_balance=False)
    log = StimulusLog()
    log.immediate(unbalanced, PATTERN)
    log.failure("immediate", "refused", plan=unbalanced, pattern=PATTERN)
    # 100 uA x 200 us - 50 uA x 200 us = 10000 pC, once, not twice.
    assert log.net_charge_pc == 10_000.0


def test_a_balanced_session_accumulates_nothing():
    log = StimulusLog()
    for _ in range(50):
        log.immediate(a_pulse(), PATTERN)
    assert log.net_charge_pc == 0.0


def test_charge_accumulates_across_a_train():
    unbalanced = PulseSpec(100.0, 200.0, 0.0, -50.0, 200.0)
    train = plan_train(
        TrainSpec(unbalanced, count=100, period_us=100_000.0),
        DUPLEX, require_charge_balance=False)
    log = StimulusLog()
    log.scheduled(train, PATTERN)
    assert log.net_charge_pc == 1_000_000.0


def test_describe_mentions_charge_when_there_is_any():
    unbalanced = plan(
        PulseSpec(100.0, 200.0, 0.0, -50.0, 200.0),
        DUPLEX, require_charge_balance=False)
    log = StimulusLog()
    log.immediate(unbalanced, PATTERN)
    assert "net charge delivered" in log.describe()


def test_describe_counts_failures():
    log = StimulusLog()
    log.immediate(a_pulse(), PATTERN)
    log.failure("immediate", "nope")
    assert "1 of 2 stimuli delivered" in log.describe()


# --------------------------------------------------------------------------
# serialising
# --------------------------------------------------------------------------

def test_to_dict_is_json_serialisable():
    log = StimulusLog()
    log.immediate(a_pulse(), PATTERN, clock_reading=a_reading(),
                  latency_cycles=100)
    log.scheduled(a_train(), PATTERN)
    log.failure("immediate", "refused", plan=a_pulse(), pattern=PATTERN)
    payload = json.dumps(log.to_dict(cycles_per_us=2.0))
    restored = json.loads(payload)
    assert restored["n_attempted"] == 3
    assert restored["n_delivered"] == 2
    assert restored["n_failed"] == 1
    assert restored["cycles_per_us"] == 2.0


def test_each_entry_carries_its_resolved_time_and_provenance():
    log = StimulusLog()
    log.immediate(a_pulse(), PATTERN, clock_reading=a_reading(),
                  latency_cycles=9_000_000)
    entry = log.to_dict(cycles_per_us=2.0)["stimuli"][0]
    assert entry["best_time_us"] == 4_500_000.0
    assert entry["time_is_measured"] is True
    assert entry["delivered"] is True


def test_write_produces_a_readable_file(tmp_path):
    log = StimulusLog()
    log.immediate(a_pulse(), PATTERN, clock_reading=a_reading())
    path = tmp_path / "stimuli.json"
    log.write(path, cycles_per_us=1.0)
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["n_delivered"] == 1
    assert payload["stimuli"][0]["positive"] == ["(10,10)"]


def test_write_leaves_no_temporary_file_behind(tmp_path):
    log = StimulusLog()
    log.immediate(a_pulse(), PATTERN)
    log.write(tmp_path / "stimuli.json")
    assert [p.name for p in tmp_path.iterdir()] == ["stimuli.json"]


def test_write_replaces_an_existing_log(tmp_path):
    path = tmp_path / "stimuli.json"
    path.write_text("stale", encoding="utf-8")
    log = StimulusLog()
    log.immediate(a_pulse(), PATTERN)
    log.write(path)
    assert json.loads(path.read_text(encoding="utf-8"))["n_delivered"] == 1


# --------------------------------------------------------------------------
# bounded growth
# --------------------------------------------------------------------------

def test_the_record_list_is_capped(monkeypatch):
    # The only unbounded structure on the stimulation path, and Phase 6's
    # closed loop stimulates from a loop. Same treatment GapTracker gives
    # gaps: cap the detail, keep the counts honest.
    import biocam.stim.log as log_module

    monkeypatch.setattr(log_module, "MAX_RETAINED_RECORDS", 10)
    log = StimulusLog()
    for _ in range(25):
        log.immediate(a_pulse(), PATTERN)
    assert len(log.records) == 10
    assert log.records_truncated == 15
    # The counts still describe all 25.
    assert len(log) == 25
    assert log.n_delivered == 25


def test_charge_keeps_accumulating_past_the_cap(monkeypatch):
    import biocam.stim.log as log_module

    monkeypatch.setattr(log_module, "MAX_RETAINED_RECORDS", 5)
    unbalanced = plan(
        PulseSpec(100.0, 200.0, 0.0, -50.0, 200.0),
        DUPLEX, require_charge_balance=False)
    log = StimulusLog()
    for _ in range(20):
        log.immediate(unbalanced, PATTERN)
    # 10000 pC each, all twenty, even though only five records were kept.
    assert log.net_charge_pc == 200_000.0


def test_truncation_is_reported_rather_than_silent(monkeypatch):
    import biocam.stim.log as log_module

    monkeypatch.setattr(log_module, "MAX_RETAINED_RECORDS", 2)
    log = StimulusLog()
    for _ in range(5):
        log.immediate(a_pulse(), PATTERN)
    assert log.to_dict()["records_truncated"] == 3
    assert "records dropped" in log.describe()


def test_counts_are_maintained_incrementally():
    log = StimulusLog()
    log.immediate(a_pulse(), PATTERN)
    log.failure("immediate", "nope")
    log.immediate(a_pulse(), PATTERN)
    assert log.n_attempted == 3
    assert log.n_delivered == 2
    assert log.n_failed == 1


# --------------------------------------------------------------------------
# a refused train keeps the times that were asked for
# --------------------------------------------------------------------------

def test_a_refused_train_records_its_requested_timestamps():
    # The most useful field for the months-later analysis: what was supposed
    # to happen, and when.
    log = StimulusLog()
    train = a_train().shifted_by(1_000_000.0)
    log.failure("scheduled", "every timestamp is in the past",
                plan=train, pattern=PATTERN)
    record = log.records[0]
    assert record.requested_timestamps_us == train.timestamps_us
    assert record.best_time_us() == 1_000_000.0


def test_a_refused_pulse_has_no_timestamps_and_that_is_fine():
    log = StimulusLog()
    log.failure("immediate", "refused", plan=a_pulse(), pattern=PATTERN)
    assert log.records[0].requested_timestamps_us == ()
