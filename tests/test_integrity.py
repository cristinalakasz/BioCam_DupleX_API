import pytest

from biocam.data.integrity import COUNTER_MODULUS, Gap, GapTracker, packets_lost

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
