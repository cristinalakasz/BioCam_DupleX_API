# CLAUDE.md

## The core constraint

The BioCAM DupleX instrument is not connected to this machine — it is ~600 km
away. No hardware-dependent code can be executed or tested here. A colleague on
site runs it, days later, and reports back.

**Never claim hardware-dependent code works.** You cannot know that. Say instead
that it is untested, and name exactly what must be verified in the lab (which
call, which behavior, which assumption). A vague "should work" is worse than
silence — it erases the one thing the colleague needs from you: a precise list
of what to check.

## The three-layer rule

- `biocam/interop/` — Layer 1, .NET interop via pythonnet. The only package
  allowed to import `clr`. Cannot be tested here; written and reviewed by hand.
- `biocam/data/` — Layer 2, pure byte/number logic. Fully testable here.
- `biocam/analysis/` — Layer 3, signal processing. Fully testable here.

Layer 2 code is never written without tests. It is testable, so untested Layer 2
code is not a limitation of the environment — it's a choice, and the wrong one.

## The callback rule

`DataReceived` runs on the acquisition thread and is time-critical. Inside it:
no disk I/O, no printing or logging, no unbounded allocation, no locks. Hand the
payload off to a bounded queue and return immediately. A blocked callback drops
samples silently — the recording looks like real signal, not an error.

## API ground truth

Never guess a .NET member name, signature, or behavior. Verify against:
- `BioCam_DupleX_API/API/3Brain.BioCamDriver.xml`
- `BioCam_DupleX_API/SampleApp_BioCamCL/MainForm.cs`

The stimulator lifecycle is `Initialize → Start → Stop → Close`. `connector.py`
currently never calls `Start()`, so stimulation silently fails to fire — a known
defect. Remediation is scheduled in
`docs/superpowers/specs/2026-08-12-api-roadmap-decomposition.md`; check that
file for the current phase rather than assuming one here.

`RectangularStimPulse` and `StimProperties` live in `_3Brain.Common`, which has
**no XML documentation in this repo**. Their members cannot be verified from
documentation here — treat any claim about them as unverifiable and say so.

## The two mandatory gates

**Gate 1 — before committing:** run `biocam-api-verifier` on any Layer 1 change;
run `realtime-safety-reviewer` on any change to the `DataReceived` path.

**Gate 2 — before a lab session** (a colleague's day on a shared instrument,
not repeatable on demand):
1. `biocam-api-verifier` clean across all interop code, not just what changed.
2. `realtime-safety-reviewer` clean across the whole data path.
3. Full test suite passing — this covers Layers 2 and 3 only, proves nothing
   about Layer 1.
4. Preflight script runs and reports correctly.
5. Every known-untested assumption written down explicitly, so the colleague
   knows what is being tried for the first time and what to report. Every
   Layer 1 change since the last session belongs on this list.

## Commands

- `python -m pytest` — runs the suite (Layers 2–3 and scaffolding only).
- `python -m biocam.preflight` — environment checks only; does not detect or
  contact the device.
- `pip install -r requirements-dev.txt` — dev machine, no pythonnet.
- `pip install -r requirements.txt` — lab machine, includes pythonnet.

A green suite is evidence for Layers 2–3. It is never evidence that instrument
code works — say so explicitly whenever reporting test results.

## Git workflow

**Never commit directly to `main`.** Feature work happens on a branch.

**Starting.** Fetch before branching, always — changes reach `origin/main` from
outside any given session, including through the GitHub web UI. `git log --all`
covers *local* refs only, so without a fetch you can look straight at a stale
`main` and conclude, wrongly, that work does not exist. This has already caused a
false "nothing found" report on this repo.

```
git fetch origin
git checkout main && git merge --ff-only origin/main
git checkout -b <feature-branch>
```

If `--ff-only` is refused, local `main` has diverged — stop and report it rather
than forcing the merge.

**Finishing.** Land the work on `main` and keep a PR as the record of what the
phase delivered. The order is fixed, because a forge will refuse to open a pull
request once `main` already contains the branch's commits:

1. Push the feature branch.
2. Open the PR against `main`. **Before the merge, not after.**
3. `git checkout main`, then `git merge --no-ff` — the phase boundary should stay
   visible in history rather than vanish into a fast-forward.
4. Run the full suite on the merged result. A green run on the branch only proves
   the branch.
5. Push `main`. The PR is then marked merged automatically.
6. Delete the local feature branch.

## Conventions

English throughout. Never commit `.dll` files, `.raw` files outside
`tests/fixtures/`, or `.env`.
