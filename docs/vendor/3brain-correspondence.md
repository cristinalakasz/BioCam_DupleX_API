# 3Brain correspondence

A running record of questions put to 3Brain and the answers received.

**Why this file exists.** Several things this project needs to decide cannot be
answered from the documentation we have — they are only known to the
manufacturer. Answers arriving by email are easy to lose, and a question nobody
remembers asking gets asked again. Each open question below names what it blocks,
so it is clear what is waiting on a reply.

**How to use it.** Append each exchange under a dated heading. When a reply
arrives, record the answer against its question number in the table, change the
status, and quote the relevant wording. Do not paraphrase a technical answer —
quote it, then note what it means for us.

---

## Open questions

| # | Question | Status | What it blocks |
| --- | --- | --- | --- |
| 1 | Is 64 kHz available on all 4,096 channels, or only fewer? | Sent 2026-08-13 | Data rate and SSD purchase. At full array this would be ~520 MB/s instead of 164 MB/s, changing the drive requirement entirely. |
| 2 | How long can the BioCAM buffer if the host stops reading? | Sent 2026-08-13 | Buffer sizing in the Phase 1 acquisition rebuild. Determines how much slack the queue between callback and disk writer must absorb. |
| 3 | Can lost data be detected after the fact, from packet counters or timestamps? | Sent 2026-08-13 | Whether a finished recording can be shown to be complete — including the existing 2026-06-24 recording, which currently cannot be. See the setup spec, Appendix A item 2. |
| 4 | Is there a tested or recommended maximum continuous recording duration? | Sent 2026-08-13 | Experiment planning and storage sizing. |
| 5 | Is `FTD3XX_NET.dll` required for the driver API, and where is it obtained? | Sent 2026-08-13 | Completeness of `biocam.preflight`, which does not currently check for it. The file is referenced by `SampleApp_BioCamCL.csproj` but is absent from the SDK folder we hold. |
| 6 | Is the `.brw` file format documented? | Sent 2026-08-13 | Phase 5. Writing `.brw`-compatible HDF5 would make recordings readable by BrainWave and by SpikeInterface, which supports 3Brain files natively — a large amount of spike-sorting tooling for the cost of matching a layout. |
| 7 | Recommended host PC specification for long recordings | Sent 2026-08-13 | Nothing directly — `docs/lab/storage-setup.md` derives its figures from real recordings. A vendor figure would corroborate them. |

---

## 2026-08-13 — sent

Sent to 3Brain support. Add the instrument serial number and institution before
sending if reusing this text.

> Subject: BioCAM DupleX — questions about long recordings using the driver API
>
> Hello,
>
> We use a BioCAM DupleX and are writing our own acquisition software with
> the BioCamDriver API v2.6 (Python via pythonnet), rather than BrainWave.
> We plan long continuous recordings and closed-loop stimulation.
>
> Six questions:
>
> 1. The specifications state 4,096 channels at 20 kHz and a maximum of
>    64 kHz. Is 64 kHz possible with all 4,096 channels, or only with fewer
>    channels? If fewer, how many?
>
> 2. If our software is too slow for a moment and stops reading data, how
>    long can the BioCAM buffer before samples are lost? Does the 2 GB of
>    on-board memory help here?
>
> 3. Besides the DataLossAsync event, is there a way to detect lost data
>    afterwards — for example from packet counters or timestamps in a saved
>    file? We need to know whether a finished recording is complete.
>
> 4. Is there a maximum continuous recording duration that you have tested
>    or recommend?
>
> 5. Our SampleApp_BioCamCL project references FTD3XX_NET.dll, but that file
>    is not in the SDK folder we received. Is it required for the driver API,
>    and where do we get it?
>
> 6. Is the .brw file format documented? We would like to save our recordings
>    in a format that BrainWave can open.
>
> We would also welcome any recommendation on host PC specifications for
> long recordings — particularly disk requirements, since we measure about
> 150 MB per second of raw data.
>
> Thank you for your help.
>
> Best regards,
> Cristina

*(Awaiting reply. Record it below when it arrives.)*

---

## Background: what we established without asking

Recorded so a future reply can be checked against it, and so these are not
re-derived.

- **The instrument has no recording storage.** Its 2 GB DDR4 belongs to the
  acquisition FPGA. Everything streams to the host in real time.
- **3Brain publishes no host-PC requirement** — not in the BioCAM DupleX user
  guide, the product page, or the BrainWave pages. The only stated host
  requirement anywhere is Windows 10 or 11.
- **The driver API cannot write files.** Its only `Write` members concern EEPROM,
  serial numbers and the COM port. Neither DLL contains any HDF5 reference. Any
  file format is ours to implement.
- **Published rates:** 4,096 channels at 20 kHz standard, 64 kHz maximum. Our
  reference recording ran at 18,557.72 Hz.
- **`RectangularStimPulse` and `StimProperties`** live in `_3Brain.Common`, which
  ships no XML documentation. Their members cannot be verified from anything we
  hold — see `docs/superpowers/specs/2026-08-12-api-roadmap-decomposition.md` §4.
