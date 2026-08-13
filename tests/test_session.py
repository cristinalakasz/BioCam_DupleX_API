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
    pulled = []

    def stopping_after_three(packets):
        for index, packet in enumerate(packets):
            pulled.append(index)
            yield packet
            if index == 2:
                stop.set()

    with RecordingWriter(raw, meta, PARAMS) as writer:
        result = record_session(stopping_after_three(source), writer, stop_event=stop)

    assert result.stop_reason == "user_stopped"
    # The session never discards a packet it has already pulled from the
    # source: it writes first, then checks stop_event. `stop` is set from
    # inside the generator's post-yield code for packet index 2, which only
    # runs once the loop asks for packet index 3 - so packet 3 has already
    # been pulled and handed to record_session by the time the flag becomes
    # visible. That packet gets written before the loop breaks, so four
    # packets (40 frames) are recorded, not three. A stop that lands one
    # packet late is invisible; a recording that silently drops signal it
    # already received is not - so this 40, not 30, is correct on purpose.
    assert pulled == [0, 1, 2, 3]
    assert result.n_frames == 40
    assert result.n_frames == len(pulled) * 10  # every pulled packet was written


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
