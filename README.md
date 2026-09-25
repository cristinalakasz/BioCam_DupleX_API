# BioCAM DupleX control software

Software to record from, stimulate, and closed-loop stimulate a 3Brain
BioCAM DupleX high-density microelectrode array (4096 electrodes, 64 × 64).

**This is the lab manual**, written for the person who runs experiments.
Developer material — code layers, test gates, DLL details, the legacy scripts,
the roadmap — is in [`docs/DEVELOPMENT.md`](docs/DEVELOPMENT.md).

> **Built is not proven.** None of this software has ever run on the
> instrument: not one recording, not one stimulus. It was written ~600 km from
> the BioCAM, against the vendor's documentation and assemblies, and checked by
> hand. Everything below describes what the code is *designed* to do. §11 lists
> what is being tried for the first time and what to report. If something here
> is wrong, report it rather than working around it.

---

## Contents

1. [What the software can do](#1-what-the-software-can-do)
2. [Before every session](#2-before-every-session)
3. [Quick start: learn it on the demo](#3-quick-start-learn-it-on-the-demo)
4. [The operator window](#4-the-operator-window)
5. [Stimulation: what you must understand first](#5-stimulation-what-you-must-understand-first)
6. [Command line](#6-command-line)
7. [What a session leaves on disk](#7-what-a-session-leaves-on-disk)
8. [Spike detection and sorting](#8-spike-detection-and-sorting)
9. [Troubleshooting](#9-troubleshooting)
10. [Known limitations before a closed-loop session](#10-known-limitations-before-a-closed-loop-session)
11. [What is untested, and what to report](#11-what-is-untested-and-what-to-report)
12. [Glossary](#12-glossary)

---

## 1. What the software can do

**Record.** It streams all 4096 channels at 18,557.72 samples per second each
(about **152 MB/s, 9 GB per minute**) to a `.raw` file, exactly as the
instrument sends them. As it goes, it checks that no data was lost. The
instrument numbers every packet, so a skipped number is a **gap**. Every
recording ends with an **integrity verdict**: `clean`, `gaps_detected`, or
`unknown`.

**Stimulate.** It sends biphasic current pulses, or trains of them, through
chosen electrodes. The vendor driver silently *changes* pulses it does not like
(§5), so every pulse is checked first and **refused, with the reason, rather
than adjusted**. After the driver builds the pulse, it is read back to confirm
nothing changed. In the window, stimulating is only allowed while a recording
is running, and every attempt is logged, including refusals.

**Watch activity live.** The window colours each electrode by how much signal
it is picking up. It also draws live traces for up to 8 chosen electrodes,
drawn so that a spike cannot fall between two displayed points.

**Detect and sort spikes.** It filters the signal (300 Hz high-pass) and
detects spikes as dips below a threshold measured in units of each electrode's
noise. It can then sort each electrode's spikes into putative neurons with one
of three techniques. Detection runs live in the window or afterwards on a file.
Sorting runs in the window between recordings, or on a file.

**Close the loop.** It can stimulate automatically when spikes are detected
(policy *echo*), or when firing drops below a target (policy *rate*). A
**safety envelope** — a minimum interval and a maximum rate — sits after the
policy and cannot be overridden by it. Read §10 before using this on tissue.

**Rehearse without the instrument.** The window runs in **simulation mode**
against a recorded file. Only the packets and the stimulator are stand-ins;
everything else is the code that runs on the instrument.

**Leave a complete record.** Each recording leaves the signal, the acquisition
parameters and integrity evidence, and a description of what the experiment
was. If anything was stimulated, it also leaves every stimulus with its time
(§7).

---

## 2. Before every session

1. **Close BrainWave** and any other 3Brain software, including a leftover
   Python process. Only one process can control the BioCAM at a time.
   Otherwise `TakeBioCamControl()` returns `None` and every command fails.
2. **Seat the MEA plate** on the DupleX head before connecting. An unseated
   plate fails at `MeaPlate.IsConnected`.
3. **Run the preflight check from the repository root:**
   `python -m biocam.preflight`. It checks the Python version, the packages
   (`numpy`, `h5py`, `pythonnet`), that the 3Brain DLLs exist, and that they
   load into .NET. It does **not** detect the device or the plate: a pass
   means the software can run, not that the instrument is ready.
4. **Check free disk space.** A full-array recording is **~152 MB/s**
   (18,557.72 × 4096 × 2 bytes), about 9 GB per minute and 91 GB per 10
   minutes. A drive that fills mid-run loses the run.
5. **Record to a local, dedicated drive.** Never use a OneDrive or other synced
   folder, a network share, or the Windows system drive. Each of these loses
   data in ways this software cannot detect. Setting up a drive, including the
   sustained-write test it must pass, is in
   [`docs/lab/storage-setup.md`](docs/lab/storage-setup.md).

Environment setup for a new lab machine (Python 3.12, `pip install -r
requirements.txt`, and the DLLs) is in [`docs/DEVELOPMENT.md`](docs/DEVELOPMENT.md).
The lab machine must run Windows 10/11 with .NET Framework 4.7 or later.

---

## 3. Quick start: learn it on the demo

```
python tools/make_demo_recording.py demo
python -m biocam.ui --replay demo.raw --meta demo_meta.json
```

The first command writes `demo.raw` and `demo_meta.json` (about 76 MB). The
second opens the window in **simulation mode**: a blue banner reading
"SIMULATION — no instrument, no stimulus leaves this machine".

**The demo is synthetic.** It is not a recording of neurons, and nothing read
off it is science. It contains:

- **1024 electrodes (32 × 32)** instead of 4096, at the real sample rate. That
  makes 2 seconds and 37,115 frames. The window sizes its grid to the file.
- **A 3 Hz sine wave** (±120 counts, about ±240 µV) on every electrode. This
  is the slow wave in the traces.
- **Random noise** that differs per electrode, raised in two soft
  **hotspots** centred on electrodes (10,13) and (23,20). These are the yellow
  patches on the array.
- **40 dead electrodes** with no signal, which show as black squares.
- **Planted spikes**, in two shapes, on **10,13 · 11,13 · 10,14 · 11,12 ·
  23,20 · 24,21**. Select these to see spikes in the traces, and to give
  detection and sorting something to find.

Try this: click 10,13 and 23,20 on the array, tick *Detect spikes*, press
*Start recording*, then choose a sorting technique and press *Sort spikes so
far* once it finishes. The replay ends after 2 seconds (`source_exhausted`),
whatever duration you asked for.

---

## 4. The operator window

```
python -m biocam.ui --live                                  # lab machine
python -m biocam.ui --replay FILE.raw --meta FILE_meta.json # simulation
```

Add `--output-dir DIR` to change where recordings go (default `recordings`).

**Which mode you are in.** A **red** banner and `LIVE INSTRUMENT` in the title
bar mean stimuli reach the preparation. A **blue** banner and `SIMULATION` mean
nothing leaves the machine. If in doubt, look at the banner.

**Layout.** There are four columns above a session log. **Every section can be
resized by dragging the edge between it and its neighbour**: the columns
sideways, the log up and down. The array redraws its electrodes larger or
smaller to fit, and the traces follow the column's width. No column can be
dragged shut, so the reason written under a greyed-out button always stays
visible.

**Every refusal is explained in place.** If a button is greyed out, the reason
is written beneath it and updates as you type.

### 4.1 Recording column

| Control | Meaning |
|---|---|
| Output folder | Where files are written |
| Name (optional) | File name prefix; a timestamp if empty |
| Duration (s) / Run until I press Stop | How long to record |
| Start recording / Stop | Begin, or end early |
| Status, Elapsed | State, and wall-clock time since Start |
| Acquisition time | How much signal has been recorded, and which clock it comes from (see below) |
| Frames / Frames missing | Time points written / time points lost in gaps |
| Verdict | `clean`, `gaps_detected`, or `unknown` (§12) |
| Stimuli delivered | Stimuli sent in this recording |

Brown text under the status is a warning the run survived; red is an error.
Both are also written to the session log. The label after *Acquisition time*
tells you which clock is being used:

- `device` — the instrument's own clock, with a conversion factor read from
  the device. This is the only case that is cross-checked.
- `device-calibrated` — the conversion factor was estimated from the same
  packets. The cross-check is then circular and cannot detect anything, and a
  warning says so. This always happens in simulation.
- `frames` — no usable device timestamps, so the time is estimated from the
  frame count and the nominal rate.

### 4.2 Electrode array column

- **The array.** One cell per electrode. Brightness is **peak-to-peak
  activity** over about 3.5 ms of a recent packet, refreshed about 10 times a
  second. The scale runs from the quietest to the loudest electrode, and the
  line under the array shows that range in µV.
- **Selecting electrodes.** **Left-click** chooses a *positive* electrode
  (red); **right-click** a *negative* one (blue). Click again to clear;
  drag to paint several. *Clear* removes all of them.
  Hovering shows an electrode's coordinates and reading.
- **One selection drives three things.** It sets where a stimulus is delivered,
  which electrodes spikes are detected on, and which electrodes are traced.
- **Traces.** One lane per selected electrode (maximum 8), each with its own
  vertical scale. Each screen column shows the minimum and maximum of its time
  slice, so no spike can be missed. Traces work whether or not detection is on.
- **The electrode set for traces and detection is fixed when you press
  Start.** Changing the selection during a recording affects the next one.
  Electrodes that are not on the array are skipped for traces and detection;
  the Stimulus column still refuses them in red.

**The picture assumes row-major channel order** (channel *i* is row
*i* ÷ columns, column *i* mod columns). No vendor document states the order.
If the picture looks transposed on the instrument, clicking one electrode would
stimulate another. Checking this is the first thing to do in the lab (§11).

### 4.3 Stimulus column

| Control | Meaning |
|---|---|
| Amplitude (µA), Phase duration (µs) | The first phase of the pulse |
| Inter-phase gap (µs) | The pause between phases |
| Second phase mirrors the first | When ticked, the second phase is the exact opposite of the first, so the pulse is charge-balanced |
| Second amplitude / Second duration | Editable when the box is unticked, so the second phase can be shaped (e.g. short and strong, then long and weak). The two phases must still cancel: an unbalanced pulse is refused. |
| Positive / Negative electrode(s) | Same as the red/blue cells: `row,col`, separated by `;`, 1-based |
| Clock resolution (µs) | Simulation only: the stimulator's time step. On the instrument it is read from the device. |
| Stimulate now | Send one pulse now; recording must be running |
| Train: Pulses, Rate (Hz), Starts in (ms), Send train | A series of identical pulses. The start is converted to acquisition time before sending (§5.2). Recording must be running. |

What the window refuses, and why:

| Refused | Because |
|---|---|
| Stimulating with nothing recording | A stimulus with no recording leaves no evidence of what it did |
| A pulse whose two phases do not cancel | Net charge drives electrolysis at the electrode |
| Amplitude or duration outside the stimulator's limits, or off its grid | The driver would silently clamp or round it (§5.1) |
| An electrode outside the array, or `0,0` | Coordinates are 1-based, and the driver does not bounds-check |
| A positive and a negative electrode in the same column | The vendor's API document forbids it |
| A train with no acquisition-clock reading yet | It would be sent to an unknown point in time |

### 4.4 Spikes and closed loop column

| Control | Meaning |
|---|---|
| Detect spikes on the selected electrodes | Live detection. Keep the set small: detection on all 4096 electrodes needs about three CPU cores. |
| Draw traces for the selected electrodes | The trace strip (§4.2) |
| Threshold (sigmas) | A spike is a dip below −threshold × the electrode's noise level (§8) |
| Sorting technique, Units per electrode, Sort spikes so far | Sort the spike waveforms collected so far. Disabled while recording, because it would slow the thread that writes data. |
| Close the loop (stimulate on a spike) | Stimulate automatically using the Stimulus column's pulse. Requires detection. |
| Policy | *echo*: one stimulus per spike. *rate*: stimulate when firing drops below a target. |
| Min interval (ms), Max rate (Hz) | The safety envelope: hard limits applied after the policy decides |

The grey text at the bottom summarises what the **next** recording will do
with these settings.

---

## 5. Stimulation: what you must understand first

### 5.1 The driver adjusts pulses instead of rejecting them

Measured against the real assembly:

| You ask for | You get | Error raised? |
|---|---|---|
| amplitude 2000 µA (range ±1000) | 1000 µA | no |
| amplitude 7.0 µA on a 5 µA grid | 5.0 µA | no |
| phase widths 8000 / 0 / 8000 ticks (cap 10000) | **8000 / 0 / 2000** | no |

The last row matters most. A charge-balanced request comes back injecting net
charge, because the overflow is taken off the *later* phase, and the driver
still reports the pulse as biphasic. Net DC through a microelectrode drives
electrolysis and corrodes it, and the run does not fail.

So this software **refuses rather than adjusts**. Every pulse is checked
against the device's reported limits before it is built, and read back
afterwards to confirm the driver kept it unchanged. The recovered API and the
measurements behind this table are in
[`docs/api/stimulation-reference.md`](docs/api/stimulation-reference.md).

### 5.2 Scheduled stimuli are timed from the start of the acquisition

The driver takes stimulus timestamps in microseconds **from the beginning of
the acquisition**, not from when you send them. A train planned as "start in
0.5 s" and sent ten minutes into a recording would have every timestamp ten
minutes in the past. What the instrument does then is untested (§11).

The window handles this for you: *Send train* shifts the plan by the current
acquisition time, and refuses if there is no clock reading yet. The command
line does not (§6.2). How precisely a stimulus is placed in time is covered in
[`docs/lab/stimulus-timing.md`](docs/lab/stimulus-timing.md).

### 5.3 Rules to remember

- Coordinates are **1-based**: the first electrode is `1,1`.
- The positive and negative electrodes of a pulse must be in **different
  columns**.
- The stimulator can hold at most **1000 queued stimuli**. Each `Send` call is
  limited to **64 pulse values and 288 endpoint values**; on overflow the vendor
  documents that the *next* call's values are ignored silently.
- Switching between spatial patterns costs **26 µs + 8.4 µs × (rows − 1)**.

---

## 6. Command line

There is no installed `biocam` command. Run everything as a module **from the
repository root**:

```
python -m biocam.cli record|stim|convert|analyse ...
```

`--help` after any subcommand lists all its options.

### 6.1 Record

```
python -m biocam.cli record --duration 60 --name slice1
python -m biocam.cli record                  # until Ctrl+C
```

| Option | Default | Meaning |
|---|---|---|
| `--duration` | run until Ctrl+C | Seconds to record |
| `--name` | timestamp | Base name for the output files |
| `--output-dir` | `recordings` | Relative to where you run the command |
| `--packet-ms` | `2` | Acquisition period, integer 1–250 ms |

It writes `name.raw` and `name_meta.json` (§7) and prints a summary ending in
the integrity verdict and an `ACQUISITION CLOCK:` line. It does not stimulate
and does not write a session record; use the window for that.

### 6.2 Stimulate

Check a protocol without an instrument or DLLs first:

```
python -m biocam.cli stim --dry-run --time-resolution-us 10 \
    --amplitude 100 --phase-us 200 --gap-us 100 \
    --positive 10,10 --negative 20,30 --count 5 --rate-hz 10
```

`--time-resolution-us` is required with `--dry-run` and has no default. It is
the stimulator's clock period, and a wrong value makes every duration wrong by
that ratio.

To send for real, drop `--dry-run`. The limits are then read from the device:

```
python -m biocam.cli stim --amplitude 100 --phase-us 200 --gap-us 100 \
    --positive 10,10 --negative 20,30 --log recordings/stim_log.json
```

| Option | Meaning |
|---|---|
| `--amplitude`, `--phase-us`, `--gap-us` | First phase (µA, µs) and the gap |
| `--amplitude2`, `--phase2-us` | Second phase; defaults mirror the first |
| `--positive`, `--negative` | `row,col` pairs separated by `;`, 1-based |
| `--count`, `--rate-hz` / `--period-us`, `--delay-us` | A train (`--dry-run` only; see below). `--count` must be at least 1. `--delay-us` counts from the start of the acquisition, not from now. |
| `--grid` | Array size for bounds checking (default `64x64`) |
| `--log PATH` | Write a JSON record of every attempt. Keep it beside the recording. |
| `--allow-unbalanced`, `--allow-short-period`, `--no-column-rule` | Waive a safety check deliberately |

Caveats for the command line:

- **`stim` starts no acquisition.** A single pulse is sent, but the latency it
  reports is then meaningless, and it says so.
- **A train (`--count` above 1) is refused without `--dry-run`.** Its
  timestamps count from the start of an acquisition, and `stim` never runs
  one. **Send trains from the window**, which records at the same time and
  converts the timing; use `--dry-run` to check a train's plan here.
- `Start()` and `Send()` have never been tried with no acquisition running. If
  the very first `stim` throws, that is the likely reason. Report it (§11).

### 6.3 Convert to HDF5

```
python -m biocam.cli convert recordings/slice1.raw recordings/slice1_meta.json recordings/slice1.h5
```

The HDF5 file holds a `data` dataset of shape (frames × channels) in raw
counts, a `gaps` dataset, and the sidecar's fields and integrity counters as
attributes. It loads the whole recording into memory, so it is only practical
for short recordings; long recordings need more RAM than a PC has.

### 6.4 Analyse a finished recording

```
python -m biocam.cli analyse recordings/slice1.raw recordings/slice1_meta.json \
    --channels 300,301 --sort pca --units 2 --out spikes.json
```

`--channels` takes 0-based channel indices (`'0-31'` or `'4,9,17'`); electrode
(row, col) is channel (row − 1) × columns + (col − 1). The other options are
`--threshold-sigmas` (default 5), `--refractory-ms` (1), `--cutoff-hz` (300),
`--sort amplitude|pca|template`, `--units`, and `--suggest-units`.
Analysing all 4096 channels is slow.

---

## 7. What a session leaves on disk

| File | Written by | Contents |
|---|---|---|
| `name.raw` | window, `record` | The signal, exactly as received |
| `name_meta.json` | window, `record` | Acquisition parameters and integrity evidence (the **sidecar**) |
| `name_session.json` | window | **What the experiment was.** Read this first. |
| `name_stimuli.json` | window, **only if something was stimulated** | Every stimulus attempt |

**`.raw`.** Frame after frame, with no header. Each frame holds one `uint16`
(little-endian) sample per channel. So a frame is 8192 bytes for 4096
channels, or 2048 bytes for the 1024-channel demo. Convert counts to
microvolts with the sidecar's own values:

```python
import json, numpy as np
meta = json.load(open("recordings/slice1_meta.json"))
data = np.fromfile("recordings/slice1.raw", dtype="<u2").reshape(-1, meta["total_channels"])
microvolts = meta["offset"] + data * meta["adc_counts_to_value"]
```

Or use `biocam.data.recording.load_recording(raw, meta)`, which returns
`(microvolts, sidecar)`. Pass `as_microvolts=False` for raw counts.

**`_meta.json`** (the sidecar). Key fields:

- **Acquisition parameters:** `frame_rate_hz`, `total_channels`,
  `adc_counts_to_value`, `offset`, and the others needed to read the file.
- **`status`:** `in_progress` is written at the start; it becomes `complete`
  or `failed` at the end. A file left at `in_progress` came from a run that
  was killed.
- **`stop_reason`, `error`, `n_frames_written`, `duration_sec`.**
- **`integrity`:**
  - `verdict`;
  - `n_frames_missing`, and `gaps` (where each gap is and how long);
  - `driver_loss_events`, `queue_overflows` and `callback_errors`;
  - `payload_length_mismatches` and `counter_anomalies` (a packet counter
    that repeated or jumped implausibly; either makes the verdict `unknown`);
  - `discarded_at_stop`, the first and last device timestamps, and
    `timestamps_unavailable`.

Any non-zero loss counter means the verdict deserves a look.

**`_session.json`.** Records the following, all read off the objects that
actually ran rather than off what the window believed:

- live or simulated, and the source;
- the start and finish times, and the requested duration;
- the detection settings;
- the closed-loop settings, and whether the loop was really armed;
- the traced channels;
- the stimulus and its electrodes;
- the outcome: frames, acquisition time, clock source, verdict, stop reason,
  stimuli delivered and failed, spikes, and loop counts;
- the warnings.

**`_stimuli.json`.** One record per attempt:

- `outcome`: `sent`, `refused` (this software declined), or `rejected` (the
  driver did);
- `kind`, the pulse and electrodes, `requested_timestamps_us` and
  `net_charge_pc`;
- `best_time_us`, the best estimate of when the stimulus went out;
- `time_is_measured`: `true` when that time is the driver's reported latency,
  `false` when it is only the acquisition clock, a lower bound;
- `simulated`.

Refusals are kept because in the signal a stimulus that never fired looks
exactly like one that evoked nothing.

---

## 8. Spike detection and sorting

**Detection** is the same code live and on a file:

1. A second-order Butterworth high-pass at 300 Hz removes slow drift. It uses
   only past samples, so it works live, at the cost of slightly distorting
   spike shape.
2. The noise level is estimated as `median(|x|) / 0.6745`, which spikes barely
   inflate.
3. A spike is a dip below −threshold × noise (default 5).
4. A refractory period (default 1 ms) stops one spike being counted twice.

**Sorting** is **per electrode**: a *unit* is a neuron as heard by one site.

| Technique | Separates on | Fails when |
|---|---|---|
| `amplitude` | trough depth | two neurons are at a similar distance from the site |
| `pca` | shape, via principal components + k-means | one unit is rare |
| `template` | correlation with per-unit mean shapes | shapes are similar but amplitudes differ |

**Read the separation score, not the raw silhouette.** Clustering scores high
even on pure noise; the amplitude technique scored 0.61 on noise. So the score
reported is the silhouette **minus what the same clustering scores on
structureless data**. Below 0.10, the sorter says its units are not to be
trusted. If all three techniques agree, that is worth more than any one score.

This is enough to see whether a preparation has separable units and to drive a
closed loop. It is **not** a publication-grade sorter: export to HDF5 and use a
dedicated tool for that. Every number here comes from synthetic data.

---

## 9. Troubleshooting

| Symptom | Likely cause | What to do |
|---|---|---|
| `TakeBioCamControl` returns `None` | BrainWave or another process holds the device | Close all 3Brain software and stray Python processes, then retry |
| No device found / timeout | USB not connected, or BioCAM not powered | Check the cable, power, and status LED |
| `MeaPlate.IsConnected` is `False` | Plate not seated | Reseat the MEA plate and retry |
| `ModuleNotFoundError: No module named 'biocam'` | Not run from the repository root | `cd` to the folder containing this README |
| `ModuleNotFoundError: No module named 'clr'` | `pythonnet` missing (preflight reports it) | On the lab machine: `pip install -r requirements.txt` |
| Verdict `gaps_detected`, or `queue_overflows` > 0 | Too much work per packet, or a slow disk | Increase `--packet-ms`, watch fewer electrodes, check the drive against [`storage-setup.md`](docs/lab/storage-setup.md). For the closed loop, see [`closed-loop-budget.md`](docs/lab/closed-loop-budget.md): 32 watched channels at 1 ms packets do not fit. |
| Warning "activity sample(s) took longer than 500 us" | The array display was slow on the data thread | Harmless once; if frequent, report it |
| *Stimulate now* greyed out | Its reason is written beneath it; usually "no recording running" | Start a recording first. Drag the log's edge down if the reason is hidden. |
| *Sort spikes so far* greyed out | Recording is running, no technique chosen, or no spikes collected (detection off) | Stop, tick detection, record, then sort |

---

## 10. Known limitations before a closed-loop session

These describe the **current** code. Do not close the loop on a live
preparation until they are resolved:

- **No stimulus-artefact blanking.** Detection watches the same electrodes
  that are stimulated. With the *echo* policy, a stimulus artefact can be
  detected as a spike and trigger the next stimulus. The loop can then run
  itself at the envelope's maximum rate for the whole session.
- **The charge budget is inert.** The envelope supports a charge-per-second
  limit, but the window never gives it the pulse's charge, so it limits
  nothing. Only the minimum interval and the maximum rate are enforced.
- **`nan` and `inf` are accepted as loop limits.** A max rate of `nan` or
  `inf` switches the rate cap off. Very large values are not rejected either.
- **Manual and loop stimuli are limited separately.** Neither path counts the
  other's stimuli.
- **The first second after detection starts is noisy.** The noise estimate
  settles over about a second, so false detections are more likely then.

---

## 11. What is untested, and what to report

Everything that talks to the instrument is being tried for the first time. The
open `hardware-verification` issues hold the procedures; this is what each
checks:

| Issue | Question |
|---|---|
| #16 | The first session protocol and handover checklist. **Start here.** |
| #11 | Do the five undocumented `BioCamDataFormat` properties hold the values assumed? |
| #12 | How long does the data callback take at the real data rate? |
| #13 | Is each packet's payload a whole number of frames, and is `PayloadLength` in bytes? |
| #14 | Does the driver honour the requested acquisition period (`--packet-ms`)? |
| #15 | Do the data-loss events (`DataLossAsync`, `DataStreamingError`) actually fire? |
| #18 | Can `StartDataStreaming` throw after the hardware has started? |
| #31 | Is the channel order row-major? If not, the array picture and electrode selection are wrong. |
| #32 | What does the activity display cost on the lab PC? |
| #21 | What are the DupleX's real stimulator limits (`StimProperties`: time resolution, amplitude range and step, max duration)? |
| #22 | Is `Start()` what makes stimuli fire? Test on the external endpoints with an oscilloscope, not on tissue. |
| #23 | Are the endpoint rules real: `GetInternalEndPoint`, and positive and negative in different columns? |
| #24 | What does the instrument do with stimulus timestamps in the past: fire them all at once, drop them, or refuse? |
| #26 | How long does `Send` take on the acquisition thread? |
| #27 | Is `Send` safe to call from another thread? |

Changed since the last lab session, and so also tried for the first time:

- **Preflight loads the assemblies** (`3Brain assemblies load` line). Report
  that line from the first preflight on the lab machine, pass or fail.
- **An interrupted connect hands the BioCAM back.** Ctrl+C while waiting for a
  free slot (BrainWave still open), or during the connection checks, now
  releases the slot and deactivates the pool. Check: after such an interrupt,
  a fresh `record` finds the device without restarting the PC.
- **A stimulator `Start()` that raises is followed by `Stop()`.** If the
  stimulator never started, the driver documents `Stop()` as throwing, so a
  warning `IBioCamStim.Stop() raised ...` right after a failed start is
  expected, not a second fault. Report whether `Close()` then succeeds.
- **After a failed connect** (timeout, plate unseated), check that a new run
  claims the BioCAM without unplugging USB or restarting the PC.
- **Traces without detection** now run during recordings. For one run with
  traces on and detection off, report `queue_overflows` and the trace timing
  warnings from `_session.json`.
- **A repeated packet counter now makes the verdict `unknown`.** If a real
  recording shows `counter_anomalies` above zero, report it: it answers
  whether the driver ever repeats a packet.

After any lab run, report:

- the console output;
- the `_meta.json`, and specifically its `integrity` block;
- the `_session.json` warnings;
- for stimulation, the `_stimuli.json`;
- anything that looked different from this manual.

---

## 12. Glossary

| Term | Meaning |
|---|---|
| **Frame** | One sample from every channel at one instant. 18,557.72 frames per second. |
| **Packet** | A block of frames the driver delivers at once, every `--packet-ms` milliseconds, with a header holding a counter, a timestamp and a length |
| **Sidecar** | The `_meta.json` beside each `.raw`: how to read the file, and whether it is complete |
| **Gap** | Packets that never arrived, detected from a skip in the packet counter |
| **Verdict** | `clean`: no loss detected. `gaps_detected`: loss found and recorded. `unknown`: completeness could not be established (e.g. a crashed run, or anomalies) |
| **Acquisition time** | Time since the instrument started acquiring. Stimulus timestamps are counted from this. |
| **Replay / simulation** | Running the window against a `.raw` file instead of the instrument. Nothing is stimulated. |
| **Tick** | The stimulator's time step. Every pulse duration must be a whole number of ticks. |
| **Charge balance** | A pulse's two phases cancel: amplitude₁ × duration₁ + amplitude₂ × duration₂ = 0. Unbalanced pulses drive electrolysis. |
| **Sigma** | The noise level of one electrode, estimated as `median(|x|)/0.6745`. The threshold is a multiple of it. |
| **Unit** | A putative neuron, as heard by one electrode, found by sorting |
| **Policy** | What the closed loop wants to do: *echo* (a stimulus per spike) or *rate* (stimulate when firing drops) |
| **Safety envelope** | Hard limits applied after the policy (minimum interval, maximum rate) that the policy cannot override |
