"""Driving a recording session.

The session consumes an iterable of packets and knows nothing about where they
came from. The driver supplies one source, a replayed file supplies another, so
this whole module - start, gap handling, stop conditions, finalisation - is
testable without an instrument.

This module is top-level and therefore covered by the no-hardware guard. It
must never import biocam.interop.
"""

import time
from dataclasses import dataclass
from typing import Optional

from biocam.data.events import DriverDataLoss, QueueOverflow

# Wall-clock ceiling for a "drain to exhaustion" pass (record_session's
# drain=True mode - see cli.py FIX 1). After Ctrl+C, the normal
# stop-as-soon-as-the-flag-is-seen behaviour is deliberately bypassed so that
# whatever is still buffered gets written to the recording instead of
# discarded uncounted. But the source is still live at that point (streaming
# has not necessarily been told to stop yet), so draining cannot simply wait
# for it to run dry - a source that keeps yielding indefinitely would hang
# the process forever. A few seconds comfortably covers the queue's own
# design budget (~2 s of buffering at the CLI's default packet rate - see
# QUEUE_BUFFER_SECONDS in cli.py) plus margin for the time it actually takes
# to pop and write that many packets. Anything still buffered when it elapses
# is real, acquired data that is being given up on - it must be counted into
# discarded_at_stop, never just dropped.
DRAIN_DEADLINE_SEC = 5.0

# Gate 1, item F: how often (in packets consumed, not time) record_session
# checks the source's queue_overflows / driver_loss_events counters for a
# change and emits QueueOverflow / DriverDataLoss if so. Both counters only
# ever move on the driver's own callback thread(s) - never here - so this
# loop only reads them; checking (and potentially emitting to a listener
# that prints) on every single packet would put per-packet listener work on
# the consumer thread under sustained loss, which is exactly the drain that
# must not stall. Checking every N packets instead keeps that off the hot
# path while still surfacing loss well within a session's lifetime; a
# trailing check in the `finally` below catches whatever moved since the
# last check, however many packets short of N that was.
COUNTER_CHECK_INTERVAL_PACKETS = 200


@dataclass(frozen=True)
class SessionResult:
    raw_path: str
    meta_path: str
    n_frames: int
    verdict: str
    stop_reason: str


def record_session(source, writer, duration_sec: Optional[float] = None,
                   stop_event=None, counters=None, drain: bool = False,
                   stop_source=None) -> SessionResult:
    """Consume packets into the writer until a stop condition is met.

    Stops when the source runs out, when duration_sec of recorded signal has
    been written, when stop_event is set, or when the writer reports
    disk_low (FIX 3 - checked ahead of every other stop condition, including
    while draining, since a full disk turns "finish the recording" into
    "corrupt the recording"). Duration is measured in recorded frames rather
    than wall-clock, so it means the same thing for a live instrument and for
    a replay.

    `drain`, if True, switches the stop condition from "break as soon as
    stop_event is seen" to "consume the source to exhaustion". This exists
    for the retry call cli.py makes after a KeyboardInterrupt: at that point
    stop_event is already set (that is *why* draining was requested), so the
    normal check would just break again on the very first packet and discard
    everything still buffered - the bug this mode closes. duration_sec and
    stop_event are not consulted while draining; the only stop conditions are
    the source running out, or DRAIN_DEADLINE_SEC elapsing (stop_reason
    becomes "drain_deadline_exceeded" in that case).

    `counters`, if given, is the packet source itself (or anything exposing
    the same attributes). Its loss counters are transferred onto the writer
    unconditionally on the way out - normal completion, a stop condition, or
    an exception raised mid-loop - so they reach the sidecar however this
    function exits. The transfer lives in a `finally` for exactly that last
    case: an exception skips straight past a transfer that sat after the
    loop, and RecordingWriter.__exit__ then writes the failed-run sidecar
    itself with the counters still at zero - the same "counters never reach
    disk" defect this parameter was added to close, reached by a different
    route. The exception still propagates; only the transfer happens before
    it does. getattr with a default lets a source without these attributes
    (the replay source) pass through untouched.

    `stop_source`, if given, is called with no arguments in that same
    `finally`, after the counter transfer - stopping the driver's stream as
    early as this function can manage, rather than leaving it to a caller
    that only regains control once this function (and, in the CLI, the
    writer's `with` block) has already finished. This is what closes the
    window described as FIX 2 in cli.py's module docstring: packets that
    arrive between "we decided to stop" and "the driver was actually told to
    stop" used to be buffered behind an already-finalised sidecar and never
    drained. A failing stop_source() is swallowed here rather than left to
    mask whatever exception (if any) is already propagating through this
    `finally`, or to skip the counter/finalise steps still to come; cli.py's
    own, later call to source.stop() surfaces a persistent failure instead.

    Once stop_source has run, whatever is still sitting in `counters`'
    queue cannot arrive by any other route, so it is counted into
    discarded_at_stop via `counters.pending_count()` (0 if the method is
    absent) - except when this call is itself aborting via an exception
    (including KeyboardInterrupt): in that case a caller may be about to
    retry with drain=True to recover exactly that buffered data, and
    counting it here as discarded would double-count it once the drain call
    also writes it.

    While not draining, the loop also breaks - stop_reason
    "source_stopped" - as soon as `counters.stopped` reads True (False if
    the attribute is absent), not only when `stop_event` does. This is what
    keeps a driver-reported streaming error ending the session promptly:
    DriverPacketSource.__iter__ deliberately drains past its STOP sentinel
    now, rather than returning the instant it is popped (see FIX 3 in
    biocam/interop/source.py), so a source that keeps yielding packets
    after on_error sets its own stop flag would otherwise never reach
    StopIteration on its own. Checking `counters.stopped` restores the
    prompt-termination guarantee that used to be a side effect of the old
    return-on-STOP behaviour, without reintroducing the defect FIX 3 closed
    (packets already queued behind the sentinel are still written first,
    since this check runs after each packet is already handed to the
    writer, not before).
    """
    frame_limit = None
    if duration_sec is not None:
        frame_limit = int(duration_sec * writer.params.frame_rate_hz)

    stop_reason = "source_exhausted"
    deadline = time.monotonic() + DRAIN_DEADLINE_SEC if drain else None
    interrupted = False

    # Gate 1, item F: cached copies of the two counters, compared against
    # their live values every COUNTER_CHECK_INTERVAL_PACKETS packets (and
    # once more in the `finally` below) so a move gets a QueueOverflow /
    # DriverDataLoss emitted through the writer's listener - the only place
    # this was visible before was the sidecar, read only after the run ends.
    last_queue_overflows = getattr(counters, "queue_overflows", 0) if counters is not None else 0
    last_driver_loss = getattr(counters, "driver_loss_events", 0) if counters is not None else 0
    packets_since_counter_check = 0

    def _check_counters() -> None:
        nonlocal last_queue_overflows, last_driver_loss
        if counters is None:
            return
        current_overflows = getattr(counters, "queue_overflows", last_queue_overflows)
        if current_overflows != last_queue_overflows:
            writer.emit(QueueOverflow(total=current_overflows))
            last_queue_overflows = current_overflows
        current_loss = getattr(counters, "driver_loss_events", last_driver_loss)
        if current_loss != last_driver_loss:
            writer.emit(DriverDataLoss(total=current_loss))
            last_driver_loss = current_loss

    try:
        for packet in source:
            writer.write_packet(
                timestamp=packet.timestamp,
                counter=packet.counter,
                payload=packet.payload,
            )
            packets_since_counter_check += 1
            if packets_since_counter_check >= COUNTER_CHECK_INTERVAL_PACKETS:
                packets_since_counter_check = 0
                _check_counters()
            if writer.disk_low:
                # FIX 3: checked first, ahead of drain/stop_event/duration -
                # stopping cleanly with a complete record beats crashing with
                # a corrupt one, whether this loop is running normally or
                # draining after Ctrl+C. The writer already emitted DiskLow
                # when it noticed (see RecordingWriter._check_disk_space);
                # this is just record_session acting on it. finalise() below
                # still runs while free space remains, by design of the
                # threshold in recording.py.
                stop_reason = "disk_low"
                break
            if drain:
                if time.monotonic() >= deadline:
                    stop_reason = "drain_deadline_exceeded"
                    break
                continue
            if stop_event is not None and stop_event.is_set():
                stop_reason = "user_stopped"
                break
            if counters is not None and getattr(counters, "stopped", False):
                stop_reason = "source_stopped"
                break
            if frame_limit is not None and writer.n_frames_written >= frame_limit:
                stop_reason = "duration_reached"
                break
    except BaseException:
        # Any exception aborting the loop early, KeyboardInterrupt included -
        # see the stop_source/discarded_at_stop reasoning in the docstring
        # above for why this must be tracked rather than just left to
        # propagate silently past the counting logic below.
        interrupted = True
        raise
    finally:
        # Trailing check: whatever moved since the last periodic check
        # (however many packets short of COUNTER_CHECK_INTERVAL_PACKETS that
        # was) still gets reported before the session ends.
        _check_counters()
        if counters is not None:
            writer.note_driver_loss(getattr(counters, "driver_loss_events", 0))
            writer.note_queue_overflow(getattr(counters, "queue_overflows", 0))
            writer.note_callback_errors(getattr(counters, "callback_errors", 0))
        if stop_source is not None:
            try:
                stop_source()
            except Exception:
                pass
        if not interrupted and counters is not None:
            get_pending = getattr(counters, "pending_count", None)
            pending = get_pending() if callable(get_pending) else 0
            if pending:
                writer.note_discarded(pending)

    writer.finalise(stop_reason)
    return SessionResult(
        raw_path=str(writer.raw_path),
        meta_path=str(writer.meta_path),
        n_frames=writer.n_frames_written,
        verdict=writer.verdict,
        stop_reason=stop_reason,
    )
