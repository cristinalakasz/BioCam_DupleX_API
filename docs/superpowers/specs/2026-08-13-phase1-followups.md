# Phase 1 follow-ups and the Gate 1 record

Phase 1 merged on 2026-08-13. Gate 1 then ran properly for the first time and
returned findings, which became **Phase 1.1**. This document records what was
found, what was fixed, and what remains.

**Status: all catalogued Gate 1 findings are fixed. The code is still not
cleared for a lab session** — see §5.

---

## 1. Why Gate 1 ran late

The gate could not run during Phase 1 implementation: the agent definitions
existed under `.claude/agents/` but Claude Code loads them at session start, and
Phase 0 created them mid-session. A general-purpose stand-in carrying the
verifier instructions ran instead. It found one real defect and missed a Critical.

Both agents have since run against the merged code, twice.

---

## 2. What Gate 1 found, and what it cost to fix

Phase 1.1 closed every finding across seven task groups. The suite went from 138
tests to 204.

**The callback path is now confirmed clean** — traced exhaustively by the
realtime reviewer: no blocking, no locks, no unbounded allocation, correct
drop-the-incoming-packet behaviour, and no listener reachable from any driver
thread. That was the hardest property to get right.

The findings worth remembering, because they characterise the risk in this
codebase:

**A recording that lost data reported `clean`.** `record_session` finalised the
sidecar before the CLI reported driver losses and queue overflows, so they never
reached disk. In the only path the on-site colleague runs. Invisible to every
per-task review because it spanned three of them.

**Every successful timed recording would have reported data loss.** The fix for
discarded data called `pending_count()`, which pops and discards rather than
peeking — so packets still buffered at the frame limit were thrown away and
counted as lost, forcing `gaps_detected` and exit code 2 on every normal run. An
integrity mechanism that cries wolf teaches the operator to ignore it, which is
worse than having none.

**Printing could stall the only drain.** On Windows, clicking inside a console
enables QuickEdit and blocks every write to stdout until Enter is pressed. An
operator glancing at the screen mid-recording would have stalled the drain and
started dropping packets. Now behind a bounded ring and a daemon thread;
measured at 3.5 ms for 2000 reports against a fully blocked sink, with drops
counted.

**A driver exception during stop hung the process.** `StopDataStreaming()` sat
outside the guarded region, so an exception skipped the unsubscribe, the stop
flag and the sentinel — leaving handlers attached and the consumer looping
forever.

**`gc.freeze()` was never undone.** Measured: the permanent generation grew from
377 objects to 20,658 after one start/stop cycle and kept growing, while the
docstring stated both settings were restored unconditionally. Now symmetric.

**A full disk destroyed the sidecar too.** The failure path wrote the `failed`
sidecar to the same full disk, masking the original error and leaving nothing to
interpret the raw bytes with. Sidecar writes are now atomic via temp file and
`os.replace`, and a sidecar failure can never mask the real exception.

### The pattern

Four fixes in this phase introduced new defects, and three docstrings described
guarantees the code did not provide. Almost none of it would have thrown an
error — it would have produced confidently wrong output. For a project where the
author cannot run the code, that is the whole risk: loud failures are cheap,
quiet lies are expensive.

---

## 3. Accepted trade-offs

**`gc.unfreeze()` unfreezes everything, not only what we froze.** Python offers
no scoped alternative. The permanent generation returns to zero rather than to
its prior 377, which is harmless for a single-session process but is not strictly
symmetric.

**`disk_low` detection is asynchronous.** The free-space poll runs on a daemon
thread because `shutil.disk_usage` can block for seconds on a network share, an
antivirus-scanned volume, or a synced tree — and stalling the drain costs data.
The consequence is that detection lags by up to one poll interval.

**The periodic flush does not `fsync`.** Adding one would put an unmeasured
disk-bound stall on the drain thread, risking the very failure the callback rule
exists to prevent. The docstrings were corrected to describe what the code does
rather than adding a protection that might cause the problem it guards against.

---

## 4. Deferred, with reasons

- A device reset from a **low** counter is still reported as loss. Indistinguishable
  from genuine loss using counters alone; the remedy is a timestamp cross-check,
  addable later as an optional parameter without breaking callers.
- The straggler window after `_unsubscribe` — a packet landing after the final
  count is neither counted nor recovered. Observable only on the instrument.
- `MIN_QUEUE_PACKETS` is applied outside the byte ceiling, so the byte bound is
  not hard. It cannot bind within the documented 1–250 ms range at this device's
  data format.
- Four test-quality items: the event-coverage test enumerates today's types by
  hand; the ragged-packet test never reaches a one-sample-pending state; the
  in-suite import guard is a weak proxy for the authoritative subprocess check;
  and the zero-frame chunk shape is guarded but untested.

---

## 5. Before a lab session

**Gate 1 must run once more against the final merged state.** Tasks 6 and 7
changed the acquisition path substantially after the last full verification.
`CLAUDE.md` Gate 2 requires both verifiers clean across all interop code, not
just what changed.

**Eight hardware questions are tracked as GitHub issues #11–#18**, each with a
runnable procedure, what failure looks like, and what to report back. Two can be
answered without an instrument: #17 needs only the DLLs, and the payload-copy
benchmark runs on any machine with the SDK.

**#17 is the cheapest and highest-value:** if pythonnet does not fill
`BioCamPool.Activate`'s optional parameter, the very first .NET call of every
session fails. The code now falls back defensively, but confirming which form
works costs a minute.

**The environment matters as much as the code.** `docs/lab/storage-setup.md`
covers the drive requirements and the sustained-write test. Three separate
OneDrive hazards have now cost real time on this project: `.git` corruption risk,
recording into a synced folder stalling writes, and .NET refusing to load DLLs
carrying Windows' mark-of-the-web until `Unblock-File` clears it.
