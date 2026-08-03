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
2. Make instrument-facing code checkable without hardware.
3. Make signal-processing code testable against real recorded signal.
4. Pin the environment so it can be reproduced in the lab.

## Non-goals

- Fixing the existing scripts. Identified defects are recorded in Appendix A as
  input to the next spec; none are fixed here.
- Implementing spike detection, sorting, or stimulation.
- Any CI/CD, packaging, or release automation.
- Project-specific workflow skills. Deferred until the recurring workflows are
  known from experience rather than guessed.

---

## 3. The hardware boundary

The central architectural decision, recorded here because everything else
depends on it.

All device access goes through one narrow interface that yields
`(hardware_timestamp, frames)` blocks. It has two implementations:

- **Live source** — the real BioCAM via pythonnet.
- **Replay source** — reads a `.raw` file and emits identical blocks.

Spike detection, sorting, and closed-loop decision logic sit **above** this line
and never import pythonnet. They are therefore fully testable on a laptop against
recorded signal.

This boundary also determines which work may be delegated to a code-writing
helper (§6).

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
| `.claude/agents/dsp-implementer.md` | Claude | Test-first signal-processing implementer. §7. |
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
11. **Development** — running tests without hardware, the replay fixture, the
    callback latency rule.
12. **Roadmap and status** — what works, what is in progress, what is planned,
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
2. **The hardware boundary rule** (§3).
3. **The callback rule.** `DataReceived` is time-critical: no disk I/O, no
   printing, no unbounded allocation, no locks. Hand off to a queue and return.
4. **API ground truth.** Never guess a .NET member name; verify against
   `API/3Brain.BioCamDriver.xml` and `SampleApp_BioCamCL/MainForm.cs`. Includes
   the stimulator lifecycle `Initialize → Start → Stop → Close`.
5. **Commands.** How to run tests and preflight.
6. **Conventions.** English throughout. Never commit DLLs or `.raw` files.
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

### `dsp-implementer` — writes code, above the boundary only

Implements signal-processing code test-first, verified against the replay
fixture. Explicitly **not permitted** to modify instrument-facing code.

The split follows testability. Above the boundary, tests can prove correctness on
a laptop, so delegated implementation is safe and covers most of the roadmap
volume. Below the boundary nothing can be tested, review is the only safety net,
and volume works against it — so that code is written in the main conversation
and checked by `biocam-api-verifier`.

---

## 8. Repository access

The colleague will have access to this private repository. Two consequences:

- They must be added as a collaborator (`cristinalakasz/BioCam_DupleX_API`).
- The gitignored DLLs mean a fresh clone cannot run. §5.3 makes that an explicit,
  verifiable setup step rather than a footnote.

---

## 9. Open items

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

Found during exploration. **Not fixed by this spec** — recorded as input to the
next one.

1. **Hardware timestamp never read.** `decode_payload` touches only the payload.
   `DataPacketReceivedEventArgs.Header.Timestamp` is the basis of all closed-loop
   timing and of any gap detection.
2. **Data loss is silent.** Neither `DataLossAsync` nor `DataStreamingError` is
   subscribed. Packets are appended contiguously and the packet log records wall
   clock (`time.time()`), not hardware time, so a dropped packet shifts every
   subsequent frame with nothing recording that it happened. The existing 1.5 GB
   recording cannot be shown to be intact.
3. **Stimulator never started.** `Start()` is missing; stimulation cannot fire.
4. **Disk I/O inside the data callback** (`recorder.py`), plus `print` calls on
   the same path.
5. **Partial frames discarded.** `decode_payload` truncates to whole frames and
   drops the remainder instead of carrying it into the next packet — progressive
   desynchronisation if payload size is not a whole multiple of frame size.
6. **Full payload copied through `bytes()`** every packet. At 4096 channels this
   is the largest obstacle to the published latency figure.
7. **Attribute name guessed** from five candidates in `decode_payload`. The XML
   confirms it is `Payload`; guessing hides breakage instead of failing loudly.
8. **`optimizeDataPacketLatency` not used.** The C# sample passes it as `true`.
9. **Inconsistent pythonnet initialisation.** `connector.py` uses
   `pythonnet.load("netfx")`; `Hello_BioCam.py` uses `set_runtime(get_netfx())`.
10. **`import threading` at line 197**, after the code that uses it. Works by
    accident of import timing; fragile.
11. **Process and thread priority not raised.** The C# sample uses
    `ProcessPriorityClass.RealTime` and `ThreadPriority.Highest`.
