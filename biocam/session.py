"""Driving a recording session.

The session consumes an iterable of packets and knows nothing about where they
came from. The driver supplies one source, a replayed file supplies another, so
this whole module - start, gap handling, stop conditions, finalisation - is
testable without an instrument.

This module is top-level and therefore covered by the no-hardware guard. It
must never import biocam.interop.
"""

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class SessionResult:
    raw_path: str
    meta_path: str
    n_frames: int
    verdict: str
    stop_reason: str


def record_session(source, writer, duration_sec: Optional[float] = None,
                   stop_event=None, counters=None) -> SessionResult:
    """Consume packets into the writer until a stop condition is met.

    Stops when the source runs out, when duration_sec of recorded signal has
    been written, or when stop_event is set. Duration is measured in recorded
    frames rather than wall-clock, so it means the same thing for a live
    instrument and for a replay.

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
    """
    frame_limit = None
    if duration_sec is not None:
        frame_limit = int(duration_sec * writer.params.frame_rate_hz)

    stop_reason = "source_exhausted"

    try:
        for packet in source:
            writer.write_packet(
                timestamp=packet.timestamp,
                counter=packet.counter,
                payload=packet.payload,
            )
            if stop_event is not None and stop_event.is_set():
                stop_reason = "user_stopped"
                break
            if frame_limit is not None and writer.n_frames_written >= frame_limit:
                stop_reason = "duration_reached"
                break
    finally:
        if counters is not None:
            writer.note_driver_loss(getattr(counters, "driver_loss_events", 0))
            writer.note_queue_overflow(getattr(counters, "queue_overflows", 0))
            writer.note_callback_errors(getattr(counters, "callback_errors", 0))

    writer.finalise(stop_reason)
    return SessionResult(
        raw_path=str(writer.raw_path),
        meta_path=str(writer.meta_path),
        n_frames=writer.n_frames_written,
        verdict=writer.verdict,
        stop_reason=stop_reason,
    )
