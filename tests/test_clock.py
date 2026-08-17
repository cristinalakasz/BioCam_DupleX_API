import pytest

from biocam.data.clock import (
    MIN_CALIBRATION_FRAMES,
    AcquisitionClock,
    ClockUnavailable,
    schedule_after,
)
from biocam.data.replay import Packet
from biocam.stim import PulseSpec, StimConstraints, TrainSpec, plan_train

RATE = 18557.720703125

# 1 clock cycle per microsecond, so device time and frame time coincide and
# any disagreement in a test is a real one rather than a unit artefact.
CYCLES_PER_US = 1.0

DUPLEX = StimConstraints(
    time_resolution_us=10, amplitude_resolution=1.0,
    min_amplitude=-1000.0, max_amplitude=1000.0, max_total_ticks=1000,
)
PULSE = PulseSpec(100.0, 200.0, 100.0, -100.0, 200.0, name="p")


def packet(timestamp):
    return Packet(timestamp=timestamp, counter=0, payload=b"")


def clock(**kwargs):
    kwargs.setdefault("cycles_per_us", CYCLES_PER_US)
    return AcquisitionClock(RATE, **kwargs)


def us_for(frames):
    return frames / RATE * 1e6


def feed(c, n_packets, frames_per_packet=20, first_timestamp=1_000_000):
    """Feed packets whose device clock and frame count agree.

    Anything else is a fixture the cross-check will (correctly) reject: 500 ms
    of device time cannot elapse across 40 frames at 18.5 kHz.
    """
    for i in range(n_packets):
        elapsed_us = us_for(i * frames_per_packet)
        timestamp = int(first_timestamp + elapsed_us * CYCLES_PER_US)
        c.observe(packet(timestamp), frames_per_packet)


# --------------------------------------------------------------------------
# the empty clock
# --------------------------------------------------------------------------

def test_a_clock_with_no_packets_refuses_to_guess():
    # The whole point: a stimulus scheduled at "time zero" of a recording
    # already ten minutes old is a worse error than a crash.
    with pytest.raises(ClockUnavailable, match="no packets observed yet"):
        clock().read()


def test_frame_rate_must_be_positive():
    with pytest.raises(ValueError, match="frame_rate must be positive"):
        AcquisitionClock(0)


def test_negative_frame_counts_are_refused():
    c = clock()
    with pytest.raises(ValueError, match="frames_in_packet"):
        c.observe(packet(1), -1)
    with pytest.raises(ValueError, match="frames_lost"):
        c.observe(packet(1), 10, frames_lost=-1)


# --------------------------------------------------------------------------
# counting frames
# --------------------------------------------------------------------------

def test_time_advances_with_frames():
    c = clock()
    for i in range(10):
        c.observe(packet(0), 20)
    assert c.frames_seen == 200
    assert c.now_us() == pytest.approx(us_for(200))


def test_lost_frames_still_count_as_time():
    # A recording that loses data must not also lose time: the instrument
    # kept acquiring while we were not receiving.
    lossy = clock()
    clean = clock()
    lossy.observe(packet(0), 20)
    lossy.observe(packet(0), 20, frames_lost=60)   # 40 seen + 60 lost = 100
    for _ in range(5):
        clean.observe(packet(0), 20)               # 100 seen
    assert lossy.frames_lost == 60
    assert lossy.frames_seen == 40
    assert lossy.now_us() == pytest.approx(clean.now_us())


def test_frames_source_is_reported_when_the_device_clock_is_absent():
    c = clock()
    c.observe(packet(0), 20)
    assert c.read().source == "frames"
    assert not c.device_timestamps_available


# --------------------------------------------------------------------------
# the device clock, and its zero sentinel
# --------------------------------------------------------------------------

def test_timestamp_zero_is_not_treated_as_a_time():
    # DataPacketHeader.Timestamp uses 0 for "not available". Adopting it as
    # time zero would place a stimulus at the start of the recording.
    c = clock()
    for _ in range(5):
        c.observe(packet(0), 20)
    assert not c.device_timestamps_available
    assert c.unavailable_timestamps == 5
    assert c.read().source == "frames"


def test_the_device_clock_is_used_once_available():
    c = clock()
    c.observe(packet(1_000_000), 20)
    c.observe(packet(1_100_000), 20)
    reading = c.read()
    assert reading.source == "device"
    # 100000 cycles at 1 cycle/us, plus the 20 frames that preceded the first
    # timestamp.
    assert reading.acquisition_us == pytest.approx(us_for(20) + 100_000)


def test_the_device_clock_is_measured_from_the_first_timestamp_seen():
    # The recording need not have started at the instrument's time zero, so a
    # large absolute timestamp must not become a large elapsed time.
    c = clock()
    c.observe(packet(999_000_000), 20)
    c.observe(packet(999_000_500), 20)
    assert c.read().acquisition_us == pytest.approx(us_for(20) + 500)


def test_mixed_available_and_unavailable_timestamps():
    c = clock()
    c.observe(packet(0), 20)          # not available
    c.observe(packet(1_000_000), 20)  # first real one
    c.observe(packet(1_000_500), 20)
    assert c.unavailable_timestamps == 1
    assert c.device_timestamps_available
    assert c.read().source == "device"


def test_a_backwards_timestamp_stalls_the_clock_rather_than_rewinding_it():
    # A backwards timestamp is ignored, so the device estimate stops
    # advancing rather than jumping into the past. Stalling is the safe
    # failure: the frame count keeps moving, the two estimates drift apart,
    # and scheduling is refused before a stimulus can land at a wrong time.
    c = clock()
    feed(c, 5)
    before = c.now_us()
    c.observe(packet(900_000), 20)
    assert c.timestamp_anomalies == 1
    assert c.now_us() == pytest.approx(before)
    assert c.frames_seen == 120
    assert any("went backwards" in w for w in c.warnings())


def test_a_repeated_timestamp_is_not_an_anomaly():
    c = clock()
    c.observe(packet(1_000_000), 20)
    c.observe(packet(1_000_000), 20)
    assert c.timestamp_anomalies == 0


# --------------------------------------------------------------------------
# calibration
# --------------------------------------------------------------------------

def test_calibration_needs_a_long_enough_baseline():
    c = AcquisitionClock(RATE)
    c.observe(packet(1_000_000), 20)
    c.observe(packet(1_000_500), 20)
    assert c.calibrated_cycles_per_us is None


def test_calibration_recovers_the_conversion_factor():
    # Feed a device clock running at exactly 2 cycles per microsecond and
    # check the ratio comes back out.
    c = AcquisitionClock(RATE)
    frames_per_packet = 1000
    packets = MIN_CALIBRATION_FRAMES // frames_per_packet + 2
    for i in range(packets):
        elapsed_us = us_for(i * frames_per_packet)
        c.observe(packet(int(500_000 + elapsed_us * 2)), frames_per_packet)
    assert c.calibrated_cycles_per_us == pytest.approx(2.0, rel=1e-6)


def test_a_supplied_factor_wins_over_calibration():
    # The driver's own ClockCyclesToMilliseconds is authoritative.
    c = AcquisitionClock(RATE, cycles_per_us=7.0)
    for i in range(30):
        c.observe(packet(1_000_000 + i * 1000), 1000)
    assert c.cycles_per_us == 7.0


def test_without_a_factor_or_calibration_the_device_estimate_is_unavailable():
    c = AcquisitionClock(RATE)
    c.observe(packet(1_000_000), 20)
    c.observe(packet(1_000_500), 20)
    assert c.elapsed_us_from_device() is None
    assert c.read().source == "frames"


# --------------------------------------------------------------------------
# cross-checking the two estimates
# --------------------------------------------------------------------------

def test_agreeing_estimates_report_no_disagreement():
    c = clock()
    for i in range(1, 20):
        c.observe(packet(1_000_000 + int(us_for(i * 20))), 20)
    assert c.estimates_agree()
    assert c.disagreement_us() == pytest.approx(0, abs=1.0)
    assert c.warnings() == []


def test_a_wrong_frame_rate_shows_up_as_disagreement():
    # Device clock says one thing, frame count says another. Exactly the
    # symptom of a frame rate that is not what we were told.
    c = AcquisitionClock(RATE, cycles_per_us=CYCLES_PER_US, tolerance_us=1000.0)
    for i in range(1, 100):
        c.observe(packet(1_000_000 + i * 5000), 20)  # clock runs fast
    assert not c.estimates_agree()
    assert any("disagree" in w for w in c.warnings())


def test_disagreement_is_none_when_only_one_estimate_exists():
    c = clock()
    c.observe(packet(0), 20)
    assert c.disagreement_us() is None
    assert c.estimates_agree()


def test_warnings_name_the_missing_device_clock():
    c = clock()
    c.observe(packet(0), 20)
    assert any("no usable timestamp" in w for w in c.warnings())


def test_warnings_name_backwards_timestamps():
    c = clock()
    c.observe(packet(1_000_000), 20)
    c.observe(packet(500_000), 20)
    assert any("went backwards" in w for w in c.warnings())


def test_reading_describes_itself():
    c = clock()
    c.observe(packet(1_000_000), 20)
    c.observe(packet(1_100_000), 20)
    text = c.read().describe()
    assert "into the acquisition" in text
    assert "device" in text


def test_reading_mentions_loss_when_there_was_some():
    c = clock()
    c.observe(packet(0), 20, frames_lost=40)
    assert "40 lost" in c.read().describe()


# --------------------------------------------------------------------------
# scheduling against the clock
# --------------------------------------------------------------------------

def a_train():
    return plan_train(TrainSpec(PULSE, count=3, period_us=100_000.0), DUPLEX)


def test_schedule_after_shifts_onto_the_acquisition_timeline():
    c = clock()
    feed(c, 50)
    now = c.now_us()
    assert now > 0
    shifted = schedule_after(a_train(), c, lead_us=2_000_000.0)
    assert shifted.timestamps_us[0] == pytest.approx(now + 2_000_000.0)
    assert shifted.timestamps_us[1] == pytest.approx(now + 2_100_000.0)


def test_schedule_after_refuses_a_zero_or_negative_lead():
    c = clock()
    c.observe(packet(1_000_000), 20)
    for lead in (0.0, -1.0):
        with pytest.raises(ValueError, match="lead_us must be positive"):
            schedule_after(a_train(), c, lead_us=lead)


def test_schedule_after_refuses_when_the_estimates_disagree():
    # Scheduling against a clock we do not trust is how a train lands in the
    # past, and what the instrument does then is untested.
    c = AcquisitionClock(RATE, cycles_per_us=CYCLES_PER_US, tolerance_us=1000.0)
    for i in range(1, 100):
        c.observe(packet(1_000_000 + i * 5000), 20)
    with pytest.raises(ClockUnavailable, match="refusing to schedule"):
        schedule_after(a_train(), c, lead_us=2_000_000.0)


def test_schedule_after_refuses_an_empty_clock():
    with pytest.raises(ClockUnavailable):
        schedule_after(a_train(), clock(), lead_us=2_000_000.0)


def test_schedule_after_works_from_the_frame_count_alone():
    # No device timestamps at all: still schedulable, just less precisely.
    c = clock()
    for _ in range(100):
        c.observe(packet(0), 20)
    shifted = schedule_after(a_train(), c, lead_us=1_000_000.0)
    assert shifted.timestamps_us[0] == pytest.approx(us_for(2000) + 1_000_000.0)


def test_the_original_plan_is_untouched():
    c = clock()
    c.observe(packet(0), 20)
    original = a_train()
    schedule_after(original, c, lead_us=1_000_000.0)
    assert original.timestamps_us[0] == 0.0


# --------------------------------------------------------------------------
# feeding from a writer's running totals
# --------------------------------------------------------------------------

def test_observe_totals_differences_the_running_totals():
    c = clock()
    c.observe_totals(packet(0), 20, 0)
    c.observe_totals(packet(0), 40, 0)
    assert c.frames_seen == 40
    assert c.frames_lost == 0


def test_observe_totals_picks_up_loss_between_packets():
    c = clock()
    c.observe_totals(packet(0), 20, 0)
    c.observe_totals(packet(0), 40, 60)
    assert c.frames_seen == 40
    assert c.frames_lost == 60
    assert c.now_us() == pytest.approx(us_for(100))


def test_observe_totals_refuses_totals_that_went_backwards():
    # These only ever increase, so a decrease means the clock is being fed
    # from a different recording.
    c = clock()
    c.observe_totals(packet(0), 40, 0)
    with pytest.raises(ValueError, match="went backwards"):
        c.observe_totals(packet(0), 20, 0)


def test_observe_totals_refuses_missing_counts_that_went_backwards():
    c = clock()
    c.observe_totals(packet(0), 40, 60)
    with pytest.raises(ValueError, match="went backwards"):
        c.observe_totals(packet(0), 60, 10)
