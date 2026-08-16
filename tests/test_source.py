"""Fake-driver tests for the Python-side control flow in DriverPacketSource.

`biocam/interop/source.py` imports no `clr`/pythonnet/clr_loader itself — the
.NET calls it makes are duck-typed onto whatever `device.biocam` is. That
means the class can be exercised here, on the development machine, against a
fake stand-in that mimics just the surface `source.py` touches
(`DataReceived`/`DataLossAsync`/`DataStreamingError` +=/-=, and
`StartDataStreaming`/`StopDataStreaming`). This covers only the Python
control flow around those calls (the re-entrancy guard, cleanup-always-runs,
exception-vs-refusal, the None-device path) — never the calls' real
behaviour, which only the instrument can confirm.
"""

import pytest

from biocam.interop.source import STOP, DriverPacketSource


class _FakeEvent:
    """Stands in for a .NET multicast delegate: supports += and -=."""

    def __init__(self):
        self.handlers = []

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self

    def __isub__(self, handler):
        if handler in self.handlers:
            self.handlers.remove(handler)
        return self


class _FakeBioCam:
    def __init__(self):
        self.DataReceived = _FakeEvent()
        self.DataLossAsync = _FakeEvent()
        self.DataStreamingError = _FakeEvent()
        self.start_calls = 0
        self.stop_calls = 0
        self.stop_return = True
        self.stop_raises = None

    def StartDataStreaming(self, dataPacketTimeSpanMs, optimizeDataPacketLatency):
        self.start_calls += 1
        return True

    def StopDataStreaming(self):
        self.stop_calls += 1
        if self.stop_raises is not None:
            raise self.stop_raises
        return self.stop_return


class _FakeDevice:
    def __init__(self, biocam):
        self.biocam = biocam


def _make_started_source():
    biocam = _FakeBioCam()
    device = _FakeDevice(biocam)
    source = DriverPacketSource(device, queue_size=10)
    source.start()
    return source, device, biocam


def test_start_guards_against_reentry():
    source, _device, biocam = _make_started_source()
    assert biocam.start_calls == 1

    with pytest.raises(RuntimeError):
        source.start()

    # The guard fires before touching the driver a second time.
    assert biocam.start_calls == 1
    assert source._streaming is True


def test_stop_succeeds_and_clears_streaming():
    source, _device, biocam = _make_started_source()
    source.stop()
    assert biocam.stop_calls == 1
    assert source._streaming is False
    assert source._stop_event.is_set()
    assert source._queue.get_nowait() is STOP


def test_stop_raises_runtime_error_on_falsy_return_but_still_cleans_up():
    source, _device, biocam = _make_started_source()
    biocam.stop_return = False

    with pytest.raises(RuntimeError, match="StopDataStreaming failed"):
        source.stop()

    # A refusal leaves _streaming True so a retried stop() tries again.
    assert source._streaming is True
    # Cleanup still ran even though the call reported failure.
    assert source._stop_event.is_set()
    assert not biocam.DataReceived.handlers
    assert not biocam.DataLossAsync.handlers
    assert not biocam.DataStreamingError.handlers
    assert source._queue.get_nowait() is STOP


def test_stop_propagates_original_exception_distinct_from_refusal():
    source, _device, biocam = _make_started_source()
    biocam.stop_raises = ValueError("driver blew up")

    with pytest.raises(ValueError, match="driver blew up"):
        source.stop()

    # The original exception surfaces, not the generic RuntimeError used
    # for a falsy return - a caller must be able to tell the two apart.
    assert source._streaming is True
    # Cleanup still ran despite the exception.
    assert source._stop_event.is_set()
    assert not biocam.DataReceived.handlers
    assert not biocam.DataLossAsync.handlers
    assert not biocam.DataStreamingError.handlers
    assert source._queue.get_nowait() is STOP


def test_stop_after_device_exit_does_not_raise():
    source, device, biocam = _make_started_source()
    # Simulate BioCamDevice.__exit__, which sets self.biocam = None. The
    # current CLI ordering never calls stop() after this, but the code must
    # not crash if a future reordering makes it happen.
    device.biocam = None

    source.stop()  # must not raise AttributeError

    # StopDataStreaming was never reachable, so it was never called.
    assert biocam.stop_calls == 0
    # Python-side cleanup still happened.
    assert source._stop_event.is_set()
    assert source._queue.get_nowait() is STOP
