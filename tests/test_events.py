from biocam.data.events import (
    DiskLow, DriverDataLoss, GapDetected, QueueOverflow, QueuePressure,
    RecordingStarted, RecordingStopped, describe,
)


def test_events_are_immutable():
    import dataclasses
    import pytest
    event = QueueOverflow(total=3)
    with pytest.raises(dataclasses.FrozenInstanceError):
        event.total = 4


def test_describe_gap_names_the_numbers():
    text = describe(GapDetected(after_frame=92100, missing_frames=371, duration_ms=19.99))
    assert "92100" in text
    assert "371" in text
    assert "19.99" in text


def test_describe_stopped_reports_reason_and_verdict():
    text = describe(RecordingStopped(reason="user_stopped", n_frames=100, verdict="clean"))
    assert "user_stopped" in text
    assert "clean" in text
    assert "100" in text


def test_describe_handles_every_event_type():
    events = [
        RecordingStarted(path="a.raw", total_channels=4096, frame_rate_hz=18557.72),
        GapDetected(after_frame=1, missing_frames=2, duration_ms=0.1),
        QueuePressure(depth=90, capacity=100),
        QueueOverflow(total=1),
        DriverDataLoss(total=1),
        DiskLow(free_bytes=1000, required_bytes=2000),
        RecordingStopped(reason="duration_reached", n_frames=5, verdict="unknown"),
    ]
    for event in events:
        text = describe(event)
        assert isinstance(text, str) and text, f"no description for {type(event).__name__}"


def test_describe_rejects_unknown_objects():
    import pytest
    with pytest.raises(TypeError):
        describe("not an event")
