"""The activity display's measurement, which runs on the drain's thread."""

import numpy as np
import pytest

from biocam.data.monitor import LiveMonitor, MonitorSnapshot
from biocam.data.recording import AcquisitionParameters
from biocam.data.replay import Packet

PARAMS = AcquisitionParameters(
    frame_rate_hz=1000.0, total_channels=64, ch_sample_byte_size=2,
    bit_depth=12, adc_counts_to_value=2.0, offset=-4125.0,
    min_digital_value=0, max_digital_value=4095,
)


def packet_from(array):
    return Packet(timestamp=0, counter=0,
                  payload=np.asarray(array, dtype=np.uint16).tobytes())


def flat_packet(frames=64, value=2048, channels=64):
    return packet_from(np.full((frames, channels), value))


def monitor(**kwargs):
    # No time decimation unless a test is about it.
    kwargs.setdefault("refresh_hz", 1e9)
    return LiveMonitor(PARAMS, n_rows=8, n_cols=8, **kwargs)


# --------------------------------------------------------------------------
# what it measures
# --------------------------------------------------------------------------

def test_a_flat_signal_reads_zero():
    m = monitor()
    assert m.observe(flat_packet())
    assert m.snapshot().activity.max() == 0.0


def test_peak_to_peak_is_scaled_to_the_analogue_unit():
    data = np.full((64, 64), 2000)
    data[0, 5] = 2000
    data[1, 5] = 2100          # 100 counts of swing on channel 5
    m = monitor()
    m.observe(packet_from(data))
    # adc_counts_to_value is 2.0, so 100 counts is 200 uV.
    assert m.snapshot().activity[5] == pytest.approx(200.0)


def test_an_offset_is_not_activity():
    # A DC offset does not change peak-to-peak, deliberately: an electrode
    # sitting at a different baseline is not a more active one.
    quiet = np.full((64, 64), 2000)
    offset = np.full((64, 64), 3000)
    a, b = monitor(), monitor()
    a.observe(packet_from(quiet))
    b.observe(packet_from(offset))
    assert a.snapshot().activity.max() == b.snapshot().activity.max() == 0.0


def test_a_lively_channel_stands_out():
    rng = np.random.default_rng(0)
    data = (2048 + rng.normal(0, 5, (64, 64)))
    data[:, 7] = 2048 + rng.normal(0, 300, 64)
    m = monitor()
    m.observe(packet_from(np.clip(data, 0, 4095)))
    activity = m.snapshot().activity
    assert activity[7] > 5 * np.median(activity)


def test_a_dead_channel_reads_zero_among_live_ones():
    rng = np.random.default_rng(1)
    data = 2048 + rng.normal(0, 50, (64, 64))
    data[:, 3] = 2048
    m = monitor()
    m.observe(packet_from(np.clip(data, 0, 4095)))
    activity = m.snapshot().activity
    assert activity[3] == 0.0
    assert activity.max() > 0


# --------------------------------------------------------------------------
# it must not cost the recording
# --------------------------------------------------------------------------

def test_most_packets_are_skipped_at_the_default_rate():
    m = LiveMonitor(PARAMS, n_rows=8, n_cols=8, refresh_hz=10.0)
    used = sum(m.observe(flat_packet()) for _ in range(200))
    assert used == 1
    assert m.skipped == 199


def test_a_short_packet_is_skipped_rather_than_decoded():
    # Fewer than two frames has no peak-to-peak to compute.
    m = monitor()
    assert m.observe(flat_packet(frames=1)) is False
    assert m.snapshot().has_data is False


def test_an_empty_payload_is_skipped():
    m = monitor()
    assert m.observe(Packet(timestamp=0, counter=0, payload=b"")) is False
    assert m.errors == 0


def test_a_malformed_payload_is_counted_and_never_raises():
    # An exception here would escape the packet loop, skip the backlog drain
    # and finalise(), and stamp an intact raw file "failed". A picture is
    # never worth that.
    m = monitor()
    bad = Packet(timestamp=0, counter=0, payload=b"\x01\x02\x03")
    assert m.observe(bad) is False
    assert m.snapshot().has_data is False


def test_every_observation_is_timed():
    m = monitor()
    m.observe(flat_packet())
    assert m.max_observe_us > 0


def test_a_slow_observation_is_counted_and_warned_about():
    m = monitor(slow_observe_us=0.0)
    m.observe(flat_packet())
    assert m.slow_observations == 1
    assert any("acquisition thread" in w for w in m.warnings())


def test_nothing_accumulates_over_a_long_run():
    # The activity array is pre-allocated and reused, so a long recording
    # allocates nothing after the first packet.
    m = monitor()
    for _ in range(500):
        m.observe(flat_packet())
    assert m.samples == 500
    assert m.snapshot().activity.nbytes == 64 * 4      # float32 per channel
    assert m._activity.nbytes == 64 * 4                # and the source array


def test_a_clean_monitor_warns_about_nothing():
    m = monitor()
    m.observe(flat_packet())
    assert m.warnings() == []


# --------------------------------------------------------------------------
# the snapshot handed to the UI
# --------------------------------------------------------------------------

def test_a_snapshot_is_a_copy_not_the_live_array():
    # The consumer thread writes into the same array in place; handing it out
    # would let the UI render a half-updated frame.
    m = monitor()
    m.observe(flat_packet())
    snapshot = m.snapshot()
    m._activity[0] = 12345.0
    assert snapshot.activity[0] != 12345.0


def test_an_empty_snapshot_says_it_has_no_data():
    snapshot = monitor().snapshot()
    assert isinstance(snapshot, MonitorSnapshot)
    assert not snapshot.has_data
    assert snapshot.as_grid() is None


def test_the_grid_is_row_major():
    data = np.full((64, 64), 2000)
    # Channel 9 is row 2, col 2 in a 1-based 8x8 array - index 9 = 1*8 + 1.
    data[1, 9] = 2100
    m = monitor()
    m.observe(packet_from(data))
    grid = m.snapshot().as_grid()
    assert grid.shape == (8, 8)
    assert grid[1][1] > 0
    assert grid[0][0] == 0


def test_the_range_is_usable_for_scaling_even_when_flat():
    m = monitor()
    m.observe(flat_packet())
    low, high = m.snapshot().range()
    assert high > low          # never a zero-width range a colour map divides by


def test_a_grid_larger_than_the_channel_count_is_refused():
    # Better a blank picture than one that reads past the end of the data.
    m = LiveMonitor(PARAMS, n_rows=64, n_cols=64, refresh_hz=1e9)
    m.observe(flat_packet())
    assert m.snapshot().as_grid() is None


# --------------------------------------------------------------------------
# construction
# --------------------------------------------------------------------------

def test_refresh_rate_must_be_positive():
    with pytest.raises(ValueError, match="refresh_hz must be positive"):
        LiveMonitor(PARAMS, refresh_hz=0)


def test_at_least_two_frames_are_needed_for_a_peak_to_peak():
    with pytest.raises(ValueError, match="at least 2"):
        LiveMonitor(PARAMS, frames_per_sample=1)
