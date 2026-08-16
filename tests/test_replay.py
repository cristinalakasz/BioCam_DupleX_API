import numpy as np

from biocam.data.recording import AcquisitionParameters
from biocam.data.replay import Packet, ReplayPacketSource

PARAMS = AcquisitionParameters(
    frame_rate_hz=1000.0, total_channels=4, ch_sample_byte_size=2, bit_depth=12,
    adc_counts_to_value=1.0, offset=0.0, min_digital_value=0, max_digital_value=4095,
)


def _write_raw(tmp_path, n_frames):
    data = np.arange(n_frames * 4, dtype=np.uint16).reshape(n_frames, 4)
    path = tmp_path / "src.raw"
    data.tofile(path)
    return path, data


def test_emits_every_frame_exactly_once(tmp_path):
    path, data = _write_raw(tmp_path, 50)
    packets = list(ReplayPacketSource(path, PARAMS, frames_per_packet=7))
    joined = b"".join(p.payload for p in packets)
    assert joined == data.tobytes()


def test_counters_increment_by_one(tmp_path):
    path, _ = _write_raw(tmp_path, 30)
    counters = [p.counter for p in ReplayPacketSource(path, PARAMS, frames_per_packet=10)]
    assert counters == [0, 1, 2]


def test_timestamps_advance(tmp_path):
    path, _ = _write_raw(tmp_path, 30)
    stamps = [p.timestamp for p in ReplayPacketSource(path, PARAMS, frames_per_packet=10)]
    assert stamps == sorted(stamps)
    assert len(set(stamps)) == len(stamps)


def test_dropped_packets_are_omitted_but_counters_still_advance(tmp_path):
    path, _ = _write_raw(tmp_path, 40)
    source = ReplayPacketSource(path, PARAMS, frames_per_packet=10, drop_packets=(1,))
    packets = list(source)
    assert [p.counter for p in packets] == [0, 2, 3]


def test_last_packet_may_be_short(tmp_path):
    path, _ = _write_raw(tmp_path, 25)
    packets = list(ReplayPacketSource(path, PARAMS, frames_per_packet=10))
    assert len(packets) == 3
    assert len(packets[-1].payload) == 5 * PARAMS.bytes_per_frame


def test_packet_is_immutable(tmp_path):
    import dataclasses
    import pytest
    packet = Packet(timestamp=1, counter=2, payload=b"x")
    with pytest.raises(dataclasses.FrozenInstanceError):
        packet.counter = 3


def test_packet_has_no_per_instance_dict():
    # LOW: slots=True removes the per-instance __dict__ a plain dataclass
    # otherwise carries - one Packet is allocated per callback on the
    # driver's own thread, so this is free there.
    packet = Packet(timestamp=1, counter=2, payload=b"x")
    assert not hasattr(packet, "__dict__")
    assert Packet.__slots__ == ("timestamp", "counter", "payload")
