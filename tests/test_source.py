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

import gc
import sys

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


class _FakeHeader:
    """Stands in for DataPacketHeader: just the two fields source.py reads."""

    def __init__(self, timestamp, counter):
        self.Timestamp = timestamp
        self.PacketCounter = counter


class _FakeDataArgs:
    """Stands in for DataPacketReceivedEventArgs."""

    def __init__(self, timestamp, counter, payload):
        self.Header = _FakeHeader(timestamp, counter)
        self.Payload = payload


class _FakeLossArgs:
    """Stands in for DataLossEventArgs, with a real Counter field."""

    def __init__(self, counter):
        self.Counter = counter


class _CounterlessLossArgs:
    """Stands in for a DataLossEventArgs whose Counter is unreadable."""


def _fire_data(biocam, counter):
    biocam.DataReceived.handlers[0](None, _FakeDataArgs(
        timestamp=counter, counter=counter, payload=bytes([counter % 256])))


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
    assert source._queue.popleft() is STOP


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
    assert source._queue.popleft() is STOP


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
    assert source._queue.popleft() is STOP


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
    assert source._queue.popleft() is STOP
    # _streaming must be cleared even though StopDataStreaming was never
    # reached - otherwise start()'s re-entrancy guard would block every
    # subsequent start() on this instance forever.
    assert source._streaming is False
    device.biocam = biocam  # restore, so a second start() has something to call
    source.start()
    assert biocam.start_calls == 2


def test_overflow_drops_new_packet_and_keeps_old_ones():
    # The deque-maxlen trap: a bounded deque would silently evict the
    # oldest entry on append. The required behaviour is the opposite - the
    # incoming packet is dropped and the old ones are kept untouched.
    source, _device, biocam = _make_started_source()  # queue_size=10

    for counter in range(10):
        _fire_data(biocam, counter)
    assert len(source._queue) == 10
    assert source.queue_overflows == 0

    _fire_data(biocam, 99)  # 11th packet - queue is already full

    assert len(source._queue) == 10
    assert source.queue_overflows == 1
    # The oldest packet is still first in line; the new one never got in.
    assert source._queue[0].counter == 0
    assert source._queue[-1].counter == 9


def test_iteration_terminates_via_stop_flag_alone_when_sentinel_dropped(monkeypatch):
    # Fill the queue completely so stop()'s STOP sentinel is dropped, then
    # confirm __iter__ still terminates - via the stop flag alone.
    source, _device, biocam = _make_started_source()  # queue_size=10
    for counter in range(10):
        _fire_data(biocam, counter)
    assert len(source._queue) == 10

    monkeypatch.setattr("biocam.interop.source.POLL_INTERVAL_SEC", 0)

    source.stop()
    assert STOP not in source._queue  # sentinel really was dropped

    consumed = list(source)

    assert len(consumed) == 10
    assert all(item is not STOP for item in consumed)
    assert source._stop_event.is_set()


def test_switch_interval_and_gc_state_restored_after_stop():
    prev_interval = sys.getswitchinterval()
    prev_gc_enabled = gc.isenabled()

    source, _device, _biocam = _make_started_source()
    assert sys.getswitchinterval() == pytest.approx(0.0005)
    assert gc.isenabled() is False

    source.stop()

    assert sys.getswitchinterval() == pytest.approx(prev_interval)
    assert gc.isenabled() is prev_gc_enabled


def test_driver_loss_events_reports_drivers_cumulative_counter():
    source, _device, biocam = _make_started_source()

    biocam.DataLossAsync.handlers[0](None, _FakeLossArgs(counter=5))
    assert source.driver_loss_events == 5

    # Counter is cumulative - the latest value replaces, not adds to, the
    # previous one.
    biocam.DataLossAsync.handlers[0](None, _FakeLossArgs(counter=9))
    assert source.driver_loss_events == 9
    assert source.callback_errors == 0


def test_driver_loss_events_falls_back_to_event_count_when_counter_missing():
    source, _device, biocam = _make_started_source()

    biocam.DataLossAsync.handlers[0](None, _CounterlessLossArgs())
    biocam.DataLossAsync.handlers[0](None, _CounterlessLossArgs())

    assert source.driver_loss_events == 2
    assert source.callback_errors == 0


def test_stopped_reflects_the_sources_own_stop_flag_not_a_callers():
    source, _device, biocam = _make_started_source()
    assert source.stopped is False

    biocam.DataStreamingError.handlers[0](None, None)  # on_error

    assert source.stopped is True


def test_iter_drains_packets_appended_after_stop_sentinel():
    # FIX 3: on_error enqueues STOP without unsubscribing, so the driver can
    # keep firing DataReceived afterwards. Those late packets land behind
    # STOP in the deque; __iter__ must still deliver them instead of
    # returning the instant it pops the sentinel.
    source, _device, biocam = _make_started_source()  # queue_size=10

    _fire_data(biocam, 0)
    _fire_data(biocam, 1)
    biocam.DataStreamingError.handlers[0](None, None)  # enqueues STOP
    _fire_data(biocam, 2)  # arrives after STOP - the on_error race
    _fire_data(biocam, 3)

    assert list(source._queue).count(STOP) == 1
    assert source._stop_event.is_set()

    consumed = list(source)

    assert [packet.counter for packet in consumed] == [0, 1, 2, 3]
    assert all(item is not STOP for item in consumed)


def test_pending_count_excludes_the_stop_sentinel_and_drains_the_queue():
    source, _device, biocam = _make_started_source()
    _fire_data(biocam, 0)
    _fire_data(biocam, 1)
    source.stop()  # appends STOP

    assert STOP in source._queue
    assert source.pending_count() == 2
    # pending_count() drains as it counts - a second call finds nothing.
    assert source.pending_count() == 0
    assert len(source._queue) == 0


class _FailingSubscribeEvent(_FakeEvent):
    """Raises on +=, simulating a driver-side failure to attach a handler."""

    def __iadd__(self, handler):
        raise RuntimeError("subscribe failed")


def test_failed_subscription_leaves_no_handler_attached_and_restores_tuning():
    # Gate 1, item A: DataReceived subscribes successfully (it is a plain
    # _FakeEvent, appended before the failure), then DataLossAsync's
    # __iadd__ raises. Before the fix, both the subscription loop and
    # _apply_runtime_tuning() ran ahead of the guarding try/except, so this
    # failure would have escaped with DataReceived still subscribed and GC
    # left disabled for the rest of the process.
    biocam = _FakeBioCam()
    biocam.DataLossAsync = _FailingSubscribeEvent()
    device = _FakeDevice(biocam)
    source = DriverPacketSource(device, queue_size=10)

    prev_interval = sys.getswitchinterval()
    prev_gc_enabled = gc.isenabled()

    with pytest.raises(RuntimeError, match="subscribe failed"):
        source.start()

    # DataReceived attached before the failure, and must be detached again
    # by the cleanup path - not left subscribed.
    assert not biocam.DataReceived.handlers
    # StartDataStreaming was never reached.
    assert biocam.start_calls == 0
    # Runtime tuning applied before the failing subscription must be undone.
    assert sys.getswitchinterval() == pytest.approx(prev_interval)
    assert gc.isenabled() is prev_gc_enabled
    assert source._streaming is False


def test_start_clears_a_buffer_left_over_from_a_prior_cycle():
    # FIX 4: a consumer that broke out of __iter__ early (e.g. a drain
    # deadline) can leave straggler packets or a stale STOP behind. A second
    # start() on the same source must not hand those to the new session.
    source, device, biocam = _make_started_source()
    _fire_data(biocam, 0)
    _fire_data(biocam, 1)
    source.stop()  # appends STOP; queue is now [pkt0, pkt1, STOP], unread

    assert len(source._queue) == 3

    source.start()

    assert len(source._queue) == 0
    assert biocam.start_calls == 2
