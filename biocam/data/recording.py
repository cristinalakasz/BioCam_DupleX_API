"""Writing and reading recordings, with an integrity record.

The writer appends payload bytes exactly as received - it never decodes. The
bytes written are the bytes that arrived, so the concatenation is a valid
frame-major stream and the partial-frame defect cannot occur.

The sidecar is written twice: once at the start marked in_progress, and again
on finalise. A killed process therefore leaves a raw file with its acquisition
parameters and an honest marker that it was never finished.

Every sidecar write is atomic (temp file + os.replace - see _write_sidecar)
and a failure writing the __exit__ failure sidecar can never mask whatever
exception is already propagating (see __exit__). The writer also watches free
disk space - see DEFAULT_MIN_FREE_BYTES and DEFAULT_DISK_POLL_INTERVAL_SEC
below. HIGH: shutil.disk_usage() is GetDiskFreeSpaceEx on Windows, which can
block for seconds on a network share, a volume under antivirus scan, or a
OneDrive-synced tree - and the packet queue upstream of this writer covers
only a couple of seconds (see QUEUE_BUFFER_SECONDS in cli.py). The check used
to run from write_packet() on the consumer thread - the same thread that
drains that queue - so a slow disk_usage() call could itself cause the drop
it exists to warn about. It now runs on its own daemon thread
(_disk_poll_loop/_poll_disk_once), started in __enter__ and stopped in
__exit__; that thread only ever sets `_disk_low` and stashes the free-byte
count it saw. write_packet() still runs on the consumer thread and still
decides when to emit the DiskLow event, but it only ever reads that flag - a
plain attribute read, not a syscall - so the listener remains consumer-thread-
only (see _emit()) without the poll itself being able to stall the drain.
_periodic_upkeep(), gated by DEFAULT_UPKEEP_INTERVAL_FRAMES below, now only
flushes; the disk check is no longer part of it. It fsyncs the raw file
exactly once, in finalise() - MEDIUM 5: earlier drafts of this docstring (and
of _periodic_upkeep's) claimed the periodic upkeep pass also fsyncs and so
bounds the power-loss exposure window to one interval. It does not:
_periodic_upkeep only flushes, which is a much weaker guarantee (see
_periodic_upkeep below), so that window is "the entire run" until finalise()
runs, not "at most one interval".
"""

import json
import os
import shutil
import tempfile
import threading
import warnings
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import numpy as np

from biocam.data.events import (
    DiskLow, GapDetected, GapSummary, RecordingStarted, RecordingStopped,
)
from biocam.data.clock import TIMESTAMP_UNAVAILABLE
from biocam.data.frames import DTYPE_BY_BYTE_SIZE, to_microvolts
from biocam.data.integrity import MAX_RETAINED_GAPS, GapTracker

SCHEMA_VERSION = 2

VERDICT_CLEAN = "clean"
VERDICT_GAPS = "gaps_detected"
VERDICT_UNKNOWN = "unknown"

# Free-space floor at which the writer reports itself disk_low (FIX 3). At up
# to ~152 MB/s (CLAUDE.md), a few hundred MB can still be written between one
# periodic check and the next (see DEFAULT_UPKEEP_INTERVAL_FRAMES below), and
# the sidecar itself needs room too (a few KB - negligible next to this, but
# the threshold must still clear it). 2 GiB gives well over ten seconds of
# headroom at the full data rate even in the worst case - a check that lands
# just after crossing the floor - which is far more than record_session needs
# to notice disk_low and stop cleanly.
DEFAULT_MIN_FREE_BYTES = 2 * 1024 ** 3  # 2 GiB

# How often (in frames written, not packets) the writer flushes the raw file
# (FIX 4). MEDIUM 5: this is a flush, not an fsync - it pushes bytes to the
# OS cache, not to disk; only finalise() fsyncs (once, at the end of the
# run). Frames arrive at up to ~18.5 kHz; flushing on every packet would mean
# tens of thousands of syscalls per second on a path that sits downstream of
# the time-critical callback. Every 50,000 frames is a couple of seconds at
# typical frame rates - rare enough that the syscall overhead is immaterial.
# The free-space check used to share this interval (FIX 3); it no longer
# does - see DEFAULT_DISK_POLL_INTERVAL_SEC below and the HIGH fix in the
# module docstring.
DEFAULT_UPKEEP_INTERVAL_FRAMES = 50_000

# How often (in wall-clock seconds) the disk-poll thread calls
# shutil.disk_usage(). Independent of DEFAULT_UPKEEP_INTERVAL_FRAMES on
# purpose: that constant is a frame count meaningful only on the consumer
# thread; this one is a real-time interval for a thread that runs
# independently of how fast (or slowly) packets are arriving. 2 seconds
# matches the packet queue's own buffering budget (QUEUE_BUFFER_SECONDS in
# cli.py) - a filling disk is caught within about one buffer's worth of time
# either way.
DEFAULT_DISK_POLL_INTERVAL_SEC = 2.0

# Gate 1, item G: how many GapDetected events RecordingWriter emits to its
# listener in full before switching to throttled GapSummary events. cli.py's
# listener prints on the consumer thread - the same thread that is the only
# thing draining the queue - so one print per lost packet under sustained
# loss risks stalling the drain that is the only thing keeping the queue
# from overflowing further, which causes more loss, which causes more
# printing. The first GAP_EMIT_FULL_COUNT are still emitted individually
# (an operator watching the console sees exactly what happened as it
# starts), after which GAP_SUMMARY_INTERVAL more gaps accumulate silently
# before one GapSummary reports the count and missing-frame total since the
# last summary. None of this touches what is *recorded*: GapTracker keeps
# (up to its own cap - see MAX_RETAINED_GAPS in integrity.py) every gap
# regardless of what reaches a listener.
GAP_EMIT_FULL_COUNT = 20
GAP_SUMMARY_INTERVAL = 50

# LOW: how many retained gaps __exit__'s failure-sidecar write will include,
# distinct from (and much smaller than) MAX_RETAINED_GAPS in integrity.py.
# MAX_RETAINED_GAPS already bounds the normal finalise() write to a sane
# size, but the failure write happens at exactly the moment memory pressure
# is most likely already the problem - an exception mid-run, possibly the
# proximate cause of the crash itself - so building a list of up to 100,000
# Gap dicts and json.dumps-ing it is exactly the wrong thing to ask for right
# then. This caps the failure write specifically, harder than the normal
# one; any gaps beyond the cap are folded into gaps_truncated (already an
# integer count, not a boolean - see GapTracker.gaps_truncated), not lost
# from what the sidecar reports, only from the retained list it writes out.
FAILURE_SIDECAR_MAX_GAPS = 1_000


@dataclass(frozen=True)
class AcquisitionParameters:
    frame_rate_hz: float
    total_channels: int
    ch_sample_byte_size: int
    bit_depth: int
    adc_counts_to_value: float
    offset: float
    min_digital_value: int
    max_digital_value: int

    @property
    def bytes_per_frame(self) -> int:
        return self.total_channels * self.ch_sample_byte_size


class RecordingWriter:
    """Appends packets to a raw file and maintains the integrity record.

    MEDIUM 6: a RecordingWriter is not safely reusable across more than one
    recording. `__exit__` joins the disk-poll thread (`_disk_poll_thread`)
    with a `timeout=2.0` and proceeds regardless of whether the join
    actually succeeded - so a thread that is genuinely stuck (e.g. blocked
    inside `shutil.disk_usage()` on an unresponsive network share) outlives
    the writer instance instead of being guaranteed to stop. That is a
    current constraint, not a bug fixed here: the CLI creates exactly one
    RecordingWriter per process and exits shortly after `__exit__` runs, so
    a leaked, already-harmless thread has nowhere to accumulate. A future
    caller that constructs many RecordingWriters in one long-lived process -
    the planned Phase 4 UI is exactly this shape - would need to revisit
    this before doing so, since repeated stuck joins there could accumulate
    daemon threads for the life of the process. Left undesigned for this
    Gate 1 pass rather than guessed at without a concrete caller to design
    against.
    """

    def __init__(self, raw_path, meta_path, params: AcquisitionParameters,
                 listener=None, min_free_bytes: int = DEFAULT_MIN_FREE_BYTES,
                 upkeep_interval_frames: int = DEFAULT_UPKEEP_INTERVAL_FRAMES,
                 max_retained_gaps: int = MAX_RETAINED_GAPS,
                 gap_emit_full_count: int = GAP_EMIT_FULL_COUNT,
                 gap_summary_interval: int = GAP_SUMMARY_INTERVAL,
                 disk_poll_interval_sec: float = DEFAULT_DISK_POLL_INTERVAL_SEC,
                 failure_sidecar_max_gaps: int = FAILURE_SIDECAR_MAX_GAPS):
        self._raw_path = Path(raw_path)
        self._meta_path = Path(meta_path)
        self._params = params
        self._listener = listener
        self._min_free_bytes = min_free_bytes
        self._upkeep_interval_frames = upkeep_interval_frames
        self._gap_emit_full_count = gap_emit_full_count
        self._gap_summary_interval = gap_summary_interval
        self._failure_sidecar_max_gaps = failure_sidecar_max_gaps
        self._disk_poll_interval_sec = disk_poll_interval_sec

        self._file = None
        self._tracker = GapTracker(frame_rate_hz=params.frame_rate_hz,
                                   max_retained_gaps=max_retained_gaps)
        self._bytes_written = 0
        self._first_timestamp: Optional[int] = None
        # Packets whose header carried the documented 0 sentinel. Counted
        # rather than silently skipped: if every packet has one, the
        # device clock is dead and every acquisition time in the session
        # is a frame-count estimate, which the operator should know.
        self._timestamps_unavailable = 0
        self._last_timestamp: Optional[int] = None
        self._driver_loss = 0
        self._queue_overflows = 0
        self._callback_errors = 0
        self._payload_mismatches = 0
        self._discarded_at_stop = 0
        self._started_utc = None
        self._finalised = False
        # HIGH: _disk_low and _disk_low_free_bytes are written by the poll
        # thread (_disk_poll_loop/_poll_disk_once) and only ever read here on
        # the consumer thread - a plain bool/int attribute read is atomic
        # under the GIL, so no lock is needed for that direction. Only the
        # consumer thread sets _disk_low_reported, gating the one-time
        # DiskLow emit in write_packet() - see the module docstring.
        self._disk_low = False
        self._disk_low_free_bytes = None
        self._disk_low_reported = False
        self._disk_poll_stop = threading.Event()
        self._disk_poll_thread = None
        self._frames_at_last_upkeep = 0
        # Gate 1, item G: how many gaps have been emitted (in full or
        # counted toward a pending summary) so far this run, and the
        # accumulator for the summary not yet flushed.
        self._gaps_emitted = 0
        self._gaps_since_summary = 0
        self._frames_missing_since_summary = 0
        # MEDIUM 4: exceptions raised by the listener itself, e.g. print()
        # on a broken stdout (OSError) or describe() meeting an event type
        # it does not recognise (TypeError). See _emit() below.
        self._listener_errors = 0

    def __enter__(self):
        self._raw_path.parent.mkdir(parents=True, exist_ok=True)
        self._file = open(self._raw_path, "wb")
        self._started_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self._write_sidecar(status="in_progress", stop_reason=None)
        self._emit(RecordingStarted(
            path=str(self._raw_path),
            total_channels=self._params.total_channels,
            frame_rate_hz=self._params.frame_rate_hz,
        ))
        # HIGH: the free-space poll runs on its own daemon thread from here
        # until __exit__ stops it - see the module docstring and
        # _disk_poll_loop below. Started only once the directory exists
        # (mkdir above), since shutil.disk_usage() needs it to.
        self._disk_poll_thread = threading.Thread(
            target=self._disk_poll_loop, name="biocam-disk-poll", daemon=True)
        self._disk_poll_thread.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        self._disk_poll_stop.set()
        if self._disk_poll_thread is not None:
            self._disk_poll_thread.join(timeout=2.0)
        if self._file is not None:
            self._file.close()
            self._file = None
        if not self._finalised:
            error = exc_type.__name__ if exc_type is not None else None
            # FIX 1: this write itself can fail - the disk that just failed
            # the caller's write is the same disk this sidecar is written to.
            # If it does, that failure must never replace whatever exception
            # is already propagating out of the `with` block (or, on a clean
            # exit that simply never reached finalise(), become the only
            # exception raised): the caller needs to see the *original*
            # problem, not a confusing one about the sidecar. Losing the
            # failure sidecar entirely is a real loss - it is the only
            # record of what the writer had observed before things went
            # wrong - so it is not swallowed silently either, just kept from
            # masking anything.
            #
            # LOW: max_gaps=self._failure_sidecar_max_gaps caps the retained
            # gap list harder for this write specifically than the normal
            # finalise() one - see FAILURE_SIDECAR_MAX_GAPS above. This is
            # exactly the moment memory pressure is most likely already the
            # problem; building and serialising a much larger list here
            # would risk turning an already-failing run into a MemoryError
            # that erases the sidecar entirely, right when it is needed most.
            try:
                self._write_sidecar(status="failed", stop_reason="error", error=error,
                                    max_gaps=self._failure_sidecar_max_gaps)
            except OSError as sidecar_exc:
                warnings.warn(
                    f"could not write failure sidecar to {self._meta_path}: "
                    f"{sidecar_exc}",
                    RuntimeWarning,
                )
        return False

    def write_packet(self, timestamp: int, counter: int, payload: bytes) -> None:
        """Append one packet. Bytes are written exactly as received.

        The frame count is derived from total bytes written, not accumulated
        packet-by-packet: a payload that is not a whole number of frames must
        never let the sidecar's frame count drift from what the raw file
        actually contains. frames_in_packet stays a floor-divided, per-packet
        quantity because that is what the gap tracker needs to size a loss.
        """
        frames_in_packet = len(payload) // self._params.bytes_per_frame
        frames_written_before = self._bytes_written // self._params.bytes_per_frame

        gap = self._tracker.observe(
            counter=counter,
            frames_in_packet=frames_in_packet,
            frames_written=frames_written_before,
        )
        if gap is not None:
            self._emit_gap(gap)

        self._file.write(payload)
        self._bytes_written += len(payload)

        # 0 means "the timestamp is not available" (XML:1923), not "the
        # acquisition just started". Recording it as a first_timestamp puts a
        # sentinel in the sidecar where a later reader takes an origin - the
        # same confusion biocam/data/clock.py exists to avoid. Unavailable
        # timestamps are counted instead, so their absence is visible.
        if timestamp == TIMESTAMP_UNAVAILABLE:
            self._timestamps_unavailable += 1
        else:
            if self._first_timestamp is None:
                self._first_timestamp = timestamp
            self._last_timestamp = timestamp

        # HIGH: this is a plain attribute read, not a syscall, so it runs on
        # every packet with none of the stall risk shutil.disk_usage() carries
        # - see the module docstring. _disk_low is set by the poll thread;
        # this is the only place it is acted on, and only once per run.
        if self._disk_low and not self._disk_low_reported:
            self._disk_low_reported = True
            self._emit(DiskLow(free_bytes=self._disk_low_free_bytes,
                               required_bytes=self._min_free_bytes))

        frames_now = self.n_frames_written
        if frames_now - self._frames_at_last_upkeep >= self._upkeep_interval_frames:
            self._frames_at_last_upkeep = frames_now
            self._periodic_upkeep()

    def note_driver_loss(self, count: int = 1) -> None:
        """Record the driver's cumulative data-loss count.

        CRITICAL 2: this used to be `+=`, but `count` is always the
        source's own *cumulative* total (see session.py:
        `writer.note_driver_loss(getattr(counters, "driver_loss_events",
        0))`), not an increment - so `+=` doubled it every extra time
        record_session's `finally` ran against the same writer. cli.py's
        KeyboardInterrupt path does exactly that: the normal call, then the
        drain=True retry, both against the same RecordingWriter. Assigning
        instead of accumulating makes repeated calls with the same
        cumulative value idempotent, matching what the value actually
        means. The method name and signature are unchanged - other code
        calls this expecting "record what the source reports now".
        """
        self._driver_loss = count

    def note_queue_overflow(self, count: int = 1) -> None:
        """Record the cumulative count of packets dropped by our own queue.

        See note_driver_loss() above - same CRITICAL 2 fix, same reasoning:
        `count` is a cumulative total, so this assigns rather than
        accumulates.
        """
        self._queue_overflows = count

    def note_payload_mismatches(self, count: int = 0) -> None:
        """Packets whose header disagreed with the payload delivered.

        Cumulative, so this assigns rather than accumulates - see
        note_driver_loss(). Non-zero means the frame alignment of this
        recording cannot be trusted: the bytes were still written exactly as
        received, but the assumption that a payload is a whole number of
        frames starting at its first byte did not hold.
        """
        self._payload_mismatches = count

    def note_callback_errors(self, count: int = 1) -> None:
        """Record the cumulative count of exceptions raised in a callback.

        See note_driver_loss() above - same CRITICAL 2 fix, same reasoning:
        `count` is a cumulative total, so this assigns rather than
        accumulates.
        """
        self._callback_errors = count

    def note_discarded(self, count: int = 1) -> None:
        """Record packets that were acquired but never made it into the file.

        Distinct from queue_overflows (a live callback dropping a packet
        because the queue was full) and driver_loss (the driver's own
        cumulative loss counter): this is data that genuinely reached the
        Python side - buffered, not lost in transit - and was then thrown
        away by the stop path instead of being written: a drain that gave up
        at its deadline (session.DRAIN_DEADLINE_SEC), or whatever leaked into
        the queue in the window between deciding to stop and the stream
        actually stopping. See FIX 1 / FIX 2 in cli.py's module docstring.
        """
        self._discarded_at_stop += count

    def finalise(self, stop_reason: str) -> None:
        # Flush any gaps counted toward a pending summary but not yet
        # emitted - otherwise a run that stops partway through an interval
        # would leave that trailing handful unreported to the listener
        # (the sidecar already has them regardless, via the tracker).
        self._flush_pending_gap_summary()
        if self._file is not None:
            # FIX 4: flush() only pushes bytes to the OS cache; fsync forces
            # them to the physical disk. Both run here, on the consumer
            # thread that calls finalise() - never inside DataReceived - so
            # this costs nothing against the acquisition budget. Without it,
            # a power loss or bugcheck right after a multi-hour recording
            # could lose whatever Windows had not yet committed on its own
            # schedule, even though the file was already "closed" from the
            # writer's point of view.
            self._file.flush()
            os.fsync(self._file.fileno())
        self._write_sidecar(status="complete", stop_reason=stop_reason)
        self._finalised = True
        self._emit(RecordingStopped(
            reason=stop_reason,
            n_frames=self.n_frames_written,
            verdict=self.verdict,
        ))

    def _emit_gap(self, gap) -> None:
        """Emit a just-detected gap, throttled (Gate 1, item G).

        The first `_gap_emit_full_count` gaps of a run are emitted in full,
        one GapDetected each. After that, gaps are accumulated silently and
        flushed as one GapSummary per `_gap_summary_interval` gaps (plus a
        final partial flush in finalise() - see _flush_pending_gap_summary).
        This only affects what reaches the listener: GapTracker has already
        recorded the gap (subject to its own retention cap - item H) before
        this method is called.
        """
        self._gaps_emitted += 1
        if self._gaps_emitted <= self._gap_emit_full_count:
            self._emit(GapDetected(
                after_frame=gap.after_frame,
                missing_frames=gap.missing_frames,
                duration_ms=gap.duration_ms,
            ))
            return
        self._gaps_since_summary += 1
        self._frames_missing_since_summary += gap.missing_frames
        if self._gaps_since_summary >= self._gap_summary_interval:
            self._flush_pending_gap_summary()

    def _flush_pending_gap_summary(self) -> None:
        if self._gaps_since_summary:
            self._emit(GapSummary(
                n_gaps=self._gaps_since_summary,
                missing_frames=self._frames_missing_since_summary,
            ))
            self._gaps_since_summary = 0
            self._frames_missing_since_summary = 0

    def _periodic_upkeep(self) -> None:
        """Run every DEFAULT_UPKEEP_INTERVAL_FRAMES frames (FIX 4).

        HIGH: this used to also call the free-space check (FIX 3), sharing
        one frame-count-gated interval with the flush below on the theory
        that both are "check something expensive every so often, on the
        consumer thread, never in the callback". That theory undercounted
        the disk check's cost: shutil.disk_usage() is GetDiskFreeSpaceEx on
        Windows, which can block for seconds on a network share, a volume
        under antivirus scan, or a OneDrive-synced tree - long enough to
        stall the very consumer thread that is the only thing draining the
        packet queue upstream of this writer, on a queue that covers only a
        couple of seconds (QUEUE_BUFFER_SECONDS in cli.py). The check now
        runs on its own daemon thread (_disk_poll_loop/_poll_disk_once,
        started in __enter__); this method only flushes.

        MEDIUM 5: the flush is honest about what it is not. Earlier drafts
        of this docstring claimed it "shrinks the power-loss exposure
        window from the entire run to at most one interval" - that would be
        true of an fsync, but flush() only pushes bytes from Python's
        buffered-writer object to the OS page cache, not to the physical
        disk; only os.fsync() (called once, in finalise()) does that, so
        the real exposure window is the whole run, not one interval. Worse,
        this call is close to a no-op even on its own terms: `open(...,
        "wb")`'s default buffer is 8 KiB, and acquisition payloads
        (multiple channels x samples per packet) routinely exceed that, so
        each write_packet() call already flushes the previous buffered
        remainder to the OS on its own - there is rarely anything left
        buffered for this call to push. It is kept anyway, harmless and
        cheap, in case a run with very small acquisition parameters ever
        does leave something in the buffer between upkeep intervals - but
        it must not be read as a durability guarantee. Fsyncing here
        instead was considered and rejected for this Gate 1 pass: it would
        add a real disk-bound stall on the consumer thread every
        DEFAULT_UPKEEP_INTERVAL_FRAMES frames, and a consumer that stalls
        is exactly what fills the queue and starts dropping packets in the
        callback (see CLAUDE.md's callback rule) - a cost/latency trade
        that has not been measured against real lab hardware and should not
        be made silently as a side effect of fixing a docstring. Runs on
        the consumer thread (write_packet is called from record_session's
        loop), never inside DataReceived.
        """
        if self._file is not None:
            self._file.flush()

    def _disk_poll_loop(self) -> None:
        """Runs on its own daemon thread - never the consumer thread.

        Started by __enter__, stopped by __exit__. Polls at a fixed
        wall-clock interval (DEFAULT_DISK_POLL_INTERVAL_SEC), independent of
        how many frames have been written, so it keeps working even for a
        source that has stalled (the exact condition a filling disk is most
        dangerous during). See the module docstring and the HIGH fix on
        _periodic_upkeep above.

        Waits one interval before the first poll (Event.wait() returning
        False means it timed out rather than being told to stop) rather
        than polling immediately on thread start - a run that finishes
        inside one interval never touches shutil.disk_usage() at all, and a
        long-lived one is checked well within its first buffer's worth of
        recording either way.
        """
        while not self._disk_poll_stop.wait(self._disk_poll_interval_sec):
            self._poll_disk_once()

    def _poll_disk_once(self) -> None:
        """One free-space check. Only ever sets `_disk_low`/stashes the
        free-byte count - never emits. write_packet(), on the consumer
        thread, is the only thing that reads those and emits DiskLow (once).
        Exposed as its own method, separate from the loop above, so a test
        can call it directly and deterministically instead of racing a real
        timer.
        """
        try:
            free = shutil.disk_usage(self._raw_path.parent).free
        except OSError:
            # A transient failure (e.g. a network share blipping) must not
            # kill the poll thread outright - try again next interval.
            return
        if free < self._min_free_bytes:
            self._disk_low = True
            self._disk_low_free_bytes = free

    @property
    def disk_low(self) -> bool:
        return self._disk_low

    @property
    def listener_errors(self) -> int:
        """Count of exceptions the listener raised (MEDIUM 4). Not part of
        the sidecar's integrity block - a console/UI failure is not itself
        evidence about the recording's data - but exposed so a caller can
        still notice and report it."""
        return self._listener_errors

    @property
    def n_frames_written(self) -> int:
        return self._bytes_written // self._params.bytes_per_frame

    @property
    def n_frames_missing(self) -> int:
        """Frames the instrument acquired that never reached this writer.

        Already reported in the sidecar's integrity block; exposed as a
        property so that AcquisitionClock can count them as elapsed time -
        a recording that loses data must not also lose time.
        """
        return self._tracker.n_frames_missing

    @property
    def params(self) -> AcquisitionParameters:
        return self._params

    @property
    def raw_path(self) -> Path:
        return self._raw_path

    @property
    def meta_path(self) -> Path:
        return self._meta_path

    @property
    def verdict(self) -> str:
        # discarded_at_stop and payload_length_mismatches join the same
        # "not clean" bucket as the other counters below (none of which are
        # literal frame-counter gaps either): a recording that discarded
        # acquired data at stop time, or whose payloads disagreed with their
        # own headers, must never report clean - and this codebase has no
        # verdict more specific than gaps_detected for "integrity was
        # compromised, but not by a counter gap". A reader who sees
        # gaps_detected with an empty `gaps` list should look at the other
        # counters in this block; they are all in the sidecar.
        if (self._tracker.has_gaps or self._driver_loss or self._queue_overflows
                or self._callback_errors or self._discarded_at_stop
                or self._payload_mismatches):
            return VERDICT_GAPS
        if self._tracker.counter_anomalies:
            # A counter anomaly is not a gap: GapTracker deliberately declines
            # to turn an anomalous step into a Gap record (see integrity.py),
            # so there are no missing frames on file to point to - reporting
            # gaps_detected here would pair that verdict with an empty gaps
            # list and n_frames_missing == 0, a self-contradictory record. It
            # is also not evidence of a clean run: something moved the
            # counter in a way we cannot interpret. `unknown` is the only
            # verdict that does not claim more than we actually know.
            return VERDICT_UNKNOWN
        return VERDICT_CLEAN

    def _emit(self, event) -> None:
        """Hand an event to the listener, if any, without letting it abort
        the recording (MEDIUM 4).

        The listener runs arbitrary caller code on the consumer thread:
        cli.py's default listener calls print(), which raises OSError on a
        broken stdout, and describe() raises TypeError for an event type it
        does not recognise - a live risk the moment a ninth event type is
        added here without a matching describe() branch. Losing the
        console (or any other listener) is not a reason to lose the
        experiment, so a listener failure is counted and swallowed, not
        allowed to propagate out of write_packet()/finalise() and abort the
        recording.
        """
        if self._listener is not None:
            try:
                self._listener(event)
            except Exception:
                self._listener_errors += 1

    def emit(self, event) -> None:
        """Public counterpart to _emit(), for callers outside this class.

        Gate 1, item F: record_session (biocam/session.py) watches the
        packet source's own counters - queue_overflows, driver_loss_events -
        on the consumer thread and needs to surface QueueOverflow /
        DriverDataLoss through the same listener this writer already emits
        RecordingStarted/GapDetected/DiskLow/RecordingStopped to, rather
        than plumbing a second, separate listener through record_session.
        """
        self._emit(event)

    def _verdict_for_status(self, status: str) -> str:
        """The verdict to write into a sidecar with the given status.

        `verdict` (the property above) answers "what does the evidence
        gathered so far say" - gaps or losses found, a counter anomaly, or
        neither. That question is only fully answered once `status` is
        `"complete"`: nothing more will be observed after that. For
        `"in_progress"` (written by __enter__, before a single packet has
        arrived) and `"failed"` (written by __exit__ for a run that never
        reached finalise()), the writer stopped watching before the
        recording was known to be over - because it is not finished yet, or
        because it crashed - so a `clean` verdict would assert "the whole
        run was fine" when what is actually known is only "nothing wrong
        was found before we stopped watching". That gap between what is
        known and what `clean` claims is exactly what must not ship in the
        sidecar itself: it is what a human, another language, or
        `load_recording()` reads directly, none of which go through
        `integrity_verdict()`'s read-path correction. So a non-`"complete"`
        status downgrades a `clean` verdict to `unknown`, the same call
        already made for a missing schema_version and for `failed` on the
        read path. Already-detected `gaps_detected` is real and is kept
        regardless of status - an unfinished or crashed run does not erase a
        gap that genuinely happened, and a counter anomaly alone already
        reads `unknown` on its own terms.
        """
        verdict = self.verdict
        if status != "complete" and verdict == VERDICT_CLEAN:
            return VERDICT_UNKNOWN
        return verdict

    def _write_sidecar(self, status: str, stop_reason, error=None,
                       max_gaps: Optional[int] = None) -> None:
        # LOW: max_gaps, when given (the failure-sidecar write in __exit__
        # passes self._failure_sidecar_max_gaps; the normal in_progress/
        # complete writes pass None and rely on MAX_RETAINED_GAPS - see
        # integrity.py - alone), caps the retained gap list harder for this
        # one write than the tracker's own retention already does.
        # gaps_truncated is an integer count, not a boolean (see
        # GapTracker.gaps_truncated), so any gaps this extra cap removes
        # from the list are folded into it rather than silently vanishing
        # from what the sidecar reports - only the retained *list* shrinks.
        gaps = self._tracker.gaps
        gaps_truncated = self._tracker.gaps_truncated
        if max_gaps is not None and len(gaps) > max_gaps:
            gaps_truncated += len(gaps) - max_gaps
            gaps = gaps[:max_gaps]

        n_frames = self.n_frames_written
        record = dict(asdict(self._params))
        record.update({
            "schema_version": SCHEMA_VERSION,
            "status": status,
            "stop_reason": stop_reason,
            "error": error,
            "started_utc": self._started_utc,
            "n_frames_written": n_frames,
            "duration_sec": n_frames / self._params.frame_rate_hz,
            "integrity": {
                "verdict": self._verdict_for_status(status),
                "first_timestamp": self._first_timestamp,
                "timestamps_unavailable": self._timestamps_unavailable,
                "last_timestamp": self._last_timestamp,
                "n_frames_missing": self._tracker.n_frames_missing,
                "gaps": [asdict(g) for g in gaps],
                "gaps_truncated": gaps_truncated,
                "driver_loss_events": self._driver_loss,
                "queue_overflows": self._queue_overflows,
                "callback_errors": self._callback_errors,
                "payload_length_mismatches": self._payload_mismatches,
                "discarded_at_stop": self._discarded_at_stop,
                "counter_anomalies": self._tracker.counter_anomalies,
            },
        })
        self._meta_path.parent.mkdir(parents=True, exist_ok=True)
        # FIX 2: write to a temp file in the same directory, then os.replace
        # it into position. write_text() would truncate the target in place -
        # a crash or full disk part-way through leaves both a corrupt,
        # unparseable sidecar AND destroys whatever complete sidecar (e.g.
        # the in_progress one from __enter__) was there before. os.replace is
        # atomic on both Windows and POSIX, so a reader (or the next write
        # attempt, on failure) always sees either the previous complete
        # sidecar or the new one, never a half-written file. Same directory
        # matters: os.replace is only guaranteed atomic within one
        # filesystem/volume.
        text = json.dumps(record, indent=2)
        fd, tmp_name = tempfile.mkstemp(
            dir=self._meta_path.parent, prefix=self._meta_path.name + ".", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as tmp_file:
                tmp_file.write(text)
            os.replace(tmp_name, self._meta_path)
        except BaseException:
            try:
                os.remove(tmp_name)
            except OSError:
                pass
            raise


def read_sidecar(path) -> dict:
    return json.loads(Path(path).read_text())


def integrity_verdict(meta: dict) -> str:
    """The integrity verdict of a sidecar.

    A sidecar without schema_version predates the integrity record and reports
    'unknown'. It must never report 'clean': absence of evidence is not evidence
    of completeness, and defaulting the other way would launder an unverifiable
    file into a trusted one.

    The same reasoning applies to `status: "failed"`. The integrity block only
    reflects what the writer observed up to the moment it crashed - it says
    nothing about what would have happened next, including whatever the raw
    file's last, possibly partial, write left on disk. If nothing had gone
    wrong yet at the point of failure, the block honestly computes 'clean',
    but reporting that as the file's verdict would claim the whole run was
    fine when what we actually know is narrower: the recorder never got to
    say so. So a failed run downgrades a 'clean' verdict to 'unknown'.
    Already-recorded loss ('gaps_detected') is real information the crash
    does not erase, and is kept rather than downgraded.
    """
    if meta.get("schema_version", 0) < SCHEMA_VERSION:
        return VERDICT_UNKNOWN
    verdict = meta.get("integrity", {}).get("verdict", VERDICT_UNKNOWN)
    if meta.get("status") == "failed" and verdict == VERDICT_CLEAN:
        return VERDICT_UNKNOWN
    return verdict


def load_recording(raw_path, meta_path, as_microvolts: bool = True):
    """Load a recording as (data, sidecar). Data is (n_frames, total_channels)."""
    meta = read_sidecar(meta_path)
    n_channels = meta["total_channels"]
    dtype = DTYPE_BY_BYTE_SIZE[meta["ch_sample_byte_size"]]
    flat = np.fromfile(raw_path, dtype=dtype)
    n_frames = len(flat) // n_channels
    data = flat[: n_frames * n_channels].reshape(n_frames, n_channels)
    if as_microvolts:
        data = to_microvolts(data, meta["offset"], meta["adc_counts_to_value"])
    return data, meta
