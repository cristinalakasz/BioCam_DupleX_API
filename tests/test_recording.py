import json

import numpy as np
import pytest

from biocam.data.events import GapDetected, RecordingStarted, RecordingStopped
from biocam.data.recording import (
    SCHEMA_VERSION, AcquisitionParameters, RecordingWriter,
    integrity_verdict, load_recording, read_sidecar,
)

PARAMS = AcquisitionParameters(
    frame_rate_hz=18557.720703125,
    total_channels=4,
    ch_sample_byte_size=2,
    bit_depth=12,
    adc_counts_to_value=2.0146520146520146,
    offset=-4125.0,
    min_digital_value=0,
    max_digital_value=4095,
)


def _frame(values):
    return np.asarray(values, dtype=np.uint16).tobytes()


def _paths(tmp_path):
    return tmp_path / "rec.raw", tmp_path / "rec_meta.json"


def test_bytes_per_frame():
    assert PARAMS.bytes_per_frame == 8


def test_clean_run_writes_bytes_verbatim_and_reports_clean(tmp_path):
    raw, meta = _paths(tmp_path)
    with RecordingWriter(raw, meta, PARAMS) as writer:
        writer.write_packet(timestamp=100, counter=1, payload=_frame([1, 2, 3, 4]))
        writer.write_packet(timestamp=200, counter=2, payload=_frame([5, 6, 7, 8]))
        writer.finalise("duration_reached")

    assert raw.read_bytes() == _frame([1, 2, 3, 4, 5, 6, 7, 8])
    record = read_sidecar(meta)
    assert record["schema_version"] == SCHEMA_VERSION
    assert record["status"] == "complete"
    assert record["stop_reason"] == "duration_reached"
    assert record["n_frames_written"] == 2
    assert record["integrity"]["verdict"] == "clean"
    assert record["integrity"]["gaps"] == []
    assert record["integrity"]["n_frames_missing"] == 0
    assert record["integrity"]["first_timestamp"] == 100
    assert record["integrity"]["last_timestamp"] == 200


def test_a_gap_is_recorded_with_position_and_size(tmp_path):
    raw, meta = _paths(tmp_path)
    with RecordingWriter(raw, meta, PARAMS) as writer:
        writer.write_packet(timestamp=100, counter=1, payload=_frame([1, 2, 3, 4]))
        writer.write_packet(timestamp=300, counter=4, payload=_frame([5, 6, 7, 8]))
        writer.finalise("duration_reached")

    record = read_sidecar(meta)
    assert record["integrity"]["verdict"] == "gaps_detected"
    assert len(record["integrity"]["gaps"]) == 1
    gap = record["integrity"]["gaps"][0]
    assert gap["after_frame"] == 1
    assert gap["missing_frames"] == 2        # 2 lost packets x 1 frame each
    assert record["integrity"]["n_frames_missing"] == 2


def test_sidecar_exists_and_says_in_progress_before_finalise(tmp_path):
    raw, meta = _paths(tmp_path)
    writer = RecordingWriter(raw, meta, PARAMS)
    writer.__enter__()
    writer.write_packet(timestamp=1, counter=1, payload=_frame([1, 2, 3, 4]))
    record = read_sidecar(meta)
    assert record["status"] == "in_progress"
    writer.finalise("user_stopped")
    writer.__exit__(None, None, None)
    assert read_sidecar(meta)["status"] == "complete"


def test_driver_loss_and_queue_overflow_are_counted_separately(tmp_path):
    raw, meta = _paths(tmp_path)
    with RecordingWriter(raw, meta, PARAMS) as writer:
        writer.write_packet(timestamp=1, counter=1, payload=_frame([1, 2, 3, 4]))
        writer.note_driver_loss(2)
        writer.note_queue_overflow(5)
        writer.finalise("error")

    integrity = read_sidecar(meta)["integrity"]
    assert integrity["driver_loss_events"] == 2
    assert integrity["queue_overflows"] == 5
    assert integrity["verdict"] == "gaps_detected"


def test_callback_errors_are_counted_and_reach_the_sidecar(tmp_path):
    raw, meta = _paths(tmp_path)
    with RecordingWriter(raw, meta, PARAMS) as writer:
        writer.write_packet(timestamp=1, counter=1, payload=_frame([1, 2, 3, 4]))
        writer.note_callback_errors(3)
        writer.finalise("error")

    integrity = read_sidecar(meta)["integrity"]
    assert integrity["callback_errors"] == 3
    assert integrity["verdict"] == "gaps_detected"


def test_discarded_at_stop_is_counted_and_never_reports_clean(tmp_path):
    raw, meta = _paths(tmp_path)
    with RecordingWriter(raw, meta, PARAMS) as writer:
        writer.write_packet(timestamp=1, counter=1, payload=_frame([1, 2, 3, 4]))
        writer.note_discarded(4)
        writer.finalise("drain_deadline_exceeded")

    integrity = read_sidecar(meta)["integrity"]
    assert integrity["discarded_at_stop"] == 4
    assert integrity["verdict"] == "gaps_detected"


def test_counter_anomaly_alone_is_non_clean_and_lands_in_sidecar(tmp_path):
    """A counter moving backwards is not a gap and not a clean run - we do not
    know what happened, so the verdict must not claim either."""
    raw, meta = _paths(tmp_path)
    with RecordingWriter(raw, meta, PARAMS) as writer:
        writer.write_packet(timestamp=100, counter=1000, payload=_frame([1, 2, 3, 4]))
        writer.write_packet(timestamp=200, counter=999, payload=_frame([5, 6, 7, 8]))
        writer.finalise("duration_reached")

    integrity = read_sidecar(meta)["integrity"]
    assert integrity["counter_anomalies"] == 1
    assert integrity["gaps"] == []
    assert integrity["verdict"] == "unknown"


def test_partial_frame_payloads_still_yield_a_correct_frame_count(tmp_path):
    """The frame count must be derived from bytes written, not accumulated
    per-packet: two payloads that are each 1.5 frames still write 3 whole
    frames of bytes to the file, and the sidecar must agree."""
    raw, meta = _paths(tmp_path)
    payload_a = np.arange(0, 6, dtype=np.uint16).tobytes()   # 12 bytes = 1.5 frames
    payload_b = np.arange(6, 12, dtype=np.uint16).tobytes()  # 12 bytes = 1.5 frames
    with RecordingWriter(raw, meta, PARAMS) as writer:
        writer.write_packet(timestamp=1, counter=1, payload=payload_a)
        writer.write_packet(timestamp=2, counter=2, payload=payload_b)
        writer.finalise("duration_reached")
        assert writer.n_frames_written == 3

    assert raw.read_bytes() == payload_a + payload_b
    record = read_sidecar(meta)
    assert record["n_frames_written"] == 3


def test_exit_without_finalise_writes_failed_status_with_exception_type(tmp_path):
    """The claim that a killed process leaves an honest marker rests on this:
    a with-block that never reaches finalise() must leave status 'failed',
    not 'in_progress' and certainly not 'complete', with the exception type
    recorded for later diagnosis."""
    raw, meta = _paths(tmp_path)
    with pytest.raises(ValueError):
        with RecordingWriter(raw, meta, PARAMS) as writer:
            writer.write_packet(timestamp=1, counter=1, payload=_frame([1, 2, 3, 4]))
            raise ValueError("boom")

    record = read_sidecar(meta)
    assert record["status"] == "failed"
    assert record["stop_reason"] == "error"
    assert record["error"] == "ValueError"


def test_a_crashed_run_with_no_losses_writes_unknown_not_clean_to_the_sidecar(tmp_path):
    """The write path, not just integrity_verdict(): a failed sidecar must
    not literally contain "verdict": "clean" in its JSON. A human opening
    the file, a reader in another language, and load_recording() all read
    the integrity block directly and never call integrity_verdict() at all -
    so the file itself has to be correct, not just correct when read through
    the one function that knows to correct it."""
    raw, meta = _paths(tmp_path)
    with pytest.raises(ValueError):
        with RecordingWriter(raw, meta, PARAMS) as writer:
            writer.write_packet(timestamp=1, counter=1, payload=_frame([1, 2, 3, 4]))
            raise ValueError("boom")

    record = read_sidecar(meta)
    assert record["status"] == "failed"
    assert record["integrity"]["verdict"] == "unknown"
    # The property being protected: the file and the function must agree.
    # Asserting the string alone would let them drift apart again.
    assert record["integrity"]["verdict"] == integrity_verdict(record)


def test_a_crashed_run_with_losses_keeps_gaps_detected_in_the_sidecar(tmp_path):
    """A crash does not erase loss that was genuinely detected before it -
    that stays gaps_detected on disk, not downgraded to unknown."""
    raw, meta = _paths(tmp_path)
    with pytest.raises(ValueError):
        with RecordingWriter(raw, meta, PARAMS) as writer:
            writer.write_packet(timestamp=1, counter=1, payload=_frame([1, 2, 3, 4]))
            writer.note_driver_loss(2)
            raise ValueError("boom")

    record = read_sidecar(meta)
    assert record["status"] == "failed"
    assert record["integrity"]["verdict"] == "gaps_detected"
    assert record["integrity"]["verdict"] == integrity_verdict(record)


def test_a_normal_clean_recording_agrees_on_disk_and_via_integrity_verdict(tmp_path):
    """The control case: a normal, complete, loss-free recording is still
    exactly "clean" both in the raw JSON and through integrity_verdict() -
    the write-path fix must not touch the case it was never about."""
    raw, meta = _paths(tmp_path)
    with RecordingWriter(raw, meta, PARAMS) as writer:
        writer.write_packet(timestamp=1, counter=1, payload=_frame([1, 2, 3, 4]))
        writer.finalise("duration_reached")

    record = read_sidecar(meta)
    assert record["status"] == "complete"
    assert record["integrity"]["verdict"] == "clean"
    assert record["integrity"]["verdict"] == integrity_verdict(record)


def test_the_in_progress_sidecar_does_not_claim_clean(tmp_path):
    """Written by __enter__ before a single packet has arrived. Nothing has
    gone wrong yet, but nothing has been verified either - the recording is
    not over, so "clean" would claim more than is known. Same reasoning as
    a failed run, applied before the run even starts writing frames."""
    raw, meta = _paths(tmp_path)
    writer = RecordingWriter(raw, meta, PARAMS)
    writer.__enter__()
    try:
        record = read_sidecar(meta)
        assert record["status"] == "in_progress"
        assert record["integrity"]["verdict"] == "unknown"
        assert record["integrity"]["verdict"] == integrity_verdict(record)
    finally:
        writer.finalise("duration_reached")
        writer.__exit__(None, None, None)


def test_events_are_emitted_to_the_listener(tmp_path):
    raw, meta = _paths(tmp_path)
    seen = []
    with RecordingWriter(raw, meta, PARAMS, listener=seen.append) as writer:
        writer.write_packet(timestamp=100, counter=1, payload=_frame([1, 2, 3, 4]))
        writer.write_packet(timestamp=300, counter=4, payload=_frame([5, 6, 7, 8]))
        writer.finalise("duration_reached")

    assert isinstance(seen[0], RecordingStarted)
    assert any(isinstance(e, GapDetected) for e in seen)
    assert isinstance(seen[-1], RecordingStopped)
    assert seen[-1].verdict == "gaps_detected"


def test_verdict_unknown_when_the_sidecar_predates_schema_2():
    assert integrity_verdict({"total_channels": 4096}) == "unknown"


def test_verdict_unknown_is_never_upgraded_to_clean():
    legacy = {"total_channels": 4096, "n_frames_total": 100, "packet_log": []}
    assert integrity_verdict(legacy) != "clean"


def test_verdict_read_from_a_schema_2_sidecar():
    modern = {"schema_version": 2, "integrity": {"verdict": "clean"}}
    assert integrity_verdict(modern) == "clean"


def test_a_failed_run_reporting_clean_is_downgraded_to_unknown():
    """status: failed means the recorder never got to say the run was fine -
    a crash right after the last write leaves whatever came next, including
    the raw file's tail, unverified. A 'clean' verdict in that block would
    claim to know the whole run was fine; we only know it never finished, so
    it must read 'unknown', the same reasoning already applied to a missing
    schema_version."""
    failed = {"schema_version": 2, "status": "failed", "integrity": {"verdict": "clean"}}
    assert integrity_verdict(failed) == "unknown"


def test_a_failed_run_with_recorded_gaps_still_reports_them():
    """A crash does not erase gaps already detected before it. That is real,
    more specific information than 'unknown', and downgrading it would throw
    away what is actually known."""
    failed = {"schema_version": 2, "status": "failed",
              "integrity": {"verdict": "gaps_detected"}}
    assert integrity_verdict(failed) == "gaps_detected"


def test_load_recording_round_trips_values(tmp_path):
    raw, meta = _paths(tmp_path)
    with RecordingWriter(raw, meta, PARAMS) as writer:
        writer.write_packet(timestamp=1, counter=1, payload=_frame([100, 200, 300, 400]))
        writer.finalise("duration_reached")

    counts, record = load_recording(raw, meta, as_microvolts=False)
    assert counts.tolist() == [[100, 200, 300, 400]]

    volts, _ = load_recording(raw, meta, as_microvolts=True)
    assert volts[0, 0] == pytest.approx(-4125.0 + 100 * 2.0146520146520146)


def test_load_recording_reports_the_verdict_it_found(tmp_path):
    raw, meta = _paths(tmp_path)
    with RecordingWriter(raw, meta, PARAMS) as writer:
        writer.write_packet(timestamp=1, counter=1, payload=_frame([1, 2, 3, 4]))
        writer.finalise("duration_reached")
    _, record = load_recording(raw, meta)
    assert record["integrity"]["verdict"] == "clean"


def test_committed_fixtures_are_reported_as_unknown():
    """The fixtures predate schema 2 and must never read as clean."""
    from tests.test_fixture_integrity import load_fixture
    _, meta = load_fixture("sample_32ch_2s")
    assert integrity_verdict(meta) == "unknown"
