import threading

import numpy as np

from biocam.data.recording import AcquisitionParameters, RecordingWriter, read_sidecar
from biocam.data.replay import ReplayPacketSource
from biocam.session import SessionResult, record_session

PARAMS = AcquisitionParameters(
    frame_rate_hz=1000.0, total_channels=4, ch_sample_byte_size=2, bit_depth=12,
    adc_counts_to_value=1.0, offset=0.0, min_digital_value=0, max_digital_value=4095,
)


def _source(tmp_path, n_frames=100, **kwargs):
    data = np.arange(n_frames * 4, dtype=np.uint16).reshape(n_frames, 4)
    path = tmp_path / "src.raw"
    data.tofile(path)
    return ReplayPacketSource(path, PARAMS, **kwargs), data


def test_records_everything_when_nothing_stops_it(tmp_path):
    source, data = _source(tmp_path, 100, frames_per_packet=10)
    raw, meta = tmp_path / "out.raw", tmp_path / "out_meta.json"
    with RecordingWriter(raw, meta, PARAMS) as writer:
        result = record_session(source, writer)

    assert isinstance(result, SessionResult)
    assert result.n_frames == 100
    assert result.verdict == "clean"
    assert result.stop_reason == "source_exhausted"
    assert raw.read_bytes() == data.tobytes()


def test_stops_at_the_requested_duration(tmp_path):
    source, _ = _source(tmp_path, 1000, frames_per_packet=10)
    raw, meta = tmp_path / "out.raw", tmp_path / "out_meta.json"
    with RecordingWriter(raw, meta, PARAMS) as writer:
        result = record_session(source, writer, duration_sec=0.05)   # 50 frames at 1 kHz

    assert result.stop_reason == "duration_reached"
    assert result.n_frames == 50
    assert read_sidecar(meta)["stop_reason"] == "duration_reached"


def test_stops_when_the_stop_event_is_set(tmp_path):
    source, _ = _source(tmp_path, 1000, frames_per_packet=10)
    raw, meta = tmp_path / "out.raw", tmp_path / "out_meta.json"
    stop = threading.Event()

    def stopping_after_three(packets):
        for index, packet in enumerate(packets):
            yield packet
            if index == 2:
                stop.set()

    with RecordingWriter(raw, meta, PARAMS) as writer:
        result = record_session(stopping_after_three(source), writer, stop_event=stop)

    assert result.stop_reason == "user_stopped"
    assert result.n_frames == 30


def test_an_injected_gap_reaches_the_sidecar(tmp_path):
    source, _ = _source(tmp_path, 100, frames_per_packet=10, drop_packets=(3,))
    raw, meta = tmp_path / "out.raw", tmp_path / "out_meta.json"
    with RecordingWriter(raw, meta, PARAMS) as writer:
        result = record_session(source, writer)

    assert result.verdict == "gaps_detected"
    integrity = read_sidecar(meta)["integrity"]
    assert integrity["n_frames_missing"] == 10
    assert len(integrity["gaps"]) == 1
    assert integrity["gaps"][0]["after_frame"] == 30


def test_result_reports_the_paths_written(tmp_path):
    source, _ = _source(tmp_path, 20, frames_per_packet=10)
    raw, meta = tmp_path / "out.raw", tmp_path / "out_meta.json"
    with RecordingWriter(raw, meta, PARAMS) as writer:
        result = record_session(source, writer)
    assert result.raw_path == str(raw)
    assert result.meta_path == str(meta)
