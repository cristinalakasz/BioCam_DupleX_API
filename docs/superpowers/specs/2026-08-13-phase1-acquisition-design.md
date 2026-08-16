# Phase 1 — Acquisition

**Date:** 2026-08-13
**Status:** Implemented and merged 2026-08-13 (PR #9). Gate 1 ran after the merge
and returned findings — the code is **not cleared for a lab session**. See
`2026-08-13-phase1-followups.md`.
**Predecessor:** Phase 0 setup, `2026-08-03-claude-project-setup-design.md`
**Roadmap:** `2026-08-12-api-roadmap-decomposition.md` §2

Rebuilds the recording path on the three-layer split. Closes eight of the
eleven defects catalogued in the Phase 0 spec's Appendix A, most of them as a
consequence of the structure rather than as individual patches. Defects 3, 6
and 11 are deferred or unverified rather than closed - see §10.

---

## 1. The problem being solved

The current recorder produces files whose completeness cannot be established.
It never reads the hardware timestamp, never subscribes to the driver's data-loss
event, and appends packets contiguously — so a dropped packet shifts every
subsequent frame with nothing recording that it happened. The existing 1.4 GB
recording cannot be shown to be intact, and neither could any future one.

It also performs disk I/O inside the acquisition callback, which is the most
likely cause of the loss it cannot detect.

**The goal of Phase 1 is that every finished recording carries an honest account
of its own integrity, and that the acquisition path stops causing the problem it
cannot see.**

---

## 2. Decisions taken

| Question | Decision |
| --- | --- |
| On detecting loss mid-recording | **Continue recording and mark the gap.** Never destroy data; make the problem visible. Refinable later. |
| Recording control | **Both** fixed duration and run-until-stopped, chosen per session. |
| Problem reporting | **Structured events**, not printing. The CLI renders them now; the Phase 4 UI subscribes to the same stream. |
| Recorder architecture | **Byte-through.** See §3. |
| Safety stops, file rollover | **Out of scope.** Deferred as a future issue. |

---

## 3. The central decision: the recorder does not decode

The current code converts each payload into a numpy array and then writes those
bytes to disk. The bytes written are the bytes received — the decode is pure
waste, and it is the direct cause of Appendix A defect 5: decoding per packet
forces handling of payloads ending mid-frame, and the current code discards the
remainder, desynchronising everything after it.

Appending payload bytes as they arrive produces the exact concatenation of every
payload, which *is* a valid frame-major stream. **The defect becomes impossible
rather than fixed**, because the situation that produces it never arises.

Decoding still matters, in two places that are not the recorder:

- **Reading a file**, where the whole stream is present and carry-over is not a
  concept.
- **Online processing in Phase 5**, where packets genuinely are decoded as they
  arrive. That is where carry-over logic belongs, and it is testable with
  synthetic buffers.

Rejected alternatives: decoding then writing (spends CPU converting bytes into
the same bytes, keeps the defect class alive); writing HDF5 live (chunking and
index updates are variable-latency work on a path that must never block).

---

## 4. Components

### Layer 2 — pure logic, fully tested with synthetic data

| File | Responsibility |
| --- | --- |
| `biocam/data/frames.py` | Decode payload bytes to frames, carrying a partial frame across packet boundaries. ADC counts to µV. Used by readers and by Phase 5 — **not** by the recorder. |
| `biocam/data/integrity.py` | Gap detection from packet counters and timestamps. Produces gap records. |
| `biocam/data/recording.py` | `RecordingWriter` — accepts `(timestamp, counter, payload)`, appends bytes, tracks gaps, writes the sidecar. `RecordingReader` — loads a raw + sidecar pair. |
| `biocam/data/replay.py` | `ReplayPacketSource` — emits a `.raw` file as packets, with optional injected gaps and overflows. See §7. |
| `biocam/data/events.py` | The event types a session emits. |

`RecordingWriter` touches no hardware: a test hands it packets. **The entire
integrity mechanism is therefore testable without an instrument.**

### Layer 1 — interop, reviewed rather than run

| File | Responsibility |
| --- | --- |
| `biocam/interop/device.py` | Load DLLs, activate `BioCamPool`, find and claim a device, release cleanly. |
| `biocam/interop/source.py` | `DriverPacketSource` — subscribes `DataReceived`, `DataLossAsync`, `DataStreamingError`; the callback reads header fields, copies the payload, and pushes to a bounded queue. |
| `biocam/interop/benchmark.py` | Measures payload-copy strategies. Needs the DLLs but **no device** — see §8. |

### Orchestration and entry points

| File | Responsibility |
| --- | --- |
| `biocam/session.py` | Drives a recording: takes a packet source, runs the writer thread, honours duration or stop signal, emits events. **Takes a source rather than a device**, so it is testable — see §7. |
| `biocam/cli.py` | Command line. Imports interop **inside** the function, never at module level. |
| `biocam/convert.py` | Offline raw + sidecar → HDF5, verify, report. No instrument code. |
| `biocam/preflight.py` | *(modify)* Add a free-disk-space check for a planned duration. |

**`cli.py` must not import `interop` at module scope.** Doing so would pull `clr`
into any process that imports the CLI, and `tests/test_no_hardware_imports.py`
would fail on a machine without the DLLs. The constraint is the guard working as
designed.

---

## 5. File format

### The raw file is unchanged

Frame-major `uint16`, little-endian, `total_channels × 2` bytes per frame.

Deliberate: the existing recording, both committed fixtures, and
`tests/test_fixture_integrity.py` all depend on this layout. The layout was never
the problem — everything around it was.

### The sidecar carries the integrity record

Schema version 2. Replaces the current `packet_log`, whose wall-clock entries
cannot detect anything, because wall-clock time says nothing about whether
hardware samples went missing.

```json
{
  "schema_version": 2,
  "frame_rate_hz": 18557.720703125,
  "total_channels": 4096,
  "ch_sample_byte_size": 2,
  "adc_counts_to_value": 2.0146520146520146,
  "offset": -4125.0,
  "min_digital_value": 0,
  "max_digital_value": 4095,

  "status": "complete",
  "stop_reason": "duration_reached",
  "started_utc": "2026-08-13T14:06:15Z",
  "n_frames_written": 184473,
  "duration_sec": 9.94,

  "integrity": {
    "verdict": "gaps_detected",
    "first_timestamp": 143000112,
    "last_timestamp": 143185021,
    "n_frames_missing": 371,
    "gaps": [
      {"after_frame": 92100, "missing_frames": 371, "duration_ms": 19.99}
    ],
    "driver_loss_events": 1,
    "queue_overflows": 0
  }
}
```

`status` is one of `in_progress`, `complete`, `failed`.
`stop_reason` is one of `duration_reached`, `user_stopped`, `error`,
`source_exhausted` (the packet source ran out on its own - a replay reaching
the end of its file, or a driver session that stopped supplying packets
without an error being reported).
`verdict` is one of `clean`, `gaps_detected`, `unknown`.

`first_timestamp` and `last_timestamp` are the instrument's own clock values in
**clock cycles**, recorded verbatim. They are not frame indices and no arithmetic
relation to `n_frames_written` should be assumed — converting cycles to time
requires the driver's own conversion, which is why the durations in `gaps` are
stored already converted to milliseconds.

**Gaps are recorded sparsely** — one entry per gap, never per packet. At 1 ms
packets an hour is 3.6 million packets; a per-packet log would exceed the useful
size of a metadata file. A clean recording records an empty list.

### Three properties that are requirements, not details

**A missing integrity record means `unknown`, never `clean`.** Files without
`schema_version` — the existing recording and both fixtures — must read as
`unknown`. Defaulting to "no gaps recorded, therefore no gaps" would launder an
unverifiable file into a trusted one.

**The sidecar is written twice.** At start with `status: "in_progress"`, and
again at the end with the final record. A killed process or crashed machine then
leaves a raw file with its acquisition parameters and an honest marker that it
was never finalised.

**The three loss signals stay separate** (§6), because they have different causes
and different fixes.

### HDF5 output

One dataset `/data`, shape `(n_frames, n_channels)`, `uint16`, chunked and
compressed. Every sidecar field carried as attributes; gaps as a small dataset.

**No `.brw` compatibility is attempted.** Whether the schema is documented is
question 6 in `docs/vendor/3brain-correspondence.md` and unanswered. Guessing at
a proprietary layout would produce files that appear compatible and are not,
which is worse than files that are plainly ours. If 3Brain replies, matching it
becomes a small, well-defined follow-up.

---

## 6. Data flow and loss detection

### Inside the callback

1. Read `Header.Timestamp` and `Header.PacketCounter`
2. Copy the payload into a buffer from a preallocated pool
3. Non-blocking put onto a bounded queue
4. Return

**If the queue is full, drop and count — never block.** Blocking the callback
stalls the driver and loses more than the packet being saved. A counted drop is
recoverable information; a stalled acquisition thread is a cascade.

Nothing else runs here: no file I/O, no printing, no logging, no numpy, no locks.

### Detection uses the packet counter

`DataPacketHeader` carries `PacketCounter` as a `UInt16` — confirmed from its
constructor signature in `3Brain.BioCamDriver.xml`:
`#ctor(Byte, BioCamUsbComSignalType, Int32, UInt64, UInt16)`, being
`Reserved, SignalType, PayloadLength, Timestamp, PacketCounter`.

```
delta = (counter - previous_counter) mod 65536
delta == 1  ->  contiguous
delta  > 1  ->  (delta - 1) packets lost
```

Exact, with no clock arithmetic and no tolerance threshold. The **timestamp**
then gives the gap's duration in real time, and frames missing is derived from
the packet size, which is fixed for a session.

The counter wraps at 65536. That wrap is the kind of edge case that works for
hours and then does not, so it gets an explicit test with synthetic packets
crossing the boundary.

### Three independent signals, kept separate

| Signal | Source | Means |
| --- | --- | --- |
| `PacketCounter` jump | the instrument | packets never reached us |
| `DataLossAsync` | the driver | the driver knows it dropped data |
| queue overflow | our writer | our software could not keep up |

Only the third is fixed by a faster disk. Collapsing them into "data loss" would
hide which part failed.

### Events

`RecordingStarted`, `GapDetected`, `QueuePressure`, `QueueOverflow`,
`DriverDataLoss`, `DiskLow`, `RecordingStopped`.

Emitted to an optional listener. The CLI renders them; Phase 4's UI subscribes to
the same stream. Printing from inside the recorder would both violate the
callback rule and leave the UI nothing to consume.

Queue default: approximately two seconds of buffering — about 300 MB at full
rate, which is why 32 GB of RAM appears in `docs/lab/storage-setup.md`.

---

## 7. Packet sources

`session.py` takes a **packet source** — anything yielding
`(timestamp, counter, payload)` — rather than a device.

- **`DriverPacketSource`** wraps `DataReceived`. Not testable here.
- **`ReplayPacketSource`** reads a `.raw` fixture and emits it as packets, with
  optional injected gaps and overflows. Pure Layer 2.

This makes the entire session testable end to end — start, stream, detect a gap,
write, finalise, stop — leaving only the driver wrapper unproven. Layer 1 shrinks
to roughly a hundred mechanical lines, all checkable against the XML.

It is the same technique as the fixtures, applied to control flow instead of data.

---

## 8. Testing

### Proven here

- **Loss detection**: clean run; one gap; several gaps; a gap on the first
  packet; **counter wrap 65535 → 0**; queue overflow. Each asserts exact sidecar
  contents, not merely that an error occurred.
- **Decoding**: payload ending mid-frame; zero-length payload; values at both
  ends of 0–4095; µV conversion against hand-computed values.
- **Backward compatibility**: a schema-1 sidecar yields `verdict: "unknown"`,
  never `"clean"`.
- **Round trip**: `sample_32ch_2s` → HDF5 → back, byte-identical.
- **End to end**: `ReplayPacketSource` through the real writer to a real file,
  converted and read back.
- **Preflight**: refuses a planned duration that will not fit on the drive.

### The payload-copy benchmark comes first

`bytes(e.Payload)` copies the whole buffer through a Python object every packet.
`Marshal.Copy` into a preallocated buffer is the cheaper route. Which is fast
enough cannot be reasoned about — it must be measured.

**It can be measured here.** Loading the 3Brain assemblies requires no device, and
the DLLs are on the development machine. `biocam/interop/benchmark.py` creates a
.NET `byte[]` of realistic size and times each strategy.

This is the **first task of the implementation plan**: if the simple approach is
fast enough the callback simplifies, and if it is not, that must be known before
the callback is written rather than after.

### Verifiable only in the lab

Goes verbatim into the first recording's handover note, per Gate 2 item 5:

- The DLLs load on that machine; the device is found and claimed
- `Header.Timestamp` and `Header.PacketCounter` contain what we believe
- `PacketCounter` increments by exactly one per packet
- `DataLossAsync` fires when data is lost
- `StartDataStreaming(dataPacketTimeSpanMs:, optimizeDataPacketLatency: true)` is accepted
- The payload copy is fast enough at the real data rate
- Two seconds of queue is sufficient buffer
- Process and thread priority behave as intended

### First-session protocol

Ten minutes of the colleague's time: run preflight, record ten seconds, send back
the sidecar.

`verdict: "clean"` with a plausible timestamp range and frame count validates the
whole chain. `gaps_detected` on an idle ten-second recording means something is
wrong **and the sidecar names which of the three signals fired**.

The first lab session should return a diagnosis, not "it didn't work."

---

## 9. Legacy scripts

`BioCam_DupleX_API/connector.py`, `recorder.py` and `Hello_BioCam.py` are
**kept, unmodified, until the new path is verified in the lab.**

They are the fallback. Removing a working recorder before its replacement has
touched hardware would leave no way to record if the new code fails on first
contact. They are deleted in the follow-up that lands after a successful session,
and the README's §7 is updated then.

---

## 10. Defects closed

From the Phase 0 spec's Appendix A:

| # | Defect | Status |
| --- | --- | --- |
| 1 | Hardware timestamp never read | Closed |
| 2 | Data loss silent | Closed — three signals, recorded in the sidecar |
| 3 | Stimulator never started | **Deferred to Phase 2** — stimulation is out of scope here |
| 4 | Disk I/O inside the callback | Closed — queue and writer thread |
| 5 | Partial frames discarded | Closed structurally — §3 |
| 6 | Payload copied through `bytes()` | Designed for; **not closed until measured at the real rate** |
| 7 | Attribute name guessed | Closed — `Header` and `Payload` used explicitly |
| 8 | `optimizeDataPacketLatency` unused | Closed |
| 9 | Inconsistent pythonnet initialisation | Closed — one `device.py` |
| 10 | `import threading` after use | Closed — rewritten |
| 11 | Process and thread priority not raised | **Not done** — nothing in this branch sets process or thread priority; belongs on the Gate 2 untested list |

**Eight closed outright.** Defect 3 belongs to Phase 2. Defect 6 is designed for
but cannot honestly be called closed until it has been measured at the real data
rate, which needs the instrument — so it belongs on the Gate 2 untested list, not
in the closed column. Defect 11 was never attempted in this branch — no code
here sets process or thread priority — so it joins defect 6 on that same
untested list rather than being counted as closed.

---

## 11. Open items

- **3Brain question 2** — how long the instrument buffers if the host stalls —
  would let the queue depth be chosen rather than estimated. Two seconds is a
  reasoned guess until then.
- **3Brain question 6** — `.brw` schema — would turn HDF5 output into something
  BrainWave and SpikeInterface can read.
- **`FTD3XX_NET.dll`** is referenced by the C# project, absent from the SDK
  folder, and unchecked by preflight. Question 5. If it proves required,
  preflight must check for it.
- Safety stops and file rollover, deferred by decision.
