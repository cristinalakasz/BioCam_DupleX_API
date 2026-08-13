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
        self._n_frames = 0
        self._first_timestamp: Optional[int] = None
        self._last_timestamp: Optional[int] = None
        self._driver_loss = 0
        self._queue_overflows = 0
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
            self._write_sidecar(status="failed", stop_reason="error")
        return False

    def write_packet(self, timestamp: int, counter: int, payload: bytes) -> None:
        """Append one packet. Bytes are written exactly as received."""
        frames_in_packet = len(payload) // self._params.bytes_per_frame

        gap = self._tracker.observe(
            counter=counter,
            frames_in_packet=frames_in_packet,
            frames_written=self._n_frames,
        )
        if gap is not None:
            self._emit(GapDetected(
                after_frame=gap.after_frame,
                missing_frames=gap.missing_frames,
                duration_ms=gap.duration_ms,
            ))

        self._file.write(payload)
        self._n_frames += frames_in_packet

        if self._first_timestamp is None:
            self._first_timestamp = timestamp
        self._last_timestamp = timestamp

    def note_driver_loss(self, count: int = 1) -> None:
        self._driver_loss += count

    def note_queue_overflow(self, count: int = 1) -> None:
        self._queue_overflows += count

    def finalise(self, stop_reason: str) -> None:
        if self._file is not None:
            self._file.flush()
        self._write_sidecar(status="complete", stop_reason=stop_reason)
        self._finalised = True
        self._emit(RecordingStopped(
            reason=stop_reason,
            n_frames=self._n_frames,
            verdict=self.verdict,
        ))

    @property
    def n_frames_written(self) -> int:
        return self._n_frames

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
        if (self._tracker.gaps or self._driver_loss or self._queue_overflows
                or self._tracker.counter_anomalies):
            return VERDICT_GAPS
        return VERDICT_CLEAN

    def _emit(self, event) -> None:
        if self._listener is not None:
            self._listener(event)

    def _write_sidecar(self, status: str, stop_reason) -> None:
        record = dict(asdict(self._params))
        record.update({
            "schema_version": SCHEMA_VERSION,
            "status": status,
            "stop_reason": stop_reason,
            "started_utc": self._started_utc,
            "n_frames_written": self._n_frames,
            "duration_sec": self._n_frames / self._params.frame_rate_hz,
            "integrity": {
                "verdict": self.verdict,
                "first_timestamp": self._first_timestamp,
                "last_timestamp": self._last_timestamp,
                "n_frames_missing": self._tracker.n_frames_missing,
                "gaps": [asdict(g) for g in self._tracker.gaps],
                "driver_loss_events": self._driver_loss,
                "queue_overflows": self._queue_overflows,
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
    """
    if meta.get("schema_version", 0) < SCHEMA_VERSION:
        return VERDICT_UNKNOWN
    return meta.get("integrity", {}).get("verdict", VERDICT_UNKNOWN)


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
