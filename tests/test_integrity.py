import pytest

from biocam.data.integrity import (
    COUNTER_MODULUS,
    COUNTER_ANOMALY_THRESHOLD,
    Gap,
    GapTracker,
    packets_lost,
)

RATE = 18557.720703125


def test_consecutive_counters_lose_nothing():
    assert packets_lost(10, 11) == 0


def test_one_skipped_packet():
    assert packets_lost(10, 12) == 1


def test_many_skipped_packets():
    assert packets_lost(10, 20) == 9


def test_counter_wraps_without_reporting_loss():
    assert packets_lost(COUNTER_MODULUS - 1, 0) == 0


def test_loss_across_the_wrap_boundary():
    assert packets_lost(COUNTER_MODULUS - 1, 2) == 2


def test_repeated_counter_is_not_treated_as_loss():
    # A duplicate is not 65536 missing packets. Report nothing and move on.
    assert packets_lost(10, 10) == 0


def test_tracker_reports_nothing_on_a_clean_run():
    tracker = GapTracker(frame_rate_hz=RATE)
    for i, counter in enumerate([5, 6, 7, 8]):
        assert tracker.observe(counter, frames_in_packet=10, frames_written=i * 10) is None
    assert tracker.gaps == []
    assert tracker.n_frames_missing == 0
    assert tracker.counter_anomalies == 0


def test_tracker_reports_a_gap_with_position_and_duration():
    tracker = GapTracker(frame_rate_hz=RATE)
    tracker.observe(5, frames_in_packet=10, frames_written=0)
    gap = tracker.observe(8, frames_in_packet=10, frames_written=10)
    assert isinstance(gap, Gap)
    assert gap.after_frame == 10
    assert gap.missing_frames == 20          # 2 lost packets x 10 frames
    assert gap.duration_ms == pytest.approx(20 / RATE * 1000)
    assert tracker.n_frames_missing == 20
    assert tracker.gaps == [gap]


def test_first_packet_never_reports_a_gap():
    tracker = GapTracker(frame_rate_hz=RATE)
    assert tracker.observe(12345, frames_in_packet=10, frames_written=0) is None


def test_tracker_accumulates_several_gaps():
    tracker = GapTracker(frame_rate_hz=RATE)
    tracker.observe(1, frames_in_packet=10, frames_written=0)
    tracker.observe(3, frames_in_packet=10, frames_written=10)
    tracker.observe(7, frames_in_packet=10, frames_written=20)
    assert len(tracker.gaps) == 2
    assert tracker.n_frames_missing == 10 + 30


def test_backwards_step_of_one_returns_zero_loss():
    # Out-of-order or anomalous packet: counter goes backwards
    assert packets_lost(10, 9) == 0


def test_backwards_step_increments_anomaly_counter():
    tracker = GapTracker(frame_rate_hz=RATE)
    tracker.observe(10, frames_in_packet=10, frames_written=0)
    tracker.observe(9, frames_in_packet=10, frames_written=10)  # backwards step
    assert tracker.counter_anomalies == 1
    assert tracker.gaps == []
    assert tracker.n_frames_missing == 0


def test_delta_at_anomaly_threshold_is_treated_as_loss():
    # delta = COUNTER_ANOMALY_THRESHOLD (32768) is exactly at the boundary.
    # We use strictly greater-than (>), so delta=32768 is plausible loss, not
    # an anomaly. This represents 32767 lost packets, which is at the edge but
    # still defensible as genuine loss rather than a transient anomaly.
    previous = 10
    current = (previous + COUNTER_ANOMALY_THRESHOLD) % COUNTER_MODULUS
    # This should be treated as a gap, not an anomaly
    result = packets_lost(previous, current)
    assert result == COUNTER_ANOMALY_THRESHOLD - 1
    assert result > 0


def test_delta_exceeding_anomaly_threshold_is_anomaly():
    # delta > COUNTER_ANOMALY_THRESHOLD is treated as anomaly
    previous = 10
    current = (previous + COUNTER_ANOMALY_THRESHOLD + 1) % COUNTER_MODULUS
    assert packets_lost(previous, current) == 0


def test_counter_reset_from_mid_range_to_zero_is_anomaly():
    # Device reset: counter goes from 10000 to 0
    # Delta = (0 - 10000) % 65536 = 55536, which exceeds COUNTER_ANOMALY_THRESHOLD
    tracker = GapTracker(frame_rate_hz=RATE)
    tracker.observe(10000, frames_in_packet=10, frames_written=0)
    tracker.observe(0, frames_in_packet=10, frames_written=10)
    assert tracker.counter_anomalies == 1
    assert tracker.gaps == []
    assert tracker.n_frames_missing == 0


def test_large_plausible_gap_still_reported_as_loss():
    # A gap of 100 packets is large but plausible (still under half modulus)
    tracker = GapTracker(frame_rate_hz=RATE)
    tracker.observe(100, frames_in_packet=10, frames_written=0)
    gap = tracker.observe(200, frames_in_packet=10, frames_written=10)
    # 200 - 100 - 1 = 99 lost packets
    assert gap is not None
    assert gap.missing_frames == 99 * 10
    assert tracker.counter_anomalies == 0
