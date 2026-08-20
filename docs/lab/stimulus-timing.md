# When did that stimulus actually happen?

How a stimulus is placed in time relative to the recording, how precise that
is, and what has never been checked on the instrument.

Read this if you intend to line stimulus artefacts up against the signal.

---

## 1. The one fact that governs everything

**Stimulation timestamps are counted from the beginning of the acquisition,
not from now.**

`Send(pulse, positive, negative, timestamps)` takes microseconds since
acquisition started. A train built with `delay_us = 0` and sent ten minutes
into a recording therefore has every timestamp ten minutes in the past. What
the instrument does with a past timestamp is **untested** — issue #24 — and the
plausible outcomes include firing the whole train at once.

The window shifts a train by the acquisition clock's current reading before
queueing it, and refuses to send if the clock has no reading yet. The CLI's
`biocam stim` starts no acquisition, so it has no time origin and refuses
scheduled trains outright.

---

## 2. Where the recorded time comes from

Every entry in `name_stimuli.json` carries `clock_us` and `clock_source`. The
source names which clock answered, and they are not equally good:

| `clock_source` | what it means | trustworthy? |
|---|---|---|
| `device` | the instrument's own timestamp, converted with a factor read from `IBioCam.ClockCyclesToMilliseconds` | yes, and cross-checked against the frame count |
| `device-calibrated` | the instrument's timestamp, but the conversion factor was derived from these same packets | the cross-check **cannot fail** — see below |
| `frames` | frames written ÷ frame rate; the device reported no usable timestamp | an estimate, and the only one available |

**Always check `clock_source` before trusting `clock_us`.** On the instrument
it should read `device`. If it reads `device-calibrated`, the sanity check that
compares the two estimates has been reduced to an identity — measured, a device
clock running 30% fast produced a disagreement of 5×10⁻¹⁰ µs. A check that
cannot fail reads exactly like a check that passed.

---

## 3. How precise it is

**The resolution is one packet period.** Measured directly, by sending
stimuli continuously and looking at the distinct time values that came back:

| frames/packet | packet period | smallest step between recorded times |
|---|---|---|
| 37 (`--packet-ms 2`) | 1993.8 µs | 1993.8 µs — exactly 1 packet |
| 200 | 10777.2 µs | 10777.2 µs — exactly 1 packet |

The reason is structural, not a defect to be tuned away: the clock advances
when a packet arrives, and stimulation is dispatched between packets on the
same thread. So `clock_us` is *the acquisition time as of the last packet the
consumer thread saw*.

Two consequences:

- **It is a lower bound, not a midpoint.** The clock is read *before* the
  stimulus is sent, deliberately, so the recorded time never claims to be
  later than the delivery.
- **Lowering `--packet-ms` tightens it.** At `--packet-ms 1` the quantum is
  ~1 ms. That trades against the per-packet budget — see
  [closed-loop-budget.md](closed-loop-budget.md).

### The better number, when the driver provides it

`Send(pulse, pos, neg, out UInt64)` reports a latency in clock cycles. Where
that exists *and* the cycles-per-microsecond factor is known,
`best_time_us` uses it instead, and `time_is_measured` becomes true — that is
the instrument's own answer rather than our estimate.

**Neither has been observed on the instrument.** Whether the driver populates
that out parameter at all is untested (issue #26).

---

## 4. What to check in the lab, in order

1. **`clock_source` in the stimulus log.** It should be `device`. If it is
   `device-calibrated` or `frames`, the acquisition clock never got a usable
   factor from `ClockCyclesToMilliseconds`, and every time in that file is an
   estimate. This is the single most important field.
2. **`cycles_per_us` at the top of the stimulus log.** Non-null means the
   factor was read. Null means it was not, and `time_is_measured` is false
   everywhere.
3. **`time_is_measured` on delivered stimuli.** True means the driver
   reported a latency and it was converted. Never seen true here.
4. **Line one stimulus artefact up against its `clock_us` by hand**, on the
   first session, before trusting any of it in bulk. A one-packet offset, a
   sign error, or a units error all look plausible in a JSON file and obvious
   in a trace.
5. **Whether a scheduled train fires at the requested times at all** — issue
   #24. Send a short train with a known delay and compare the artefacts to
   `requested_timestamps_us`.

---

## 5. What is guaranteed regardless of the clock

Loss detection does **not** depend on any of the above. It uses
`DataPacketHeader.PacketCounter`, an exact integer sequence, so a gap is
detected by arithmetic rather than by timing tolerance — including a packet
dropped by our own queue, which leaves a hole in the counter the writer sees.

`name_meta.json` carries `n_frames_missing`, the `gaps` list,
`queue_overflows`, `driver_loss_events` and `discarded_at_stop`. A recording
with `verdict: clean` and `queue_overflows: 0` lost nothing between the
instrument and the file.

The frames themselves are written exactly as received and never decoded, so
the byte stream cannot be corrupted by an interpretation error. What is *not*
guaranteed is durability against power loss mid-run: the raw file is flushed
periodically but `fsync`ed only at `finalise()`.
