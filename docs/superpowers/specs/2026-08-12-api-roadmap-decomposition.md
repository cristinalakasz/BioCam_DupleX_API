# API roadmap: decomposition and hardware findings

**Date:** 2026-08-12
**Status:** Decomposition agreed. Phases 0–2 built and merged; Phase 3 in
progress. Section 4's open item was closed on 2026-08-17 by reflecting over the
assembly; section 6 records what Phase 3 found in its place.
**Purpose:** Records the build order and the stimulator capabilities discovered
while decomposing, so that later phases start from findings rather than
rediscovering them.

This is not itself an implementation spec. Each phase below gets its own.

---

## 1. What was requested

Custom stimulation; continuous recording while stimulation changes; custom
recording modes; saving recordings; a UI.

Stimulation changes were confirmed to be needed in all three forms:

| Mode | What decides timing | Timing pressure |
|---|---|---|
| (a) Manual | a human clicking | none — human reaction time ≈200 ms |
| (b) Scheduled | a protocol timeline | mild |
| (c) Closed-loop | the signal itself | severe — sub-millisecond, inside the data path |

These are **not three features**. They are three sources of one action: *send a
stimulus to these endpoints with these parameters*. One stimulation engine serves
all three; only the trigger differs.

(a) and (b) may route through an ordinary queue. (c) may not — to reach the
≈1.5 ms the hardware permits, the decision must happen inside the acquisition
path, as 3Brain's C# sample does by calling `Send` from the thread its data
callback wakes. The engine therefore needs a normal path and a fast path.

(c) additionally depends on spike detection existing. That dependency, not
preference, places it last.

---

## 2. Build order

| Phase | Contents | Status |
|---|---|---|
| **0** | Setup: `CLAUDE.md`, verifier agents, test scaffolding, README | Specced 2026-08-03. **Done** (2026-08-12). |
| **1** | Acquisition: recording, saving, data integrity | Specced and **merged** (2026-08-13). Gate 1 ran afterwards and did **not** come back clean — see `2026-08-13-phase1-followups.md`. Not cleared for the lab. |
| **2** | Stimulation engine + manual (a) + scheduled (b) | **Merged** (2026-08-17, PR #25). `biocam/stim/` (Layer 2: pulses, patterns, trains, sequences, validation) and `biocam/interop/stimulator.py` (Layer 1: lifecycle and the three `Send` paths). Gate 1 clean. Not cleared for the lab — issues #21–#24. |
| **3** | Session control: recording and stimulation together, changing live | **In progress** (2026-08-17). `biocam/data/clock.py` (the acquisition clock scheduled stimulation needs a time origin from) and `biocam/stim/log.py` (the stimulus record every later analysis depends on). Not cleared for the lab. |
| **4** | UI | |
| **5** | Spike detection | |
| **6** | Closed-loop (c) | Depends on 5 |

Phase 1 precedes stimulation because everything calls into it, because it fixes
the eleven defects catalogued in the setup spec's Appendix A as a consequence of
restructuring, and because it is the only phase fully verifiable on the
development laptop using the existing recording.

---

## 3. Stimulator capabilities (from `3Brain.BioCamDriver.xml`)

Discovered during decomposition. Materially larger than `recorder.py` or the C#
sample suggest.

### Three send paths

| Signature | Use |
|---|---|
| `Send(pulse, pos, neg)` | immediate |
| `Send(pulse, pos, neg, out ulong latency)` | immediate, reports latency in clock cycles — the closed-loop path |
| `Send(pulse, pos, neg, double[] timestamps)` | **schedules stimuli at future times** |

### On-device protocol engine

`IBioCamStimProtocolManager`: `InitializeProtocols(n)`, `LoadProtocol(i, p)`,
`StartProtocol(i)`, `StopProtocol(i)`, `ResetProtocol(i)`, `GetProtocolStatus(i)`,
`MaxProtocols`.

`IStimProtocol`: `Name`, `IsDynamic`, `WellsIndexes`, `PositiveEndPoints`,
`NegativeEndPoints`, `Pulses`, `TimestampsMicroSec`.

`StimTrainProtocol` provides `NewByPulseRate(...)` and `NewByPulsePeriod(...)`.

**A dynamic protocol pairs one pulse configuration with each timestamp**
(`Pulses.Length == TimestampsMicroSec.Length`). A static one reuses a single
pulse across all timestamps.

### Decision: schedule on the hardware, not in Python

Phase 2 must implement mode (b) by programming the timeline into the instrument,
**not** by driving a Python timer.

The stimulator's resolution is 10 µs. A Python loop on Windows drifts and can be
preempted for milliseconds, and 3Brain's own documentation states Windows is not
a real-time OS and that latency fluctuates with processor load. Hardware-executed
protocols are both more accurate and immune to the controlling software stalling.

### The requested capabilities map onto one structure

A protocol is *(endpoints) + (pulses[]) + (timestamps[])*:

- single pulse — one timestamp, one pulse
- regular train — N timestamps evenly spaced, one pulse reused
- arbitrary timed sequence — N timestamps, N pulses, `IsDynamic`

Built once, "custom stimulation" becomes filling in a table rather than writing
code per protocol type.

### Changing spatial patterns is the exception

`IStimProtocol` carries **one** endpoint set for the whole protocol; electrodes
cannot vary within a single protocol. Changing patterns therefore requires either
several protocols run in sequence, or repeated `Send` calls each carrying
different endpoints, queued in advance.

Costs and limits, to be respected by Phase 2:

- Per `Send`: **≤64 pulse values, ≤288 endpoint values**. Buffer overflow is
  silent — the documentation states the *next* invocation's values are ignored.
- Per spatial configuration: ≤1000 endpoints; positive and negative endpoints may
  never share an electrode column.
- ≤1000 queued future stimuli. Concurrently buffered spatial configurations are
  guaranteed ≥4, and up to ~50 when each uses two electrodes.
- Chip reconfiguration between differing configurations costs
  **26 µs + 8.4 µs × (rows − 1)**, which bounds how fast patterns can cycle.

---

## 4. Resolved: the undocumented pulse parameters

`RectangularStimPulse` and `StimProperties` belong to the `_3Brain.Common`
assembly, for which this repository holds no XML. This was recorded as an open
item to be settled by reflection, since loading an assembly needs no device.

**Settled on 2026-08-17.** `python -m biocam.interop.reflect` reads the members
directly off the DLL; the full surface is recorded in
`docs/api/stimulation-reference.md`. The pulse is:

```
RectangularStimPulse(String friendlyName, StimProperties constraints,
                     Double amplitude1, Int32 width1, Int32 interWidth,
                     Double amplitude2, Int32 width2)
```

Amplitudes are µA (`IsCurrentStimulator = True`); widths are integer counts of
`TimeResolutionMicroSec`, not microseconds.

Reflection settled the shape. Probing settled the behaviour, and the behaviour
turned out to be the real finding: **the driver adjusts invalid pulses instead
of rejecting them.** Amplitudes are clamped and rounded; a pulse longer than
`MaxPulseDuration` has its *later* phases shortened, so `8000/0/8000` comes back
as `8000/0/2000` — a charge-balanced request silently delivering net DC, with
`IsBiphasic` still true. Nothing raises.

That is what `biocam/stim/` was built to prevent, and it is why Phase 2 gained a
validation layer that was not in the original decomposition.

---

## 5. UI constraints (for Phase 4)

The driver runs only on the machine physically connected to the BioCAM, so the UI
runs on the lab workstation.

Operated by **the on-site colleague first, the author remotely later**. The
stricter constraint governs: it must be usable by someone who did not write it
and cannot ask questions mid-experiment. Guardrails, clear feedback, and controls
that are hard to misuse are requirements, not decoration.

Note for closed-loop work: timing must not be measured during an active
remote-desktop session, since screen encoding loads the same CPU the process sets
to `RealTime` priority.

---

## 6. Phase 3 note: where acquisition time comes from

Phase 2 shipped `biocam stim --count N` unable to run, for one reason: the
driver's scheduled-stimulation overload takes timestamps "relative to the
beginning of the acquisition", and nothing here could say how far into an
acquisition it was.

The answer had been flowing past since Phase 1. `DataPacketHeader.Timestamp`
is documented as *"the timestamp of the data packet in number of BioCAM's
clock cycles or 0 when the timestamp is not available"*, and
`biocam/interop/source.py` puts it on every `Packet`. Nothing consumed it.

`biocam/data/clock.py` does. Three things it must get right, each a way of
being silently wrong rather than loudly:

1. **A timestamp of 0 is a sentinel, not a time.** Reading it as "the
   acquisition just started" places a stimulus at the beginning of a recording
   that may be hours old.
2. **Lost frames still count as elapsed time.** The instrument kept acquiring
   through the gap; counting only what arrived schedules early by exactly the
   duration of the loss.
3. **Two independent estimates, cross-checked.** The device's timestamps and
   our frame count should agree. `schedule_after()` refuses when they do not,
   rather than picking one.

### Still an inference

That `DataPacketHeader.Timestamp` and the stimulation timestamps share an
origin. Both are documented "relative to the beginning of the acquisition",
but that they mean the same instant is not stated anywhere. If they differ by
a constant offset, every scheduled train is wrong by it. Issue #24.

### What Phase 3 still owes

- Reading `IBioCam.ClockCyclesToMilliseconds` to get the real
  cycles-per-microsecond factor, rather than calibrating it.
- A combined session: one process recording while a control thread stimulates.
  The open question there is whether `Send` may be called from a thread other
  than the one the data callback wakes — 3Brain's sample only ever does the
  latter, so the safe pattern is unestablished.
