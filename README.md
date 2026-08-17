# BioCam DupleX API

Software to record from, and — in later phases — closed-loop stimulate, a
3Brain BioCAM DupleX high-density microelectrode array (4096 channels).

**This is the lab manual.** It is written for the person who runs experiments
on the instrument, not for the person who wrote the code. The author is
~600 km from the BioCAM and cannot run any of this; if something here is wrong,
it was wrong on the page, not caught by hand. Report discrepancies rather than
working around them.

**What is built:** the rebuilt acquisition path (`python -m biocam.cli record`)
and the stimulation engine (`python -m biocam.cli stim`, §15). **What does not
exist yet:** combined recording-and-stimulation sessions, a UI, online spike
detection, spike sorting, and closed-loop stimulation.

**Built is not the same as proven.** None of this software has ever run on the
instrument — not one recording, not one stimulus. It was written against the
vendor's documentation and assemblies and reviewed by hand. The open
`hardware-verification` issues list what has never executed and how to check
each one; #16 is the ten-minute first session, #21 the first stimulation step.
§14 gives the current status; do not assume anything not listed there works.

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
15. [Stimulation](#15-stimulation)

---

## 1. What this is

This repository controls a 3Brain BioCAM DupleX: a 4096-channel high-density
microelectrode array (MEA) system capable of both recording extracellular
signal and delivering electrical stimulation.

- **Built:** the acquisition path (Phase 1) — `python -m biocam.cli record`,
  recording all 4096 channels to disk with gap detection and an integrity
  sidecar, plus conversion to HDF5 — and the stimulation engine (Phase 2),
  `python -m biocam.cli stim` (§15).
- **Superseded:** `BioCam_DupleX_API/recorder.py` and `connector.py`, the
  original scripts. They have known defects (§7, Appendix A of the design
  spec); Phase 1 replaced them.
- **Does not exist yet:** combined recording+stimulation sessions (Phase 3), a
  UI (Phase 4), online spike detection (Phase 5), and closed-loop stimulation
  (Phase 6). See §14.
- **Never executed on the instrument:** all of it. The development machine is
  ~600 km from the BioCAM. A green test suite is evidence about Layers 2–3
  only (§12) and proves nothing about the .NET interop.

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
3. **Run preflight** (§6), **from the repository root**: `python -m
   biocam.preflight`. It confirms the environment only — Python version,
   `numpy`, and the seven DLLs on disk. It does **not** confirm the device is
   detected or the plate is seated; that check is not implemented yet
   (arrives with Phase 1).
4. **Check free disk space.** Recording all 4096 channels consumes
   **~152 MB per second — about 9 GB per minute**
   (18,557.72 Hz × 4096 channels × 2 bytes/sample ≈ 152 MB/s; verify with
   `python -c "print(18557.720703125*4096*2)"` — this one can be run from
   anywhere — any time this figure is in doubt). A drive that looks empty
   enough for "a quick recording" can fill up mid-session and lose the run —
   there is no resume. A real 10-second session recorded on the development
   machine this manual was verified on (its metadata sidecar is committed at
   `BioCam_DupleX_API/recordings/20260624_140615_meta.json`; the `.raw` itself
   is gitignored and is **not** in a fresh clone) is ~1.5 GB, consistent with
   this rate.

   **Where the recording is written matters as much as how much room there is.**
   Never record into a OneDrive or other synced folder, onto a network share, or
   onto the Windows drive — each loses data in a way this software cannot yet
   detect. Setting a machine up correctly, including a sustained-write test the
   drive must pass, is covered in
   [`docs/lab/storage-setup.md`](docs/lab/storage-setup.md). Read it before the
   first recording on any new machine.

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

- One full set is **~70 MB** (measured: sum of the sizes in the table below).
  They are needed in **two directories** (below); as populated on the
  development machine this manual was verified on, `API/` (all seven) plus
  `SampleApp_BioCamCL/Dependencies/` (six of the seven — see below) together
  measure **140,236,048 bytes, ≈ 140 MB** — unnecessary repository weight for
  files that never change per-commit, in either count.
- They are 3Brain's licensed SDK, not code we wrote, and not ours to
  redistribute.

They come from the **3Brain SDK / BrainWave installation** on the lab machine,
and are read from **two directories**:

- `BioCam_DupleX_API/API/` — used by the Python scripts (`connector.py`,
  `recorder.py`, `Hello_BioCam.py`) and by `biocam.preflight`, which checks
  **only this directory** (§6) — a clean preflight pass says nothing about
  the second directory below.
- `BioCam_DupleX_API/SampleApp_BioCamCL/Dependencies/` — used by 3Brain's own
  C# reference application (§11), which you will want buildable if you're
  cross-checking a .NET call against known-working code rather than against
  the XML alone. Its `.csproj` explicitly references five of the seven from
  here (`3Brain.BioCamDriver`, `3Brain.Common`, `3Brain.Deployment.Drivers`,
  `3Brain.Diagnostic`, `3Brain.Processing.Core`), plus `FTD3XX_NET.dll` — an
  FTDI USB driver assembly that preflight does not track and that is not in
  this repository at all; get it from the 3Brain/FTDI install if you need to
  build the sample app. `3Brain.Processing.Native.dll` is also present here
  (a native runtime dependency, not a compile-time `<Reference>`).
  `Newtonsoft.Json.dll` is the one file **not** currently present in this
  directory and not referenced by the `.csproj` — there is no evidence it is
  needed here; copy it in only if a build or run specifically reports it
  missing.

For the DLLs preflight does check (`API/`), copy the full set:

| File | Size (bytes) |
|---|---|
| `3Brain.BioCamDriver.dll` | 4,191,232 |
| `3Brain.Common.dll` | 545,792 |
| `3Brain.Deployment.Drivers.dll` | 2,174,976 |
| `3Brain.Diagnostic.dll` | 18,944 |
| `3Brain.Processing.Core.dll` | 12,108,288 |
| `3Brain.Processing.Native.dll` | 50,722,816 |
| `Newtonsoft.Json.dll` | 711,952 |

Sizes above come from a real preflight run on this development machine (§6).
Preflight does **not** compare against them — it only confirms each file
exists and is not zero-length, then reports the size it found so **you** can
eyeball it against this table. A DLL that is truncated to a small but
nonzero size, or is simply the wrong version, will still pass — only a
completely empty (0-byte) file is caught automatically. If your copy's
reported size differs noticeably from the table, re-copy it from the SDK
rather than assuming it's fine; a size of a similar order but not identical
can be legitimate (SDK versions differ across installs), a size wildly off
or near-zero is not.

**Check that all seven are present, from the repository root:**

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

The activation command depends on which shell you're using. Getting this
wrong is easy to miss: the wrong command does not error, it just fails to
activate, and `pip install` then silently installs into your global Python
instead of the venv.

**PowerShell:**

```
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt        # lab machine
pip install -r requirements-dev.txt    # development machine
```

**cmd.exe:**

```
python -m venv .venv
.venv\Scripts\activate.bat
pip install -r requirements.txt        # lab machine
pip install -r requirements-dev.txt    # development machine
```

Do **not** use plain `.venv\Scripts\activate` (no extension) in PowerShell —
that file is a POSIX shell script. PowerShell does not error on it or run it;
it silently does nothing, `$env:VIRTUAL_ENV` stays empty, and every `pip
install` after that goes into the global interpreter with no sign anything is
wrong. After activating (either shell), confirm it worked before installing
anything: `echo $env:VIRTUAL_ENV` (PowerShell) or `echo %VIRTUAL_ENV%`
(cmd.exe) should print the path to `.venv`, not be empty.

(`conda` was not available to re-verify on the machine this README was written
on — the syntax above is standard conda usage. The **venv path was run
end-to-end in PowerShell** on this machine: a fresh venv, `.\.venv\Scripts\
Activate.ps1`, `pip install -r requirements-dev.txt`, then `python -m pytest`
and `python -m biocam.preflight` (from the repository root — see §6) both
succeeded from inside it — see the real output in §6 and §12.)

---

## 6. Preflight check

Run this before every experiment (§2) and any time the environment might have
changed, **from the repository root** (the directory containing this
README and the `biocam/` folder — not from inside `BioCam_DupleX_API/`;
`python -m biocam.preflight` needs `biocam` importable from the current
directory, and it fails with `ModuleNotFoundError: No module named 'biocam'`
from anywhere else, including `BioCam_DupleX_API/`, which §7 sends you into):

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

**Command line**, run from inside `BioCam_DupleX_API/` (not the repository
root):

```
python recorder.py --duration 10 --name test1
```

The cwd requirement here is **not** about the DLLs — `connector.py` finds
`API/` from its own file location (`os.path.dirname(__file__)`, connector.py
line 26), not from the current working directory, so DLL loading works
regardless of where you run from. The cwd matters for two other reasons:
`--output-dir` defaults to `recordings`, a path resolved relative to the
current working directory (see the table below); and `recorder.py` imports
`connector` by bare module name (`from connector import ...`), which only
resolves when `BioCam_DupleX_API/` is on the Python path — guaranteed when
you run `python recorder.py` from inside that directory. If you see a DLL
error, the cause is a missing/misplaced file in `API/` (§4), not the working
directory.

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

**Return to the repository root when you're done recording.** Both commands
above must be run from inside `BioCam_DupleX_API/`, but preflight (§6) and
the full gate checklist (§13, item 4) must be run from the repository root —
staying inside `BioCam_DupleX_API/` after a recording session will make the
next preflight run fail with `ModuleNotFoundError: No module named 'biocam'`.

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
  - `biocam/interop/` — **Layer 1**: .NET interop via `pythonnet`. The only package allowed to import `clr`. Mostly needs the instrument; `reflect.py` and `verify_stim_model.py` are the exceptions, needing the DLLs but no BioCAM (§15).
  - `biocam/data/` — **Layer 2**: pure byte/number logic (payload decoding, frame reassembly, unit conversion, integrity). Fully testable with synthetic buffers.
  - `biocam/stim/` — **Layer 2**: stimulation modelling — pulses, electrode patterns, trains, arbitrary sequences, and the validation that keeps the driver from silently altering any of them (§15). Pure arithmetic; no `clr`, no device.
  - `biocam/analysis/` — **Layer 3**: signal processing (spike detection, sorting). Fully testable against fixtures. Populated starting Phase 5.
  - `biocam/cli.py` — the `record`, `convert` and `stim` subcommands
  - `biocam/session.py` — the recording loop that joins a packet source to a writer
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

`tests/fixtures/`, used by **Layer 3** tests as a **replay source** — real
recorded signal rather than synthetic data, because real recordings contain
noise, drift, and artifacts nobody thinks to simulate. Layer 2 tests use
synthetic buffers instead, not these fixtures: a decoder test needs an exact
expected output, and only a constructed input lets you assert one (§12 "The
three-layer rule"; `.claude/agents/dsp-implementer.md`).

- `sample_32ch_2s` — 37,115 frames × 32 channels (2,375,360 bytes). The 32
  most active channels (by variance) from a real session, 2 seconds.
- `sample_full_100frames` — 100 frames × 4096 channels (819,200 bytes). Full
  channel width, for testing anything that depends on `total_channels == 4096`.

Both are cut from the same source recording (`20260624_140615`), byte-for-byte
identical to it over the slices taken. That source recording's own
completeness was never verifiable: no subscription to `DataLoss` existed when
it was captured, and its packet log recorded wall-clock time rather than
hardware timestamps, so a dropped packet during capture would be invisible in
hindsight (design spec Appendix A, item 2). The fixtures are still useful —
they carry real noise and artifacts synthetic data wouldn't — just don't treat
them as certified gap-free.

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
| 1 | Acquisition: recording, saving, data integrity, on the three-layer split | **Done and merged.** Gate 1 clean. Never run on the instrument — issues #11–#18 |
| 2 | Stimulation engine + manual and scheduled triggering | **In progress.** `biocam/stim/` and `biocam/interop/stimulator.py` written, Gate 1 clean. No stimulus ever delivered — issues #21–#24 (§15) |
| 3 | Session control: recording and stimulation together, changing live | Not started |
| 4 | UI | Not started |
| 5 | Spike detection | Not started |
| 6 | Closed-loop stimulation (depends on Phase 5) | Not started |

If this table and the roadmap document ever disagree, the roadmap document is
correct — this table is a pointer, not a second source of truth.

---

## 15. Stimulation

**Nothing in this section has ever run on the instrument.** The API was
recovered by reading the shipped assemblies; the code was checked against them;
no stimulus has been delivered. Issues #21–#24 carry the procedures, and #21
comes first because everything here is parameterised on numbers only the
instrument can report.

### 15.1 The thing to understand before you use it

The driver's `RectangularStimPulse` **adjusts pulses it does not like instead
of rejecting them.** Measured against the real assembly:

| You ask for | You get | Anything raised? |
|---|---|---|
| amplitude 2000 µA (range ±1000) | 1000 µA | no |
| amplitude 7.0 µA on a 5 µA grid | 5.0 µA | no |
| widths 8000/0/8000 ticks (cap 10000) | **8000/0/2000** | no |

The last row is why this layer exists. A **charge-balanced** request — equal
and opposite phases, net charge zero — comes back as one that injects a net
600 nC, because the overflow is taken off the *later* phases. And
`IsBiphasic` still reports `True`, so there is no signal in the returned object
that anything happened.

Net DC through a microelectrode drives electrolysis and corrodes it. The run
does not fail; it looks fine.

So `biocam/stim/` **refuses rather than adjusts**. A pulse either passes
unchanged or is rejected with every reason listed at once, because a colleague
on the instrument gets one attempt per turnaround. After the driver builds the
pulse, `verify_built_pulse()` reads back all five fields and compares them to
what was asked, which also catches limits this repository does not know about.

### 15.2 Check a protocol without an instrument

`--dry-run` needs no BioCAM and no DLLs. Use it before a lab session, not
during one:

```
python -m biocam.cli stim --dry-run --time-resolution-us 10 \
    --amplitude 100 --phase-us 200 --gap-us 100 \
    --positive 10,10 --negative 20,30 \
    --count 5 --rate-hz 10
```

```
cli-train: 5 x [cli-pulse: +100 uA for 200 us, gap 100 us, -100 uA for 200 us
                (500 us total, 50 ticks; balanced)]
           every 100000 us (10 Hz), starting at 0 us, lasting 400500 us
positive: (10,10)
negative: (20,30)
timestamps (us from start of acquisition): 0, 100000, 200000, 300000, 400000
train net charge: +0 pC

NOT SENT (--dry-run). The constraints above were supplied on the command line,
not read from an instrument; if they differ from the device's, this plan is wrong.
```

`--time-resolution-us` is **required** for `--dry-run` and deliberately has no
default. It is the stimulator's clock period, and every duration is an integer
count of it — guess it wrong and every pulse is wrong by that ratio, silently.
Get the real value from issue #21.

Amplitudes are µA and durations are µs throughout. The second phase mirrors the
first unless you say otherwise, because that is the charge-balanced pulse.

### 15.3 Sending for real

Drop `--dry-run`. The constraints are then read from the device and the
command-line limits are ignored:

```
python -m biocam.cli stim --amplitude 100 --phase-us 200 --gap-us 100 \
    --positive 10,10 --negative 20,30
```

**Electrode coordinates are 1-based**, matching `ChCoord`: the first electrode
is `1,1`, not `0,0`. `--grid` (default `64x64`) bounds-checks them — and it is
the *only* thing that does, because `ChCoord.IsValid` reports `(65,65)` as
valid on a 64×64 plate.

### 15.4 What will refuse to run, and why

| Refusal | Reason |
|---|---|
| net charge is not zero | electrolysis; pass `--allow-unbalanced` if deliberate |
| total duration over `MaxPulseDuration` | the driver would shorten the later phases silently |
| amplitude out of range, or off the resolution grid | the driver would clamp or round it silently |
| a duration that is not a whole number of ticks | the driver would snap it silently |
| electrode outside the grid, or 0-indexed | `ChCoord` would not catch it |
| positive and negative sharing a column | the API PDF forbids it; `--no-column-rule` overrides |
| train period shorter than the pulse | the stimuli would overlap — never waived, it is arithmetic |
| train period below 1000 µs | the driver's own minimum; `--allow-short-period` overrides |

### 15.5 Scheduled trains fire on acquisition time, not wall-clock

The XML is explicit that timestamps are *"in microsecond relative to the
beginning of the acquisition"* — **not** relative to when you send them.

A train planned as "start in half a second" and sent ten minutes into a
recording has every timestamp ten minutes in the past. What the instrument does
then is untested (issue #24), and the plausible outcomes include firing the
whole train at once. `TrainPlan.shifted_by(current_acquisition_time_us)` does
the conversion — but note that nothing in this repository can yet *read* the
current acquisition time, which is the other half of issue #24.

**So `biocam stim --count N` cannot run yet.** Nothing on the `stim` path
starts an acquisition, so a scheduled train has no time origin, and the command
refuses with a clear message rather than sending one into an undefined one.
Single pulses work. Trains wait on issue #24 and on Phase 3, where recording
and stimulation are driven together.

One more thing to expect on the first session: **`Start()` and `Send()` have
never been tried with no acquisition running**, and `biocam stim` without a
recording is exactly that case. The XML does not list streaming among `Send`'s
preconditions and the vendor's sample never tests it, so this software warns
rather than refusing — but if the very first `biocam stim` throws, that is the
likely reason, and it is worth reporting on issue #22 rather than working
around.

### 15.6 Reading the API yourself

`_3Brain.Common` ships no XML here, but the assembly can be read directly.
This needs the DLLs and no instrument:

```
python -m biocam.interop.reflect RectangularStimPulse StimProperties
python -m biocam.interop.reflect --all-stim
python -m biocam.interop.verify_stim_model
```

The last one checks `biocam/stim/`'s rules against the real driver in **both**
directions: every pulse it accepts must come back unchanged, and every pulse it
refuses must be one the driver would have altered. A rule that blocks a pulse
the driver builds correctly is a bug, not caution. Run it after any change to
the validation.

The full recovered surface, the measurements behind every claim above, and the
list of what still needs the instrument are in
`docs/api/stimulation-reference.md`.
