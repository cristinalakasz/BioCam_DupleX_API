"""Events emitted while a recording session runs.

The recorder never prints. It emits these to an optional listener, so the CLI
can render them today and the Phase 4 UI can subscribe to the same stream
without any re-plumbing. Printing from inside the recorder would also violate
the callback rule in CLAUDE.md.
"""

from dataclasses import dataclass
from typing import Callable, Union


@dataclass(frozen=True)
class RecordingStarted:
    path: str
    total_channels: int
    frame_rate_hz: float


@dataclass(frozen=True)
class GapDetected:
    after_frame: int
    missing_frames: int
    duration_ms: float


@dataclass(frozen=True)
class GapSummary:
    """A throttled stand-in for a run of individual GapDetected events.

    RecordingWriter emits the first N gaps of a run in full (as GapDetected)
    and then, under sustained loss, batches the rest: one of these per
    interval instead of one GapDetected per gap (Gate 1, item G) - printing
    one line per lost packet on the consumer thread would itself risk
    stalling the drain that is the only thing keeping the queue from
    overflowing further. The sidecar's own gap list is unaffected by this -
    it is a listener-side throttle only.
    """
    n_gaps: int
    missing_frames: int


@dataclass(frozen=True)
class QueuePressure:
    depth: int
    capacity: int


@dataclass(frozen=True)
class QueueOverflow:
    total: int


@dataclass(frozen=True)
class DriverDataLoss:
    total: int


@dataclass(frozen=True)
class DiskLow:
    free_bytes: int
    required_bytes: int


@dataclass(frozen=True)
class RecordingStopped:
    reason: str
    n_frames: int
    verdict: str


RecordingEvent = Union[
    RecordingStarted, GapDetected, GapSummary, QueuePressure, QueueOverflow,
    DriverDataLoss, DiskLow, RecordingStopped,
]

Listener = Callable[[RecordingEvent], None]


def describe(event) -> str:
    """Render an event as one line of human-readable text."""
    if isinstance(event, RecordingStarted):
        return (f"Recording to {event.path} "
                f"({event.total_channels} channels at {event.frame_rate_hz:.2f} Hz)")
    if isinstance(event, GapDetected):
        return (f"GAP after frame {event.after_frame}: "
                f"{event.missing_frames} frames missing ({event.duration_ms:.2f} ms)")
    if isinstance(event, GapSummary):
        return (f"{event.n_gaps} more gaps since the last summary "
                f"({event.missing_frames} frames missing)")
    if isinstance(event, QueuePressure):
        return f"Queue {event.depth}/{event.capacity} - the writer is falling behind"
    if isinstance(event, QueueOverflow):
        return f"QUEUE OVERFLOW: {event.total} packets dropped by our software"
    if isinstance(event, DriverDataLoss):
        return f"DRIVER DATA LOSS: {event.total} events reported by the driver"
    if isinstance(event, DiskLow):
        return (f"DISK LOW: {event.free_bytes:,} bytes free, "
                f"{event.required_bytes:,} required")
    if isinstance(event, RecordingStopped):
        return (f"Stopped ({event.reason}): {event.n_frames} frames, "
                f"integrity verdict '{event.verdict}'")
    raise TypeError(f"not a recording event: {type(event).__name__}")
