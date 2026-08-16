# Phase 1 follow-ups

Items carried forward from the Phase 1 implementation, plus the findings of the
Gate 1 review that ran after the merge.

**Phase 1 is merged. It is not cleared for the lab.** Gate 1 has now run properly
and did not come back clean.

---

## 1. Gate 1 — run 2026-08-13, after the merge

Gate 1 could not run during implementation: the agent definitions existed under
`.claude/agents/` but Claude Code loads them at session start, and Phase 0 created
them mid-session. A general-purpose stand-in carrying the verifier instructions
ran instead. It found one real defect and missed a Critical.

Both agents have now run against the merged code.

**`biocam-api-verifier`:** every .NET member name, signature, argument order and
keyword name in `biocam/interop/` is correct, verified against
`3Brain.BioCamDriver.xml` and the C# sample. Event subscription is balanced on
every path but one. The two defects reported earlier are genuinely fixed.

**`realtime-safety-reviewer`:** the previously-fixed blocking `put()` is gone, but
the callback is neither lock-free nor allocation-free, and three failure modes
appear only after hours of running.

One reported Critical was a **false positive** and is recorded here so it is not
re-raised: the reviewer claimed `bytes(args.Payload)` was an unmeasured strategy
and that the DLLs were not executable on the development machine. Both are wrong.
`python -m biocam.interop.benchmark` runs here, and measures `bytes()` at 13.8 µs
mean / 12.4 µs median per 152 KB payload with content verification — roughly 72×
headroom against the 1 ms budget, against 534 µs for `Marshal.Copy`.

---

## 2. Must be fixed before a lab session

**A driver exception during stop hangs the process.**
`biocam/interop/source.py:154` — `biocam.StopDataStreaming()` is the one driver
call outside a `try`. If it raises rather than returning falsy, the unsubscribe,
the stop flag and the queue sentinel are all skipped: three Python closures stay
attached to live .NET events, and `__iter__` loops forever because its only two
exits are the sentinel and the flag. The XML documents the return value and says
nothing about exceptions — the same silence this codebase already reasons about
for `StartDataStreaming`, which *is* guarded. One-line fix, same treatment.

**Ctrl+C discards up to two seconds of acquired data and reports `clean`.**
`biocam/cli.py:134-144`. The `KeyboardInterrupt` handler sets the stop flag, then
calls `record_session` again with it *already set*. The loop writes one packet,
sees the flag and breaks; everything still queued — up to 2000 packets, roughly
304 MB — is discarded uncounted, and `finalise("user_stopped")` writes a sidecar
saying `clean`. Ctrl+C is the documented way to end an open-ended recording, so
this fires on every such run. The second pass must drain to exhaustion against a
wall-clock deadline, and anything still queued when it expires must be counted
into the integrity block.

**A full disk destroys the sidecar as well as the recording.**
`biocam/cli.py:112` runs the disk check only when `--duration` is given, so
open-ended recordings get none. When the disk fills, `write()` raises, and
`RecordingWriter.__exit__` then tries to write a `failed` sidecar to the same full
disk — that write raises too, masking the original error and leaving no sidecar at
all. You would be left with terabytes of raw bytes and no acquisition parameters
to interpret them. Needs: a periodic free-space poll on the consumer thread, a
guarded `__exit__` write, and an atomic sidecar write via temp file plus
`os.replace`.

---

## 3. Should be fixed before a lab session

**`--packet-ms` has no upper bound; the documented range is 1–250 ms.**
`biocam/cli.py:57-59`. The PDF states the acquisition time period is settable
"between 1ms and 250ms" (p. 7). Values above that claim the device and then fail
inside `StartDataStreaming`, defeating the validator's stated purpose of refusing
before the device is opened.

**The queue floor reintroduces multi-gigabyte sizing.** `biocam/cli.py:33,39` —
`MIN_QUEUE_SIZE = 100` dominates above 20 ms, so buffered duration grows with the
packet period: 250 ms gives 25 seconds queued, roughly 3.8 GB. Express the floor
in bytes rather than packets.

**The driver's own loss count is thrown away.** `biocam/interop/source.py:106-110`
increments a counter per `DataLossAsync` event, but `DataLossEventArgs.Counter`
carries "the number of data losses since the start of the acquisition"
(XML line 3497), and the event is documented as asynchronous, so events can
coalesce and the event count is not the loss count. The C# sample reports
`e.Counter` (`MainForm.cs:535`). Reading it is a plain field access, callback-safe.

**`QueueOverflow` and `DriverDataLoss` are defined, rendered, tested — and never
emitted.** Nothing in the package constructs either. The operator gets one
pressure warning at 80% and then silence while packets drop; the only record is
the sidecar, after the run. The consumer thread can emit both safely.

**Gap emission is unthrottled and can feed back on itself.**
`biocam/cli.py:107` prints on the consumer thread, one line per gap. Under
sustained loss that is one print per packet, and a blocked stdout stalls the only
drain, filling the queue and causing more loss. Rate-limit, and put the listener
behind a bounded queue.

---

## 4. Worth doing, lower risk

- **The callback takes locks.** `queue.Queue.put_nowait` acquires the queue mutex
  and `Event.set()` takes a condition lock; CLAUDE.md's callback rule says no
  locks. `collections.deque(maxlen=N)` with `append` is genuinely lock-free and
  there is exactly one producer. This also removes the per-packet `qsize()`.
- **The GIL switch interval is five times the packet budget.**
  `sys.setswitchinterval()` defaults to 5 ms; a callback needing the GIL can wait
  that long if the consumer is in pure Python. Set it to ~0.5 ms in
  `record_command`.
- **Cyclic GC can run on the driver thread.** `gc.freeze()` after setup and
  `gc.disable()` for the acquisition window; the hot path creates no cycles.
- **Gap accumulation is unbounded in the pathological case.** One `Gap` per packet
  under persistent loss is ~14M objects over four hours, and `json.dumps` of that
  at finalise would likely raise `MemoryError` — into the masking bug above. Cap
  the list and count the remainder.
- **`finalise` flushes but never `fsync`s**, so a power loss can lose whatever
  Windows had not committed.
- **The benchmark does not compare like with like.** `Marshal.Copy` writes into one
  preallocated buffer while `bytes()` allocates fresh, so the comparison flatters
  `Marshal.Copy` — which still lost by 40×. More importantly, transplanting the
  benchmarked shape verbatim would alias one buffer across every queued packet and
  produce a recording that looks like signal and is corrupt.
- `start()` has no re-entrancy guard; the C# sample guards this at the caller.
- `benchmark.py` duplicates the assembly loading in `device.py` and omits its PATH
  setup and file checks.

---

## 5. Confirm on the instrument

**Five `BioCamDataFormat` properties are undocumented.** `BitDepth`,
`ADCCountsToValue`, `Offset`, `MinDigitalValue` and `MaxDigitalValue` appear
nowhere in `3Brain.BioCamDriver.xml` — verified, zero occurrences, while
`FrameRate` and `NWells` have one each, so the absence is meaningful rather than
an artefact of partial documentation. They are almost certainly inherited from
`_3Brain.Common`, which ships no XML in this repository.

They do exist: the real hardware run at
`BioCam_DupleX_API/recordings/20260624_140615_meta.json` recorded
`bit_depth: 12`, `adc_counts_to_value: 2.0146520146520146`, `offset: -4125.0`,
`min_digital_value: 0`, `max_digital_value: 4095`. Say it to the colleague that
way: expected to work, proven once, not verifiable from documentation.

**Also confirm:** that `data_format` is fully populated before
`StartDataStreaming` (its constructor takes `dataPacketTimeSpanMs`, suggesting it
may be rebuilt when streaming starts); that `len(Payload)` is an exact multiple of
`bytes_per_frame`, since `write_packet` floor-divides and would truncate silently;
and that `pythonnet==3.1.0` resolves on the lab machine.

**Reconsider the 1 ms default.** `--packet-ms` defaults to 1, the most aggressive
documented setting. The vendor's own sample defaults to 2 ms, and the only
successful hardware run in this repository used roughly 50 ms. Starting at 2 ms
costs nothing and leaves twice the budget.

---

## 6. Test quality, deferred

- `test_describe_handles_every_event_type` enumerates today's seven types by hand;
  an eighth added without a `describe()` branch would not be caught, though it
  would raise loudly at runtime.
- The ragged-packet decoder test never produces a one-sample-pending state, so its
  docstring overstates what it exercises.
- `test_importing_the_cli_does_not_load_interop` is a weak in-suite proxy — a
  module-scope violation would surface as a collection error rather than that
  assertion failing. The fresh-subprocess check is authoritative.
- `convert()` produces a `(1, n_channels)` chunk for a zero-frame recording;
  guarded but untested.

---

## 7. Documentation

**.NET refuses to load the 3Brain DLLs from a OneDrive-synced path** until
`Unblock-File` clears Windows' mark-of-the-web. This cost real time during
implementation and affects anyone cloning into OneDrive. `docs/lab/storage-setup.md`
already documents two other OneDrive hazards; this belongs with them.

**Recording into a synced folder will stall writes** for seconds at a time as the
sync client reads the growing file — enough to exhaust the queue. `--output-dir`
defaults to `recordings`, resolved relative to the working directory, and this
repository lives under OneDrive. The colleague needs an absolute path to a local
NVMe outside any synced tree, with an antivirus exclusion on it.

---

## 8. Hardware-only assumptions

Six remain; the current list is in the Phase 1 spec §8 and must accompany the
first recording, per Gate 2 item 5. Three were struck during review by reflecting
on the loaded assemblies, which needs no instrument — a technique worth reusing.
