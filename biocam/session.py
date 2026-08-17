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

from biocam.data.events import (
    DriverDataLoss, QueueOverflow, StimulationSuspended,
)

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


def _feed_clock(clock, packet, writer):
    """Feed the acquisition clock one packet. Returns the clock, or None.

    Fed from the writer's own running totals rather than counted a second
    time here, so the clock and the sidecar can never disagree about how many
    frames a recording holds. Pure integer arithmetic on the consumer thread -
    nothing on the callback path, nothing that allocates, nothing that grows.

    The guard is the point. This is the only call on the packet loop that can
    raise something other than an OSError, and an escaping exception here is
    catastrophically out of proportion to its cause: `interrupted` would be
    set, which skips the backlog drain, `pending_count()` and
    `note_discarded()`; `finalise()` would never run; and
    `RecordingWriter.__exit__` would stamp an otherwise intact raw file
    `status="failed"`. Up to a full queue of acquired data - hundreds of
    megabytes - would be written nowhere and counted nowhere, because a clock
    used for scheduling stimuli disagreed with itself.

    Losing the clock is not a reason to lose the recording. So a failure drops
    the clock and lets the recording continue, exactly as
    `RecordingWriter._emit` already does for a listener that raises.
    """
    if clock is None:
        return None
    try:
        clock.observe_totals(
            packet, writer.n_frames_written, writer.n_frames_missing
        )
    except Exception as exc:  # noqa: BLE001 - the recording outranks the clock
        # Emitted, not warned. warnings.warn writes to stderr from the
        # consumer thread, bypassing the CLI's bounded printer ring - the ring
        # that exists because print() on this thread stalls under Windows
        # QuickEdit, a full pipe or a slow log collector. One blocked write on
        # the only thread draining the packet queue is a stall, one-shot or
        # not. writer.emit is already the route everything else on this path
        # takes, and it is guarded against a listener that raises.
        writer.emit(StimulationSuspended(
            reason=f"the acquisition clock stopped being fed ({exc}); "
                   "scheduled stimulation must not be timed against this "
                   "session",
            after_frame=writer.n_frames_written,
        ))
        return None
    return clock


def _run_monitor(monitor, packet, writer):
    """Sample one packet for the activity display. Returns it, or None.

    `LiveMonitor.observe` already refuses to raise, so this is belt and
    braces - but the belt is the one that matters: an exception escaping the
    packet loop sets `interrupted`, which skips the backlog drain and
    `finalise()`, and stamps an intact raw file `failed`. Nobody should lose
    a recording because a picture could not be drawn.
    """
    try:
        monitor.observe(packet)
    except Exception as exc:  # noqa: BLE001 - the recording outranks the picture
        writer.emit(StimulationSuspended(
            reason=f"the activity display failed ({exc}) and has been "
                   "disconnected for the rest of this recording; the "
                   "recording itself is unaffected",
            after_frame=writer.n_frames_written,
        ))
        return None
    return monitor


def _run_service(service, writer):
    """Run the between-packets hook once. Returns it, or None if it failed.

    Guarded for the reason `_feed_clock` is: this runs inside the packet loop,
    and an escaping exception there sets `interrupted`, which skips the
    backlog drain, `pending_count()` and `finalise()`, leaving an intact raw
    file stamped `failed` and a queue's worth of acquired data written
    nowhere. A stimulus that could not be dispatched must not cost the
    recording.

    A hook that raises is dropped rather than retried every packet: if it is
    broken it will stay broken, and re-entering it 9000 times a second would
    turn one fault into a stall.
    """
    try:
        service()
    except Exception as exc:  # noqa: BLE001 - the recording outranks the stimulus
        # Emitted rather than warned, for the reason given in _feed_clock.
        writer.emit(StimulationSuspended(
            reason=f"the stimulation service raised ({exc}) and has been "
                   "disconnected for the rest of this recording",
            after_frame=writer.n_frames_written,
        ))
        return None
    return service


@dataclass(frozen=True)
class SessionResult:
    raw_path: str
    meta_path: str
    n_frames: int
    verdict: str
    stop_reason: str


def record_session(source, writer, duration_sec: Optional[float] = None,
                   stop_event=None, counters=None, drain: bool = False,
                   stop_source=None, clock=None, service=None,
                   monitor=None) -> SessionResult:
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
    queue cannot arrive by any other route. CRITICAL 1: `pending_count()`
    does not peek - it pops and discards - so it must never be called
    before that backlog has had a chance to be written. On a normal
    (non-drain) exit this used to call it directly, throwing away real,
    already-acquired, immediately-drainable data every time the frame limit
    landed mid-burst (the consumer sleeps when the queue is empty, so it
    runs in bursts, and the limit essentially never lands exactly on the
    last packet of one) - forcing a false gaps_detected verdict on
    essentially every successful timed run. Instead, while not already
    draining, whatever `counters` still holds is drained into the writer
    first, against the same DRAIN_DEADLINE_SEC bound the Ctrl+C drain path
    uses (stop_source has already run by this point, so the backlog is
    finite and no longer growing); only what is still unwritten when that
    deadline elapses - or, while already draining, whatever the drain loop
    above gave up on at its own deadline - is counted into
    discarded_at_stop via `counters.pending_count()` (0 if the method is
    absent). This is skipped entirely when this call is itself aborting via
    an exception (including KeyboardInterrupt): in that case a caller may be
    about to retry with drain=True to recover exactly that buffered data,
    and counting or writing it here would double-count it once the drain
    call also writes it.

    MEDIUM 1/2/3 (Gate 1 final pass) apply to this same finally-block drain:

    - MEDIUM 1: it now checks `writer.disk_low` on every packet written,
      exactly like the main loop above - a full disk turns "finish the
      recording" into "corrupt the recording" here just as much as it does
      anywhere else on this path, and this drain used to keep writing for
      up to the full DRAIN_DEADLINE_SEC regardless.
    - MEDIUM 2: each `writer.write_packet()` call in this drain is now
      guarded, because it is the one remaining unguarded write on this
      path - most plausibly to raise is an OSError from the same full disk
      that just set `writer.disk_low`. Left unguarded, that exception would
      propagate straight out of this `finally`, skipping `pending_count()`,
      `note_discarded()`, and `finalise()` below: `RecordingWriter.__exit__`
      would then write status="failed", stop_reason="error", erasing the
      accurate "disk_low" reason and leaving the rest of the backlog
      uncounted. Swallowing the exception here means whatever remains
      unwritten simply falls through to `pending_count()` below and is
      counted as abandoned, the same as any other backlog this drain does
      not finish.
    - MEDIUM 3: the loop is only entered once `counters.stopped` reads True
      (False, and the loop is skipped, if the attribute is absent or still
      False). `for packet in counters` re-enters the source's own
      `__iter__`, which sleeps and re-checks its stop flag on an empty
      queue rather than returning - so the DRAIN_DEADLINE_SEC deadline
      below, checked only inside the loop body between yields, is never
      reached if the source never yields at all. `stop_source` is optional
      and the CLI always supplies it (so `counters.stopped` is normally
      already True by the time this code runs - stop_source() ran just
      above), making this latent rather than live today; refusing to enter
      an unconfirmed-stopped source's drain is what keeps it that way if a
      future caller ever omits stop_source.

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
            clock = _feed_clock(clock, packet, writer)
            if monitor is not None:
                # The activity display. Decimated internally to a few times a
                # second and guarded the same way the clock is: a picture is
                # never worth a recording, so a failure drops the monitor and
                # the packets keep being written.
                monitor = _run_monitor(monitor, packet, writer)
            if service is not None:
                # Stimulation runs here, on the consumer thread, between
                # packets - the arrangement 3Brain's own sample uses
                # (MainForm.cs:383, inside the loop its data callback drives),
                # which avoids depending on whether Send is safe from another
                # thread. Nothing in the XML says whether it is.
                #
                # The cost is that this time comes out of the drain's budget,
                # so `service` must dispatch at most one stimulus and must not
                # raise. StimulationQueue.service does both, and times itself
                # so the cost is measured rather than assumed.
                service = _run_service(service, writer)
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
                    # MEDIUM 6: previously the only two ways out of a drain
                    # were exhaustion and this deadline, so a source whose
                    # own stop was never confirmed (`stopped` still False -
                    # e.g. stop() failing while the driver keeps producing)
                    # looked identical in the sidecar to the routine case of
                    # simply draining a large, genuinely bounded backlog
                    # past DRAIN_DEADLINE_SEC: both just read
                    # "drain_deadline_exceeded". `stopped` is normally
                    # already True by the time a drain call starts (the
                    # preceding non-drain call's own stop_source() sets it
                    # before returning/raising - see cli.py's
                    # KeyboardInterrupt retry), so this only fires the
                    # distinct reason when that confirmation never
                    # happened - it cannot, by itself, prove the driver
                    # actually stopped producing (Layer 1, unverifiable
                    # here), only that this drain never observed the signal
                    # that it had.
                    stopped = (getattr(counters, "stopped", False)
                              if counters is not None else False)
                    stop_reason = ("drain_deadline_exceeded" if stopped
                                  else "drain_deadline_exceeded_unconfirmed_stop")
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
            # MEDIUM 3: only enter the drain loop once the source confirms
            # it has actually stopped - see the docstring above. A source
            # that never yields would otherwise let this loop wait forever,
            # since the deadline below is only checked between yields.
            if (not drain and callable(get_pending)
                    and getattr(counters, "stopped", False)):
                # CRITICAL 1: pending_count() pops and discards, so it must
                # never be the first thing that touches this backlog. By
                # this point stop_source() has already run (above), so the
                # queue is finite and no longer growing - drain it into the
                # writer, exactly as the drain=True loop above does, before
                # deciding what (if anything) is genuinely abandoned.
                drain_deadline = time.monotonic() + DRAIN_DEADLINE_SEC
                try:
                    for packet in counters:
                        writer.write_packet(
                            timestamp=packet.timestamp,
                            counter=packet.counter,
                            payload=packet.payload,
                        )
                        # This drain writes real frames, so the clock has to
                        # see them. Omitting it left the clock short by the
                        # whole backlog - and on an ordinary --duration run
                        # this path fires almost every time, because the
                        # frame limit lands mid-burst. Two seconds of
                        # unobserved frames is 40x the clock's disagreement
                        # tolerance, so the omission printed a confident
                        # "do not schedule stimulation against this" at the
                        # end of a perfectly healthy recording.
                        clock = _feed_clock(clock, packet, writer)
                        if writer.disk_low:
                            # MEDIUM 1: same reasoning as the main loop -
                            # a full disk must stop this drain too, not
                            # just the primary one.
                            break
                        if time.monotonic() >= drain_deadline:
                            break
                except Exception:
                    # MEDIUM 2: most plausibly an OSError from the same
                    # full disk that just tripped writer.disk_low above.
                    # Left unguarded, this would propagate past
                    # pending_count()/note_discarded()/finalise() below,
                    # so RecordingWriter.__exit__ would write
                    # status="failed", stop_reason="error" - erasing the
                    # accurate reason this run actually stopped for.
                    # Swallowing it here lets whatever remains unwritten
                    # fall through to pending_count() below, counted as
                    # abandoned like any other undrained backlog.
                    pass
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
