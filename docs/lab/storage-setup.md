# Storage setup for recordings

How to prepare a machine to record from the BioCAM DupleX without losing data.

Read this **before the first recording on a new machine**, and re-check it
whenever the drive changes or free space gets low. It is the companion to the
main [README](../../README.md); that document covers software setup, this one
covers where the data goes.

---

## 1. Why this needs planning

The BioCAM produces one frame per sample containing every channel:

```
4096 channels x 2 bytes = 8192 bytes per frame
8192 bytes x 18,557.72 frames/s = 152,024,848 bytes/s
```

**That is about 152 MB every second.** Not per minute.

| Recording time | Raw size | After conversion (~2x) |
| --- | --- | --- |
| 1 minute | 9.1 GB | ~4.6 GB |
| 10 minutes | 91 GB | ~46 GB |
| 30 minutes | 274 GB | ~137 GB |
| 1 hour | 547 GB | ~274 GB |
| 8 hours | 4.4 TB | ~2.2 TB |

**The table is configuration-specific and is a floor, not a ceiling.** It is
derived from a real recording made at 18,557.72 Hz. 3Brain specifies the
DupleX's standard operation as 4,096 channels at 20 kHz, which is *higher*:

| Configuration | Rate | Per hour |
| --- | --- | --- |
| The reference recording, 18.56 kHz | 152 MB/s | 547 GB |
| Vendor standard, 20 kHz | **164 MB/s** | **590 GB** |

The published maximum sampling frequency is 64 kHz, which almost certainly
applies to a reduced channel count rather than the full array — treat any figure
derived from it as unverified until confirmed with 3Brain.

Always recompute for the configuration actually in use rather than trusting
either table. The frame rate and channel count come from the instrument and are
written into every recording's `_meta.json`:

```
bytes_per_second = total_channels x ch_sample_byte_size x frame_rate_hz
```

The ~2x figure is measured, not assumed: lossless compression of real 4096-channel
data from this instrument gives 1.86x with fast settings and 2.18x with byte
splitting. Neural noise limits how much better it gets.

Two consequences drive everything below. The disk must **sustain** this rate
without pausing, and it fills **fast**.

### Why these numbers are derived rather than quoted

3Brain publishes **no host-computer storage requirement** — none appears in the
BioCAM DupleX user guide, the product page, or the BrainWave documentation. The
only stated host requirement anywhere is Windows 10 or 11. The figures above are
therefore computed from real recordings rather than taken from a specification.

The instrument itself has **no recording storage**. Its only memory is 2 GB of
DDR4 attached to the acquisition FPGA — a working buffer for the pipeline, not
somewhere data is kept. Everything streams over USB to the host in real time,
which is also why a disk stall is unforgiving: there is no meaningful buffer
upstream to absorb it.

If an official figure is ever needed, it is a support enquiry to 3Brain.

---

## 2. What the recording computer needs

### The recording drive

| Requirement | Why |
| --- | --- |
| **NVMe SSD** | Sustains 1-3 GB/s, roughly 10x the needed rate. A SATA SSD (~500 MB/s) works with less margin. **A hard disk does not** — at ~150 MB/s it sits at the data rate and will drop samples. |
| **TLC, not QLC** | See below — this is the single most important choice for long recordings. |
| **With a DRAM cache** | DRAM-less models collapse once their write cache fills. |
| **With a heatsink** | Either on the drive or on the motherboard slot. Sustained writes heat an NVMe until it throttles to protect itself. |
| **Internal, not USB** | A USB bridge adds a controller that can stall. Stalls lose data. |
| **A separate drive from Windows** | The OS competes for the same disk, and a full system drive destabilises Windows mid-experiment rather than merely stopping the recording. |
| **Enough space for a week of sessions** | So a failed or delayed transfer does not block the next experiment. |

2 TB suits recording in bouts; 4 TB suits multi-hour sessions.

#### Why long recordings fail on drives that benchmark well

Two effects appear only after sustained writing, so a short test never sees them.

**Cache exhaustion.** Consumer SSDs write into a small fast pseudo-SLC region and
fall back to their native speed once it fills. That region is typically 50-200 GB.
At 590 GB/hour it is exhausted in **six to twenty minutes**. A QLC drive can then
drop to around 100 MB/s — *below the data rate*. The drive that benchmarked at
3 GB/s starts losing samples in minute fifteen. TLC degrades far less and stays
usable; this is why the TLC-not-QLC line above matters more than peak speed.

**Thermal throttling.** An NVMe under continuous write heats up and reduces its
own speed to survive. A ten-minute recording never reaches this point. A two-hour
one does.

Both are the reason §3's verification writes ~100 GB and reports the *minimum*
rate rather than the average. A 10 GB test fits inside the cache and would pass a
drive that fails twenty minutes into an experiment.

### The rest of the machine

| Component | Requirement | Why |
| --- | --- | --- |
| **RAM** | 16 GB minimum, **32 GB comfortable** | The buffer between the acquisition callback and the disk writer needs room to absorb hiccups without dropping packets. |
| **CPU** | Any modern multi-core | The acquisition thread runs at elevated priority and must never wait. Core count matters less than not competing with other work. |
| **USB** | USB 3, **directly on the motherboard** | Never through a hub, never sharing a controller with other high-bandwidth devices. The link to the instrument is the one path with no redundancy. |
| **OS** | Windows 10 or 11 | The 3Brain driver targets .NET Framework; there is no Linux or macOS option. |

There is no GPU requirement for recording.

### Windows configuration

These cost nothing and matter as much as the hardware:

- **Exclude the recordings folder from Windows Defender** and any other
  antivirus. Real-time scanning of a file being written at 164 MB/s is a genuine
  throughput problem.
- **Set the power plan to High Performance.** Disable sleep and hibernation, and
  turn off **USB selective suspend** — it can drop the instrument mid-session.
- **Pause Windows Update** before a long recording. An automatic restart ends the
  experiment.
- **Close everything else**, including browsers, BrainWave, and any sync client.

### Somewhere to archive

Recordings must not stay on the recording drive. Options, best first:

1. **Institutional research storage.** Ask your IT or research support whether it
   exists before buying anything — it is usually free at the point of use,
   professionally backed up, and large. Mention that you generate roughly 500 GB
   per hour of raw electrophysiology.
2. **A NAS with redundancy.** A 4-bay unit with RAID survives one disk failure.
3. **An external drive.** Cheapest, but a single drive is a single point of
   failure. Acceptable as a stopgap, not as the only copy.

**A recording from a live culture cannot be repeated.** The same cells at the
same age will never exist again. Keep at least two copies on two devices, and
ideally one of them somewhere else.

### What you do not need

- **A database for the signal.** Nobody queries 547 GB/hour by value. Databases
  are worth it only for a *catalogue* — one small row per recording (§6).
- **Cloud storage as the primary target.** See §4.

---

## 3. Setting it up

**Step 1 — inspect the machine.**

```powershell
Get-PhysicalDisk | Select-Object FriendlyName, MediaType, @{n='SizeGB';e={[math]::Round($_.Size/1GB)}}
Get-PSDrive -PSProvider FileSystem | Where-Object {$_.Used} |
  Select-Object Name, @{n='FreeGB';e={[math]::Round($_.Free/1GB)}}, @{n='TotalGB';e={[math]::Round(($_.Used+$_.Free)/1GB)}}
```

You want `MediaType = SSD`, ideally an NVMe model name, on a drive letter that is
not `C:`. Compare free space against the table in §1.

**Step 2 — create the recording folder** on the dedicated drive, at the drive
root, outside any user profile:

```
D:\recordings\
```

Not under `Documents`, `Desktop`, or `OneDrive` — see §4.

**Step 3 — confirm the drive sustains the rate.** Model numbers and short
benchmarks describe *peak* performance, which every candidate drive will pass.
What matters is the speed after the cache is exhausted and the drive is hot, so
this test writes **100 GB** and reports the rate for each window rather than one
average. Needs 100 GB free and takes several minutes.

```powershell
$path    = "D:\recordings\_speedtest.bin"
$chunkMB = 64
$totalGB = 100
$chunks  = [int]($totalGB * 1024 / $chunkMB)
$window  = 10                                    # report every 640 MB

$buf = New-Object byte[] ($chunkMB * 1MB)
(New-Object Random).NextBytes($buf)              # random: defeats any compression
$fs  = [IO.File]::Create($path)
$min = [double]::MaxValue
$sw  = [Diagnostics.Stopwatch]::StartNew(); $t0 = $sw.Elapsed.TotalSeconds

for ($i = 1; $i -le $chunks; $i++) {
    $fs.Write($buf, 0, $buf.Length)
    if ($i % $window -eq 0) {
        $fs.Flush($true)                         # force to the device, not the OS cache
        $now  = $sw.Elapsed.TotalSeconds
        $rate = ($window * $chunkMB) / ($now - $t0)
        if ($rate -lt $min) { $min = $rate }
        "{0,6:N1} GB written   {1,7:N0} MB/s" -f ($i * $chunkMB / 1024), $rate
        $t0 = $now
    }
}
$fs.Close(); $sw.Stop()
"`nAverage {0:N0} MB/s    MINIMUM {1:N0} MB/s   <-- judge the drive on this" -f `
    ($totalGB * 1024 / $sw.Elapsed.TotalSeconds), $min
Remove-Item $path
```

**Judge the drive on the MINIMUM, not the average. Anything under 250 MB/s is not
safe to record on.** That threshold leaves roughly 50% headroom over the
164 MB/s of the vendor's standard 20 kHz configuration.

Watch the per-window numbers as they print. A healthy TLC drive stays roughly
flat. A drive that starts near 2,000 MB/s and drops to a few hundred partway
through has just exhausted its cache in front of you — that collapse point is
where a real recording would begin losing samples, and it is exactly what a short
benchmark hides.

**Step 4 — exclude the folder from every sync and backup client** that runs live.
Sync during recording is the single most likely cause of silent data loss on an
otherwise healthy machine.

**Step 5 — point the recorder at it.**

```powershell
python recorder.py --duration 60 --output-dir D:\recordings
```

**Step 6 — check free space before every session.** Multiply your planned
duration by 9.1 GB/minute and confirm the drive holds it with room to spare.

> Automating this check in `biocam.preflight` is planned for Phase 1. Until then
> it is a manual step, and it is the one most worth not skipping — losing the
> final hour of a four-hour experiment to a full disk is entirely preventable.

---

## 4. Never record to these

**A synced folder** — OneDrive, Dropbox, Google Drive, iCloud. The client will
try to upload at 152 MB/s while you write at 152 MB/s, competing for the same
disk and CPU. Worse, these clients turn local files into cloud placeholders,
so a script that expects a local file can silently trigger a multi-gigabyte
download later.

**A network drive or NAS share.** A two-second network hiccup is a two-second
hole in the recording. Record locally, transfer afterwards.

**The Windows drive.** Filling `C:` mid-experiment does not merely stop the
recording; it destabilises the machine that is running the experiment.

**An external USB drive**, unless nothing else exists. The bridge chip can stall
under sustained load.

---

## 5. After the recording

Uploading to cloud or network storage **after** the recording is finished is
completely fine — the risk is only during acquisition. Once the file is closed,
it is an ordinary file.

The intended order:

1. **Recording** writes raw binary to the local NVMe. Flat, uncompressed writes,
   because they are the most predictable thing a disk can be asked to do.
2. **Convert to HDF5** after the session, with compression. Roughly halves the
   size, and HDF5 keeps the signal and its metadata in one self-describing file.
   *(The converter is Phase 1 work; until then the `.raw` plus its `_meta.json`
   sidecar is the archival unit and both must be kept together.)*
3. **Verify** the converted file opens and its shape matches the metadata.
4. **Archive** to institutional storage or NAS.
5. **Delete the raw** only after the converted file is confirmed readable.

If you upload to consumer cloud storage, note two limits: quota (OneDrive gives
5 GB free, 1 TB with Microsoft 365 — roughly 3.6 hours of compressed recording),
and upload bandwidth (274 GB over a 50 Mbps uplink takes about twelve hours).
Upload the converted file, never the raw.

---

## 6. Keeping track of recordings

After a few months, "which session was the one where bursting changed?" becomes
genuinely hard. A small catalogue solves it — one row per recording holding date,
culture identifier, age, protocol, duration, notes, file path, and a checksum.

SQLite is the right size for this: a single file, no server, queryable. It stores
*metadata only* — the signal stays in files.

Not yet implemented; recorded here so it is not reinvented later.

---

## 7. Checklist for a new machine

**Hardware**

- [ ] Dedicated NVMe SSD, separate from the Windows drive
- [ ] TLC with DRAM cache, not QLC, and heatsinked
- [ ] 16 GB RAM minimum, 32 GB preferred
- [ ] Instrument on a motherboard USB 3 port, not a hub

**Verification**

- [ ] 100 GB sustained-write test run, **minimum** window above 250 MB/s (§3 step 3)
- [ ] Free space checked against planned session length
- [ ] `python -m biocam.preflight` passes

**Configuration**

- [ ] `D:\recordings\` created outside any user profile
- [ ] Folder excluded from OneDrive and every other sync client
- [ ] Folder excluded from Windows Defender / antivirus
- [ ] Power plan High Performance; sleep, hibernation and USB selective suspend off
- [ ] Windows Update paused before long sessions

**Data safety**

- [ ] Archive destination decided and reachable
- [ ] Backup covers the archive — at least two copies, two devices
