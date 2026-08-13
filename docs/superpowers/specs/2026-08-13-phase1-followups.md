# Phase 1 follow-ups

Items deliberately carried forward from the Phase 1 implementation, recorded here
because the working notes that held them are deleted once the branch merges.

Nothing below blocks the merge. The first item blocks the **lab handover**.

---

## Blocks the lab handover

**Gate 1 was only partially satisfied.** The `biocam-api-verifier` subagent could
not be dispatched during implementation: the definition files exist under
`.claude/agents/`, but Claude Code loads agent definitions at session start and
those files were created mid-session by Phase 0. A general-purpose agent carrying
the verifier's instructions was substituted. It found a real defect — a discarded
`StopDataStreaming()` return value — but missed the Critical that the independent
review caught: a blocking queue put inside a driver callback.

`biocam/interop/` is the only code in the acquisition path with no automated
coverage. **Re-run the verifier in a fresh session against
`biocam/interop/device.py` and `biocam/interop/source.py` before any code goes to
the lab**, and confirm the agent is registered before relying on it.

---

## Correctness, deferred with reasons

**A device reset from a low counter is still reported as loss.** `packets_lost`
treats a delta above half the modulus as an anomaly, which catches a backwards
counter and a reset from a high value. A reset from below 32768 — say 50000 → 0,
a delta of 15536 — is arithmetically identical to a genuine 15,535-packet loss and
is reported as such.

This is a limit of the counter signal, not a defect in the threshold, which is the
maximum-likelihood split for counters alone. The fix is a timestamp cross-check: a
real fifteen-second loss advances the instrument's clock by fifteen seconds, a
counter reset does not. `GapTracker.observe()` can take an optional `timestamp=`
without breaking existing callers, so this stays cheap to add.

**`callback_errors` reached the sidecar late.** Resolved during the final fix
wave — recorded here only because the ledger entry predates the fix.

**The packet queue is not cleared between `start()` and `stop()` cycles.** A
consumer that breaks out of iteration before draining could leave a stale sentinel
or a straggler packet for a later session. The current CLI opens one session per
process and cannot reach this, so it is latent rather than live.

---

## Test quality, deferred

- `test_describe_handles_every_event_type` enumerates today's seven event types by
  hand. An eighth added without a `describe()` branch would not be caught here,
  though it would raise `TypeError` loudly at runtime.
- The ragged-packet decoder test never produces a one-sample-pending state, so its
  docstring claim of exercising "every possible offset within a frame" overstates
  what it does. The carry-over logic is offset-uniform, so the practical risk is low.
- `test_importing_the_cli_does_not_load_interop` is a weak in-suite proxy: a
  module-scope import violation would surface as a collection error for the whole
  file rather than as that assertion failing. The authoritative check is the
  fresh-subprocess run in the task report, which passes.
- `convert()` produces a `(1, n_channels)` chunk shape for a zero-frame recording.
  Guarded, but untested.

---

## Documentation

**.NET refuses to load the 3Brain DLLs from a OneDrive-synced path** until
`Unblock-File` clears Windows' mark-of-the-web. This cost real time during
implementation and will affect anyone cloning the repository into OneDrive. It
belongs in `README.md` or `docs/lab/storage-setup.md`, which already documents two
other OneDrive hazards — `.git` corruption risk and the recordings-folder sync
problem.

---

## Hardware-only assumptions

Six remain, down from eleven; three were resolved during review by reflecting on
the loaded assemblies, which needs no instrument. The current list lives in the
Phase 1 spec §8 and must accompany the first recording sent to the lab, per Gate 2
item 5.
