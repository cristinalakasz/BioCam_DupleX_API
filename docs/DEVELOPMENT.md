# Development guide

For people changing the code. The lab manual is the [README](../README.md).

**The core constraint:** the BioCAM is ~600 km from the development machine.
No hardware-dependent code can be run here. A colleague on site runs it days
later and reports back. Never describe instrument code as working. Say that it
is untested, and name exactly what the lab must check. `CLAUDE.md` holds the
full rules for AI-assisted sessions.

---

## 1. Environment

**Python 3.12** (preflight enforces `>= 3.12`).

| Machine | Install | Pins |
|---|---|---|
| Lab (BioCAM attached, Windows 10/11, .NET Framework 4.7+) | `pip install -r requirements.txt` | numpy, pythonnet, h5py |
| Development (no instrument, any OS) | `pip install -r requirements-dev.txt` | numpy, pytest, h5py (**no pythonnet**) |

The development set deliberately excludes `pythonnet`: the test suite must run
with no SDK installed, and `tests/test_no_hardware_imports.py` enforces that.

With venv on Windows, activate with `.\.venv\Scripts\Activate.ps1`
(PowerShell) or `.venv\Scripts\activate.bat` (cmd.exe). Plain `activate` in
PowerShell does nothing, silently, and `pip` then installs globally. Confirm
with `echo $env:VIRTUAL_ENV` before installing. With conda: `conda create -n
biocam python=3.12 && conda activate biocam`.

Run everything from the repository root. `python -m biocam...` fails with
`ModuleNotFoundError: No module named 'biocam'` from anywhere else.

## 2. The DLLs

The driver needs seven 3Brain DLLs. They are **not committed**: `.gitignore`
excludes `*.dll`, they are licensed SDK files, and they total about 70 MB. Copy
them from the 3Brain SDK or BrainWave installation into
`BioCam_DupleX_API/API/`:

| File | Size on the dev machine (bytes) |
|---|---|
| `3Brain.BioCamDriver.dll` | 4,191,232 |
| `3Brain.Common.dll` | 545,792 |
| `3Brain.Deployment.Drivers.dll` | 2,174,976 |
| `3Brain.Diagnostic.dll` | 18,944 |
| `3Brain.Processing.Core.dll` | 12,108,288 |
| `3Brain.Processing.Native.dll` | 50,722,816 |
| `Newtonsoft.Json.dll` | 711,952 |

To build 3Brain's C# sample app (`SampleApp_BioCamCL/`), its `.csproj` also
expects six of these in `SampleApp_BioCamCL/Dependencies/`, plus
`FTD3XX_NET.dll` (an FTDI USB driver, not tracked here).

## 3. Preflight

`python -m biocam.preflight` checks:

- the Python version;
- that `numpy`, `h5py` and `pythonnet` are importable (`pythonnet` fails on a
  development machine, correctly: that machine cannot drive the instrument);
- that each of the seven DLLs exists in `BioCam_DupleX_API/API/` and is not
  empty, printing each size for comparison with the table above;
- that `3Brain.Common` and `3Brain.BioCamDriver` load into the .NET runtime
  (`load_assemblies`). This makes no USB call and claims nothing, so it is safe
  while another process holds the BioCAM. It catches a missing .NET Framework,
  a 32/64-bit mismatch, or DLLs Windows has blocked.

It does not detect the device or the plate. It exits 0 on a pass and 1 on a
failure. The assembly-load check has never run on the lab machine: report its
line from the first preflight there.

## 4. The layers

| Package | Layer | Testable here? |
|---|---|---|
| `biocam/interop/` | **1**: .NET interop through pythonnet. The only package allowed to import `clr`. | No: written and reviewed by hand against the XML and the C# sample. `reflect.py` and `verify_stim_model.py` are exceptions: they need the DLLs but not the instrument. |
| `biocam/data/` | **2**: bytes and numbers. Recording writer, sidecar, gap tracking, clock, frame decoding, activity monitor, traces, replay. | Yes, fully |
| `biocam/stim/` | **2**: stimulation modelling and validation. Pulses, trains, electrode patterns, constraints, stimulus log. Pure arithmetic. | Yes, fully |
| `biocam/analysis/` | **3**: filters, spike detection, sorting | Yes, fully |
| `biocam/session.py`, `loop.py`, `control.py`, `manifest.py`, `convert.py` | Orchestration: the per-packet session loop, the closed loop and its safety envelope, the stimulation queue, the session record, HDF5 export | Yes, against replayed packets and fake stimulators |
| `biocam/ui/` | The Tkinter window. `app.py` is the window, `arrayview.py` and `traceview.py` the drawings, `controller.py` the thread boundary, and `factories.py` the one place the live and simulated paths differ. | Partly: the tests need a Tk display and skip on a headless machine. The live path is untested. |
| `biocam/cli.py` | The `record`, `stim`, `convert` and `analyse` subcommands | Only the parts that avoid the instrument |

**Never write Layer 2 or 3 code without tests.** It is testable, so leaving it
untested is a choice, not a limitation.

### Ground truth for the .NET API

Never guess a member name, signature or behaviour. Check it against:

- `BioCam_DupleX_API/API/3Brain.BioCamDriver.xml`
- `BioCam_DupleX_API/SampleApp_BioCamCL/MainForm.cs`
- For `_3Brain.Common` types, which ship no XML: `python -m
  biocam.interop.reflect <TypeName>`. Recorded results are in
  [`docs/api/stimulation-reference.md`](api/stimulation-reference.md) and
  [`docs/api/device-reference.md`](api/device-reference.md). Regenerate them
  rather than trusting the transcription.
- `python -m biocam.interop.verify_stim_model` checks `biocam/stim/`'s
  validation against the real `RectangularStimPulse` in both directions.

### The callback rule

`DataReceived` runs on the driver's thread and is time-critical. Inside it:
no disk I/O, no printing or logging, no unbounded allocation, no locks. Hand
the payload to a bounded queue and return. A blocked callback drops samples
silently, and the recording looks like real signal, not an error.

## 5. Tests

```
python -m pytest
```

This runs without the instrument and without DLLs. **A green suite is evidence
for Layers 2–3, the orchestration and the simulated UI. It proves nothing about
Layer 1**, which the suite never executes. Say so whenever you report results.

**Fixtures** (`tests/fixtures/`) are real signal cut from the hardware
recording `20260624_140615`:

- `sample_32ch_2s`: 37,115 frames × 32 channels, the 32 most active channels;
- `sample_full_100frames`: 100 frames × 4096 channels.

That source recording's completeness was never verifiable, because the legacy
recorder did not subscribe to loss events. So treat the fixtures as realistic,
not as certified gap-free. They were cut with `tools/make_fixtures.py`, and
`tests/test_fixture_integrity.py` checks their sizes and provides
`load_fixture(name)`.

`tools/make_demo_recording.py` builds the synthetic 32×32 demo used to learn
the window. See README §3.

## 6. Gates

**Gate 1, before committing:**

- run the `biocam-api-verifier` agent on any Layer 1 change;
- run `realtime-safety-reviewer` on any change to the `DataReceived` path.

**Gate 2, before a lab session.** A lab session is a colleague's day on a
shared instrument and cannot be repeated on demand. Before one:

1. `biocam-api-verifier` is clean across **all** interop code, not just what
   changed.
2. `realtime-safety-reviewer` is clean across the whole data path.
3. The full test suite passes. This covers Layers 2–3 only.
4. Preflight runs and reports correctly (§3).
5. Every known-untested assumption is written down, so the colleague knows what
   is being tried for the first time and what to report. Every Layer 1 change
   since the last session belongs on this list.

## 7. Git workflow

- Never commit to `main` directly.
- Before branching: `git fetch origin`, then `git checkout main && git merge
  --ff-only origin/main`, then create the branch. If `--ff-only` is refused,
  stop and report it.
- To finish:
  1. push the branch;
  2. open the PR **before** merging;
  3. `git merge --no-ff` into `main`;
  4. run the full suite on the merged result;
  5. push `main`;
  6. delete the local branch.
- Never commit `.dll` files, `.raw` files outside `tests/fixtures/`, or `.env`.

## 8. Legacy scripts

`BioCam_DupleX_API/recorder.py`, `connector.py` and `Hello_BioCam.py` are the
original scripts, superseded by `biocam/`. Do not use them for experiments.
Their known defects:

- **In the data callback:** it writes to disk, prints, does numpy decoding, and
  appends to an unbounded list. This breaks every part of the callback rule.
- **Loss is invisible.** It subscribes to neither `DataLossAsync` nor
  `DataStreamingError`, and its packet log stores wall-clock time rather than
  the packet counter or the hardware timestamp.
- **An incomplete stimulator lifecycle.** It calls only `Initialize()` and
  `Close()`, never `Start`/`Stop`. `connector.py` never calls `Send`, so this is
  an incomplete lifecycle, not an observed silent failure. Issue #22 settles
  what the DupleX does.
- **It ignores the `StartDataStreaming` return value**, and some error paths
  leave the device claimed or the pool active.
- **Its sidecar format differs.** It has `n_wells`, `n_frames_total` and a
  `packet_log`, and it cannot be converted by `biocam.cli convert`.

The one real hardware recording, `BioCam_DupleX_API/recordings/20260624_140615`,
was made with it. Only its `_meta.json` is committed.

The full original defect list is Appendix A of
`docs/superpowers/specs/2026-08-03-claude-project-setup-design.md`.

## 9. Roadmap and status

| Phase | Contents | Status |
|---|---|---|
| 0 | Setup: `CLAUDE.md`, verifier agents, test scaffolding | Done |
| 1 | Acquisition: recording, integrity, HDF5 conversion | Merged. Never run on the instrument (issues #11–#18) |
| 2 | Stimulation engine, manual and scheduled | Merged (PR #25). No stimulus ever delivered (#21–#24) |
| 3 | Recording and stimulation together: clock, stimulus log, control queue | Merged (PR #28) (#26, #27) |
| 4 | The operator window | Merged (PR #29). Live path untested |
| 5 | Online spike detection | Merged (PR #35) |
| 6 | Closed-loop stimulation | Merged (PR #36) (#26, #27, #38, #39) |
| 7 | Spike sorting, `analyse`, the analysis panel | Merged (PR #37) |
| 8 | Live traces, trains from the window | Merged (PR #41) |
| 9 | Session record (`_session.json`), an independently shaped second phase | Merged (PR #42) |
| 10 | Stimulus timing, and fixes from the API review | Merged (PR #43) |

`docs/superpowers/specs/2026-08-12-api-roadmap-decomposition.md` holds the
original decomposition and its rationale. **Its status line is stale beyond
Phase 3.** Use this table for status and the spec for reasoning.

## 10. Project layout

```
biocam/                  the package (layers in §4)
  interop/ data/ stim/ analysis/ ui/
  cli.py session.py loop.py control.py manifest.py convert.py preflight.py
tests/                   pytest suite; tests/fixtures/ holds real-signal slices
tools/                   make_demo_recording.py, make_fixtures.py
docs/lab/                read before a session: storage-setup, closed-loop-budget, stimulus-timing
docs/api/                API surface read off the assemblies by reflection
docs/superpowers/        design specs and plans
docs/vendor/             correspondence with 3Brain
BioCam_DupleX_API/       legacy scripts, vendor XML and PDF, C# sample app, API/ (DLLs, gitignored)
.claude/                 agent definitions and permissions
CLAUDE.md                rules for AI-assisted development
requirements*.txt        lab and development dependencies
```
