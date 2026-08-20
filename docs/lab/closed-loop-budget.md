# The closed loop's time budget

What detection and closed-loop stimulation cost on the thread that drains the
packet queue, what has actually been measured, and what has not.

Read this **before arming the closed loop on the instrument**. It is the
companion to [storage-setup.md](storage-setup.md): that one covers whether the
disk can keep up, this one covers whether the CPU can.

---

## 1. Where the work happens, and why it matters

`DataReceived` runs on the acquisition thread. It reads the header, copies the
payload, appends it to a bounded queue, and returns — nothing else. That part
is unchanged by any of this.

Everything else runs on the **consumer thread**, the single thread draining
that queue:

```
write_packet  ->  clock  ->  activity display  ->  CLOSED LOOP  ->  stimulation
```

A stall here does not raise. The queue fills, the driver's packets are dropped
at the callback, and the recording afterwards **looks like real signal** rather
than an error. That is the failure mode this whole document exists to avoid.

The per-packet budget is the packet period:

| `--packet-ms` | frames/packet | budget |
|---|---|---|
| 1 | ~19 | 1000 µs |
| 2 | ~37 | 2000 µs |
| 10 | ~186 | 10,000 µs |

The disk write alone is ~152 MB/s sustained and is the dominant term. The
closed loop has to fit in what is left.

---

## 2. What was measured, on this machine

Dev machine, replay, no instrument. **These numbers do not transfer to the lab
machine** — they establish shape and order of magnitude, not the lab's answer.

### Steady state, 1 ms packets

| watched channels | mean | p99 | worst |
|---|---|---|---|
| 1 | 152 µs | 286 µs | 1143 µs |
| 4 | 206 µs | 572 µs | 4102 µs |
| 32 | 501 µs | 959 µs | 2400 µs |

### During a burst (a spike on every watched channel, every 50th packet)

| watched channels | mean | p99 | worst |
|---|---|---|---|
| 4 | 195 µs | 401 µs | 1496 µs |
| 32 | 527 µs | 1188 µs | 3362 µs |

**Read the p99 and worst columns, not the mean.** The mean fits comfortably at
every width; the tail does not. At 32 channels the p99 alone consumes the whole
1 ms budget before the disk write is counted.

### The trace strip

Drawing traces for selected electrodes costs, per packet:

| frames/packet | 1 channel | 4 channels | 8 channels | share of budget |
|---|---|---|---|---|
| 19 (1 ms) | 32 µs | 52 µs | 56 µs | 3–6% |
| 37 (2 ms) | 39 µs | 71 µs | 58 µs | 2–4% |
| 186 (10 ms) | 50 µs | 74 µs | 85 µs | <1% |

Cheap enough to leave on. It is capped at eight electrodes — past that the
lanes are unreadable anyway, and the cap is enforced rather than trusted.

Unlike the activity display, traces are **not** decimated in time: every
packet is folded in, because a trace with gaps lies about what the electrode
did. What bounds the cost is the channel count.

### Recommendation

- **1–4 watched channels at 1 ms** — plausible, not demonstrated. See §4.
- **32 watched channels at 1 ms** — not safe as written. Use `--packet-ms 2`
  or more, or watch fewer electrodes.
- Detection across the whole 4096-channel array is not supported at any packet
  size. It costs about three times one core.

---

## 3. The first-packet cost, and what warm-up actually buys

Building the loop calls `ClosedLoop.warm_up()`, which runs synthetic blocks
through the whole path before acquisition starts and then resets everything.

Median worst single packet, five runs per condition, **separate processes**
(first-touch costs are process-global, so measuring warmed and unwarmed in one
process measures nothing):

| watched channels | no warm-up | with warm-up |
|---|---|---|
| 1 | 18,327 µs | 340 µs |
| 32 | 25,091 µs | 1345 µs |

Without a warm-up the first packet costs **18–25 ms**, which at a 1 ms period
is 18–25 dropped packets at the start of every closed-loop recording. It would
have been seen on the instrument as "we always lose a few at the beginning"
and attributed to almost anything else.

**One correction worth recording.** A review found that `warm_up` was running
512 frames against a noise estimator needing 928, so detection never actually
executed during it — the crossing search, waveform windows and envelope checks
were never warmed. That was true, and is fixed. But measuring it showed the
performance consequence is **negligible**: the old warm-up already scored
291 µs / 1417 µs, against 340 µs / 1345 µs for the corrected one — the same
within noise. The dominant first-touch cost is numpy and the filter, which the
old version already paid. The fix makes the function do what it says; it does
not buy the ~10 ms the review implied. Both are worth knowing.

### What warm-up does not cover

`send` is replaced by a **sentinel** for the duration, so everything around
the call is warmed — the branch, the sent-stimulus `Decision`, the guard — but
the real `send` is never invoked, because invoking it would deliver a stimulus.

The Python side of that real send is warmed separately: `StimulusLog.warm_up()`
runs a record through a throwaway log before acquisition starts. Measured, that
cuts the first log write from **108 µs to 47 µs**, against 34 µs steady state.

**What is still cold is the driver call itself** — pythonnet marshalling into
`Send`. Nothing off the instrument can warm it. Expect the *first delivered
stimulus* of a session to be slower than every one after it, and report by how
much (issue #39).

---

## 3b. What the callback costs, measured here

The `DataReceived` callback reads five header members per packet and copies
the payload. Measured on the development machine with
`python -m biocam.interop.benchmark` — **DLLs, no instrument**:

| operation | cost |
|---|---|
| `bytes(payload)` (152 KB) | 6.9 µs mean |
| a `DataPacketHeader` property read | **0.52 µs** |

The payload-length check adds one property read — about 7% of the copy beside
it, and 0.05% of the 1000 µs budget at a 1 ms period. That was an assumption
until it was measured.

`Marshal.Copy` into a `bytearray` was **211.7 µs** for the same payload, 30×
slower than `bytes()`. It is not used, and this is why.

---

## 4. Not tested — report these

None of this involves the instrument, but none of it has been measured on the
lab machine either. Everything above is a dev-machine replay.

1. **Per-packet decision time at `--packet-ms 1` on the lab machine**, over a
   full session. Check `max_decision_us` and `slow_decisions` in the sidecar.
   The numbers in §2 are dev-machine.
2. **The worst per-packet cost during a real burst**, not the mean. A culture
   bursting across several watched electrodes completes tens of waveforms on a
   single packet. §2's burst rows are synthetic.
3. **Garbage-collection pauses.** Retained waveforms are capped at 2000, which
   is 4000 tracked objects. `source.py` already records
   `gc_counts_at_start`/`gc_objects_at_start` against their `_at_stop`
   counterparts — compare them across a closed-loop session with sorting on.
   This repo has previously measured a `gc.collect(1)` at 31.8 ms stalling an
   unrelated thread, which at 1 ms is 32 dropped packets.
4. **`queue_overflows` in the sidecar**, on any closed-loop session. It is the
   cheapest single number that says whether the consumer kept up. It should be
   zero.
5. **Whether the first packet after the ~50 ms detection warm-up shows a
   `slow_decisions` increment.** Reproducible on a replay before the session,
   and worth doing there first.
6. **Do not record into a OneDrive-synced folder.** The sync client can stall a
   `write()` for hundreds of milliseconds. This repository's own working tree
   lives under `OneDrive\Desktop`; recordings must not.

---

## 5. Limits that are enforced, and what they do not cover

`SafetyEnvelope` sits between the policy and the stimulator. The policy decides
whether to stimulate; the envelope decides whether it may. It cannot be
overridden by a policy.

| limit | default | refuses when |
|---|---|---|
| minimum interval | 20 ms | too soon after the last stimulus |
| sustained rate | 10 Hz | too many in the last second |
| charge budget | 500,000 pC/s | net charge accumulating too fast |
| session total | none | the session's stimulus count is reached |

Refusals are counted, not silent, and appear in the session warnings with the
numbers attached.

**What the envelope does not bound is staleness.** A backlog drain after a stop
can be seconds deep; deciding over it would deliver stimuli triggered by data
seconds old, after the operator asked to stop. The loop is therefore skipped
entirely while draining, and the detector is told the frames went past so spike
frame numbers stay aligned with the recording.

---

## 6. If something goes wrong

Every counter below appears in the session warnings or the sidecar.

| symptom | what it means |
|---|---|
| `queue_overflows > 0` | the consumer stalled; packets were dropped |
| `slow_decisions > 0` | the loop exceeded 400 µs on that many packets |
| `decode_errors > 0` | spike frame numbers past that point are offset from the raw file |
| `frames_skipped > 0` | frames were recorded but never analysed |
| `waveforms_dropped > 0` | spikes were counted but their shapes were not kept |
| loop suspended | the loop hit an error and disconnected; the recording continued |

A suspended loop stops stimulating and keeps recording. That is deliberate: a
loop that has broken must stop being a loop, not stop the recording.
