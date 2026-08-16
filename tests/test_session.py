import collections
import threading
import time

import numpy as np
import pytest

from biocam.data.events import DriverDataLoss, QueueOverflow
from biocam.data.recording import AcquisitionParameters, RecordingWriter, read_sidecar
from biocam.data.replay import Packet, ReplayPacketSource
from biocam.session import (
    COUNTER_CHECK_INTERVAL_PACKETS, DRAIN_DEADLINE_SEC, SessionResult, record_session,
)

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


class _ExplodingSource:
    """Yields a couple of packets, like a driver session, then raises - the
    scenario the reviewer probed: a source that dies mid-recording with
    non-zero loss counters already accumulated."""

    def __init__(self, driver_loss_events, queue_overflows, callback_errors):
        self.driver_loss_events = driver_loss_events
        self.queue_overflows = queue_overflows
        self.callback_errors = callback_errors

    def __iter__(self):
        for counter in range(2):
            yield Packet(timestamp=counter, counter=counter,
                        payload=np.arange(4, dtype=np.uint16).tobytes())
        raise RuntimeError("driver connection lost")


def test_counters_reach_the_sidecar_even_when_the_source_raises(tmp_path):
    """Before this fix, the counter transfer sat after the packet loop: an
    exception raised mid-loop skipped it entirely, and
    RecordingWriter.__exit__ then wrote the failed-run sidecar itself with
    driver_loss_events, queue_overflows and callback_errors all still at
    zero and verdict 'clean' - the same defect Critical 1 closed, reached by
    a different route (the reviewer reproduced exactly this: 7/3/1 producing
    an all-zero, 'clean' sidecar alongside status: 'failed'). Moving the
    transfer into a `finally` closes it for every exit, not just the normal
    ones. This test fails if the transfer moves back out of the `finally`,
    because the RuntimeError below would once again skip it."""
    raw, meta = tmp_path / "out.raw", tmp_path / "out_meta.json"
    source = _ExplodingSource(driver_loss_events=7, queue_overflows=3, callback_errors=1)

    with pytest.raises(RuntimeError):
        with RecordingWriter(raw, meta, PARAMS) as writer:
            record_session(source, writer, counters=source)

    record = read_sidecar(meta)
    assert record["status"] == "failed"
    integrity = record["integrity"]
    assert integrity["driver_loss_events"] == 7
    assert integrity["queue_overflows"] == 3
    assert integrity["callback_errors"] == 1
    assert integrity["verdict"] != "clean"


class _FakeDrainSource:
    """Stands in for DriverPacketSource for drain-mode tests: exposes the
    counters + pending_count() surface record_session's `finally` reads
    (pending_count is a method on the real class too - see its docstring in
    biocam/interop/source.py - because it drains rather than peeks), and
    pops from an internal buffer one at a time like the real deque-backed
    __iter__ - so what is left in the buffer when a test walks away
    (deadline exceeded) is exactly what pending_count() should report."""

    def __init__(self, n_packets, pop_delay=0.0):
        self._buffer = collections.deque(
            Packet(timestamp=i, counter=i,
                   payload=np.arange(4, dtype=np.uint16).tobytes())
            for i in range(n_packets)
        )
        self.driver_loss_events = 0
        self.queue_overflows = 0
        self.callback_errors = 0
        self._pop_delay = pop_delay
        self.stop_calls = 0
        self.stopped = False

    def pending_count(self) -> int:
        return len(self._buffer)

    def stop(self):
        self.stop_calls += 1

    def __iter__(self):
        while self._buffer:
            if self._pop_delay:
                time.sleep(self._pop_delay)
            yield self._buffer.popleft()


def test_drain_writes_everything_still_buffered_instead_of_discarding_it(tmp_path):
    """FIX 1: a drain pass over a source that runs dry well within the
    deadline must write every packet it holds, not just the first one seen
    after the stop flag was set."""
    raw, meta = tmp_path / "out.raw", tmp_path / "out_meta.json"
    source = _FakeDrainSource(n_packets=5)

    with RecordingWriter(raw, meta, PARAMS) as writer:
        result = record_session(source, writer, drain=True, counters=source,
                                stop_source=source.stop)

    assert result.stop_reason == "source_exhausted"
    assert result.n_frames == 5
    assert source.pending_count() == 0
    integrity = read_sidecar(meta)["integrity"]
    assert integrity["discarded_at_stop"] == 0
    assert result.verdict == "clean"
    assert source.stop_calls == 1


def test_a_drain_that_exceeds_the_deadline_counts_what_it_abandons(tmp_path, monkeypatch):
    """FIX 1: a source that keeps yielding must not hang record_session
    forever - the drain gives up at DRAIN_DEADLINE_SEC, and whatever is
    still sitting in the source's buffer at that point is counted into
    discarded_at_stop rather than silently dropped.

    _FakeDrainSource never sets `stopped` True on its own (stop() only
    increments stop_calls, and stop_source() is not called until after this
    drain's own loop has already exited) - unlike the real CLI's
    KeyboardInterrupt retry, where the preceding non-drain call's
    stop_source() already sets it before this drain call even starts. So
    per MEDIUM 6, this reports the more specific
    "drain_deadline_exceeded_unconfirmed_stop", not plain
    "drain_deadline_exceeded" - see
    test_a_drain_that_exceeds_the_deadline_with_a_confirmed_stop_reports_the_plain_reason
    below for that case."""
    monkeypatch.setattr("biocam.session.DRAIN_DEADLINE_SEC", 0.05)
    raw, meta = tmp_path / "out.raw", tmp_path / "out_meta.json"
    # Far more packets than can be popped inside a 0.05 s deadline at a
    # 0.02 s delay per pop (roughly two or three get through).
    source = _FakeDrainSource(n_packets=1000, pop_delay=0.02)

    with RecordingWriter(raw, meta, PARAMS) as writer:
        result = record_session(source, writer, drain=True, counters=source,
                                stop_source=source.stop)

    assert result.stop_reason == "drain_deadline_exceeded_unconfirmed_stop"
    written = result.n_frames
    abandoned = source.pending_count()
    assert written > 0
    assert abandoned > 0
    assert written + abandoned == 1000
    integrity = read_sidecar(meta)["integrity"]
    assert integrity["discarded_at_stop"] == abandoned
    assert result.verdict != "clean"


def test_a_drain_that_exceeds_the_deadline_with_a_confirmed_stop_reports_the_plain_reason(
        tmp_path, monkeypatch):
    """MEDIUM 6: the shape a real drain call normally has - the source's own
    `stopped` flag already True before the drain loop starts, because the
    preceding non-drain call's stop_source() already set it (see cli.py's
    KeyboardInterrupt retry) - must still report the plain
    "drain_deadline_exceeded", not the "...unconfirmed_stop" variant that
    exists to flag the opposite, more concerning case."""
    monkeypatch.setattr("biocam.session.DRAIN_DEADLINE_SEC", 0.05)
    raw, meta = tmp_path / "out.raw", tmp_path / "out_meta.json"
    source = _FakeDrainSource(n_packets=1000, pop_delay=0.02)
    source.stopped = True

    with RecordingWriter(raw, meta, PARAMS) as writer:
        result = record_session(source, writer, drain=True, counters=source,
                                stop_source=source.stop)

    assert result.stop_reason == "drain_deadline_exceeded"


def test_a_backlog_still_buffered_when_duration_is_reached_is_drained_not_discarded(tmp_path):
    """CRITICAL 1: pending_count() pops and discards, so record_session's
    `finally` used to throw away whatever was still buffered - real,
    already-acquired, immediately-drainable data - the moment a normal
    (non-drain) stop condition landed mid-burst, counting it into
    discarded_at_stop and forcing a false gaps_detected verdict. This is
    what essentially every successful timed run looked like in practice
    (the consumer sleeps when the queue empties and so runs in bursts; the
    frame limit essentially never lands exactly on a burst's last packet).
    The backlog must be drained into the writer first - only what survives
    a short deadline counts as genuinely abandoned."""
    raw, meta = tmp_path / "out.raw", tmp_path / "out_meta.json"
    # frame_rate_hz=1000 in PARAMS, 1 frame per packet (see _FakeDrainSource) -
    # 0.05 s of duration is a 50-frame/50-packet limit, well short of the
    # 100 packets buffered, so the main loop breaks on duration_reached with
    # 50 packets still sitting in the source's buffer.
    source = _FakeDrainSource(n_packets=100)

    with RecordingWriter(raw, meta, PARAMS) as writer:
        result = record_session(source, writer, duration_sec=0.05,
                                counters=source, stop_source=source.stop)

    assert result.stop_reason == "duration_reached"
    assert result.n_frames == 100  # all 100 packets, not just the first 50
    assert source.pending_count() == 0
    integrity = read_sidecar(meta)["integrity"]
    assert integrity["discarded_at_stop"] == 0
    assert result.verdict == "clean"
    assert source.stop_calls == 1


def test_a_backlog_that_outlives_the_finally_drain_deadline_is_counted_abandoned(
        tmp_path, monkeypatch):
    """CRITICAL 1's drain is itself bounded, the same way the drain=True
    path is: a backlog that does not finish arriving before
    DRAIN_DEADLINE_SEC elapses is genuinely abandoned and must still be
    counted, not silently dropped nor waited on forever."""
    monkeypatch.setattr("biocam.session.DRAIN_DEADLINE_SEC", 0.05)
    raw, meta = tmp_path / "out.raw", tmp_path / "out_meta.json"
    # duration_sec=0.01 s at PARAMS' 1 kHz frame rate is a 10-frame limit -
    # the main loop breaks on duration_reached after 10 of the 1000
    # packets, leaving 990 buffered. The finally-block drain then pops a
    # few more (0.02 s/pop) before DRAIN_DEADLINE_SEC cuts it off.
    source = _FakeDrainSource(n_packets=1000, pop_delay=0.02)

    with RecordingWriter(raw, meta, PARAMS) as writer:
        result = record_session(source, writer, duration_sec=0.01,
                                counters=source, stop_source=source.stop)

    assert result.stop_reason == "duration_reached"
    written = result.n_frames
    abandoned = source.pending_count()
    assert written > 10  # more than the main loop alone wrote
    assert abandoned > 0
    assert written + abandoned == 1000
    integrity = read_sidecar(meta)["integrity"]
    assert integrity["discarded_at_stop"] == abandoned
    assert result.verdict != "clean"


class _FakeCumulativeCountsSource:
    """A source whose loss/overflow/error counters are cumulative totals,
    like the real DriverPacketSource - used to prove record_session's
    `finally` does not double them when called twice against one writer,
    the way cli.py's KeyboardInterrupt retry does (CRITICAL 2)."""

    def __init__(self, n_packets, driver_loss_events=0, queue_overflows=0,
                callback_errors=0):
        self._packets = [
            Packet(timestamp=i, counter=i,
                  payload=np.arange(4, dtype=np.uint16).tobytes())
            for i in range(n_packets)
        ]
        self.driver_loss_events = driver_loss_events
        self.queue_overflows = queue_overflows
        self.callback_errors = callback_errors

    def __iter__(self):
        return iter(self._packets)


def test_two_record_session_calls_against_one_writer_do_not_double_the_loss_counters(
        tmp_path):
    """CRITICAL 2: note_driver_loss/note_queue_overflow/note_callback_errors
    receive the source's cumulative totals on every call - record_session's
    `finally` runs on every call, and cli.py calls record_session twice
    against the same writer on the KeyboardInterrupt path (the normal call,
    then the drain=True retry). If these were still `+=` accumulators
    instead of setters, a source reporting 7 driver-loss events would write
    14 to the sidecar, not 7."""
    raw, meta = tmp_path / "out.raw", tmp_path / "out_meta.json"
    source = _FakeCumulativeCountsSource(
        4, driver_loss_events=7, queue_overflows=3, callback_errors=1)

    with RecordingWriter(raw, meta, PARAMS) as writer:
        record_session(source, writer, counters=source)
        record_session(source, writer, counters=source)

    integrity = read_sidecar(meta)["integrity"]
    assert integrity["driver_loss_events"] == 7
    assert integrity["queue_overflows"] == 3
    assert integrity["callback_errors"] == 1


def test_sidecar_with_nonzero_discarded_at_stop_never_reports_clean(tmp_path):
    """A recording that discarded acquired data at stop time must never read
    'clean', even with no gaps, driver loss, overflow or callback errors."""
    raw, meta = tmp_path / "out.raw", tmp_path / "out_meta.json"
    with RecordingWriter(raw, meta, PARAMS) as writer:
        writer.write_packet(timestamp=1, counter=1,
                            payload=np.arange(4, dtype=np.uint16).tobytes())
        writer.note_discarded(3)
        writer.finalise("drain_deadline_exceeded")

    record = read_sidecar(meta)
    assert record["integrity"]["discarded_at_stop"] == 3
    assert record["integrity"]["verdict"] != "clean"


class _FakeCounterSource:
    """A source whose queue_overflows / driver_loss_events change partway
    through iteration, the way the real DriverPacketSource's counters move
    on the driver's own callback thread(s) while record_session's loop
    consumes packets on the consumer thread."""

    def __init__(self, n_packets, overflow_at=None, loss_at=None):
        self._packets = [
            Packet(timestamp=i, counter=i,
                   payload=np.arange(4, dtype=np.uint16).tobytes())
            for i in range(n_packets)
        ]
        self.queue_overflows = 0
        self.driver_loss_events = 0
        self.callback_errors = 0
        self._overflow_at = overflow_at
        self._loss_at = loss_at

    def __iter__(self):
        for index, packet in enumerate(self._packets):
            if self._overflow_at is not None and index == self._overflow_at:
                self.queue_overflows = 5
            if self._loss_at is not None and index == self._loss_at:
                self.driver_loss_events = 3
            yield packet


def test_queue_overflow_and_driver_data_loss_are_emitted_when_counters_move(tmp_path):
    """Gate 1, item F: before this, nothing ever constructed QueueOverflow
    or DriverDataLoss - the only record of dropped data was the sidecar,
    read after the run was over. record_session must notice the source's
    counters moving during the run and emit through the writer's listener,
    the same one GapDetected/DiskLow already use."""
    raw, meta = tmp_path / "out.raw", tmp_path / "out_meta.json"
    n_packets = COUNTER_CHECK_INTERVAL_PACKETS * 2 + 5
    source = _FakeCounterSource(n_packets, overflow_at=10, loss_at=20)
    seen = []

    with RecordingWriter(raw, meta, PARAMS, listener=seen.append) as writer:
        record_session(source, writer, counters=source)

    overflow_events = [e for e in seen if isinstance(e, QueueOverflow)]
    loss_events = [e for e in seen if isinstance(e, DriverDataLoss)]
    assert overflow_events and overflow_events[-1].total == 5
    assert loss_events and loss_events[-1].total == 3
    # Each counter only moved once, so exactly one event each - the check is
    # throttled to every COUNTER_CHECK_INTERVAL_PACKETS packets (plus one
    # trailing flush), not run - and not emitted - on every packet.
    assert len(overflow_events) == 1
    assert len(loss_events) == 1


def test_counter_move_below_the_check_interval_still_surfaces_via_trailing_flush(tmp_path):
    """A run shorter than COUNTER_CHECK_INTERVAL_PACKETS never hits the
    periodic check inside the loop - the trailing check in the `finally`
    must still catch a counter that moved."""
    raw, meta = tmp_path / "out.raw", tmp_path / "out_meta.json"
    source = _FakeCounterSource(5, overflow_at=2)
    seen = []

    with RecordingWriter(raw, meta, PARAMS, listener=seen.append) as writer:
        record_session(source, writer, counters=source)

    overflow_events = [e for e in seen if isinstance(e, QueueOverflow)]
    assert len(overflow_events) == 1
    assert overflow_events[0].total == 5


def test_no_counter_events_emitted_when_nothing_moves(tmp_path):
    raw, meta = tmp_path / "out.raw", tmp_path / "out_meta.json"
    source = _FakeCounterSource(5)
    seen = []

    with RecordingWriter(raw, meta, PARAMS, listener=seen.append) as writer:
        record_session(source, writer, counters=source)

    assert not [e for e in seen if isinstance(e, (QueueOverflow, DriverDataLoss))]


class _FakeErroringSource:
    """A source whose own `stopped` flag flips True partway through, the way
    DriverPacketSource.stopped does after on_error - and which, like the
    real __iter__ post-FIX-3, keeps yielding packets after that point
    instead of raising StopIteration on its own."""

    def __init__(self, n_packets, stop_after):
        self._packets = [
            Packet(timestamp=i, counter=i,
                   payload=np.arange(4, dtype=np.uint16).tobytes())
            for i in range(n_packets)
        ]
        self._stop_after = stop_after
        self.stopped = False
        self.driver_loss_events = 0
        self.queue_overflows = 0
        self.callback_errors = 0

    def __iter__(self):
        for index, packet in enumerate(self._packets):
            yield packet
            if index + 1 == self._stop_after:
                self.stopped = True


def test_a_stopped_source_ends_the_session_promptly_even_if_it_keeps_yielding(tmp_path):
    """FIX 3 makes __iter__ drain past its STOP sentinel instead of
    returning immediately, which removes the side effect that used to end
    record_session's loop promptly on a driver error. counters.stopped
    restores that: the loop must not keep consuming everything a source
    that has already flagged itself stopped still happens to yield."""
    raw, meta = tmp_path / "out.raw", tmp_path / "out_meta.json"
    source = _FakeErroringSource(n_packets=20, stop_after=3)

    with RecordingWriter(raw, meta, PARAMS) as writer:
        result = record_session(source, writer, counters=source)

    assert result.stop_reason == "source_stopped"
    # `stopped` flips inside the generator's post-yield code for packet
    # index 2, which only runs once the loop asks for packet index 3 - the
    # same one-packet-late visibility test_stops_when_the_stop_event_is_set
    # documents for stop_event above. That packet (the 4th) is already
    # pulled and written by the time the flag is visible; stopping promptly
    # must not throw away what was already handed to the writer, so 4
    # frames, not 3, is correct here.
    assert result.n_frames == 4
    assert source.stopped is True
    # Only 4 of the 20 packets the source could still yield were ever
    # pulled - the whole point of checking `stopped` promptly.


def test_stop_source_runs_before_finalise_on_a_normal_completion(tmp_path):
    """FIX 2: stop_source must run - and whatever it leaves pending must be
    counted - before the sidecar is finalised, not after, even when the run
    ends normally (no Ctrl+C involved)."""
    raw, meta = tmp_path / "out.raw", tmp_path / "out_meta.json"
    source, _ = _source(tmp_path, 20, frames_per_packet=10)
    calls = []

    def stop_source():
        calls.append("stopped")

    with RecordingWriter(raw, meta, PARAMS) as writer:
        result = record_session(source, writer, stop_source=stop_source)

    assert calls == ["stopped"]
    assert result.stop_reason == "source_exhausted"
    assert read_sidecar(meta)["status"] == "complete"


def test_stop_source_is_not_called_when_a_normal_exception_propagates_without_it_masking_the_error(tmp_path):
    """A failing stop_source must not replace whatever exception is already
    propagating, nor prevent the counter transfer and cleanup from running."""
    raw, meta = tmp_path / "out.raw", tmp_path / "out_meta.json"
    source = _ExplodingSource(driver_loss_events=1, queue_overflows=0, callback_errors=0)

    def failing_stop_source():
        raise RuntimeError("stop failed too")

    with pytest.raises(RuntimeError, match="driver connection lost"):
        with RecordingWriter(raw, meta, PARAMS) as writer:
            record_session(source, writer, counters=source,
                           stop_source=failing_stop_source)

    integrity = read_sidecar(meta)["integrity"]
    assert integrity["driver_loss_events"] == 1


# --- FIX 3: a running disk-space check stops an open-ended recording ---

def test_session_stops_cleanly_with_reason_disk_low(tmp_path, monkeypatch):
    """The Ctrl+C (open-ended) path gets no upfront duration-based disk
    check at all - the writer's own periodic check is what has to catch a
    filling disk there. record_session must notice writer.disk_low and stop
    cleanly, with a properly finalised (status: complete) sidecar, rather
    than running the source dry or crashing."""
    source, _ = _source(tmp_path, 1000, frames_per_packet=10)
    raw, meta = tmp_path / "out.raw", tmp_path / "out_meta.json"

    class FakeUsage:
        free = 10  # far below any threshold

    monkeypatch.setattr("biocam.data.recording.shutil.disk_usage",
                        lambda path: FakeUsage())

    with RecordingWriter(raw, meta, PARAMS, min_free_bytes=1_000_000,
                         upkeep_interval_frames=1) as writer:
        result = record_session(source, writer)

    assert result.stop_reason == "disk_low"
    # Stopped almost immediately (first packet trips the check at interval=1)
    # rather than consuming the full 1000-frame source.
    assert result.n_frames < 1000
    record = read_sidecar(meta)
    assert record["status"] == "complete"
    assert record["stop_reason"] == "disk_low"
