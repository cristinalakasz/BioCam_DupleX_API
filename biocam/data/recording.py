"""Writing and reading recordings, with an integrity record.

The writer appends payload bytes exactly as received - it never decodes. The
bytes written are the bytes that arrived, so the concatenation is a valid
frame-major stream and the partial-frame defect cannot occur.

The sidecar is written twice: once at the start marked in_progress, and again
on finalise. A killed process therefore leaves a raw file with its acquisition
parameters and an honest marker that it was never finished.
"""

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import numpy as np

from biocam.data.events import GapDetected, RecordingStarted, RecordingStopped
from biocam.data.frames import DTYPE_BY_BYTE_SIZE, to_microvolts
from biocam.data.integrity import GapTracker

SCHEMA_VERSION = 2

VERDICT_CLEAN = "clean"
VERDICT_GAPS = "gaps_detected"
VERDICT_UNKNOWN = "unknown"


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
    """Appends packets to a raw file and maintains the integrity record."""

    def __init__(self, raw_path, meta_path, params: AcquisitionParameters,
                 listener=None):
        self._raw_path = Path(raw_path)
        self._meta_path = Path(meta_path)
        self._params = params
        self._listener = listener

        self._file = None
        self._tracker = GapTracker(frame_rate_hz=params.frame_rate_hz)
        self._bytes_written = 0
        self._first_timestamp: Optional[int] = None
        self._last_timestamp: Optional[int] = None
        self._driver_loss = 0
        self._queue_overflows = 0
        self._callback_errors = 0
        self._discarded_at_stop = 0
        self._started_utc = None
        self._finalised = False

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
        return self

    def __exit__(self, exc_type, exc, tb):
        if self._file is not None:
            self._file.close()
            self._file = None
        if not self._finalised:
            error = exc_type.__name__ if exc_type is not None else None
            self._write_sidecar(status="failed", stop_reason="error", error=error)
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
            self._emit(GapDetected(
                after_frame=gap.after_frame,
                missing_frames=gap.missing_frames,
                duration_ms=gap.duration_ms,
            ))

        self._file.write(payload)
        self._bytes_written += len(payload)

        if self._first_timestamp is None:
            self._first_timestamp = timestamp
        self._last_timestamp = timestamp

    def note_driver_loss(self, count: int = 1) -> None:
        self._driver_loss += count

    def note_queue_overflow(self, count: int = 1) -> None:
        self._queue_overflows += count

    def note_callback_errors(self, count: int = 1) -> None:
        self._callback_errors += count

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
        if self._file is not None:
            self._file.flush()
        self._write_sidecar(status="complete", stop_reason=stop_reason)
        self._finalised = True
        self._emit(RecordingStopped(
            reason=stop_reason,
            n_frames=self.n_frames_written,
            verdict=self.verdict,
        ))

    @property
    def n_frames_written(self) -> int:
        return self._bytes_written // self._params.bytes_per_frame

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
        # discarded_at_stop joins the same "not clean" bucket as the other
        # counters below (none of which are literal frame-counter gaps
        # either): a recording that discarded acquired data at stop time
        # must never report clean, and this codebase has no verdict more
        # specific than gaps_detected for "integrity was compromised, but
        # not by a counter gap".
        if (self._tracker.gaps or self._driver_loss or self._queue_overflows
                or self._callback_errors or self._discarded_at_stop):
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
        if self._listener is not None:
            self._listener(event)

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

    def _write_sidecar(self, status: str, stop_reason, error=None) -> None:
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
                "last_timestamp": self._last_timestamp,
                "n_frames_missing": self._tracker.n_frames_missing,
                "gaps": [asdict(g) for g in self._tracker.gaps],
                "driver_loss_events": self._driver_loss,
                "queue_overflows": self._queue_overflows,
                "callback_errors": self._callback_errors,
                "discarded_at_stop": self._discarded_at_stop,
                "counter_anomalies": self._tracker.counter_anomalies,
            },
        })
        self._meta_path.parent.mkdir(parents=True, exist_ok=True)
        self._meta_path.write_text(json.dumps(record, indent=2))


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
