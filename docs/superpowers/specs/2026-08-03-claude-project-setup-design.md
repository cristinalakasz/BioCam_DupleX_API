# Claude Code project setup for BioCam_DupleX_API

**Date:** 2026-08-03
**Status:** Approved, ready for implementation planning
**Scope:** Project setup only. The engineering roadmap (recorder fixes, spike
detection, spike sorting, closed-loop stimulation) is explicitly out of scope
and gets its own spec per phase.

---

## 1. Context

This repository controls a 3Brain BioCAM DupleX high-density MEA — a 4096-channel
recording and stimulation instrument. The immediate code records raw signal to
disk. The planned roadmap adds online spike detection, online spike sorting, and
closed-loop stimulation protocols.

### The constraint that shapes everything

**The developer cannot run the code.** The instrument is ~600 km away. A
colleague on site runs the software and reports results back. Remote access to a
lab workstation may be requested later but is not available now and the design
must not depend on it.

The normal write-run-fix loop, measured in seconds, is here measured in days, and
each iteration consumes another person's time on a shared instrument.

**Therefore the goal of this setup is: catch on the laptop the mistakes that
would otherwise only be discovered in the lab.**

### Two failure modes dominate

**Incorrect .NET interop.** The Python code calls 3Brain's .NET library through
pythonnet. Wrong method names, wrong argument order, or violated call-order
requirements produce no error at edit time and fail only against real hardware.

A live example already in the code: the stimulator requires
`Initialize → Start → Stop → Close`. `connector.py` calls `Initialize` and
`Close` but never `Start`. Stimulation will silently fail to fire, with no error,
and the failure will be observed by someone who cannot debug it.

**Latency violations.** Closed-loop operation requires detect → decide →
stimulate within roughly 1.5 ms (3Brain's published figure at a 1 ms acquisition
period). The driver invokes a user callback every acquisition period; work done
inside that callback must complete before the next packet arrives or data is
dropped. Dropped samples in electrophysiology resemble real signal rather than an
error, so this corrupts data silently.

`recorder.py` currently performs disk writes inside that callback.

### Environment is currently unreproducible

The default interpreter on the development machine is Python 3.12.10 with neither
`numpy` nor `pythonnet` installed, yet `__pycache__` contains `cpython-313`
artifacts from an interpreter that cannot be located. There is no
`requirements.txt`, `environment.yml`, or virtualenv. The on-site colleague
cannot reproduce an environment that has never been written down.

---

## 2. Goals

1. Make the project self-explanatory to a second person who will run it without
   the author present.
2. Draw the layer boundaries (§3) so that as much code as possible falls on the
   testable side, and make the genuinely untestable remainder as thin as it can be.
3. Make that untestable remainder checkable against documentation, and make
   checking a required step rather than an available one (§8).
4. Pin the environment so it can be reproduced in the lab.

## Non-goals

- Fixing the existing scripts. See "Successor spec" below.
- Implementing spike detection, sorting, or stimulation.
- Any CI/CD, packaging, or release automation.
- Project-specific workflow skills. Deferred until the recurring workflows are
  known from experience rather than guessed.

## Successor spec

The defects in Appendix A are **owned by the next spec**, not left unassigned:

> **"Introduce the three-layer split and rebuild the acquisition path on it"** —
> to be written immediately after this setup is implemented, with Appendix A as
> its acceptance criteria.

It is deliberately not framed as "fix eleven bugs." Most of those defects are
symptoms of one missing structure. Introducing the layer split and a queue-based
callback fixes 1, 4, 5, 6, 7 and 8 as a consequence, because that path gets
rewritten with the hardware timestamp and frame carry-over designed in. Framed as
a bug list, it would instead produce eleven patches to an architecture that
should change.

---

## 3. Three layers, split by testability

The central architectural decision, recorded here because everything else
depends on it.

The obvious split — "hardware code" versus "analysis code" — is wrong, because it
buries a large amount of perfectly testable logic inside the hardware half. The
useful split has **three** layers, divided by whether a laptop can prove the code
correct.

### Layer 1 — .NET interop. Not testable here.

`BioCamPool.Activate()`, `TakeBioCamControl()`, `StartDataStreaming()`,
`Stimulator.Send()`. Anything that calls into the 3Brain assemblies or depends on
a device responding.

Cannot be executed on the development machine. Review and documentation
cross-checking are the only available safety nets, so this layer is written in
the main conversation, kept as thin as possible, and **must** pass
`biocam-api-verifier` (§8).

### Layer 2 — pure data logic. Fully testable here.

Decoding payload bytes into frames, carrying partial frames across packet
boundaries, applying `offset + counts × scale` to get µV, reading and writing
metadata, detecting gaps from hardware timestamps.

This layer touches no hardware. It is a function from bytes to numbers, and a
synthetic byte buffer tests it completely — including the awkward cases that are
hard to produce on real hardware, such as a payload ending mid-frame or a
timestamp jump indicating loss.

Five of the eleven defects in Appendix A live wholly or partly here (1, 2, 5, 6,
7) — which means the nastiest of them, progressive frame desynchronisation, is
catchable by a unit test written today, with no hardware and no lab session.
Under a two-way split these would all have been filed as untestable.

### Layer 3 — analysis. Fully testable here.

Spike detection, sorting, closed-loop decision logic. Never imports pythonnet.
Tested against the replay fixture (§4).

### The source interface

Layers 2 and 3 are joined by one narrow interface yielding
`(hardware_timestamp, frames)` blocks, with two implementations:

- **Live source** — Layer 1 plus Layer 2, driven by the real BioCAM.
- **Replay source** — Layer 2 alone, reading a `.raw` file, emitting identical
  blocks.

Because the replay source reuses the *same* Layer 2 code as the live path, tests
against recorded data exercise the real decoding logic rather than a stand-in.

### Consequence for delegation

Layers 2 and 3 may be implemented by `dsp-implementer`, test-first, because tests
can prove the result. Layer 1 may not: nothing can verify it here, and volume
works against the only safety net it has.

---

## 4. Deliverables

Eight deliverables (nine files, counting the fixture's metadata sidecar).
Nothing installs or executes as part of this setup.

| File | Audience | Purpose |
|---|---|---|
| `README.md` | humans | Complete lab manual. §5. |
| `requirements.txt` | humans | Exact pinned dependencies. |
| `CLAUDE.md` | Claude | Project briefing loaded every session. §6. |
| `.claude/agents/biocam-api-verifier.md` | Claude | Read-only .NET interop checker. §7. |
| `.claude/agents/realtime-safety-reviewer.md` | Claude | Read-only callback-latency checker. §7. |
| `.claude/agents/dsp-implementer.md` | Claude | Test-first implementer, Layers 2–3 only. §7. |
| `.claude/settings.json` | Claude | Permissions for routine commands. |
| `tests/fixtures/sample_5s.raw` + `_meta.json` | tests | Real recorded signal, small enough to commit. |

### Test fixture

A few seconds cut from the existing 1.5 GB recording, with a matching metadata
sidecar. Real signal rather than synthetic: recordings contain noise, drift, and
artifacts nobody thinks to simulate, and detectors tuned on clean synthetic data
degrade on real input. Cost is a few hundred KB committed permanently.

---

## 5. README

The primary human-facing deliverable, written for the on-site colleague rather
than for the author.

**Staleness policy.** Every fact that can drift — versions, channel counts,
sampling rate, acquisition parameters — lives in exactly one place, and the
preflight script prints values read from the hardware rather than the README
asserting them. Detail where stable, generated output where not.

### Sections

1. **What this is** — plain-language purpose, current capability vs planned.
2. **Hardware and requirements** — BioCAM DupleX, MEA plate, Windows 10/11,
   .NET Framework 4.7+, USB. Windows-only, because the 3Brain driver targets
   .NET Framework.
3. **The DLL step** — which files, where they come from, exactly where they go,
   why they are not in the repo, and a command to verify all seven are present.
   Its own section because it is the first thing that will block a fresh clone.
4. **Environment setup** — exact Python version, both conda and venv paths (the
   development machine has conda; the lab machine may not).
5. **Preflight check** — one command, run before any experiment. Verifies DLLs
   load, .NET version, device detected, plate connected; prints actual
   acquisition parameters read from the instrument. Sample pass and fail output.
6. **Recording** — commands, every option, output locations, how to read data back.
7. **Data formats** — `.raw` layout (frame-major: all channels for frame 0, then
   frame 1, …), `_meta.json` fields, ADC-counts-to-µV conversion. Currently
   undocumented and required by anyone analysing this data later.
8. **How the instrument works** — condensed conceptual model: timestamped data
   packets, acquisition time period configurable 1–250 ms, stimulation via
   positive/negative endpoint pairs, closed-loop latency ≈1.5 ms at 1 ms
   acquisition period. Includes the easily-violated constraints: endpoints may
   not share an electrode column, ≤1000 endpoints per spatial configuration,
   ≤1000 queued future stimuli.
9. **Troubleshooting** — symptom → cause → fix table, seeded from known failures
   (BrainWave holding the device, no device found, plate not seated, no
   stimulation output because `Start()` was not called, data-loss warnings).
10. **Project layout** — one line per file.
11. **Development** — the three-layer model and which layer new code belongs in,
    running tests without hardware, the replay fixture, the callback latency rule.
12. **Before handing code to the lab** — the Gate 2 checklist (§8) reproduced in
    full, so it survives independently of `CLAUDE.md` and of any tooling. Written
    for a human to work through, including the "what is untested" note that
    accompanies the handover.
13. **Roadmap and status** — what works, what is in progress, what is planned,
    so the colleague knows what to trust.

Plus a short **"before you run an experiment"** checklist near the top: close
BrainWave, seat the plate, run preflight, check free disk space. The last one
matters because a few minutes of recording produced 1.5 GB; a full session can
exhaust a drive mid-experiment and lose the run.

---

## 6. CLAUDE.md

Contains only what would otherwise be got wrong or rediscovered. It deliberately
does not duplicate the README — duplicated facts drift apart, and a long
briefing dilutes the important parts.

1. **The core constraint, stated first.** The BioCAM is not connected to this
   machine; no hardware-dependent code can be executed here; never claim such
   code works — state that it is untested and name what must be verified in the
   lab.
2. **The three-layer rule** (§3), including which layer new code belongs in and
   the rule that Layer 2 logic must never be written without tests — it is
   testable, so untested Layer 2 code is a choice, not a constraint.
3. **The callback rule.** `DataReceived` is time-critical: no disk I/O, no
   printing, no unbounded allocation, no locks. Hand off to a queue and return.
4. **API ground truth.** Never guess a .NET member name; verify against
   `API/3Brain.BioCamDriver.xml` and `SampleApp_BioCamCL/MainForm.cs`. Includes
   the stimulator lifecycle `Initialize → Start → Stop → Close`.
5. **The mandatory checkpoints** (§8), stated as rules rather than suggestions.
6. **Commands.** How to run tests and preflight.
7. **Conventions.** English throughout. Never commit DLLs or `.raw` files.
   Fixture location.

---

## 7. Helpers

### `biocam-api-verifier` — read-only

Given instrument-facing code, verifies every .NET call against
`3Brain.BioCamDriver.xml` and the C# sample: member existence, argument order and
type, `out` parameter handling, required call ordering, property names. Reports
findings with file and line, or reports clean. Never edits.

A separate helper rather than inline care for two reasons: it works in its own
context and can read the full 289 KB reference without displacing the working
conversation, and having exactly one job means it does not abandon a tedious
check partway through.

### `realtime-safety-reviewer` — read-only

Given data-path code, traces execution inside the callback, including into called
functions, and flags blocking operations: file and network I/O, `print`/`logging`,
large copies, lock acquisition, unbounded queues, and Python-specific traps such
as `bytes()` copies of the payload. Reports cost and remedy per finding. Never
edits.

### `dsp-implementer` — writes code, Layers 2 and 3 only

Implements Layer 2 (pure data logic) and Layer 3 (analysis) code test-first,
verified against synthetic buffers and the replay fixture. Explicitly **not
permitted** to modify Layer 1 interop code.

The permission follows testability, not subject matter. Layer 2 is *about* the
instrument's data format but contains no instrument calls, so it is as safe to
delegate as analysis code — provided the tests come first.

---

## 8. Mandatory checkpoints

The verifiers are worthless as capabilities that someone must remember to invoke.
They are therefore written into `CLAUDE.md` as rules, at two gates that catch
different kinds of failure.

### Gate 1 — before committing Layer 1 changes

Any change to interop code must pass `biocam-api-verifier`. Any change to code on
the `DataReceived` path must pass `realtime-safety-reviewer`. Cheap, frequent,
catches mistakes while the context is fresh.

### Gate 2 — before a lab session

The gate that actually matters. A commit is cheap and reversible; a lab session
consumes a colleague's day on a shared instrument and cannot be repeated on
demand. Before code is handed over for a run:

1. `biocam-api-verifier` clean across all interop code, not just what changed.
2. `realtime-safety-reviewer` clean across the whole data path.
3. Full test suite passing against the replay fixture.
4. Preflight script runs and reports correctly.
5. Every known-untested assumption written down explicitly, so the colleague
   knows what is being tried for the first time and what to report.

Item 5 exists because the highest-value output of a lab session is not "it
worked" but a precise answer about the specific things that could not be checked
beforehand.

### Enforcement

These are rules in `CLAUDE.md`, not automation. A git hook that ran a subagent on
every commit would be slow enough to be disabled within a week. The gates are
stated where they are read every session, and Gate 2 additionally appears as a
checklist in the README so it survives independently of any tooling.

---

## 9. Repository access

The colleague will have access to this private repository. Two consequences:

- They must be added as a collaborator (`cristinalakasz/BioCam_DupleX_API`).
- The gitignored DLLs mean a fresh clone cannot run. §5.3 makes that an explicit,
  verifiable setup step rather than a footnote.

---

## 10. Open items

- Whether anyone beyond the author and the one colleague will use the repository
  (supervisor, other students) — affects README entry level. Assumed: no.
- Remote access to a lab workstation — worth requesting, but nothing here
  depends on it.
- Note for later: closed-loop timing must not be measured during an active
  remote-desktop session, since screen encoding loads the same CPU that the
  sample sets to `RealTime` priority. RDP can be disconnected while the run
  continues.

---

## Appendix A — Existing defects

Found during exploration. **Not fixed by this spec** — these are the acceptance
criteria for the successor spec (§2).

Each is tagged with its layer (§3). The **L2** items are testable on a laptop
today; the **L1** items can only be verified in the lab, which is why they are
the ones that must be listed explicitly under Gate 2, item 5.

1. **[L1→L2] Hardware timestamp never read.** `decode_payload` touches only the
   payload. `DataPacketReceivedEventArgs.Header.Timestamp` is the basis of all
   closed-loop timing and of any gap detection. Reading it is L1; everything done
   with it afterwards is L2 and testable.
2. **[L1+L2] Data loss is silent.** Neither `DataLossAsync` nor
   `DataStreamingError` is subscribed. Packets are appended contiguously and the
   packet log records wall clock (`time.time()`), not hardware time, so a dropped
   packet shifts every subsequent frame with nothing recording that it happened.
   The existing 1.5 GB recording cannot be shown to be intact. Subscription is
   L1; gap detection from timestamp discontinuities is L2 and testable.
3. **[L1] Stimulator never started.** `Start()` is missing; stimulation cannot
   fire. Verifiable only in the lab — belongs on the Gate 2 untested list.
4. **[L1] Disk I/O inside the data callback** (`recorder.py`), plus `print` calls
   on the same path.
5. **[L2] Partial frames discarded.** `decode_payload` truncates to whole frames
   and drops the remainder instead of carrying it into the next packet —
   progressive desynchronisation if payload size is not a whole multiple of frame
   size. **Testable today** with a synthetic buffer ending mid-frame.
6. **[L2] Full payload copied through `bytes()`** every packet. At 4096 channels
   this is the largest obstacle to the published latency figure. Measurable today
   with a benchmark over synthetic buffers.
7. **[L2] Attribute name guessed** from five candidates in `decode_payload`. The
   XML confirms it is `Payload`; guessing hides breakage instead of failing
   loudly.
8. **[L1] `optimizeDataPacketLatency` not used.** The C# sample passes it as
   `true`.
9. **[L1] Inconsistent pythonnet initialisation.** `connector.py` uses
   `pythonnet.load("netfx")`; `Hello_BioCam.py` uses `set_runtime(get_netfx())`.
10. **[—] `import threading` at line 197**, after the code that uses it. Works by
    accident of import timing; fragile.
11. **[L1] Process and thread priority not raised.** The C# sample uses
    `ProcessPriorityClass.RealTime` and `ThreadPriority.Highest`.
