# First hardware test: record, stimulate, save

Step-by-step instructions for the first time this software runs on the BioCAM
DupleX. **No cells on the chip.** Nothing in this repository has ever run on
the instrument, so each step below is being tried for the first time. The
point is not only "does it work", but to write down **exactly** what happens,
so that anything unexpected can be fixed from 600 km away.

- **Time needed:** about 90 minutes, including setup.
- **You need:** the lab PC (Windows), the BioCAM DupleX with its USB cable, one
  MEA chip, this repository, and a GitHub account to post results.
- **You do not need:** cells, an oscilloscope, or any programming.

**The golden rule:** if anything differs from the "Expected" box, **do not try
to fix or work around it.** Take a screenshot, copy the terminal text, write
down what you did, and report it (§ Reporting). Then carry on with the next
test unless the test says to stop. A clear description of something
unexpected is worth more than a run that "eventually worked".

---

## Contents

- [Reporting](#reporting)
- [Part A: setup (once)](#part-a-setup-once)
- [Part B: the chip](#part-b-the-chip)
- [T1 Preflight](#t1-preflight-5-min)
- [T2 Probe: read the instrument, send nothing](#t2-probe-read-the-instrument-send-nothing-5-min)
- [T3 Record 10 seconds from the command line](#t3-record-10-seconds-from-the-command-line-5-min)
- [T4 Convert the recording to HDF5](#t4-convert-the-recording-to-hdf5-5-min)
- [T5 Record 30 seconds in the window](#t5-record-30-seconds-in-the-window-10-min)
- [T6 Single stimuli, and is the picture the right way round?](#t6-single-stimuli-and-is-the-picture-the-right-way-round-10-min)
- [T7 A train of stimuli](#t7-a-train-of-stimuli-5-min)
- [T8 Release the instrument to BrainWave](#t8-release-the-instrument-to-brainwave-5-min)
- [T9 An interrupted connection gives the instrument back](#t9-an-interrupted-connection-gives-the-instrument-back-5-min)
- [Summary comment](#summary-comment)

---

## Reporting

Results go to **GitHub issue comments** on
<https://github.com/cristinalakasz/BioCam_DupleX_API/issues>. Each test names
its issue. Post one comment per test, **including tests that went exactly as
expected**: "worked as described" is a result too, and it lets the issue be
closed.

Copy this into each comment and fill it in:

```
**Test:** T_  (docs/lab/first-hardware-test.md)
**Date / time:**
**Chip:** liquid (which: ______) / dry
**Result:** AS EXPECTED / DIFFERENT / FAILED
**What I did** (only if different from the steps):
**What happened** (only if different from "Expected"):

Terminal output:
```
(paste the full terminal text here, between the triple backticks)
```

Attached: (screenshots, and the small files listed in the test)
```

To attach a file, drag it into the GitHub comment box. **Never attach `.raw`
or `.h5` files**: they are gigabytes. Screenshots: `Win + Shift + S`, then
paste into the comment with `Ctrl + V`.

**Copying terminal text:** in PowerShell, select the text with the mouse and
press `Enter` (or right-click) to copy. If the text scrolled away, run the
command again rather than retyping from memory.

---

## Part A: setup (once)

Do this once, on the lab PC. Each step says what you should see.

1. **Close BrainWave** and any other 3Brain program. Only one program can
   control the BioCAM at a time.
2. Connect the BioCAM by USB and switch it on.
3. Open **PowerShell** (Start menu → type `PowerShell` → Enter).
4. Go to the repository and get the latest version:
   ```
   cd C:\path\to\BioCam_DupleX_API
   git pull
   ```
   Expected: `Already up to date.` or a list of updated files. Replace
   `C:\path\to\BioCam_DupleX_API` with where the repository actually is.
5. Create and activate the Python environment (first time only for the first
   two lines):
   ```
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1
   pip install -r requirements.txt
   ```
   Expected: the prompt now starts with `(.venv)`, and `pip` ends with
   `Successfully installed ...` including `pythonnet-3.1.0` and `h5py`.
   **Every later time**, only the middle line is needed: open PowerShell, `cd`
   to the repository, run `.\.venv\Scripts\Activate.ps1`.
6. **Copy the seven 3Brain DLLs** from the BrainWave / 3Brain SDK installation
   into `BioCam_DupleX_API\API\` inside the repository:
   `3Brain.BioCamDriver.dll`, `3Brain.Common.dll`,
   `3Brain.Deployment.Drivers.dll`, `3Brain.Diagnostic.dll`,
   `3Brain.Processing.Core.dll`, `3Brain.Processing.Native.dll`,
   `Newtonsoft.Json.dll`. Then unblock them (Windows refuses to load DLLs it
   thinks came from the internet):
   ```
   Get-ChildItem .\BioCam_DupleX_API\API\*.dll | Unblock-File
   ```
   Expected: no output.
7. **Choose a folder for the test recordings** on a local drive with **at least
   30 GB free**. Not OneDrive, not a network drive, not a synced folder. The
   instructions below use `D:\biocam-test`; if yours is different, replace it
   everywhere. Create it:
   ```
   mkdir D:\biocam-test
   ```
   A recording writes about **152 MB per second** (9 GB per minute).

**Keep this PowerShell window open for the whole session**, in the repository
folder, with `(.venv)` at the start of the prompt. Every command below is typed
there.

---

## Part B: the chip

1. Seat an MEA chip on the DupleX head, **with no cells**, as you normally
   would.
2. **Decide: liquid or dry.** This changes what the stimulation tests can show.
   - **Liquid** (saline, PBS or medium, whatever your lab uses and is safe for
     the chip): a stimulus produces a visible **artefact** in the recording, so
     T6 and T7 can confirm that pulses really leave the stimulator, and that
     the array picture is not transposed.
   - **Dry:** recording can be tested, but a stimulus cannot be seen in the
     recording. T6 and T7 can then only confirm that the software sent the
     pulses without error, not that they were delivered.

   Either is useful. **Write which one in every report.** If you can, do the
   whole protocol dry first, then add liquid and repeat T6 and T7.
3. **Stimulation amplitude.** Nothing in this repository says what amplitude is
   safe for your chip. Before T6, check 3Brain's documentation for the chip or
   ask whoever is responsible for it. The tests below start at **10 µA** and go
   up only if no artefact is visible. Always write the amplitude you used in the
   report.

---

## T1 Preflight (5 min)

**Checks:** that this PC can run the software. Does not touch the BioCAM.
**Report on:** issue **#16**.

1. In PowerShell, type:
   ```
   python -m biocam.preflight
   ```

**Expected:** every line starts with `[PASS]`, and the last line is
`ALL CHECKS PASSED`. In particular, look for these lines:

```
[PASS] package pythonnet   ...
[PASS] 3Brain.BioCamDriver.dll   <size> bytes
[PASS] 3Brain assemblies load    3Brain.Common, 3Brain.BioCamDriver
```

**If a line says `[FAIL]`:**

| The failing line | Most likely cause | What to do |
|---|---|---|
| `package ...` | step A5 did not finish | run `pip install -r requirements.txt` again, then retry T1 |
| a `.dll` line | the file is missing or empty | redo step A6 |
| `3Brain assemblies load` | DLLs blocked, or .NET Framework missing | run the `Unblock-File` line from A6, retry; if it still fails, **report it** (paste the whole line) and stop here |

Report the full output in any case.

---

## T2 Probe: read the instrument, send nothing (5 min)

**Checks:** that the software can connect to the BioCAM and read what it
reports about itself: data format, clock, stimulator limits, and which
electrodes can be used for stimulation. **No pulse is sent and nothing is
recorded.** Safe with anything on the chip.
**Report on:** issues **#11**, **#21** and **#23** (post the same comment on
all three; each issue reads its own section).

1. Check that BrainWave is closed.
2. Type:
   ```
   python -m biocam.cli probe --output-dir D:\biocam-test
   ```
3. Wait. The electrode check at the end reads 4096 electrodes and can take up
   to a minute.

**Expected** (the values are what we want to learn; the structure should look
like this):

```
BioCamDevice: Activate() succeeded with no arguments ... SupportBioCamWithInvalidSerial=False
== CONNECT
claimed the BioCAM: IsConnected and MeaPlate.IsConnected are true

== DATA FORMAT (issue #11)
FrameRate                    = 18557.72...
NWells                       = 1
NChsPerWell                  = 4096
...
== CLOCK
ClockCyclesToMilliseconds: <a number> clock cycles per microsecond

== STIMULATOR (issue #21) - Initialize only; nothing is sent
Stimulator.MaxPulseCount     = ...
...
Properties.UnitMeasureString = 'µA'
...
== ELECTRODE ENDPOINTS (issue #23), 64x64 grid
electrodes checked: 4096
without an endpoint (None): 0
endpoint not valid or not internal: 0
raised an error: 0

== RELEASED the stimulator and the BioCAM

saved: D:\biocam-test\probe_<date>_<time>.txt
```

Things to look at, and say in the report if they differ:

- `FrameRate` is about **18557.72**, `NWells` is **1**, `NChsPerWell` is
  **4096**.
- Nothing in the `DATA FORMAT` or `STIMULATOR` sections says `FAILED`.
- `Properties.IsCurrentStimulator = True` and `UnitMeasureString = 'µA'`. If it
  says a voltage unit, **stop and report**: every amplitude in this software
  would be wrong.
- `Stimulator.TimeResolutionMicroSec` and `Properties.TimeResolutionMicroSec`
  show the same number.
- `without an endpoint (None): 0`, `endpoint not valid or not internal: 0` and
  `raised an error: 0`. If any is not zero, the probe lists the electrodes (or
  the first error); paste them. **If they are not all zero, do not stimulate
  on the listed electrodes in T6 and T7**; choose others and say which.
- Any line that says `is a method, not a property - not called`, or `FAILED`
  next to `Properties.MinTime` or `Properties.IsValid`: that is fine, those two
  are being checked for existence. Report them anyway.

**If it fails:**

| What you see | Most likely cause | What to do |
|---|---|---|
| `FAILED: ... No free BioCAM found` after about 30 s | BrainWave open, USB unplugged, or BioCAM off | close BrainWave, check cable and power, retry once; report either way |
| `The MEA plate is not seated.` | chip not detected | reseat the chip, retry once; report either way |
| `stimulator unavailable: ...` | the stimulator did not initialize | report the line exactly. Recording tests (T3 to T5) can still run |

**Attach:** the `probe_<date>_<time>.txt` file from `D:\biocam-test`.

---

## T3 Record 10 seconds from the command line (5 min)

**Checks:** the basic recording path: streaming, writing to disk, loss
detection, and the integrity record.
**Report on:** issue **#16**.

1. Check that BrainWave is closed.
2. Type:
   ```
   python -m biocam.cli record --duration 10 --name t3_record10s --output-dir D:\biocam-test
   ```
3. Wait about 15 seconds. Do not touch anything.
4. Then type:
   ```
   echo $LASTEXITCODE
   ```

**Expected in the terminal:**

- A `DataFormat probe` block with five values and no `FAILED`.
- `Recording to D:\biocam-test\t3_record10s.raw (4096 channels at 18557.72 Hz)`.
- **No** line containing `GAP`, `QUEUE OVERFLOW`, `DRIVER DATA LOSS`,
  `Queue ... falling behind` or `DISK LOW`.
- `Stopped (duration_reached): about 185,577 frames, integrity verdict 'clean'`
  (18,557.72 frames per second × 10 s; a few hundred more or fewer is fine).
- An `End-of-run summary` including an `ACQUISITION CLOCK:` line.
- `echo $LASTEXITCODE` prints `0`.

**Expected files** in `D:\biocam-test`:

- `t3_record10s.raw`, about **1.5 GB**. Check the size in File Explorer.
- `t3_record10s_meta.json`. Open it in Notepad and check that it says:
  - `"status": "complete"` and `"stop_reason": "duration_reached"`
  - `"n_frames_written":` about 185,577
  - inside `"integrity"`: `"verdict": "clean"`, `"n_frames_missing": 0`,
    `"gaps": []`, and `0` for `driver_loss_events`, `queue_overflows`,
    `callback_errors`, `payload_length_mismatches`, `counter_anomalies`,
    `discarded_at_stop` and `timestamps_unavailable`.

**If different:** the sidecar says which kind of loss happened; that is the
point of it. Report the whole terminal output and attach the `_meta.json`. If
the verdict is `gaps_detected`, run T3 once more with `--packet-ms 10` added
to the command (name it `t3_record10s_packet10`) and report both.

**Attach:** `t3_record10s_meta.json`.

---

## T4 Convert the recording to HDF5 (5 min)

**Checks:** that a recording can be turned into a standard HDF5 file, and that
the copy is exact.
**Report on:** issue **#16** (same comment as T3 is fine).

1. Type:
   ```
   python -m biocam.cli convert D:\biocam-test\t3_record10s.raw D:\biocam-test\t3_record10s_meta.json D:\biocam-test\t3_record10s.h5
   ```

**Expected:** a line with the number of frames (the same as T3), then
`Verified: the HDF5 file reproduces the raw data exactly.` A file
`t3_record10s.h5` of about 1.5 GB appears.

**If different:** paste the output. `VERIFICATION FAILED` means the file
cannot be trusted; say so clearly.

---

## T5 Record 30 seconds in the window (10 min)

**Checks:** the operator window driving the real instrument: recording, the
live array picture, and live traces. **No stimulation.**
**Report on:** issues **#16** and **#32**.

1. Type:
   ```
   python -m biocam.ui --live --output-dir D:\biocam-test
   ```
   **Expected:** a window opens with a **red** banner: `LIVE — stimuli will be
   delivered to the preparation`. The title bar says `LIVE INSTRUMENT`. If the
   banner is blue, close the window and report: it is not driving the
   instrument.
2. In the **Recording** column:
   - **Name (optional):** type `t5_window30s`
   - **Duration (s):** type `30`
   - **Run until I press Stop:** leave unticked
3. In the **Stimulus** column, delete everything in **Positive electrode(s)**
   and in **Negative electrode(s)**, so both are empty. The text under
   **Stimulate now** then reads `no electrodes given`. That is expected: this
   test does not stimulate.
4. In the **Spikes and closed loop** column: **Detect spikes** unticked, **Draw
   traces** ticked, **Close the loop** unticked.
5. Click **Start recording**.
6. **Expected within a few seconds:**
   - Session log: `Recording to D:\biocam-test\t5_window30s.raw (4096 channels
     at 18557.72 Hz)`.
   - Status panel: **Elapsed** counts up; **Frames** increases by about 18,558
     every second; **Frames missing** stays `0`.
   - The **Electrode array** fills with colour, a 64 × 64 grid, and the line
     below it reads `peak-to-peak <low> - <high> uV`.
   - **Take a screenshot** of the whole window now.
7. While it records, click **three electrodes** on the array: one near the top
   left, one in the middle, one near the bottom right. **Expected:** a trace
   lane appears under the array for each, starting empty and filling from the
   moment you clicked. Hover over an electrode: the line under the array shows
   its coordinates and a µV reading.
8. Wait until the 30 seconds are over. **Expected:**
   - **Status** `finished`, **Verdict** `clean`, **Frames** about 556,700,
     **Frames missing** `0`, **Stimuli delivered** `0`.
   - The session log ends with `Finished: duration_reached, <frames> frames,
     verdict clean`.
   - Any brown warning text in the Recording column: copy it into the report
     word for word.
   - **Take a second screenshot.**
9. Leave the window open for T6.

**Expected files** in `D:\biocam-test`: `t5_window30s.raw` (about 4.6 GB),
`t5_window30s_meta.json`, `t5_window30s_session.json`. There is no
`_stimuli.json`, because nothing was stimulated.

**Also look at** the terminal where you started the window: copy any text it
printed.

**Report:** both screenshots; the terminal text; the session log text (click
in the log, `Ctrl + A`, `Ctrl + C`). **Attach** `t5_window30s_meta.json` and
`t5_window30s_session.json`. For #32, say whether the window felt responsive
or froze at any moment.

---

## T6 Single stimuli, and is the picture the right way round? (10 min)

**Checks:** that pressing *Stimulate now* delivers a pulse, and (with liquid)
that the electrode you click is the electrode that is stimulated. That second
point matters: if the picture were transposed, clicking one electrode would
stimulate another, and nothing would say so.
**Report on:** issues **#22** and **#31**.

**Read Part B.3 about amplitude first.**

1. In the window (still open from T5), **Recording** column:
   - **Name:** `t6_single_pulses_10uA`
   - **Duration (s):** `60`
2. **Stimulus** column, type exactly:
   - **Amplitude (µA):** `10`
   - **Phase duration (µs):** `200`
   - **Inter-phase gap (µs):** `100`
   - **Second phase mirrors the first:** ticked
   - **Positive electrode(s):** `10,40`
   - **Negative electrode(s):** `50,20`

   These two electrodes are chosen on purpose: they are far apart, in
   different columns, and not mirror images of each other across the
   diagonal. On the array you should now see a **red** square about one sixth
   of the way down and two thirds of the way across, and a **blue** square
   about four fifths down and one third across.
3. **Draw traces** ticked, **Detect spikes** unticked, **Close the loop**
   unticked. The trace lanes will show electrodes 10,40 and 50,20.
4. Click **Start recording**. Wait until **Frames** is counting.
5. Look under **Stimulate now**. **Expected:** text beginning `ui-pulse: +10 uA
   for 200 us, gap 100 us, -10 uA for 200 us` in green. If the text is **red**,
   the stimulator refused the pulse: copy the red text into the report exactly,
   click **Stop**, and skip to T8.
6. Click **Stimulate now** once. Wait 5 seconds. Click it again. Wait 5
   seconds. Click it a third time.
7. **Expected after each click:**
   - The session log shows `Requested: ui-pulse: +10 uA ...`.
   - **Stimuli delivered** goes up by one: 1, 2, 3.
   - **With liquid:** a sharp spike (the stimulus artefact) appears on both
     trace lanes at the moment of the click, and the array briefly shows
     bright cells **at the red and blue squares**.
   - **Dry:** probably nothing visible in the traces or on the array. That is
     expected. Say what you saw.
8. **Take a screenshot** right after the third click.
9. Wait until the 60 seconds are over (or click **Stop**).

**With liquid, the key question for #31:** where did the bright cells
appear?

- **At the red square (row 10, column 40)**, one sixth down and two thirds
  across: **correct**.
- **At row 40, column 10**, two thirds down and one sixth across: the picture
  is **transposed**. Report it clearly. **Do not stimulate any further in this
  session**, because the electrode you click is not the one stimulated.
- **Somewhere else, or everywhere:** describe it, with the screenshot.

**If no artefact is visible with liquid at 10 µA:** repeat steps 1 to 9 with
**Name** `t6_single_pulses_50uA` and **Amplitude** `50`, then, if still
nothing, `t6_single_pulses_100uA` and `100`, provided your chip's safe limit
(Part B.3) allows it. Stop at the first amplitude that shows an artefact.

**Expected files:** for each run, `.raw`, `_meta.json`, `_session.json`, and
now **`_stimuli.json`**. Open `_stimuli.json` in Notepad: `"n_attempted": 3`,
`"n_delivered": 3`, `"simulated": false`; each of the three entries has
`"outcome": "sent"` and a number (not `null`) for `"latency_cycles"`.

**Report:** the screenshot, the session log text, the terminal text, whether
it was liquid or dry, the amplitude(s) used, where the bright cells appeared.
**Attach** the `_stimuli.json` and `_session.json` of each run.

---

## T7 A train of stimuli (5 min)

**Checks:** that a scheduled train fires at the right times. Nobody knows yet
how the instrument treats scheduled pulses; this test finds out.
**Report on:** issue **#24**.

Only if T6 showed no problem. Use the **lowest amplitude that showed an
artefact in T6** (dry: `10`).

1. **Recording** column: **Name** `t7_train`, **Duration (s)** `30`.
2. **Stimulus** column: the same values as T6 (amplitude as above, `10,40`
   positive, `50,20` negative).
3. **Train** section:
   - **Pulses:** `10`
   - **Rate (Hz):** `10`
   - **Starts in (ms):** `500`

   **Expected** below **Send train**: green text `ui-train: 10 x [...] every
   100000 us (10 Hz), starting at 500000 us ...`.
4. Click **Start recording**. Wait 5 seconds.
5. Click **Send train** once.
6. **Expected with liquid:** half a second after the click, ten artefacts in
   the trace lanes, evenly spaced over one second. Watch closely: possible
   wrong outcomes are all ten at once, fewer than ten, none, or not evenly
   spaced. Any of those is exactly what #24 needs to know.
   **Dry:** nothing visible; report what you saw.
7. Take a screenshot about 2 seconds after clicking.
8. Wait until the recording finishes.

**Expected in `t7_train_stimuli.json`:** one entry with `"kind":
"scheduled"`, `"outcome": "sent"`, and ten numbers in
`"requested_timestamps_us"`, each 100000 larger than the previous one.

**Report:** the screenshot, the session log text, and what the artefacts did.
**Attach** `t7_train_stimuli.json` and `t7_train_session.json`.

---

## T8 Release the instrument to BrainWave (5 min)

**Checks:** that this software gives the BioCAM back cleanly.
**Report on:** issue **#16**.

1. In the window, click **Release instrument**. **Expected:** the session log
   says `Instrument released.`
2. Close the window (the X in the corner). **Expected:** it closes without an
   error in the terminal.
3. Open **BrainWave**. **Expected:** BrainWave finds the BioCAM and the chip as
   usual.
4. Close BrainWave again.

**If BrainWave cannot find the device:** unplug and replug the USB, then try
BrainWave again. Report both what happened first and whether replugging fixed
it.

---

## T9 An interrupted connection gives the instrument back (5 min)

**Checks:** a recent fix. If the software is stopped while waiting for the
BioCAM, it must still release it, so the next attempt works without
restarting anything.
**Report on:** issue **#16**.

1. **Open BrainWave** and let it take the BioCAM. That makes the device busy
   on purpose.
2. In PowerShell, type:
   ```
   python -m biocam.cli probe --output-dir D:\biocam-test
   ```
   It waits, because BrainWave holds the device.
3. After about 10 seconds, press **Ctrl + C**. **Expected:** `INTERRUPTED
   (Ctrl+C). The BioCAM was released.` and a `saved:` line.
4. **Close BrainWave.**
5. Run the same probe command again. **Expected:** it completes as in T2 and
   ends with `== RELEASED the stimulator and the BioCAM`.

**If step 5 fails** with `No free BioCAM found`: the instrument was not given
back. Report that, then unplug and replug the USB before doing anything else.

---

## Summary comment

When you have finished, post one more comment on issue **#16**:

```
**First hardware test — summary** (docs/lab/first-hardware-test.md)
Date:            Chip: liquid (____) / dry
Amplitude(s) used:

| Test | Result (AS EXPECTED / DIFFERENT / FAILED / SKIPPED) | One line |
|---|---|---|
| T1 Preflight | | |
| T2 Probe | | |
| T3 Record 10 s (CLI) | | |
| T4 Convert to HDF5 | | |
| T5 Record 30 s (window) | | |
| T6 Single stimuli | | |
| T7 Train | | |
| T8 Release to BrainWave | | |
| T9 Interrupted connection | | |

Anything else that surprised me:
```

When everything is done, the recordings in `D:\biocam-test` can be deleted,
except the small `.json` and `.txt` files, which you may want to keep.
