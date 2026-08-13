# Phase 1 — Acquisition Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rebuild the recording path so every finished recording carries an honest, machine-readable account of its own integrity.

**Architecture:** Three layers split by testability (`biocam/__init__.py`). The recorder never decodes — it appends payload bytes as they arrive, which makes the partial-frame defect impossible. Loss is detected from `DataPacketHeader.PacketCounter`. A session consumes an **iterable of packets**, so a replay source drives the real writer end-to-end in tests and only the driver wrapper stays unproven.

**Tech Stack:** Python 3.12, numpy, h5py (new), pytest, pythonnet (runtime only, never imported by tests).

## Global Constraints

- **The BioCAM is not attached.** No task may require running hardware code.
- **The whole suite must pass with no BioCAM and no 3Brain DLLs.** No test, and nothing under `biocam/data/`, `biocam/analysis/`, `tools/`, or top-level `biocam/*.py`, may import `clr`, `pythonnet` or `clr_loader`. Only `biocam/interop/` may. `tests/test_no_hardware_imports.py` enforces this and must keep passing.
- **English throughout.**
- **Never commit** `*.dll`, `*.raw` outside `tests/fixtures/`, or `.env`.
- **Legacy scripts are not touched.** `BioCam_DupleX_API/connector.py`, `recorder.py`, `Hello_BioCam.py` stay exactly as they are — they are the fallback until the new path is lab-verified (spec §9).
- Sidecar `SCHEMA_VERSION = 2`. A sidecar without `schema_version` yields verdict `"unknown"`, **never** `"clean"`.
- Reference recording constants: `frame_rate_hz = 18557.720703125`, `total_channels = 4096`, `ch_sample_byte_size = 2` → **8192 bytes per frame**, `adc_counts_to_value = 2.0146520146520146`, `offset = -4125.0`, `min_digital_value = 0`, `max_digital_value = 4095`, `bit_depth = 12`.
- Committed fixtures: `tests/fixtures/sample_32ch_2s` (37,115 frames × 32 ch) and `tests/fixtures/sample_full_100frames` (100 frames × 4096 ch). Loader `load_fixture(name) -> (data, meta)` lives in `tests/test_fixture_integrity.py`.
- Baseline suite before this plan: **28 tests passing**.

---

## File structure

| Path | Responsibility | Layer |
|---|---|---|
| `biocam/data/events.py` | Event types emitted by a session, plus `describe()` for rendering | 2 |
| `biocam/data/integrity.py` | `packets_lost()`, `Gap`, `GapTracker` | 2 |
| `biocam/data/frames.py` | `FrameDecoder` with carry-over, `to_microvolts()` | 2 |
| `biocam/data/recording.py` | `AcquisitionParameters`, `RecordingWriter`, sidecar read, `integrity_verdict()`, `load_recording()` | 2 |
| `biocam/data/replay.py` | `Packet`, `ReplayPacketSource` | 2 |
| `biocam/session.py` | `record_session()` — consumes any packet iterable | 2 |
| `biocam/convert.py` | raw + sidecar → HDF5, and verification | 2 |
| `biocam/preflight.py` | *(modify)* add disk-space check | 2 |
| `biocam/interop/benchmark.py` | Payload-copy measurement. Needs DLLs, no device | 1 |
| `biocam/interop/device.py` | DLL load, pool, claim/release | 1 |
| `biocam/interop/source.py` | `DriverPacketSource` — callback → queue → iterable | 1 |
| `biocam/cli.py` | Command line. Imports interop **inside functions only** | 2 |

---

### Task 1: Payload-copy benchmark

**Files:**
- Create: `biocam/interop/benchmark.py`

**Interfaces:**
- Consumes: nothing.
- Produces: a measurement that informs Task 10's callback. No code depends on it.

**Why first.** How expensive it is to copy a payload out of .NET decides how the callback is written, and it cannot be reasoned about — only measured. It *can* be measured here: loading the assemblies needs no device.

- [ ] **Step 1: Write the benchmark**

```python
"""Measure the cost of copying a packet payload out of .NET.

The acquisition callback must copy each payload and return. At 152 MB/s with
1 ms packets that is roughly 152 KB every millisecond, so the copy strategy
matters. This measures the candidates.

Requires the 3Brain DLLs on disk. Does NOT require a BioCAM — creating a .NET
byte[] and copying from it involves no hardware.

Usage:
    python -m biocam.interop.benchmark
"""

import time
from pathlib import Path

DLL_DIR = Path(__file__).resolve().parent.parent.parent / "BioCam_DupleX_API" / "API"

PAYLOAD_BYTES = 152_000      # ~1 ms at the reference rate
REPEATS = 2000


def _load_runtime():
    import pythonnet
    pythonnet.load("netfx")
    import clr
    clr.AddReference(str(DLL_DIR / "3Brain.Common.dll"))
    clr.AddReference(str(DLL_DIR / "3Brain.BioCamDriver.dll"))


def _time(label, fn, payload):
    fn(payload)                                  # warm up
    start = time.perf_counter()
    for _ in range(REPEATS):
        fn(payload)
    elapsed = time.perf_counter() - start
    per_call_us = elapsed / REPEATS * 1e6
    mb_per_s = (PAYLOAD_BYTES * REPEATS / 1e6) / elapsed
    print(f"{label:<34} {per_call_us:8.1f} us/packet   {mb_per_s:8.0f} MB/s")
    return per_call_us


def main():
    _load_runtime()
    import System
    from System.Runtime.InteropServices import Marshal

    payload = System.Array.CreateInstance(System.Byte, PAYLOAD_BYTES)

    print(f"payload {PAYLOAD_BYTES:,} bytes, {REPEATS:,} repeats\n")

    _time("bytes(payload)", lambda p: bytes(p), payload)

    buffer = bytearray(PAYLOAD_BYTES)
    def marshal_copy(p):
        from ctypes import addressof, c_char
        target = (c_char * PAYLOAD_BYTES).from_buffer(buffer)
        Marshal.Copy(p, 0, System.IntPtr(addressof(target)), PAYLOAD_BYTES)
    _time("Marshal.Copy into bytearray", marshal_copy, payload)

    budget_us = 1000.0
    print(f"\nBudget is {budget_us:.0f} us per packet at a 1 ms acquisition period.")
    print("A strategy above that cannot keep up and must not be used in the callback.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 2: Run it and record the numbers**

Run: `python -m biocam.interop.benchmark`

Both strategies must print a time. Record the exact output in your report — Task 10 chooses between them on these numbers. If `Marshal.Copy` raises, record the traceback and report `bytes()` alone; do not invent a workaround.

- [ ] **Step 3: Confirm the guard still passes**

Run: `python -m pytest tests/test_no_hardware_imports.py -v`
Expected: PASS. `biocam/interop/` is exempt, so importing `clr` there is allowed. If this fails, the file is in the wrong place.

- [ ] **Step 4: Commit**

```bash
git add biocam/interop/benchmark.py
git commit -m "Add payload-copy benchmark"
```

---

### Task 2: Session events

**Files:**
- Create: `biocam/data/events.py`
- Test: `tests/test_events.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `RecordingStarted(path, total_channels, frame_rate_hz)`, `GapDetected(after_frame, missing_frames, duration_ms)`, `QueuePressure(depth, capacity)`, `QueueOverflow(total)`, `DriverDataLoss(total)`, `DiskLow(free_bytes, required_bytes)`, `RecordingStopped(reason, n_frames, verdict)`, and `describe(event) -> str`. Tasks 5, 7, 10 and 11 use these.

- [ ] **Step 1: Write the failing test**

```python
from biocam.data.events import (
    DiskLow, DriverDataLoss, GapDetected, QueueOverflow, QueuePressure,
    RecordingStarted, RecordingStopped, describe,
)


def test_events_are_immutable():
    import dataclasses
    import pytest
    event = QueueOverflow(total=3)
    with pytest.raises(dataclasses.FrozenInstanceError):
        event.total = 4


def test_describe_gap_names_the_numbers():
    text = describe(GapDetected(after_frame=92100, missing_frames=371, duration_ms=19.99))
    assert "92100" in text
    assert "371" in text
    assert "19.99" in text


def test_describe_stopped_reports_reason_and_verdict():
    text = describe(RecordingStopped(reason="user_stopped", n_frames=100, verdict="clean"))
    assert "user_stopped" in text
    assert "clean" in text
    assert "100" in text


def test_describe_handles_every_event_type():
    events = [
        RecordingStarted(path="a.raw", total_channels=4096, frame_rate_hz=18557.72),
        GapDetected(after_frame=1, missing_frames=2, duration_ms=0.1),
        QueuePressure(depth=90, capacity=100),
        QueueOverflow(total=1),
        DriverDataLoss(total=1),
        DiskLow(free_bytes=1000, required_bytes=2000),
        RecordingStopped(reason="duration_reached", n_frames=5, verdict="unknown"),
    ]
    for event in events:
        text = describe(event)
        assert isinstance(text, str) and text, f"no description for {type(event).__name__}"


def test_describe_rejects_unknown_objects():
    import pytest
    with pytest.raises(TypeError):
        describe("not an event")
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python -m pytest tests/test_events.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'biocam.data.events'`

- [ ] **Step 3: Write the implementation**

```python
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
    RecordingStarted, GapDetected, QueuePressure, QueueOverflow,
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
```

- [ ] **Step 4: Run the tests**

Run: `python -m pytest tests/test_events.py -v`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add biocam/data/events.py tests/test_events.py
git commit -m "Add session event types"
```

---

### Task 3: Gap detection

**Files:**
- Create: `biocam/data/integrity.py`
- Test: `tests/test_integrity.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `COUNTER_MODULUS = 65536`, `packets_lost(previous, current) -> int`, `Gap(after_frame, missing_frames, duration_ms)`, `GapTracker(frame_rate_hz)` with `.observe(counter, frames_in_packet, frames_written) -> Gap | None`, `.gaps`, `.n_frames_missing`. Task 5 uses `GapTracker`.

- [ ] **Step 1: Write the failing test**

```python
import pytest

from biocam.data.integrity import COUNTER_MODULUS, Gap, GapTracker, packets_lost

RATE = 18557.720703125


def test_consecutive_counters_lose_nothing():
    assert packets_lost(10, 11) == 0


def test_one_skipped_packet():
    assert packets_lost(10, 12) == 1


def test_many_skipped_packets():
    assert packets_lost(10, 20) == 9


def test_counter_wraps_without_reporting_loss():
    assert packets_lost(COUNTER_MODULUS - 1, 0) == 0


def test_loss_across_the_wrap_boundary():
    assert packets_lost(COUNTER_MODULUS - 1, 2) == 2


def test_repeated_counter_is_not_treated_as_loss():
    # A duplicate is not 65536 missing packets. Report nothing and move on.
    assert packets_lost(10, 10) == 0


def test_tracker_reports_nothing_on_a_clean_run():
    tracker = GapTracker(frame_rate_hz=RATE)
    for i, counter in enumerate([5, 6, 7, 8]):
        assert tracker.observe(counter, frames_in_packet=10, frames_written=i * 10) is None
    assert tracker.gaps == []
    assert tracker.n_frames_missing == 0


def test_tracker_reports_a_gap_with_position_and_duration():
    tracker = GapTracker(frame_rate_hz=RATE)
    tracker.observe(5, frames_in_packet=10, frames_written=0)
    gap = tracker.observe(8, frames_in_packet=10, frames_written=10)
    assert isinstance(gap, Gap)
    assert gap.after_frame == 10
    assert gap.missing_frames == 20          # 2 lost packets x 10 frames
    assert gap.duration_ms == pytest.approx(20 / RATE * 1000)
    assert tracker.n_frames_missing == 20
    assert tracker.gaps == [gap]


def test_first_packet_never_reports_a_gap():
    tracker = GapTracker(frame_rate_hz=RATE)
    assert tracker.observe(12345, frames_in_packet=10, frames_written=0) is None


def test_tracker_accumulates_several_gaps():
    tracker = GapTracker(frame_rate_hz=RATE)
    tracker.observe(1, frames_in_packet=10, frames_written=0)
    tracker.observe(3, frames_in_packet=10, frames_written=10)
    tracker.observe(7, frames_in_packet=10, frames_written=20)
    assert len(tracker.gaps) == 2
    assert tracker.n_frames_missing == 10 + 30
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python -m pytest tests/test_integrity.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'biocam.data.integrity'`

- [ ] **Step 3: Write the implementation**

```python
"""Detecting lost data from the instrument's own packet counter.

DataPacketHeader carries a UInt16 PacketCounter alongside the timestamp,
confirmed from its constructor signature in 3Brain.BioCamDriver.xml:
    #ctor(Byte, BioCamUsbComSignalType, Int32, UInt64, UInt16)
being (Reserved, SignalType, PayloadLength, Timestamp, PacketCounter).

That makes loss detection exact rather than inferred: no clock arithmetic, no
tolerance threshold. The counter wraps at 65536, which is handled explicitly -
that wrap is the kind of edge case that works for hours and then does not.
"""

from dataclasses import dataclass
from typing import List, Optional

COUNTER_MODULUS = 65536


@dataclass(frozen=True)
class Gap:
    """A run of packets that never arrived."""
    after_frame: int
    missing_frames: int
    duration_ms: float


def packets_lost(previous_counter: int, counter: int) -> int:
    """How many packets are missing between two counter values.

    Returns 0 for consecutive counters and for a repeated counter. A repeat is
    treated as a duplicate rather than as a full 65536-packet wrap, because a
    duplicate is plausible and losing exactly one modulus is not.
    """
    delta = (counter - previous_counter) % COUNTER_MODULUS
    if delta == 0:
        return 0
    return delta - 1


class GapTracker:
    """Accumulates gaps across a recording.

    frames_in_packet is taken from the packet being observed. Packet size is
    fixed for the duration of a session, so the packets that went missing are
    assumed to have carried the same number of frames as the one that followed
    them.
    """

    def __init__(self, frame_rate_hz: float):
        self._frame_rate_hz = frame_rate_hz
        self._previous_counter: Optional[int] = None
        self._gaps: List[Gap] = []
        self._n_frames_missing = 0

    def observe(self, counter: int, frames_in_packet: int,
                frames_written: int) -> Optional[Gap]:
        """Record a packet. Returns a Gap if packets were lost before it."""
        previous = self._previous_counter
        self._previous_counter = counter
        if previous is None:
            return None

        lost = packets_lost(previous, counter)
        if lost == 0:
            return None

        missing_frames = lost * frames_in_packet
        gap = Gap(
            after_frame=frames_written,
            missing_frames=missing_frames,
            duration_ms=missing_frames / self._frame_rate_hz * 1000.0,
        )
        self._gaps.append(gap)
        self._n_frames_missing += missing_frames
        return gap

    @property
    def gaps(self) -> List[Gap]:
        return list(self._gaps)

    @property
    def n_frames_missing(self) -> int:
        return self._n_frames_missing
```

- [ ] **Step 4: Run the tests**

Run: `python -m pytest tests/test_integrity.py -v`
Expected: 10 passed

- [ ] **Step 5: Commit**

```bash
git add biocam/data/integrity.py tests/test_integrity.py
git commit -m "Add packet-counter gap detection"
```

---

### Task 4: Frame decoding

**Files:**
- Create: `biocam/data/frames.py`
- Test: `tests/test_frames.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `DTYPE_BY_BYTE_SIZE`, `FrameDecoder(total_channels, ch_sample_byte_size)` with `.decode(payload) -> np.ndarray` and `.pending_bytes`, `to_microvolts(counts, offset, adc_counts_to_value) -> np.ndarray`. Tasks 5 and 8 use these.

**Note.** The recorder does **not** use this — it appends bytes untouched. This exists for reading files and for Phase 5's online path, which is where carry-over genuinely matters.

- [ ] **Step 1: Write the failing test**

```python
import numpy as np
import pytest

from biocam.data.frames import DTYPE_BY_BYTE_SIZE, FrameDecoder, to_microvolts


def _payload(values):
    return np.asarray(values, dtype=np.uint16).tobytes()


def test_decodes_whole_frames():
    decoder = FrameDecoder(total_channels=4, ch_sample_byte_size=2)
    out = decoder.decode(_payload([1, 2, 3, 4, 5, 6, 7, 8]))
    assert out.shape == (2, 4)
    assert out.tolist() == [[1, 2, 3, 4], [5, 6, 7, 8]]
    assert decoder.pending_bytes == 0


def test_carries_a_partial_frame_to_the_next_call():
    decoder = FrameDecoder(total_channels=4, ch_sample_byte_size=2)
    first = decoder.decode(_payload([1, 2, 3, 4, 5, 6]))     # 1.5 frames
    assert first.shape == (1, 4)
    assert decoder.pending_bytes == 4                         # 2 samples held back

    second = decoder.decode(_payload([7, 8]))                 # completes frame 2
    assert second.shape == (1, 4)
    assert second.tolist() == [[5, 6, 7, 8]]
    assert decoder.pending_bytes == 0


def test_no_sample_is_ever_dropped_across_many_ragged_packets():
    """Feed deliberately uneven packets; every sample must come back in order.

    Sizes cycle so that packet boundaries land at every possible offset within
    a frame - which is the situation that made the original code lose data.
    """
    import itertools

    decoder = FrameDecoder(total_channels=4, ch_sample_byte_size=2)
    source = np.arange(400, dtype=np.uint16)
    sizes = itertools.cycle([3, 7, 1, 11, 5, 13])
    recovered = []
    cursor = 0
    while cursor < len(source):
        size = next(sizes)
        chunk = source[cursor:cursor + size]
        cursor += size
        block = decoder.decode(chunk.tobytes())
        if block.size:
            recovered.append(block)

    joined = np.concatenate(recovered).reshape(-1)
    assert joined.tolist() == source[:len(joined)].tolist()
    # At most one incomplete frame may remain held back.
    assert len(source) - len(joined) < 4
    assert decoder.pending_bytes == (len(source) - len(joined)) * 2


def test_empty_payload_yields_no_frames():
    decoder = FrameDecoder(total_channels=4, ch_sample_byte_size=2)
    out = decoder.decode(b"")
    assert out.shape == (0, 4)
    assert decoder.pending_bytes == 0


def test_payload_shorter_than_one_frame_yields_nothing_yet():
    decoder = FrameDecoder(total_channels=4, ch_sample_byte_size=2)
    out = decoder.decode(_payload([1, 2]))
    assert out.shape == (0, 4)
    assert decoder.pending_bytes == 4


def test_rejects_unsupported_sample_size():
    with pytest.raises(ValueError):
        FrameDecoder(total_channels=4, ch_sample_byte_size=3)


def test_microvolt_conversion_matches_hand_computation():
    counts = np.array([[686, 3050]], dtype=np.uint16)
    out = to_microvolts(counts, offset=-4125.0, adc_counts_to_value=2.0146520146520146)
    assert out[0, 0] == pytest.approx(-4125.0 + 686 * 2.0146520146520146)
    assert out[0, 1] == pytest.approx(-4125.0 + 3050 * 2.0146520146520146)


def test_microvolt_conversion_returns_float_not_integer():
    out = to_microvolts(np.array([[0]], dtype=np.uint16), offset=-1.5,
                        adc_counts_to_value=0.5)
    assert out.dtype == np.float64
    assert out[0, 0] == pytest.approx(-1.5)


def test_supported_sample_sizes():
    assert set(DTYPE_BY_BYTE_SIZE) == {1, 2, 4}
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python -m pytest tests/test_frames.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'biocam.data.frames'`

- [ ] **Step 3: Write the implementation**

```python
"""Turning payload bytes into frames.

A frame holds one sample from every channel. Payloads do not necessarily end on
a frame boundary, so a decoder that discards the remainder desynchronises
everything after it - that is Appendix A defect 5. FrameDecoder carries the
remainder into the next call instead.

The recorder does NOT use this. It appends payload bytes untouched, which is
why the defect cannot occur there at all. This is for reading files and for
Phase 5's online path, where packets genuinely are decoded as they arrive.
"""

import numpy as np

DTYPE_BY_BYTE_SIZE = {1: np.uint8, 2: np.uint16, 4: np.uint32}


class FrameDecoder:
    """Decodes payload bytes into whole frames, carrying any partial frame."""

    def __init__(self, total_channels: int, ch_sample_byte_size: int):
        if ch_sample_byte_size not in DTYPE_BY_BYTE_SIZE:
            raise ValueError(
                f"ch_sample_byte_size={ch_sample_byte_size} is not supported; "
                f"expected one of {sorted(DTYPE_BY_BYTE_SIZE)}"
            )
        self._total_channels = total_channels
        self._dtype = DTYPE_BY_BYTE_SIZE[ch_sample_byte_size]
        self._bytes_per_frame = total_channels * ch_sample_byte_size
        self._pending = b""

    def decode(self, payload: bytes) -> np.ndarray:
        """Return the whole frames available, holding any remainder back."""
        buffer = self._pending + bytes(payload)
        n_frames = len(buffer) // self._bytes_per_frame
        used = n_frames * self._bytes_per_frame
        self._pending = buffer[used:]
        if n_frames == 0:
            return np.empty((0, self._total_channels), dtype=self._dtype)
        return np.frombuffer(buffer[:used], dtype=self._dtype).reshape(
            n_frames, self._total_channels
        )

    @property
    def pending_bytes(self) -> int:
        """Bytes of an incomplete frame held for the next call."""
        return len(self._pending)


def to_microvolts(counts, offset: float, adc_counts_to_value: float) -> np.ndarray:
    """Convert raw ADC counts to microvolts."""
    return offset + np.asarray(counts, dtype=np.float64) * adc_counts_to_value
```

- [ ] **Step 4: Run the tests**

Run: `python -m pytest tests/test_frames.py -v`
Expected: 9 passed

- [ ] **Step 5: Commit**

```bash
git add biocam/data/frames.py tests/test_frames.py
git commit -m "Add frame decoding with partial-frame carry-over"
```

---

### Task 5: Recording writer and sidecar

**Files:**
- Create: `biocam/data/recording.py`
- Test: `tests/test_recording.py`

**Interfaces:**
- Consumes: `GapTracker`, `Gap` (Task 3); `FrameDecoder`, `to_microvolts`, `DTYPE_BY_BYTE_SIZE` (Task 4); `GapDetected`, `RecordingStarted`, `RecordingStopped` (Task 2).
- Produces: `SCHEMA_VERSION = 2`; `AcquisitionParameters(frame_rate_hz, total_channels, ch_sample_byte_size, bit_depth, adc_counts_to_value, offset, min_digital_value, max_digital_value)` with `.bytes_per_frame`; `RecordingWriter(raw_path, meta_path, params, listener=None)` with `.write_packet(timestamp, counter, payload)`, `.note_driver_loss(n)`, `.note_queue_overflow(n)`, `.finalise(stop_reason)`, `.n_frames_written`, `.verdict`; `read_sidecar(path) -> dict`; `integrity_verdict(meta) -> str`; `load_recording(raw_path, meta_path, as_microvolts=True) -> (np.ndarray, dict)`. Tasks 7, 8 and 11 use these.

- [ ] **Step 1: Write the failing test**

```python
import json

import numpy as np
import pytest

from biocam.data.events import GapDetected, RecordingStarted, RecordingStopped
from biocam.data.recording import (
    SCHEMA_VERSION, AcquisitionParameters, RecordingWriter,
    integrity_verdict, load_recording, read_sidecar,
)

PARAMS = AcquisitionParameters(
    frame_rate_hz=18557.720703125,
    total_channels=4,
    ch_sample_byte_size=2,
    bit_depth=12,
    adc_counts_to_value=2.0146520146520146,
    offset=-4125.0,
    min_digital_value=0,
    max_digital_value=4095,
)


def _frame(values):
    return np.asarray(values, dtype=np.uint16).tobytes()


def _paths(tmp_path):
    return tmp_path / "rec.raw", tmp_path / "rec_meta.json"


def test_bytes_per_frame():
    assert PARAMS.bytes_per_frame == 8


def test_clean_run_writes_bytes_verbatim_and_reports_clean(tmp_path):
    raw, meta = _paths(tmp_path)
    with RecordingWriter(raw, meta, PARAMS) as writer:
        writer.write_packet(timestamp=100, counter=1, payload=_frame([1, 2, 3, 4]))
        writer.write_packet(timestamp=200, counter=2, payload=_frame([5, 6, 7, 8]))
        writer.finalise("duration_reached")

    assert raw.read_bytes() == _frame([1, 2, 3, 4, 5, 6, 7, 8])
    record = read_sidecar(meta)
    assert record["schema_version"] == SCHEMA_VERSION
    assert record["status"] == "complete"
    assert record["stop_reason"] == "duration_reached"
    assert record["n_frames_written"] == 2
    assert record["integrity"]["verdict"] == "clean"
    assert record["integrity"]["gaps"] == []
    assert record["integrity"]["n_frames_missing"] == 0
    assert record["integrity"]["first_timestamp"] == 100
    assert record["integrity"]["last_timestamp"] == 200


def test_a_gap_is_recorded_with_position_and_size(tmp_path):
    raw, meta = _paths(tmp_path)
    with RecordingWriter(raw, meta, PARAMS) as writer:
        writer.write_packet(timestamp=100, counter=1, payload=_frame([1, 2, 3, 4]))
        writer.write_packet(timestamp=300, counter=4, payload=_frame([5, 6, 7, 8]))
        writer.finalise("duration_reached")

    record = read_sidecar(meta)
    assert record["integrity"]["verdict"] == "gaps_detected"
    assert len(record["integrity"]["gaps"]) == 1
    gap = record["integrity"]["gaps"][0]
    assert gap["after_frame"] == 1
    assert gap["missing_frames"] == 2        # 2 lost packets x 1 frame each
    assert record["integrity"]["n_frames_missing"] == 2


def test_sidecar_exists_and_says_in_progress_before_finalise(tmp_path):
    raw, meta = _paths(tmp_path)
    writer = RecordingWriter(raw, meta, PARAMS)
    writer.__enter__()
    writer.write_packet(timestamp=1, counter=1, payload=_frame([1, 2, 3, 4]))
    record = read_sidecar(meta)
    assert record["status"] == "in_progress"
    writer.finalise("user_stopped")
    writer.__exit__(None, None, None)
    assert read_sidecar(meta)["status"] == "complete"


def test_driver_loss_and_queue_overflow_are_counted_separately(tmp_path):
    raw, meta = _paths(tmp_path)
    with RecordingWriter(raw, meta, PARAMS) as writer:
        writer.write_packet(timestamp=1, counter=1, payload=_frame([1, 2, 3, 4]))
        writer.note_driver_loss(2)
        writer.note_queue_overflow(5)
        writer.finalise("error")

    integrity = read_sidecar(meta)["integrity"]
    assert integrity["driver_loss_events"] == 2
    assert integrity["queue_overflows"] == 5
    assert integrity["verdict"] == "gaps_detected"


def test_events_are_emitted_to_the_listener(tmp_path):
    raw, meta = _paths(tmp_path)
    seen = []
    with RecordingWriter(raw, meta, PARAMS, listener=seen.append) as writer:
        writer.write_packet(timestamp=100, counter=1, payload=_frame([1, 2, 3, 4]))
        writer.write_packet(timestamp=300, counter=4, payload=_frame([5, 6, 7, 8]))
        writer.finalise("duration_reached")

    assert isinstance(seen[0], RecordingStarted)
    assert any(isinstance(e, GapDetected) for e in seen)
    assert isinstance(seen[-1], RecordingStopped)
    assert seen[-1].verdict == "gaps_detected"


def test_verdict_unknown_when_the_sidecar_predates_schema_2():
    assert integrity_verdict({"total_channels": 4096}) == "unknown"


def test_verdict_unknown_is_never_upgraded_to_clean():
    legacy = {"total_channels": 4096, "n_frames_total": 100, "packet_log": []}
    assert integrity_verdict(legacy) != "clean"


def test_verdict_read_from_a_schema_2_sidecar():
    modern = {"schema_version": 2, "integrity": {"verdict": "clean"}}
    assert integrity_verdict(modern) == "clean"


def test_load_recording_round_trips_values(tmp_path):
    raw, meta = _paths(tmp_path)
    with RecordingWriter(raw, meta, PARAMS) as writer:
        writer.write_packet(timestamp=1, counter=1, payload=_frame([100, 200, 300, 400]))
        writer.finalise("duration_reached")

    counts, record = load_recording(raw, meta, as_microvolts=False)
    assert counts.tolist() == [[100, 200, 300, 400]]

    volts, _ = load_recording(raw, meta, as_microvolts=True)
    assert volts[0, 0] == pytest.approx(-4125.0 + 100 * 2.0146520146520146)


def test_load_recording_reports_the_verdict_it_found(tmp_path):
    raw, meta = _paths(tmp_path)
    with RecordingWriter(raw, meta, PARAMS) as writer:
        writer.write_packet(timestamp=1, counter=1, payload=_frame([1, 2, 3, 4]))
        writer.finalise("duration_reached")
    _, record = load_recording(raw, meta)
    assert record["integrity"]["verdict"] == "clean"


def test_committed_fixtures_are_reported_as_unknown():
    """The fixtures predate schema 2 and must never read as clean."""
    from tests.test_fixture_integrity import load_fixture
    _, meta = load_fixture("sample_32ch_2s")
    assert integrity_verdict(meta) == "unknown"
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python -m pytest tests/test_recording.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'biocam.data.recording'`

- [ ] **Step 3: Write the implementation**

```python
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
        if self._tracker.gaps or self._driver_loss or self._queue_overflows:
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
```

- [ ] **Step 4: Run the tests**

Run: `python -m pytest tests/test_recording.py -v`
Expected: 12 passed

- [ ] **Step 5: Run the whole suite**

Run: `python -m pytest -q`
Expected: all pass. The guard now scans four new files; none imports `clr`.

- [ ] **Step 6: Commit**

```bash
git add biocam/data/recording.py tests/test_recording.py
git commit -m "Add recording writer with integrity sidecar"
```

---

### Task 6: Replay packet source

**Files:**
- Create: `biocam/data/replay.py`
- Test: `tests/test_replay.py`

**Interfaces:**
- Consumes: `AcquisitionParameters` (Task 5).
- Produces: `Packet(timestamp, counter, payload)`; `ReplayPacketSource(raw_path, params, frames_per_packet=20, drop_packets=())` which is iterable over `Packet`. Task 7 uses it.

- [ ] **Step 1: Write the failing test**

```python
import numpy as np

from biocam.data.recording import AcquisitionParameters
from biocam.data.replay import Packet, ReplayPacketSource

PARAMS = AcquisitionParameters(
    frame_rate_hz=1000.0, total_channels=4, ch_sample_byte_size=2, bit_depth=12,
    adc_counts_to_value=1.0, offset=0.0, min_digital_value=0, max_digital_value=4095,
)


def _write_raw(tmp_path, n_frames):
    data = np.arange(n_frames * 4, dtype=np.uint16).reshape(n_frames, 4)
    path = tmp_path / "src.raw"
    data.tofile(path)
    return path, data


def test_emits_every_frame_exactly_once(tmp_path):
    path, data = _write_raw(tmp_path, 50)
    packets = list(ReplayPacketSource(path, PARAMS, frames_per_packet=7))
    joined = b"".join(p.payload for p in packets)
    assert joined == data.tobytes()


def test_counters_increment_by_one(tmp_path):
    path, _ = _write_raw(tmp_path, 30)
    counters = [p.counter for p in ReplayPacketSource(path, PARAMS, frames_per_packet=10)]
    assert counters == [0, 1, 2]


def test_timestamps_advance(tmp_path):
    path, _ = _write_raw(tmp_path, 30)
    stamps = [p.timestamp for p in ReplayPacketSource(path, PARAMS, frames_per_packet=10)]
    assert stamps == sorted(stamps)
    assert len(set(stamps)) == len(stamps)


def test_dropped_packets_are_omitted_but_counters_still_advance(tmp_path):
    path, _ = _write_raw(tmp_path, 40)
    source = ReplayPacketSource(path, PARAMS, frames_per_packet=10, drop_packets=(1,))
    packets = list(source)
    assert [p.counter for p in packets] == [0, 2, 3]


def test_last_packet_may_be_short(tmp_path):
    path, _ = _write_raw(tmp_path, 25)
    packets = list(ReplayPacketSource(path, PARAMS, frames_per_packet=10))
    assert len(packets) == 3
    assert len(packets[-1].payload) == 5 * PARAMS.bytes_per_frame


def test_packet_is_immutable(tmp_path):
    import dataclasses
    import pytest
    packet = Packet(timestamp=1, counter=2, payload=b"x")
    with pytest.raises(dataclasses.FrozenInstanceError):
        packet.counter = 3
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python -m pytest tests/test_replay.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'biocam.data.replay'`

- [ ] **Step 3: Write the implementation**

```python
"""Replaying a recorded file as if it were arriving from the instrument.

This is what makes a whole recording session testable without hardware. The
session consumes an iterable of packets; the driver provides one and this
provides another, reading a .raw file and chopping it into packets. Losses can
be injected to exercise the gap detection.

The counter advances for dropped packets, exactly as the instrument's would -
that is precisely how a gap becomes detectable.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Sequence

from biocam.data.integrity import COUNTER_MODULUS
from biocam.data.recording import AcquisitionParameters


@dataclass(frozen=True)
class Packet:
    timestamp: int
    counter: int
    payload: bytes


class ReplayPacketSource:
    """Emits a .raw file as a sequence of packets."""

    def __init__(self, raw_path, params: AcquisitionParameters,
                 frames_per_packet: int = 20,
                 drop_packets: Sequence[int] = ()):
        self._raw_path = Path(raw_path)
        self._params = params
        self._frames_per_packet = frames_per_packet
        self._drop = set(drop_packets)

    def __iter__(self) -> Iterator[Packet]:
        chunk_bytes = self._frames_per_packet * self._params.bytes_per_frame
        timestamp = 0
        counter = 0
        with open(self._raw_path, "rb") as handle:
            while True:
                payload = handle.read(chunk_bytes)
                if not payload:
                    return
                index = counter
                counter = (counter + 1) % COUNTER_MODULUS
                timestamp += self._frames_per_packet
                if index in self._drop:
                    continue
                yield Packet(timestamp=timestamp, counter=index, payload=payload)
```

- [ ] **Step 4: Run the tests**

Run: `python -m pytest tests/test_replay.py -v`
Expected: 6 passed

- [ ] **Step 5: Commit**

```bash
git add biocam/data/replay.py tests/test_replay.py
git commit -m "Add replay packet source"
```

---

### Task 7: Session orchestration

**Files:**
- Create: `biocam/session.py`
- Test: `tests/test_session.py`

**Interfaces:**
- Consumes: `RecordingWriter`, `AcquisitionParameters` (Task 5); `ReplayPacketSource`, `Packet` (Task 6).
- Produces: `SessionResult(raw_path, meta_path, n_frames, verdict, stop_reason)`; `record_session(source, writer, duration_sec=None, stop_event=None) -> SessionResult`. Tasks 10 and 11 use it.

**Note.** `biocam/session.py` is top-level and therefore guarded — it must never import `biocam.interop`. It takes a source, which is exactly what keeps it testable.

- [ ] **Step 1: Write the failing test**

```python
import threading

import numpy as np

from biocam.data.recording import AcquisitionParameters, RecordingWriter, read_sidecar
from biocam.data.replay import ReplayPacketSource
from biocam.session import SessionResult, record_session

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

    def stopping_after_three(packets):
        for index, packet in enumerate(packets):
            yield packet
            if index == 2:
                stop.set()

    with RecordingWriter(raw, meta, PARAMS) as writer:
        result = record_session(stopping_after_three(source), writer, stop_event=stop)

    assert result.stop_reason == "user_stopped"
    assert result.n_frames == 30


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
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python -m pytest tests/test_session.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'biocam.session'`

- [ ] **Step 3: Write the implementation**

```python
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
                   stop_event=None) -> SessionResult:
    """Consume packets into the writer until a stop condition is met.

    Stops when the source runs out, when duration_sec of recorded signal has
    been written, or when stop_event is set. Duration is measured in recorded
    frames rather than wall-clock, so it means the same thing for a live
    instrument and for a replay.
    """
    frame_limit = None
    if duration_sec is not None:
        frame_limit = int(duration_sec * writer.params.frame_rate_hz)

    stop_reason = "source_exhausted"

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

    writer.finalise(stop_reason)
    return SessionResult(
        raw_path=str(writer.raw_path),
        meta_path=str(writer.meta_path),
        n_frames=writer.n_frames_written,
        verdict=writer.verdict,
        stop_reason=stop_reason,
    )
```

- [ ] **Step 4: Run the tests**

Run: `python -m pytest tests/test_session.py -v`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add biocam/session.py tests/test_session.py
git commit -m "Add session orchestration over a packet source"
```

---

### Task 8: HDF5 converter

**Files:**
- Create: `biocam/convert.py`
- Test: `tests/test_convert.py`
- Modify: `requirements.txt`, `requirements-dev.txt`

**Interfaces:**
- Consumes: `read_sidecar`, `integrity_verdict`, `DTYPE_BY_BYTE_SIZE` (Tasks 4, 5).
- Produces: `convert(raw_path, meta_path, out_path, compression="gzip", level=4) -> dict`; `verify(out_path, raw_path, meta_path) -> bool`.

- [ ] **Step 1: Install h5py and pin it**

```powershell
python -m pip install h5py
python -m pip show h5py | Select-String '^Version:'
```

Add `h5py==<version>` to **both** `requirements.txt` and `requirements-dev.txt` — the converter runs on the lab machine and on a development machine.

- [ ] **Step 2: Write the failing test**

```python
import h5py
import numpy as np
import pytest

from biocam.convert import convert, verify
from biocam.data.recording import AcquisitionParameters, RecordingWriter

PARAMS = AcquisitionParameters(
    frame_rate_hz=1000.0, total_channels=4, ch_sample_byte_size=2, bit_depth=12,
    adc_counts_to_value=2.0, offset=-10.0, min_digital_value=0, max_digital_value=4095,
)


def _recording(tmp_path, drop_gap=False):
    raw, meta = tmp_path / "r.raw", tmp_path / "r_meta.json"
    with RecordingWriter(raw, meta, PARAMS) as writer:
        writer.write_packet(1, 1, np.arange(8, dtype=np.uint16).tobytes())
        writer.write_packet(2, 4 if drop_gap else 2,
                            np.arange(8, 16, dtype=np.uint16).tobytes())
        writer.finalise("duration_reached")
    return raw, meta


def test_round_trip_is_byte_identical(tmp_path):
    raw, meta = _recording(tmp_path)
    out = tmp_path / "r.h5"
    convert(raw, meta, out)
    with h5py.File(out, "r") as handle:
        restored = handle["data"][:]
    assert restored.tobytes() == raw.read_bytes()


def test_dataset_shape_matches_metadata(tmp_path):
    raw, meta = _recording(tmp_path)
    out = tmp_path / "r.h5"
    convert(raw, meta, out)
    with h5py.File(out, "r") as handle:
        assert handle["data"].shape == (2, 4)
        assert handle["data"].dtype == np.uint16


def test_metadata_is_carried_as_attributes(tmp_path):
    raw, meta = _recording(tmp_path)
    out = tmp_path / "r.h5"
    convert(raw, meta, out)
    with h5py.File(out, "r") as handle:
        assert handle.attrs["frame_rate_hz"] == pytest.approx(1000.0)
        assert handle.attrs["total_channels"] == 4
        assert handle.attrs["integrity_verdict"] == "clean"


def test_gaps_are_carried_across(tmp_path):
    raw, meta = _recording(tmp_path, drop_gap=True)
    out = tmp_path / "r.h5"
    convert(raw, meta, out)
    with h5py.File(out, "r") as handle:
        assert handle.attrs["integrity_verdict"] == "gaps_detected"
        assert handle["gaps"].shape[0] == 1


def test_convert_reports_what_it_did(tmp_path):
    raw, meta = _recording(tmp_path)
    out = tmp_path / "r.h5"
    report = convert(raw, meta, out)
    assert report["n_frames"] == 2
    assert report["raw_bytes"] == 32
    assert report["output_bytes"] > 0
    assert report["verdict"] == "clean"


def test_verify_accepts_a_good_conversion(tmp_path):
    raw, meta = _recording(tmp_path)
    out = tmp_path / "r.h5"
    convert(raw, meta, out)
    assert verify(out, raw, meta) is True


def test_verify_rejects_a_corrupted_conversion(tmp_path):
    raw, meta = _recording(tmp_path)
    out = tmp_path / "r.h5"
    convert(raw, meta, out)
    with h5py.File(out, "a") as handle:
        handle["data"][0, 0] = 999
    assert verify(out, raw, meta) is False


def test_real_fixture_round_trips(tmp_path):
    """The strongest test available: real recorded signal, in and out."""
    from tests.test_fixture_integrity import FIXTURE_DIR
    raw = FIXTURE_DIR / "sample_32ch_2s.raw"
    meta = FIXTURE_DIR / "sample_32ch_2s_meta.json"
    out = tmp_path / "fixture.h5"
    convert(raw, meta, out)
    assert verify(out, raw, meta) is True
    with h5py.File(out, "r") as handle:
        assert handle["data"].shape == (37115, 32)
        assert handle.attrs["integrity_verdict"] == "unknown"
```

- [ ] **Step 3: Run it to verify it fails**

Run: `python -m pytest tests/test_convert.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'biocam.convert'`

- [ ] **Step 4: Write the implementation**

```python
"""Converting a raw recording to HDF5.

Runs after a session, never during one. HDF5's chunking and index updates are
variable-latency work, which is exactly what must not happen on the acquisition
path - so recording writes flat bytes and conversion happens here, with the
experiment already over and the whole machine available.

No .brw compatibility is attempted: whether that schema is documented is an open
question with 3Brain (docs/vendor/3brain-correspondence.md). Producing files
that look compatible but are not would be worse than producing files that are
plainly ours.

Usage:
    python -m biocam.convert recording.raw recording_meta.json recording.h5
"""

import sys
from pathlib import Path

import h5py
import numpy as np

from biocam.data.frames import DTYPE_BY_BYTE_SIZE
from biocam.data.recording import integrity_verdict, read_sidecar

GAP_COLUMNS = ("after_frame", "missing_frames", "duration_ms")


def _load_raw(raw_path, meta):
    dtype = DTYPE_BY_BYTE_SIZE[meta["ch_sample_byte_size"]]
    n_channels = meta["total_channels"]
    flat = np.fromfile(raw_path, dtype=dtype)
    n_frames = len(flat) // n_channels
    return flat[: n_frames * n_channels].reshape(n_frames, n_channels)


def convert(raw_path, meta_path, out_path, compression: str = "gzip",
            level: int = 4) -> dict:
    """Write a raw recording and its sidecar into one HDF5 file."""
    raw_path, meta_path, out_path = Path(raw_path), Path(meta_path), Path(out_path)
    meta = read_sidecar(meta_path)
    data = _load_raw(raw_path, meta)
    verdict = integrity_verdict(meta)

    gaps = meta.get("integrity", {}).get("gaps", [])
    gap_rows = np.array(
        [[g["after_frame"], g["missing_frames"], g["duration_ms"]] for g in gaps],
        dtype=np.float64,
    ).reshape(-1, len(GAP_COLUMNS))

    with h5py.File(out_path, "w") as handle:
        handle.create_dataset(
            "data", data=data,
            chunks=(min(4096, data.shape[0]) or 1, data.shape[1]),
            compression=compression, compression_opts=level,
        )
        gap_set = handle.create_dataset("gaps", data=gap_rows)
        gap_set.attrs["columns"] = list(GAP_COLUMNS)

        for key, value in meta.items():
            if key == "integrity":
                continue
            if value is None:
                continue
            handle.attrs[key] = value
        handle.attrs["integrity_verdict"] = verdict
        for key in ("n_frames_missing", "driver_loss_events", "queue_overflows"):
            handle.attrs[key] = meta.get("integrity", {}).get(key, -1)

    return {
        "n_frames": int(data.shape[0]),
        "raw_bytes": raw_path.stat().st_size,
        "output_bytes": out_path.stat().st_size,
        "verdict": verdict,
    }


def verify(out_path, raw_path, meta_path) -> bool:
    """True if the HDF5 file reproduces the raw bytes exactly."""
    meta = read_sidecar(meta_path)
    expected = _load_raw(raw_path, meta)
    with h5py.File(out_path, "r") as handle:
        restored = handle["data"][:]
    return bool(restored.shape == expected.shape and np.array_equal(restored, expected))


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 3:
        print(__doc__)
        return 1
    raw_path, meta_path, out_path = argv
    report = convert(raw_path, meta_path, out_path)
    ratio = report["raw_bytes"] / report["output_bytes"] if report["output_bytes"] else 0
    print(f"{report['n_frames']:,} frames   "
          f"{report['raw_bytes']:,} -> {report['output_bytes']:,} bytes "
          f"({ratio:.2f}x)   integrity: {report['verdict']}")
    if not verify(out_path, raw_path, meta_path):
        print("VERIFICATION FAILED - the HDF5 file does not match the raw data")
        return 1
    print("Verified: the HDF5 file reproduces the raw data exactly.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 5: Run the tests**

Run: `python -m pytest tests/test_convert.py -v`
Expected: 8 passed

- [ ] **Step 6: Convert a real fixture from the command line**

Run: `python -m biocam.convert tests/fixtures/sample_32ch_2s.raw tests/fixtures/sample_32ch_2s_meta.json sample.h5`

Expected: a line reporting 37,115 frames, a compression ratio, `integrity: unknown`, then the verification line. Record the real output in your report, then delete `sample.h5` — it must not be committed.

- [ ] **Step 7: Commit**

```bash
git add biocam/convert.py tests/test_convert.py requirements.txt requirements-dev.txt
git commit -m "Add HDF5 converter with byte-exact verification"
```

---

### Task 9: Disk-space preflight

**Files:**
- Modify: `biocam/preflight.py`
- Modify: `tests/test_preflight.py`

**Interfaces:**
- Consumes: existing `CheckResult(name, ok, detail)`, `check_environment(dll_dir)`, `format_report(results)`.
- Produces: `bytes_per_second(total_channels, ch_sample_byte_size, frame_rate_hz) -> float`; `check_disk_space(directory, planned_seconds, bytes_per_sec) -> CheckResult`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_preflight.py`:

```python
def test_bytes_per_second_matches_the_reference_recording():
    from biocam.preflight import bytes_per_second
    rate = bytes_per_second(total_channels=4096, ch_sample_byte_size=2,
                            frame_rate_hz=18557.720703125)
    assert rate == pytest.approx(152_024_848, rel=1e-6)


def test_disk_check_passes_when_there_is_room(tmp_path):
    from biocam.preflight import check_disk_space
    result = check_disk_space(tmp_path, planned_seconds=1, bytes_per_sec=1000)
    assert result.ok is True
    assert "1,000" in result.detail or "1000" in result.detail


def test_disk_check_fails_when_the_requirement_exceeds_free_space(tmp_path):
    from biocam.preflight import check_disk_space
    result = check_disk_space(tmp_path, planned_seconds=10**9, bytes_per_sec=10**9)
    assert result.ok is False


def test_disk_check_names_the_directory_it_examined(tmp_path):
    from biocam.preflight import check_disk_space
    result = check_disk_space(tmp_path, planned_seconds=1, bytes_per_sec=1)
    assert str(tmp_path) in result.detail
```

Add `import pytest` at the top of the file if it is not already present.

- [ ] **Step 2: Run it to verify it fails**

Run: `python -m pytest tests/test_preflight.py -v`
Expected: FAIL with `ImportError: cannot import name 'bytes_per_second'`

- [ ] **Step 3: Add the implementation**

Append to `biocam/preflight.py`, before `main()`:

```python
def bytes_per_second(total_channels: int, ch_sample_byte_size: int,
                     frame_rate_hz: float) -> float:
    """Raw data rate of a recording, in bytes per second."""
    return total_channels * ch_sample_byte_size * frame_rate_hz


def check_disk_space(directory, planned_seconds: float,
                     bytes_per_sec: float) -> CheckResult:
    """Whether the drive holds a recording of the planned length.

    Losing the final hour of an experiment to a full disk is entirely
    preventable, and this is where it is prevented. See
    docs/lab/storage-setup.md for the arithmetic behind the rate.
    """
    directory = Path(directory)
    required = int(planned_seconds * bytes_per_sec)
    free = shutil.disk_usage(directory).free
    return CheckResult(
        f"disk space for {planned_seconds:g}s",
        free >= required,
        f"{free:,} bytes free in {directory}, {required:,} required",
    )
```

Add `import shutil` to the imports at the top of the file.

- [ ] **Step 4: Run the tests**

Run: `python -m pytest tests/test_preflight.py -v`
Expected: all pass, including the four new cases.

- [ ] **Step 5: Commit**

```bash
git add biocam/preflight.py tests/test_preflight.py
git commit -m "Add disk-space check to preflight"
```

---

### Task 10: Layer 1 interop

**Files:**
- Create: `biocam/interop/device.py`, `biocam/interop/source.py`

**Interfaces:**
- Consumes: `Packet` (Task 6); benchmark findings (Task 1).
- Produces: `load_assemblies(dll_dir)`; `BioCamDevice(dll_dir=None, timeout_sec=30)` as a context manager exposing `.biocam` and `.data_format`; `DriverPacketSource(device, queue_size=2000, listener=None)` with `.start(packet_timespan_ms=1)`, `.stop()`, iteration yielding `Packet`, and `.queue_overflows` / `.driver_loss_events`. Task 11 uses both.

**This task cannot be tested.** It is the only code in Phase 1 that cannot run here. Keep it thin, write nothing speculative, and verify every .NET member against `BioCam_DupleX_API/API/3Brain.BioCamDriver.xml` and `BioCam_DupleX_API/SampleApp_BioCamCL/MainForm.cs`.

- [ ] **Step 1: Write `biocam/interop/device.py`**

```python
"""Layer 1 - connecting to the BioCAM.

Nothing here can be executed without the instrument and the 3Brain DLLs. Every
.NET call is verified against API/3Brain.BioCamDriver.xml and the C# reference
sample rather than tested. Keep this module as small as it can be: it is the
only code in the acquisition path with no automated coverage.
"""

import time
from pathlib import Path

DEFAULT_DLL_DIR = (
    Path(__file__).resolve().parent.parent.parent / "BioCam_DupleX_API" / "API"
)

ASSEMBLIES = ("3Brain.Common", "3Brain.BioCamDriver")


def load_assemblies(dll_dir=None) -> None:
    """Load the 3Brain assemblies into the .NET runtime."""
    import os
    import sys

    dll_dir = Path(dll_dir or DEFAULT_DLL_DIR)
    os.environ["PATH"] = str(dll_dir) + os.pathsep + os.environ["PATH"]
    if str(dll_dir) not in sys.path:
        sys.path.insert(0, str(dll_dir))

    import pythonnet
    pythonnet.load("netfx")
    import clr

    for name in ASSEMBLIES:
        path = dll_dir / f"{name}.dll"
        if not path.is_file():
            raise FileNotFoundError(f"assembly not found: {path}")
        clr.AddReference(str(path))


class BioCamDevice:
    """Claims a BioCAM for the duration of a with-block."""

    def __init__(self, dll_dir=None, timeout_sec: int = 30):
        self._dll_dir = dll_dir
        self._timeout_sec = timeout_sec
        self._pool = None
        self._slot_index = -1
        self.biocam = None

    def __enter__(self):
        load_assemblies(self._dll_dir)
        from _3Brain.BioCamDriver import BioCamPool

        self._pool = BioCamPool
        BioCamPool.Activate()

        deadline = time.time() + self._timeout_sec
        while time.time() < deadline:
            free = list(BioCamPool.GetSlotIndexesFreeBioCam())
            if free:
                self._slot_index = free[0]
                break
            time.sleep(0.5)
        else:
            BioCamPool.Deactivate()
            raise TimeoutError(
                "No free BioCAM found. Check USB, power, and that BrainWave "
                "is closed - it holds the device."
            )

        self.biocam = BioCamPool.TakeBioCamControl(self._slot_index)
        if self.biocam is None:
            BioCamPool.Deactivate()
            raise RuntimeError(
                "TakeBioCamControl returned nothing. Close BrainWave or any "
                "other 3Brain software and try again."
            )
        if not self.biocam.IsConnected:
            self.__exit__(None, None, None)
            raise RuntimeError("BioCAM reports it is not connected.")
        if not self.biocam.MeaPlate.IsConnected:
            self.__exit__(None, None, None)
            raise RuntimeError("The MEA plate is not seated.")
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if self._slot_index >= 0:
                self._pool.ReleaseBioCamControl(self._slot_index)
        finally:
            self._slot_index = -1
            self.biocam = None
            self._pool.Deactivate()
        return False

    @property
    def data_format(self):
        return self.biocam.DataFormat
```

- [ ] **Step 2: Write `biocam/interop/source.py`**

Use the copy strategy Task 1 measured as fastest. The code below uses `bytes()`; **if the benchmark showed `Marshal.Copy` is materially faster, use that instead and say so in your report.**

```python
"""Layer 1 - packets from the instrument.

The callback does four things and returns: read the header, copy the payload,
put it on a bounded queue, return. Nothing else. No file I/O, no printing, no
allocation beyond the copy, no locks.

If the queue is full the packet is dropped and counted. Blocking the callback
would stall the driver and lose more than the packet being saved.
"""

import queue

from biocam.data.events import QueuePressure
from biocam.data.replay import Packet

STOP = object()

PRESSURE_FRACTION = 0.8


class DriverPacketSource:
    """Turns the driver's DataReceived event into an iterable of packets."""

    def __init__(self, device, queue_size: int = 2000, listener=None):
        self._device = device
        self._queue = queue.Queue(maxsize=queue_size)
        self._queue_size = queue_size
        self._listener = listener
        self._pressure_reported = False
        self._handler = None
        self._loss_handler = None
        self._error_handler = None
        self.queue_overflows = 0
        self.driver_loss_events = 0
        self._streaming = False

    def start(self, packet_timespan_ms: int = 1) -> None:
        biocam = self._device.biocam

        def on_data(_sender, args):
            try:
                self._queue.put_nowait(Packet(
                    timestamp=args.Header.Timestamp,
                    counter=args.Header.PacketCounter,
                    payload=bytes(args.Payload),
                ))
            except queue.Full:
                self.queue_overflows += 1

        def on_loss(_sender, args):
            self.driver_loss_events += 1

        def on_error(_sender, _args):
            self._queue.put(STOP)

        self._handler = on_data
        self._loss_handler = on_loss
        self._error_handler = on_error

        biocam.DataReceived += self._handler
        biocam.DataLossAsync += self._loss_handler
        biocam.DataStreamingError += self._error_handler

        started = biocam.StartDataStreaming(
            dataPacketTimeSpanMs=packet_timespan_ms,
            optimizeDataPacketLatency=True,
        )
        if not started:
            raise RuntimeError("StartDataStreaming failed.")
        self._streaming = True

    def stop(self) -> None:
        biocam = self._device.biocam
        if self._streaming:
            biocam.StopDataStreaming()
            self._streaming = False
        for event, handler in (
            ("DataReceived", self._handler),
            ("DataLossAsync", self._loss_handler),
            ("DataStreamingError", self._error_handler),
        ):
            if handler is not None:
                try:
                    setattr(biocam, event, getattr(biocam, event).__isub__(handler))
                except Exception:
                    pass
        self._queue.put(STOP)

    def __iter__(self):
        threshold = int(self._queue_size * PRESSURE_FRACTION)
        while True:
            item = self._queue.get()
            if item is STOP:
                return
            depth = self._queue.qsize()
            if depth >= threshold and not self._pressure_reported:
                self._pressure_reported = True
                if self._listener is not None:
                    self._listener(QueuePressure(depth=depth,
                                                 capacity=self._queue_size))
            elif depth < threshold // 2:
                self._pressure_reported = False
            yield item
```

- [ ] **Step 3: Confirm the guard still passes**

Run: `python -m pytest -q`
Expected: all tests pass. `biocam/interop/` is exempt from the import guard; if anything fails, a forbidden import has leaked outside that package.

- [ ] **Step 4: Run the API verifier**

This is Gate 1 from `CLAUDE.md` and is not optional for Layer 1 code. Dispatch the `biocam-api-verifier` subagent against `biocam/interop/device.py` and `biocam/interop/source.py`. Record its findings verbatim in your report. Fix anything it reports as wrong before committing.

- [ ] **Step 5: Commit**

```bash
git add biocam/interop/device.py biocam/interop/source.py
git commit -m "Add Layer 1 device connection and packet source"
```

---

### Task 11: Command line

**Files:**
- Create: `biocam/cli.py`
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: everything above.
- Produces: `build_parser() -> argparse.ArgumentParser`; `main(argv=None) -> int`.

**Critical constraint.** `biocam/cli.py` is top-level and therefore guarded. It must import `biocam.interop` **inside** the function that needs it, never at module scope — otherwise importing the CLI pulls in `clr` and the suite stops running on a machine without the DLLs.

- [ ] **Step 1: Write the failing test**

```python
import pytest

from biocam.cli import build_parser, main


def test_parser_accepts_a_duration():
    args = build_parser().parse_args(["record", "--duration", "30"])
    assert args.duration == 30.0


def test_parser_accepts_run_until_stopped():
    args = build_parser().parse_args(["record"])
    assert args.duration is None


def test_parser_has_an_output_directory_default():
    args = build_parser().parse_args(["record"])
    assert args.output_dir == "recordings"


def test_parser_accepts_convert():
    args = build_parser().parse_args(["convert", "a.raw", "a_meta.json", "a.h5"])
    assert args.raw == "a.raw"
    assert args.out == "a.h5"


def test_importing_the_cli_does_not_load_interop():
    """The guard's whole purpose: the CLI must be importable with no DLLs."""
    import sys
    assert "clr" not in sys.modules
    assert "pythonnet" not in sys.modules


def test_convert_command_runs_without_hardware(tmp_path):
    import numpy as np
    from biocam.data.recording import AcquisitionParameters, RecordingWriter

    params = AcquisitionParameters(
        frame_rate_hz=1000.0, total_channels=4, ch_sample_byte_size=2, bit_depth=12,
        adc_counts_to_value=1.0, offset=0.0, min_digital_value=0, max_digital_value=4095,
    )
    raw, meta = tmp_path / "r.raw", tmp_path / "r_meta.json"
    with RecordingWriter(raw, meta, params) as writer:
        writer.write_packet(1, 1, np.arange(8, dtype=np.uint16).tobytes())
        writer.finalise("duration_reached")

    exit_code = main(["convert", str(raw), str(meta), str(tmp_path / "r.h5")])
    assert exit_code == 0
    assert (tmp_path / "r.h5").exists()


def test_unknown_command_returns_an_error_code():
    with pytest.raises(SystemExit):
        main(["nonsense"])
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python -m pytest tests/test_cli.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'biocam.cli'`

- [ ] **Step 3: Write the implementation**

```python
"""Command line for recording and conversion.

    python -m biocam.cli record --duration 60
    python -m biocam.cli record                     (until Ctrl+C)
    python -m biocam.cli convert in.raw in_meta.json out.h5

This module must NOT import biocam.interop at module scope. Doing so would pull
clr into any process that imports the CLI, and the suite would stop running on a
machine without the 3Brain DLLs. The import happens inside record_command().
"""

import argparse
import shutil
import threading
import time
from pathlib import Path

from biocam.data.events import DiskLow, describe
from biocam.data.recording import AcquisitionParameters, RecordingWriter
from biocam.preflight import bytes_per_second, check_disk_space
from biocam.session import record_session


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="biocam")
    sub = parser.add_subparsers(dest="command", required=True)

    record = sub.add_parser("record", help="record from the instrument")
    record.add_argument("--duration", type=float, default=None,
                        help="seconds to record; omit to run until stopped")
    record.add_argument("--name", type=str, default=None,
                        help="base name for the output files")
    record.add_argument("--output-dir", type=str, default="recordings")
    record.add_argument("--packet-ms", type=int, default=1,
                        help="acquisition period in milliseconds")

    convert = sub.add_parser("convert", help="convert a recording to HDF5")
    convert.add_argument("raw")
    convert.add_argument("meta")
    convert.add_argument("out")

    return parser


def _parameters_from(data_format) -> AcquisitionParameters:
    return AcquisitionParameters(
        frame_rate_hz=data_format.FrameRate,
        total_channels=data_format.NWells * data_format.NChsPerWell,
        ch_sample_byte_size=data_format.ChSampleByteSize,
        bit_depth=data_format.BitDepth,
        adc_counts_to_value=data_format.ADCCountsToValue,
        offset=data_format.Offset,
        min_digital_value=data_format.MinDigitalValue,
        max_digital_value=data_format.MaxDigitalValue,
    )


def record_command(args) -> int:
    from biocam.interop.device import BioCamDevice
    from biocam.interop.source import DriverPacketSource

    base = args.name or time.strftime("%Y%m%d_%H%M%S")
    out_dir = Path(args.output_dir)
    raw_path = out_dir / f"{base}.raw"
    meta_path = out_dir / f"{base}_meta.json"

    stop = threading.Event()
    report = lambda event: print(describe(event))

    with BioCamDevice() as device:
        params = _parameters_from(device.data_format)

        if args.duration is not None:
            out_dir.mkdir(parents=True, exist_ok=True)
            rate = bytes_per_second(params.total_channels,
                                    params.ch_sample_byte_size,
                                    params.frame_rate_hz)
            space = check_disk_space(out_dir, args.duration, rate)
            if not space.ok:
                free = shutil.disk_usage(out_dir).free
                report(DiskLow(free_bytes=free,
                               required_bytes=int(args.duration * rate)))
                return 1

        source = DriverPacketSource(device, listener=report)
        source.start(packet_timespan_ms=args.packet_ms)
        try:
            with RecordingWriter(raw_path, meta_path, params,
                                 listener=report) as writer:
                try:
                    result = record_session(source, writer,
                                            duration_sec=args.duration,
                                            stop_event=stop)
                except KeyboardInterrupt:
                    stop.set()
                    result = record_session(source, writer, stop_event=stop)
                writer.note_driver_loss(source.driver_loss_events)
                writer.note_queue_overflow(source.queue_overflows)
        finally:
            source.stop()

    return 0 if result.verdict == "clean" else 2


def convert_command(args) -> int:
    from biocam.convert import main as convert_main
    return convert_main([args.raw, args.meta, args.out])


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "record":
        return record_command(args)
    return convert_command(args)


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run the tests**

Run: `python -m pytest tests/test_cli.py -v`
Expected: 7 passed

- [ ] **Step 5: Run the whole suite and confirm the guard**

Run: `python -m pytest -q`
Expected: all pass. Then confirm the CLI is importable with no interop loaded:

Run: `python -c "import sys, biocam.cli; print('clr' in sys.modules)"`
Expected: `False`

- [ ] **Step 6: Commit**

```bash
git add biocam/cli.py tests/test_cli.py
git commit -m "Add command line for recording and conversion"
```

---

## Not covered by this plan

- **Stimulation.** Appendix A defect 3 (`Stimulator.Start()` never called) belongs to Phase 2.
- **Legacy script removal.** `connector.py`, `recorder.py` and `Hello_BioCam.py` stay untouched as the fallback until the new path is lab-verified (spec §9).
- **README updates.** The README still documents the legacy recorder. It is rewritten in the follow-up that lands after a successful lab session, when there is something verified to document.
- **Safety stops and file rollover**, deferred by decision.

## Done when

- `python -m pytest` and `pytest` both pass with no BioCAM and no 3Brain DLLs.
- A replayed fixture with an injected gap produces a sidecar naming the gap's position, size and duration.
- A sidecar without `schema_version` reports `unknown`, never `clean`.
- `python -m biocam.convert` round-trips a real fixture and verifies byte-identical.
- `python -c "import sys, biocam.cli; print('clr' in sys.modules)"` prints `False`.
- `biocam-api-verifier` has reviewed both Layer 1 files and its findings are recorded.
- Nothing in `git status` is uncommitted.
