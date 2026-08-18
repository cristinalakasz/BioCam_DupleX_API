"""The rolling trace window.

The property that matters most is the one a naive implementation gets wrong:
a spike must survive decimation. Everything else is bookkeeping.
"""

import numpy as np
import pytest

from biocam.data.recording import AcquisitionParameters
from biocam.data.traces import (
    MAX_TRACE_CHANNELS, TraceRecorder,
)

PARAMS = AcquisitionParameters(
    frame_rate_hz=1000.0, total_channels=8, ch_sample_byte_size=2,
    bit_depth=12, adc_counts_to_value=1.0, offset=0.0,
    min_digital_value=0, max_digital_value=4095,
)


class Packet:
    def __init__(self, payload):
        self.payload = payload


def packet_from(values):
    """values is (frames, 8) of counts."""
    return Packet(np.asarray(values, dtype=np.uint16).tobytes())


def recorder(channels=(0, 1), **kw):
    return TraceRecorder(PARAMS, channels, **kw)


def flat(frames, value=100, total=8):
    return np.full((frames, total), value, dtype=np.uint16)


# --------------------------------------------------------------------------
# The point of the whole module
# --------------------------------------------------------------------------

def test_a_spike_survives_decimation():
    # 20 frames per column, and a spike one frame wide. Taking every 20th
    # sample would miss it 19 times out of 20; keeping the min and max of each
    # bin cannot miss it at all. A display that loses spikes is worse than no
    # display, because it is read as evidence of a quiet culture.
    r = recorder(channels=(0,), columns=10, span_sec=0.2)   # 20 frames/column
    data = flat(200, 100)
    data[137, 0] = 4000            # one frame, in the middle of one bin
    r.observe(packet_from(data))
    snap = r.snapshot()
    assert snap.maxima[0].max() == 4000, "the spike vanished"
    # And it lands in exactly one column, not smeared across the window.
    assert (snap.maxima[0] == 4000).sum() == 1


def test_a_negative_excursion_survives_too():
    # Extracellular spikes are negative-going. A window that only kept maxima
    # would show a flat line for the signal this software exists to record.
    r = recorder(channels=(0,), columns=10, span_sec=0.2)
    data = flat(200, 2000)
    data[137, 0] = 5
    r.observe(packet_from(data))
    snap = r.snapshot()
    assert snap.minima[0].min() == 5
    assert (snap.minima[0] == 5).sum() == 1


def test_subsampling_would_have_missed_it():
    # A negative control on the test above: confirm the naive approach really
    # does fail here, so the property being checked is not vacuous.
    data = flat(200, 100)
    data[137, 0] = 4000
    subsampled = data[::20, 0]
    assert 4000 not in subsampled


# --------------------------------------------------------------------------
# Windowing
# --------------------------------------------------------------------------

def test_columns_are_oldest_first():
    r = recorder(channels=(0,), columns=4, span_sec=0.004)   # 1 frame/column
    for value in (10, 20, 30, 40):
        r.observe(packet_from(flat(1, value)))
    assert list(r.snapshot().maxima[0]) == [10, 20, 30, 40]


def test_the_window_rolls_and_keeps_the_newest():
    r = recorder(channels=(0,), columns=4, span_sec=0.004)
    for value in (10, 20, 30, 40, 50, 60):
        r.observe(packet_from(flat(1, value)))
    assert list(r.snapshot().maxima[0]) == [30, 40, 50, 60]


def test_a_partly_filled_window_reports_only_what_arrived():
    r = recorder(channels=(0,), columns=100, span_sec=0.1)
    r.observe(packet_from(flat(1, 42)))
    snap = r.snapshot()
    assert snap.filled == 1
    assert snap.columns == 1
    # Not 100 columns of zeros, which would draw as a flat line that never
    # happened.
    assert snap.maxima.shape == (1, 1)


def test_an_empty_window_says_so():
    snap = recorder().snapshot()
    assert not snap.has_data
    assert snap.range_of(0) == (0.0, 0.0)


def test_frames_that_do_not_fill_a_column_are_carried_not_dropped():
    # 20 frames per column, fed 3 frames at a time. Nothing should be lost.
    r = recorder(channels=(0,), columns=10, span_sec=0.2)
    for i in range(20):
        block = flat(3, 100)
        if i == 7:
            block[1, 0] = 3000     # inside a packet that does not fill a column
        r.observe(packet_from(block))
    assert r.snapshot().maxima[0].max() == 3000


def test_the_carry_cannot_grow_with_the_recording():
    r = recorder(channels=(0,), columns=10, span_sec=0.2)   # 20 frames/column
    for _ in range(500):
        r.observe(packet_from(flat(3, 100)))
    assert r._carry.shape[0] < 20


def test_a_packet_longer_than_the_window_keeps_its_tail():
    r = recorder(channels=(0,), columns=4, span_sec=0.004)  # 1 frame/column
    data = flat(10, 0)
    data[:, 0] = np.arange(10) * 10
    r.observe(packet_from(data))
    assert list(r.snapshot().maxima[0]) == [60, 70, 80, 90]


# --------------------------------------------------------------------------
# Channels and scaling
# --------------------------------------------------------------------------

def test_each_channel_keeps_its_own_trace():
    r = recorder(channels=(0, 3), columns=4, span_sec=0.004)
    data = flat(1, 0)
    data[0, 0], data[0, 3] = 111, 222
    r.observe(packet_from(data))
    snap = r.snapshot()
    assert snap.channels == (0, 3)
    assert snap.maxima[0, 0] == 111
    assert snap.maxima[1, 0] == 222


def test_values_are_converted_to_microvolts():
    params = AcquisitionParameters(
        frame_rate_hz=1000.0, total_channels=8, ch_sample_byte_size=2,
        bit_depth=12, adc_counts_to_value=0.5, offset=-100.0,
        min_digital_value=0, max_digital_value=4095,
    )
    r = TraceRecorder(params, (0,), columns=4, span_sec=0.004)
    r.observe(packet_from(flat(1, 200)))
    # A trace read against a threshold in microvolts must be in microvolts.
    assert r.snapshot().maxima[0, 0] == pytest.approx(-100.0 + 200 * 0.5)


def test_raw_counts_can_be_kept_instead():
    r = recorder(channels=(0,), columns=4, span_sec=0.004, as_microvolts=False)
    r.observe(packet_from(flat(1, 200)))
    assert r.snapshot().maxima[0, 0] == 200


def test_watching_nothing_costs_nothing():
    r = recorder(channels=())
    assert r.observe(packet_from(flat(10, 100))) is False
    assert not r.snapshot().has_data


def test_too_many_channels_is_refused_rather_than_truncated():
    params = AcquisitionParameters(
        frame_rate_hz=1000.0, total_channels=64, ch_sample_byte_size=2,
        bit_depth=12, adc_counts_to_value=1.0, offset=0.0,
        min_digital_value=0, max_digital_value=4095,
    )
    with pytest.raises(ValueError, match="at most"):
        TraceRecorder(params, range(MAX_TRACE_CHANNELS + 1))


def test_a_channel_outside_the_array_is_refused():
    with pytest.raises(ValueError, match="outside"):
        recorder(channels=(99,))


# --------------------------------------------------------------------------
# It must not cost the recording
# --------------------------------------------------------------------------

def test_a_broken_packet_does_not_raise():
    r = recorder(channels=(0,))
    assert r.observe(Packet(b"\x01\x02\x03")) in (False, True)
    assert r.observe(Packet(None)) is False
    assert r.decode_errors >= 1
    assert any("decode" in w for w in r.warnings())


def test_a_slow_observation_is_reported():
    r = recorder(channels=(0,))
    r.slow_observations = 3
    r.max_observation_us = 900.0
    assert any("longer than" in w for w in r.warnings())


def test_the_snapshot_is_a_copy():
    r = recorder(channels=(0,), columns=4, span_sec=0.004)
    r.observe(packet_from(flat(1, 50)))
    snap = r.snapshot()
    r.observe(packet_from(flat(1, 60)))
    # The UI draws from a snapshot on another thread; it must not change
    # underneath while it is being drawn.
    assert snap.maxima[0, 0] == 50
