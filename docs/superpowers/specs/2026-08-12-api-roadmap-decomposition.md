# API roadmap: decomposition and hardware findings

**Date:** 2026-08-12
**Status:** Decomposition agreed. Phase 0 selected to build first.
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
| **0** | Setup: `CLAUDE.md`, verifier agents, test scaffolding, README | Specced 2026-08-03. **Building first.** |
| **1** | Acquisition: recording, saving, data integrity | Next to spec |
| **2** | Stimulation engine + manual (a) + scheduled (b) | |
| **3** | Session control: recording and stimulation together, changing live | |
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

## 4. Open item: undocumented pulse parameters

`RectangularStimPulse` and `StimProperties` belong to the `_3Brain.Common`
assembly. The repository contains only `3Brain.BioCamDriver.xml`; there is no
`3Brain.Common.xml`. The actual pulse parameters — amplitude, phase durations,
polarity — are therefore undocumented in available material.

**Resolvable without hardware.** The DLLs are present on the development machine,
and loading an assembly to reflect over its types requires no attached device.
Phase 2 should begin by introspecting `RectangularStimPulse` and `StimProperties`
and recording their members, either in the README or as a generated reference.

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
