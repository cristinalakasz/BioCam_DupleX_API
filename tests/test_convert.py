import h5py
import numpy as np
import pytest

from biocam.convert import convert, verify
from biocam.data.recording import AcquisitionParameters, RecordingWriter

PARAMS = AcquisitionParameters(
    frame_rate_hz=1000.0, total_channels=4, ch_sample_byte_size=2, bit_depth=12,
    adc_counts_to_value=2.0, offset=-10.0, min_digital_value=0, max_digital_value=4095,
)


def _recording(tmp_path, drop_gap=False):
    raw, meta = tmp_path / "r.raw", tmp_path / "r_meta.json"
    with RecordingWriter(raw, meta, PARAMS) as writer:
        writer.write_packet(1, 1, np.arange(8, dtype=np.uint16).tobytes())
        writer.write_packet(2, 4 if drop_gap else 2,
                            np.arange(8, 16, dtype=np.uint16).tobytes())
        writer.finalise("duration_reached")
    return raw, meta


def test_round_trip_is_byte_identical(tmp_path):
    raw, meta = _recording(tmp_path)
    out = tmp_path / "r.h5"
    convert(raw, meta, out)
    with h5py.File(out, "r") as handle:
        restored = handle["data"][:]
    assert restored.tobytes() == raw.read_bytes()


def test_dataset_shape_matches_metadata(tmp_path):
    raw, meta = _recording(tmp_path)
    out = tmp_path / "r.h5"
    convert(raw, meta, out)
    with h5py.File(out, "r") as handle:
        assert handle["data"].shape == (4, 4)
        assert handle["data"].dtype == np.uint16


def test_metadata_is_carried_as_attributes(tmp_path):
    raw, meta = _recording(tmp_path)
    out = tmp_path / "r.h5"
    convert(raw, meta, out)
    with h5py.File(out, "r") as handle:
        assert handle.attrs["frame_rate_hz"] == pytest.approx(1000.0)
        assert handle.attrs["total_channels"] == 4
        assert handle.attrs["integrity_verdict"] == "clean"


def test_gaps_are_carried_across(tmp_path):
    raw, meta = _recording(tmp_path, drop_gap=True)
    out = tmp_path / "r.h5"
    convert(raw, meta, out)
    with h5py.File(out, "r") as handle:
        assert handle.attrs["integrity_verdict"] == "gaps_detected"
        assert handle["gaps"].shape[0] == 1


def test_convert_reports_what_it_did(tmp_path):
    raw, meta = _recording(tmp_path)
    out = tmp_path / "r.h5"
    report = convert(raw, meta, out)
    assert report["n_frames"] == 4
    assert report["raw_bytes"] == 32
    assert report["output_bytes"] > 0
    assert report["verdict"] == "clean"


def test_verify_accepts_a_good_conversion(tmp_path):
    raw, meta = _recording(tmp_path)
    out = tmp_path / "r.h5"
    convert(raw, meta, out)
    assert verify(out, raw, meta) is True


def test_verify_rejects_a_corrupted_conversion(tmp_path):
    raw, meta = _recording(tmp_path)
    out = tmp_path / "r.h5"
    convert(raw, meta, out)
    with h5py.File(out, "a") as handle:
        handle["data"][0, 0] = 999
    assert verify(out, raw, meta) is False


def test_verify_detects_a_truncated_tail(tmp_path):
    """write_packet never requires a payload to be frame-aligned (see its
    docstring in recording.py), so a session whose last packet ends mid-frame
    - a crash, or a driver-reported partial chunk - leaves a raw file whose
    byte count is not a whole multiple of bytes_per_frame. _load_raw() floor-
    divides that remainder away on every call, so comparing two calls to it
    against each other can never see the truncation - verify() must check the
    raw file's actual size instead."""
    raw, meta = tmp_path / "r.raw", tmp_path / "r_meta.json"
    with RecordingWriter(raw, meta, PARAMS) as writer:
        # bytes_per_frame is 8 (4 channels x 2 bytes); 12 bytes is 1.5 frames,
        # so 4 trailing bytes are not a whole frame.
        writer.write_packet(1, 1, np.arange(6, dtype=np.uint16).tobytes())
        writer.finalise("duration_reached")

    out = tmp_path / "r.h5"
    convert(raw, meta, out)
    assert verify(out, raw, meta) is False


def test_real_fixture_round_trips(tmp_path):
    """The strongest test available: real recorded signal, in and out."""
    from tests.test_fixture_integrity import FIXTURE_DIR
    raw = FIXTURE_DIR / "sample_32ch_2s.raw"
    meta = FIXTURE_DIR / "sample_32ch_2s_meta.json"
    out = tmp_path / "fixture.h5"
    convert(raw, meta, out)
    assert verify(out, raw, meta) is True
    with h5py.File(out, "r") as handle:
        assert handle["data"].shape == (37115, 32)
        assert handle.attrs["integrity_verdict"] == "unknown"
