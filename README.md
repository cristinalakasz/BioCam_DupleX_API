# BioCam DupleX API

Software to record from, and — in later phases — closed-loop stimulate, a
3Brain BioCAM DupleX high-density microelectrode array (4096 channels).

**This is the lab manual.** It is written for the person who runs experiments
on the instrument, not for the person who wrote the code. The author is
~600 km from the BioCAM and cannot run any of this; if something here is wrong,
it was wrong on the page, not caught by hand. Report discrepancies rather than
working around them.

**What works today:** recording raw signal to disk (`BioCam_DupleX_API/recorder.py`,
with known defects — see §7). **What does not exist yet:** the rebuilt
acquisition path, online spike detection, spike sorting, and closed-loop
stimulation. §14 gives the current status; do not assume anything not listed
there works.

---

## Table of contents

1. [What this is](#1-what-this-is)
2. [Before you run an experiment](#2-before-you-run-an-experiment)
3. [Hardware and requirements](#3-hardware-and-requirements)
4. [The DLL step](#4-the-dll-step)
5. [Environment setup](#5-environment-setup)
6. [Preflight check](#6-preflight-check)
7. [Recording](#7-recording)
8. [Data formats](#8-data-formats)
9. [How the instrument works](#9-how-the-instrument-works)
10. [Troubleshooting](#10-troubleshooting)
11. [Project layout](#11-project-layout)
12. [Development](#12-development)
13. [Before handing code to the lab](#13-before-handing-code-to-the-lab)
14. [Roadmap and status](#14-roadmap-and-status)

---

## 1. What this is

This repository controls a 3Brain BioCAM DupleX: a 4096-channel high-density
microelectrode array (MEA) system capable of both recording extracellular
signal and delivering electrical stimulation.

- **Works today:** recording raw signal from all 4096 channels to disk, and
  reading it back into a NumPy array (`BioCam_DupleX_API/recorder.py`). This
  script has known defects (§7, Appendix A of the design spec) and is
  scheduled for a rebuild.
- **Does not exist yet:** the rebuilt acquisition path (Phase 1), a stimulation
  engine (Phase 2), combined recording+stimulation sessions (Phase 3), a UI
  (Phase 4), online spike detection (Phase 5), and closed-loop stimulation
  (Phase 6). See §14.

If you were told "it can already do X" and X is not in the list above, ask
before relying on it.

---

## 2. Before you run an experiment

Read this section every time, even if you've run experiments before. It is
short on purpose.

1. **Close BrainWave** (and any other 3Brain software). The BioCAM can only be
   controlled by one process at a time. If BrainWave (or a leftover Python
   process) still holds the device, `TakeBioCamControl()` returns `None`, and
   every script in this repository will fail with no clearer explanation than
   that.
2. **Seat the MEA plate** on the DupleX head before connecting. A recording
   started with an unseated plate fails at `MeaPlate.IsConnected`.
3. **Run preflight** (§6): `python -m biocam.preflight`. It confirms the
   environment only — Python version, `numpy`, and the seven DLLs on disk. It
   does **not** confirm the device is detected or the plate is seated; that
   check is not implemented yet (arrives with Phase 1).
4. **Check free disk space.** Recording all 4096 channels consumes
   **~152 MB per second — about 9 GB per minute**
   (18,557.72 Hz × 4096 channels × 2 bytes/sample ≈ 152 MB/s; verify with
   `python -c "print(18557.720703125*4096*2)"` any time this figure is in
   doubt). A drive that looks empty enough for "a quick recording" can fill up
   mid-session and lose the run — there is no resume. A real 10-second session
   already recorded in this repository (`BioCam_DupleX_API/recordings/`) is
   ~1.5 GB, consistent with this rate.

---

## 3. Hardware and requirements

- 3Brain BioCAM DupleX instrument
- MEA plate
- Windows 10 or 11 — **this project is Windows-only.** The 3Brain driver
  targets .NET Framework, loaded through `pythonnet`'s `netfx` runtime; there
  is no cross-platform path.
- .NET Framework 4.7 or later installed (4.8 satisfies this)
- USB connection to the BioCAM

A **development machine** (no instrument attached, running only the test
suite) needs none of the above — not the instrument, not USB, not even
Windows or .NET Framework. It only needs Python 3.12 and the packages in
`requirements-dev.txt` (§5). Everything in this section is required on the
**lab machine** only.

---

## 4. The DLL step

This is usually the first thing that blocks a fresh clone, so it gets its own
section.

The 3Brain driver requires seven DLLs that are **not committed to this
repository** — `.gitignore` excludes all `*.dll` files. Reasons:

- One set is **~70 MB** (measured: sum of the sizes in the table below), which
  is unnecessary repository weight for files that never change per-commit. A
  full install is **two copies** — see below — so **~140 MB** total.
- They are 3Brain's licensed SDK, not code we wrote, and not ours to
  redistribute.

They come from the **3Brain SDK / BrainWave installation** on the lab machine,
and are needed in **two places**:

- `BioCam_DupleX_API/API/` — used by the Python scripts (`connector.py`,
  `recorder.py`, `Hello_BioCam.py`, and `biocam.preflight`).
- `BioCam_DupleX_API/SampleApp_BioCamCL/Dependencies/` — used by 3Brain's own
  C# reference application (§11), which you will want buildable if you're
  cross-checking a .NET call against known-working code rather than against
  the XML alone. Copy the same seven files here too; a build of
  `SampleApp_BioCamCL` will fail with missing-assembly errors otherwise.

Copy the full set into both locations:

| File | Size (bytes) |
|---|---|
| `3Brain.BioCamDriver.dll` | 4,191,232 |
| `3Brain.Common.dll` | 545,792 |
| `3Brain.Deployment.Drivers.dll` | 2,174,976 |
| `3Brain.Diagnostic.dll` | 18,944 |
| `3Brain.Processing.Core.dll` | 12,108,288 |
| `3Brain.Processing.Native.dll` | 50,722,816 |
| `Newtonsoft.Json.dll` | 711,952 |

Sizes are exact — they come from a real preflight run on this development
machine (§6) and are the number to check against, not "roughly the right
file." If your copy differs in size, re-copy it from the SDK rather than
assuming it's fine.

**Verify all seven are present and correctly sized:**

```
python -m biocam.preflight
```

This also checks the Python version and `numpy` — see §6 for real pass/fail
output.

---

## 5. Environment setup

**Python 3.12** is required (`biocam/preflight.py` enforces `>= 3.12`; this
manual was verified against 3.12.10).

Two dependency files exist, and which one you need depends on the machine:

- **Lab machine** (BioCAM attached): `requirements.txt` — pins `numpy` and
  `pythonnet`. Requires Windows + .NET Framework 4.7+ (§3).
- **Development machine** (no instrument, running only the test suite):
  `requirements-dev.txt` — pins `numpy` and `pytest`, and deliberately
  **excludes** `pythonnet`. The whole point of this split is that the test
  suite runs without the 3Brain SDK installed at all
  (`tests/test_no_hardware_imports.py` enforces this).

### conda

```
conda create -n biocam python=3.12
conda activate biocam
pip install -r requirements.txt        # lab machine
pip install -r requirements-dev.txt    # development machine
```

### venv

```
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt        # lab machine
pip install -r requirements-dev.txt    # development machine
```

(`conda` was not available to re-verify on the machine this README was written
on — the syntax above is standard conda usage. The **venv path was run
end-to-end** on this machine: a fresh venv, `pip install -r
requirements-dev.txt`, then `python -m pytest` and `python -m
biocam.preflight` both succeeded from inside it — see the real output in §6
and §12.)

---

## 6. Preflight check

Run this before every experiment (§2) and any time the environment might have
changed:

```
python -m biocam.preflight
```

It checks Python version, `numpy`, and the seven DLLs (§4) — nothing that
requires the instrument to be attached. Exits `0` if everything passes, `1` if
anything fails.

### Real pass output

Captured on this development machine with all seven DLLs present in
`BioCam_DupleX_API/API/` (re-run and confirmed identical while writing this
manual):

```
[PASS] Python version                 found 3.12.10, need >= 3.12
[PASS] package numpy                  2.5.2
[PASS] 3Brain.BioCamDriver.dll        4191232 bytes
[PASS] 3Brain.Common.dll              545792 bytes
[PASS] 3Brain.Deployment.Drivers.dll  2174976 bytes
[PASS] 3Brain.Diagnostic.dll          18944 bytes
[PASS] 3Brain.Processing.Core.dll     12108288 bytes
[PASS] 3Brain.Processing.Native.dll   50722816 bytes
[PASS] Newtonsoft.Json.dll            711952 bytes

ALL CHECKS PASSED
```
Exit code: `0`

### Real fail output

Captured pointing preflight at an empty DLL directory (identical failure path
to a DLL being missing or misnamed, e.g. after a rename):

```
=== FAIL case: DLL directory empty ===
[PASS] Python version                 found 3.12.10, need >= 3.12
[PASS] package numpy                  2.5.2
[FAIL] 3Brain.BioCamDriver.dll        not found in \tmp\nodlls
[FAIL] 3Brain.Common.dll              not found in \tmp\nodlls
[FAIL] 3Brain.Deployment.Drivers.dll  not found in \tmp\nodlls
[FAIL] 3Brain.Diagnostic.dll          not found in \tmp\nodlls
[FAIL] 3Brain.Processing.Core.dll     not found in \tmp\nodlls
[FAIL] 3Brain.Processing.Native.dll   not found in \tmp\nodlls
[FAIL] Newtonsoft.Json.dll            not found in \tmp\nodlls

7 CHECKS FAILED
```
Exit code: `1`

On your machine the "not found in ..." path will be
`BioCam_DupleX_API/API/` — the directory shown above is a test directory used
to produce this example, not the real DLL location.

**What preflight does not check:** device detection or MEA plate connection.
That requires Layer 1 code that doesn't exist until Phase 1 (§14). A clean
preflight pass tells you the environment is correct — it does not tell you the
BioCAM is connected, powered, or ready.

---

## 7. Recording

Recording is done by `BioCam_DupleX_API/recorder.py`. It is a legacy script —
it works, but it has known defects (see the Appendix A defect list in
`docs/superpowers/specs/2026-08-03-claude-project-setup-design.md`) and is
being rebuilt on the three-layer architecture in Phase 1 (§14). In particular:
data loss is currently silent (no subscription to loss/error events), disk
writes happen inside the time-critical data callback, and the stimulator's
`Start()` call is missing (§9, §10). Treat its output as usable but not
provably complete.

**Command line**, run from inside `BioCam_DupleX_API/` so the DLLs at
`API/` are found by the default path:

```
python recorder.py --duration 10 --name test1
```

Options (from `recorder.py`'s `argparse` definitions):

| Option | Default | Meaning |
|---|---|---|
| `--duration` | `10.0` | Recording length in seconds |
| `--name` | current timestamp (`YYYYMMDD_HHMMSS`) | Base filename for the two output files |
| `--output-dir` | `recordings` | Output folder — **relative to the current working directory when you invoke the script**, not to the script's own location. Running from `BioCam_DupleX_API/` puts files in `BioCam_DupleX_API/recordings/`; running from the repo root puts them in `./recordings/` instead. |
| `--packet-ms` | driver default | Acquisition packet interval in ms (§9) |

Each run writes two files: `<name>.raw` (the signal, §8) and
`<name>_meta.json` (the metadata sidecar, §8). Only the `_meta.json` files are
committed to git — `.raw` files are gitignored everywhere except
`tests/fixtures/` (§12), because a single session is gigabytes (§2).

**Reading a recording back**, programmatically. As with the CLI above, run
this from inside `BioCam_DupleX_API/` (or otherwise put that directory on
`sys.path`) — `recorder.py` imports `connector` by bare module name, which
only resolves when `BioCam_DupleX_API/` is the working directory:

```python
from recorder import load_recording

data, meta = load_recording("recordings/test1.raw", "recordings/test1_meta.json")
# data: float64 array, shape (n_frames, total_channels), in microvolts
# meta: dict — see §8 for every field
```

Pass `as_analog=False` to `load_recording` to get raw ADC counts (`uint16`)
instead of microvolts.

---

## 8. Data formats

### `.raw` layout

Frame-major, raw ADC counts, no header: all channels for frame 0, then all
channels for frame 1, and so on.

- dtype: `uint16`, little-endian
- 4096 channels × 2 bytes/channel = **8192 bytes per frame**
- file size is always an exact multiple of 8192 bytes for a full-width
  recording (`tests/test_fixture_integrity.py` checks this for the committed
  fixtures)

### `_meta.json` fields

Written by `recorder.py` alongside every `.raw` file:

| Field | Meaning |
|---|---|
| `frame_rate_hz` | Sample rate (18,557.720703125 Hz for this instrument) |
| `n_wells` | Number of wells (1 for the DupleX) |
| `n_channels_per_well` | Channels per well |
| `total_channels` | `n_wells × n_channels_per_well` — the row width of the reshaped array |
| `ch_sample_byte_size` | Bytes per sample (2 → `uint16`) |
| `bit_depth` | ADC resolution in bits (12 → digital range 0–4095) |
| `adc_counts_to_value` | Multiplicative factor, counts → µV |
| `offset` | Additive offset, counts → µV |
| `min_digital_value`, `max_digital_value` | Valid ADC count range (0–4095 for 12-bit) |
| `n_frames_total` | Frame count — `.raw` size ÷ 8192 should equal this |
| `duration_sec` | Recording length in seconds |
| `packet_log` | List of `{timestamp, frame_offset, n_frames}` — one entry per packet received, wall-clock based (see the known defect about hardware timestamps in §9/design-spec Appendix A) |

The committed test fixtures (`tests/fixtures/`, §12) additionally carry
`source_recording` and `source_channels`, recording which real session and
channel subset they were cut from; they have no `packet_log` because they are
static slices, not live recordings.

### Counts → microvolts

```
microvolts = offset + counts * adc_counts_to_value
```

For the reference recording these two fields are `offset = -4125.0` and
`adc_counts_to_value = 2.0146520146520146` — but **read them from each
recording's own `_meta.json` rather than hardcoding these**; that is exactly
why they are written per-session instead of assumed constant.

### Worked example

Verified against the committed fixture `tests/fixtures/sample_32ch_2s`
(37,115 frames × 32 channels, 2,375,360 bytes):

```python
import json
import numpy as np

meta = json.load(open("tests/fixtures/sample_32ch_2s_meta.json"))
raw = np.fromfile("tests/fixtures/sample_32ch_2s.raw", dtype=np.uint16)
data = raw.reshape(-1, meta["total_channels"])  # (37115, 32) ADC counts

microvolts = meta["offset"] + data.astype(np.float64) * meta["adc_counts_to_value"]
```

Actual output on this fixture: counts range 686–3050 (within the declared
0–4095 digital range); converted, µV range is about −2743 to +2020, mean
≈ 1.27 µV — a plausible noise-and-signal range for extracellular recording,
which is the sanity check to apply to any new recording.

---

## 9. How the instrument works

Condensed from `3Brain_BioCamDriverAPI_v2.6_Introduction.pdf`. Read the PDF
for anything not covered here.

- Data arrives as **packets**, each carrying a hardware timestamp
  (`DataPacketReceivedEventArgs.Header.Timestamp`). `recorder.py` does not
  currently read this timestamp — it logs wall-clock time instead, which is a
  known defect (§7, design spec Appendix A item 1–2).
- The **acquisition time period** (how often a packet, and a callback, fires)
  is configurable from **1 to 250 ms** (`recorder.py --packet-ms`).
- **Closed-loop latency** (detect → decide → stimulate) is published at
  **≈1.15 ms mean, ≈1.52 ms worst case**, measured at a 1 ms acquisition
  period. Nothing in this repository implements closed-loop stimulation yet
  (Phase 6, §14) — this figure is the budget any future implementation must
  fit inside, most of which is consumed by whatever work runs inside the data
  callback (§12 callback rule).
- **Stimulation** is delivered through positive/negative electrode endpoint
  pairs. Constraints that are easy to violate and fail silently or ignore
  extra data rather than erroring loudly:
  - Positive and negative endpoints of a pulse may **never share an electrode
    column**.
  - **≤1000 endpoints** per spatial configuration.
  - **≤1000 queued** future stimuli.
  - Per `Send` call: **≤64 pulse values and ≤288 endpoint values** — the
    documented behavior on overflow is that the *next* call's values are
    silently ignored, not an error.
  - **Chip reconfiguration** between different spatial patterns costs
    **26 µs + 8.4 µs × (rows − 1)**, which bounds how fast stimulation
    patterns can be cycled.

None of the stimulation engine exists in `biocam/` yet — see Phase 2 in
`docs/superpowers/specs/2026-08-12-api-roadmap-decomposition.md` for the
planned design (three `Send` overloads, an on-device protocol engine, and why
scheduling belongs on the instrument rather than in a Python loop).

---

## 10. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `TakeBioCamControl` returns `None` | BrainWave, or another 3Brain program (including a previous crashed Python process), still holds the device — only one process may control the BioCAM at a time | Close BrainWave and any other 3Brain software, then retry (§2) |
| No device found / connection times out | USB not connected, or BioCAM not powered | Check the USB cable and power; confirm the BioCAM's status LED is on |
| `MeaPlate.IsConnected` is `False` after `TakeBioCamControl` succeeds | The MEA plate is not seated on the DupleX head | Reseat the MEA plate on the DupleX head and retry |
| **No stimulation output, with no error reported** | **Known defect.** The stimulator lifecycle is `Initialize → Start → Stop → Close`; `BioCam_DupleX_API/connector.py` calls `Initialize` and `Close` but never `Start()`, so pulses silently never fire | Add the missing `Start()` call, or wait for the Phase 1/2 rebuild (§14) — do not assume stimulation is working just because nothing errored |
| Data-loss warnings, or gaps that look like signal but aren't | Acquisition period too short for the work being done per packet, or slow work inside the data callback (`recorder.py` currently writes to disk and prints inside the callback — a known defect, §7) | Increase `--packet-ms`; longer term, wait for the Phase 1 rebuild that moves I/O off the callback thread (§12 callback rule) |
| `ModuleNotFoundError: No module named 'clr'` | `pythonnet` is not installed — you are on a development machine, not the lab machine | Use `requirements-dev.txt`, not `requirements.txt` (§5). Installing `pythonnet` is neither necessary nor sufficient without Windows + .NET Framework anyway (§3) |

---

## 11. Project layout

One line per top-level path:

- `README.md` — this document
- `CLAUDE.md` — project briefing for Claude Code sessions; not duplicated here, see there for AI-agent-specific rules
- `.claude/` — agent definitions (`biocam-api-verifier`, `realtime-safety-reviewer`, `dsp-implementer`) and tool permissions (`settings.json`)
- `biocam/` — the Python package under active development, split by testability (§12):
  - `biocam/interop/` — **Layer 1**: .NET interop via `pythonnet`. Cannot run without hardware and the DLLs (§4); the only package allowed to import `clr`. Empty in Phase 0, populated by Phase 1.
  - `biocam/data/` — **Layer 2**: pure byte/number logic (payload decoding, frame reassembly, unit conversion). Fully testable with synthetic buffers. Empty in Phase 0, populated by Phase 1.
  - `biocam/analysis/` — **Layer 3**: signal processing (spike detection, sorting). Fully testable against fixtures. Empty in Phase 0, populated starting Phase 5.
  - `biocam/preflight.py` — the environment check run by `python -m biocam.preflight` (§6)
- `BioCam_DupleX_API/` — legacy, pre-layer-split code and vendor material:
  - `connector.py`, `recorder.py`, `Hello_BioCam.py` — the working-but-defective scripts described in §7 and §10
  - `API/` — the seven DLLs (§4, gitignored) plus `3Brain.BioCamDriver.xml`, the reference documentation for every .NET member
  - `SampleApp_BioCamCL/` — 3Brain's own C# reference application; the second ground-truth source alongside the XML
  - `recordings/` — output of `recorder.py`; `.raw` files are gitignored, `_meta.json` sidecars are committed
- `tests/` — the pytest suite (§12) and `tests/fixtures/` — the two committed real-signal fixtures
- `tools/make_fixtures.py` — the one-off script that produced the committed fixtures from a full (uncommitted) recording
- `docs/superpowers/` — specs (`specs/`) and plans (`plans/`) for this project's own development, including the setup spec and the six-phase roadmap referenced throughout this document
- `requirements.txt` / `requirements-dev.txt` — pinned dependencies for the lab machine and a development machine respectively (§5)
- `pytest.ini` — test configuration
- `3Brain_BioCamDriverAPI_v2.6_Introduction.pdf` — vendor manual, source for §9

---

## 12. Development

### The three-layer rule

New code belongs in exactly one of the three layers under `biocam/`
(§11) by whether a laptop with no instrument can prove it correct:

- **Layer 1** (`biocam/interop/`) — anything that calls into the 3Brain
  assemblies or depends on a device responding. Not testable here; written and
  reviewed by hand against `API/3Brain.BioCamDriver.xml` and
  `SampleApp_BioCamCL/MainForm.cs`.
- **Layer 2** (`biocam/data/`) — pure functions from bytes/numbers to
  bytes/numbers: decoding, frame reassembly, unit conversion, gap detection.
  Fully testable with synthetic buffers. Never write Layer 2 code without
  tests — it's testable, so untested Layer 2 code is a choice, not a
  limitation.
- **Layer 3** (`biocam/analysis/`) — signal processing, tested against the
  replay fixtures below.

### Running the suite

```
python -m pytest
```

Runs the entire suite with **no BioCAM and no 3Brain DLLs installed** — this
is a structural guarantee, enforced by `tests/test_no_hardware_imports.py`,
not a convention someone has to remember. Verified on this machine, from a
freshly created virtual environment with only `requirements-dev.txt`
installed: every test passed, with `test_no_hardware_imports.py` confirming
that nothing under `tests/` or `biocam/` (outside `biocam/interop/`) imports
`clr`, `pythonnet`, or `clr_loader`. Run the command yourself for the current
pass/fail count and test names — pasting them here would go stale the moment
Phase 1 adds a test.

### Fixtures

`tests/fixtures/`, used by Layer 2/3 tests as a **replay source** — real
recorded signal rather than synthetic data, because real recordings contain
noise, drift, and artifacts nobody thinks to simulate:

- `sample_32ch_2s` — 37,115 frames × 32 channels (2,375,360 bytes). The 32
  most active channels (by variance) from a real session, 2 seconds.
- `sample_full_100frames` — 100 frames × 4096 channels (819,200 bytes). Full
  channel width, for testing anything that depends on `total_channels == 4096`.

Load either with `load_fixture(name)` from `tests/test_fixture_integrity.py`,
returning `(data, meta)` — `data` is raw ADC counts, shape
`(n_frames, total_channels)`.

### The callback rule

`DataReceived` runs on the acquisition thread and is time-critical: no disk
I/O, no printing or logging, no unbounded allocation, no locks. Hand the
payload off to a bounded queue and return immediately. A blocked callback
drops samples silently — the recording looks like real signal, not an error
(§9, §10).

### What a green suite does not prove

`python -m pytest` passing is evidence for **Layers 2 and 3 only.** It proves
nothing about Layer 1 — that code is never executed by the suite, by
construction (§11). Never report a green suite as evidence that
instrument-facing code works. Say instead exactly what has and hasn't been
verified — that is what §13 exists to force.

---

## 13. Before handing code to the lab

The gate that actually matters — reproduced here in full so it survives
independently of `CLAUDE.md` or any tooling. A commit is cheap and reversible;
a lab session consumes a colleague's day on a shared instrument and cannot be
repeated on demand. Work through all five before code goes to the lab machine:

1. **`biocam-api-verifier` clean across all interop code**, not just what
   changed since the last session.
2. **`realtime-safety-reviewer` clean across the whole data path** (everything
   reachable from `DataReceived`).
3. **Full test suite passing** (`python -m pytest`) — noting that this covers
   Layers 2 and 3 only and proves nothing about Layer 1 (§12).
4. **Preflight runs and reports correctly** (`python -m biocam.preflight`,
   §6) — this confirms the environment, not the device.
5. **Every known-untested assumption written down explicitly**, so the
   colleague running the session knows what is being tried for the first time
   and exactly what to report back. Since Layer 1 has no automated coverage
   by construction, **every Layer 1 change since the last session belongs on
   this list** — that is what turns "untested" from a vague worry into a
   finite, checkable set.

---

## 14. Roadmap and status

The full six-phase build order, findings, and rationale live in
`docs/superpowers/specs/2026-08-12-api-roadmap-decomposition.md` — read it
there rather than trusting a restatement here, which will go stale. Summary
only:

| Phase | Contents | Status |
|---|---|---|
| 0 | Setup: `CLAUDE.md`, verifier agents, test scaffolding, this README | **Done** |
| 1 | Acquisition: recording, saving, data integrity, on the three-layer split | Not started |
| 2 | Stimulation engine + manual and scheduled triggering | Not started |
| 3 | Session control: recording and stimulation together, changing live | Not started |
| 4 | UI | Not started |
| 5 | Spike detection | Not started |
| 6 | Closed-loop stimulation (depends on Phase 5) | Not started |

If this table and the roadmap document ever disagree, the roadmap document is
correct — this table is a pointer, not a second source of truth.
