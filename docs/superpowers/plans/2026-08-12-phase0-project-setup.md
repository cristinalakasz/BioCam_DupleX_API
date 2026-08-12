# Phase 0 — Project Setup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Create the documentation, dependency pinning, package skeleton, verifier agents, and test scaffolding that make all later phases verifiable on a laptop with no BioCAM attached.

**Architecture:** Three layers split by testability (setup spec §3). Layer 1 (`biocam/interop/`) calls the 3Brain .NET assemblies and cannot be executed here. Layer 2 (`biocam/data/`) is pure byte-and-number logic. Layer 3 (`biocam/analysis/`) is signal processing. This phase creates the package skeleton and a guard test that mechanically enforces the split, so the boundary is a property of the code rather than a paragraph in a document.

**Tech Stack:** Python 3.12, numpy, pythonnet (runtime only, never imported by tests), pytest.

## Global Constraints

- **The BioCAM is not attached to the development machine.** No task in this plan may require running hardware code.
- **The whole test suite must pass on a machine with no BioCAM and no 3Brain DLLs.** No test may import `clr` or `pythonnet`, directly or transitively.
- **English throughout** — identifiers, docstrings, comments, log messages.
- **Never commit** `*.dll`, `*.raw` outside `tests/fixtures/`, or `.env`.
- Target Python version: **3.12** (3.12.10 verified present on the development machine).
- Existing scripts `connector.py`, `recorder.py`, `Hello_BioCam.py` are **not modified in this phase**. They are Phase 1's work.
- Source recording constants, measured from `BioCam_DupleX_API/recordings/20260624_140615*`:
  - `frame_rate_hz = 18557.720703125`
  - `total_channels = 4096`, `ch_sample_byte_size = 2` → **8192 bytes per frame**
  - `adc_counts_to_value = 2.0146520146520146`, `offset = -4125.0`
  - `min_digital_value = 0`, `max_digital_value = 4095`, `bit_depth = 12`
  - file is 1,511,202,816 bytes = exactly 184,473 frames, zero remainder
- The seven vendor DLLs, with exact byte sizes for the README's verification step:

  | File | Bytes |
  |---|---|
  | `3Brain.BioCamDriver.dll` | 4,191,232 |
  | `3Brain.Common.dll` | 545,792 |
  | `3Brain.Deployment.Drivers.dll` | 2,174,976 |
  | `3Brain.Diagnostic.dll` | 18,944 |
  | `3Brain.Processing.Core.dll` | 12,108,288 |
  | `3Brain.Processing.Native.dll` | 50,722,816 |
  | `Newtonsoft.Json.dll` | 711,952 |

---

## Deviations from the spec, resolved here

Two problems in `docs/superpowers/specs/2026-08-03-claude-project-setup-design.md` were found while measuring. This plan resolves them; the spec is corrected in Task 8.

1. **Fixture size.** The spec assumed a 5-second fixture costs "a few hundred KB". Real cost is 152 MB/s, so 5 s = 760 MB. Replaced by two fixtures totalling ~3.2 MB (Task 3), and decoder tests use synthetic buffers instead of recorded data, because only a constructed input allows asserting an exact expected output.
2. **The preflight script was referenced but never listed as a deliverable** (spec §5.5 and Gate 2 item 4 depend on it; §4 omits it). Device detection is Layer 1 and belongs to Phase 1. This phase delivers the *environment* half — DLL presence, Python version, package imports — which is pure logic and fully testable. Task 6.

---

## File structure

| Path | Responsibility |
|---|---|
| `requirements.txt` | Runtime deps: numpy, pythonnet |
| `requirements-dev.txt` | Test deps: pytest, numpy. **No pythonnet.** |
| `pytest.ini` | Test configuration and import mode |
| `biocam/__init__.py` | Package root, states the layer rules |
| `biocam/interop/__init__.py` | Layer 1. The only place `clr` may be imported |
| `biocam/data/__init__.py` | Layer 2. Pure byte/number logic |
| `biocam/analysis/__init__.py` | Layer 3. Signal processing |
| `biocam/preflight.py` | Environment checks (no device) |
| `tools/make_fixtures.py` | One-off fixture generator, committed for provenance |
| `tests/fixtures/*.raw`, `*_meta.json` | Committed test signal |
| `tests/test_no_hardware_imports.py` | Guard enforcing the layer split |
| `tests/test_fixture_integrity.py` | Proves fixtures are self-consistent |
| `tests/test_preflight.py` | Tests the environment checks |
| `CLAUDE.md` | Briefing loaded every session |
| `.claude/agents/*.md` | Three subagents |
| `.claude/settings.json` | Permissions |
| `README.md` | Lab manual |

---

### Task 1: Dependency pinning and test configuration

**Files:**
- Create: `requirements.txt`, `requirements-dev.txt`, `pytest.ini`

**Interfaces:**
- Consumes: nothing.
- Produces: a working `pytest` invocation. All later tasks run `pytest` and assume `numpy` is importable.

- [ ] **Step 1: Install the dependencies**

```powershell
python -m pip install pytest numpy pythonnet
```

`pythonnet` is installed here even though no test may import it. Installing a package is not importing it, so the guard test in Task 4 is unaffected — and installing it is the only way to pin a real version rather than guessing one.

- [ ] **Step 2: Capture the exact installed versions**

```powershell
python -m pip show numpy pytest pythonnet | Select-String '^(Name|Version):'
```

Record the three version numbers and use them verbatim below in place of `<numpy-version>`, `<pytest-version>` and `<pythonnet-version>`. Pinning exact versions is the whole point of this task: the on-site colleague must reproduce the environment, and `>=` ranges do not reproduce.

- [ ] **Step 3: Write `requirements.txt`**

```
# Runtime dependencies for running the BioCAM on the lab machine.
# Requires Windows and .NET Framework 4.7+ — pythonnet will not install elsewhere.
numpy==<numpy-version>
pythonnet==<pythonnet-version>
```

- [ ] **Step 4: Write `requirements-dev.txt`**

```
# Development and test dependencies.
# Deliberately EXCLUDES pythonnet: the test suite must run on a machine with
# no BioCAM and no 3Brain DLLs. See tests/test_no_hardware_imports.py.
numpy==<numpy-version>
pytest==<pytest-version>
```

- [ ] **Step 5: Write `pytest.ini`**

```ini
[pytest]
testpaths = tests
python_files = test_*.py
python_functions = test_*
addopts = -v --strict-markers
```

- [ ] **Step 6: Verify pytest runs and collects nothing yet**

Run: `python -m pytest`
Expected: exit code 5, `no tests ran`. Exit code 5 means "no tests collected", which is correct at this point — it proves configuration is valid without any tests existing.

- [ ] **Step 7: Commit**

```bash
git add requirements.txt requirements-dev.txt pytest.ini
git commit -m "Pin dependencies and add pytest configuration"
```

---

### Task 2: Package skeleton with the layer split

**Files:**
- Create: `biocam/__init__.py`, `biocam/interop/__init__.py`, `biocam/data/__init__.py`, `biocam/analysis/__init__.py`

**Interfaces:**
- Consumes: Task 1's pytest configuration.
- Produces: the four packages. Task 4's guard test scans `biocam/data`, `biocam/analysis` and `tests`; Task 6 adds `biocam/preflight.py`.

- [ ] **Step 1: Create `biocam/__init__.py`**

```python
"""BioCAM DupleX control and analysis.

The package is split into three layers by whether a laptop can prove the code
correct. This is not a stylistic preference; it is what allows development
without the instrument attached.

    biocam.interop   Layer 1  Calls the 3Brain .NET assemblies. NOT testable
                              without hardware. The only layer permitted to
                              import `clr` or `pythonnet`.

    biocam.data      Layer 2  Pure byte-and-number logic: payload decoding,
                              frame reassembly, unit conversion, gap detection.
                              Fully testable with synthetic buffers.

    biocam.analysis  Layer 3  Signal processing: spike detection, sorting.
                              Fully testable against recorded fixtures.

tests/test_no_hardware_imports.py enforces that Layers 2 and 3 never import the
interop layer's dependencies. If that test fails, the boundary has been broken.
"""
```

- [ ] **Step 2: Create `biocam/interop/__init__.py`**

```python
"""Layer 1 — .NET interop with the 3Brain BioCamDriver assemblies.

Nothing here can be executed on a machine without the BioCAM and the 3Brain
DLLs. Code in this layer is verified by reading, not by running: every .NET call
must be checked against API/3Brain.BioCamDriver.xml and the C# reference sample
before it reaches the lab. See the biocam-api-verifier agent.

This is the ONLY package permitted to import `clr` or `pythonnet`.

Empty in Phase 0. Populated by Phase 1.
"""
```

- [ ] **Step 3: Create `biocam/data/__init__.py`**

```python
"""Layer 2 — pure data logic.

Payload bytes to frames, partial-frame carry-over across packet boundaries,
ADC counts to microvolts, metadata handling, gap detection from hardware
timestamps.

Touches no hardware. Every function here is a function from bytes and numbers to
bytes and numbers, and must be unit-tested with synthetic buffers — including the
awkward cases that are hard to produce on real hardware, such as a payload ending
mid-frame or a timestamp discontinuity indicating packet loss.

MUST NOT import `clr` or `pythonnet`, directly or transitively.

Empty in Phase 0. Populated by Phase 1.
"""
```

- [ ] **Step 4: Create `biocam/analysis/__init__.py`**

```python
"""Layer 3 — signal processing.

Spike detection, spike sorting, closed-loop decision logic. Tested against the
recorded fixtures in tests/fixtures/.

MUST NOT import `clr` or `pythonnet`, directly or transitively.

Empty in Phase 0. Populated by Phase 5.
"""
```

- [ ] **Step 5: Verify the packages import**

Run: `python -c "import biocam, biocam.interop, biocam.data, biocam.analysis; print('ok')"`
Expected: `ok`

- [ ] **Step 6: Commit**

```bash
git add biocam/
git commit -m "Add package skeleton with the three-layer split"
```

---

### Task 3: Generate and commit the test fixtures

**Files:**
- Create: `tools/make_fixtures.py`, `tests/fixtures/sample_32ch_2s.raw`, `tests/fixtures/sample_32ch_2s_meta.json`, `tests/fixtures/sample_full_100frames.raw`, `tests/fixtures/sample_full_100frames_meta.json`
- Modify: `.gitignore`

**Interfaces:**
- Consumes: numpy from Task 1.
- Produces: two fixture pairs. Task 5 asserts their integrity. Later phases read them via `tests/fixtures/<name>.raw` + `<name>_meta.json`.

**Why two fixtures.** `sample_32ch_2s` carries enough duration for detection work at a manageable size. `sample_full_100frames` is short but full-width, so code that assumes 4096 channels per frame is exercised against real layout. Neither is used for decoder unit tests — those use synthetic buffers, because asserting an exact expected output requires a constructed input.

- [ ] **Step 1: Allow the fixtures through `.gitignore`**

The existing `*.raw` rule would exclude them. Add immediately after the `recordings/` block in `.gitignore`:

```
# Test fixtures are deliberately committed: small slices of real signal, needed
# to test analysis code on a machine with no instrument.
!tests/fixtures/*.raw
```

- [ ] **Step 2: Write `tools/make_fixtures.py`**

```python
"""Generate committed test fixtures from a full BioCAM recording.

Run once, by hand. The source recording is ~1.4 GB and is NOT in the repository,
so this script cannot be re-run from a fresh clone. It is committed to document
exactly how the fixtures were produced.

Usage:
    python tools/make_fixtures.py <source.raw> <source_meta.json>
"""

import json
import sys
from pathlib import Path

import numpy as np

FIXTURE_DIR = Path(__file__).resolve().parent.parent / "tests" / "fixtures"

SUBSET_CHANNELS = 32
SUBSET_SECONDS = 2.0
FULL_FRAMES = 100


def _load(raw_path, meta_path):
    meta = json.loads(Path(meta_path).read_text())
    n_ch = meta["total_channels"]
    dtype = {1: np.uint8, 2: np.uint16, 4: np.uint32}[meta["ch_sample_byte_size"]]
    data = np.memmap(raw_path, dtype=dtype, mode="r").reshape(-1, n_ch)
    return data, meta


def _pick_active_channels(data, n_wanted, sample_frames=20000):
    """Choose the n_wanted channels with the largest variance.

    Variance is a crude activity proxy, but it reliably separates live
    electrodes from flat or saturated ones, which is all that is needed to make
    the fixture useful for detection work.
    """
    window = data[:sample_frames].astype(np.float64)
    variance = window.var(axis=0)
    return np.sort(np.argsort(variance)[-n_wanted:])


def _write(name, block, meta, channels=None):
    raw_out = FIXTURE_DIR / f"{name}.raw"
    meta_out = FIXTURE_DIR / f"{name}_meta.json"

    block.tofile(raw_out)

    fixture_meta = {
        "frame_rate_hz": meta["frame_rate_hz"],
        "n_wells": 1,
        "n_channels_per_well": int(block.shape[1]),
        "total_channels": int(block.shape[1]),
        "ch_sample_byte_size": meta["ch_sample_byte_size"],
        "bit_depth": meta["bit_depth"],
        "adc_counts_to_value": meta["adc_counts_to_value"],
        "offset": meta["offset"],
        "min_digital_value": meta["min_digital_value"],
        "max_digital_value": meta["max_digital_value"],
        "n_frames_total": int(block.shape[0]),
        "duration_sec": float(block.shape[0] / meta["frame_rate_hz"]),
        "source_recording": "20260624_140615",
        "source_channels": None if channels is None else [int(c) for c in channels],
    }
    meta_out.write_text(json.dumps(fixture_meta, indent=2))
    print(f"{raw_out.name}: {block.shape[0]} frames x {block.shape[1]} ch "
          f"= {raw_out.stat().st_size} bytes")


def main():
    if len(sys.argv) != 3:
        print(__doc__)
        return 1

    data, meta = _load(sys.argv[1], sys.argv[2])
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)

    n_frames = int(SUBSET_SECONDS * meta["frame_rate_hz"])
    channels = _pick_active_channels(data, SUBSET_CHANNELS)
    _write("sample_32ch_2s",
           np.ascontiguousarray(data[:n_frames][:, channels]), meta, channels)

    _write("sample_full_100frames",
           np.ascontiguousarray(data[:FULL_FRAMES]), meta)
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 3: Run the generator**

```powershell
python tools/make_fixtures.py `
  "BioCam_DupleX_API/recordings/20260624_140615.raw" `
  "BioCam_DupleX_API/recordings/20260624_140615_meta.json"
```

Expected output, approximately:
```
sample_32ch_2s.raw: 37115 frames x 32 ch = 2375360 bytes
sample_full_100frames.raw: 100 frames x 4096 ch = 819200 bytes
```

- [ ] **Step 4: Verify the fixtures are tracked, not ignored**

Run: `git status --short tests/fixtures/`
Expected: four untracked files listed. If the `.raw` files are missing, Step 1's negation rule is wrong — `!tests/fixtures/*.raw` must appear *after* the `*.raw` rule in the file to take effect.

- [ ] **Step 5: Commit**

```bash
git add .gitignore tools/make_fixtures.py tests/fixtures/
git commit -m "Add test fixtures cut from a real recording"
```

---

### Task 4: Guard test enforcing the layer split

**Files:**
- Create: `tests/test_no_hardware_imports.py`

**Interfaces:**
- Consumes: the packages from Task 2.
- Produces: nothing consumed by later tasks. This is the mechanism that keeps the architecture honest.

**Why both a static and a dynamic check.** The static AST scan catches an `import clr` written anywhere in Layers 2–3 or in the tests. The dynamic check catches the case the AST scan cannot see: a first-party module that pulls in the interop layer through a chain of ordinary-looking imports. Either alone leaves a real gap.

- [ ] **Step 1: Write the failing test**

```python
"""Guard: the test suite must run with no BioCAM and no 3Brain DLLs.

This is stronger than "no instrument attached". Importing the interop layer
requires the 3Brain assemblies to be present on disk, so no test and no Layer
2/3 module may import `clr` or `pythonnet` at all.

If this test fails, the layer boundary described in biocam/__init__.py has been
broken, and the suite is about to become unrunnable on a development machine.
"""

import ast
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# Directories that must stay free of interop dependencies.
# biocam/interop is deliberately absent: it is the one place `clr` is allowed.
GUARDED_DIRS = [
    REPO_ROOT / "biocam" / "data",
    REPO_ROOT / "biocam" / "analysis",
    REPO_ROOT / "tests",
    REPO_ROOT / "tools",
]

FORBIDDEN_ROOTS = {"clr", "pythonnet", "clr_loader"}


def _guarded_python_files():
    for directory in GUARDED_DIRS:
        if directory.exists():
            yield from sorted(directory.rglob("*.py"))


def _imported_roots(path):
    """Return the top-level module names imported by a Python file."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            # level > 0 is a relative import; it has no top-level root to check.
            if node.level == 0 and node.module:
                roots.add(node.module.split(".")[0])
    return roots


@pytest.mark.parametrize(
    "path", list(_guarded_python_files()), ids=lambda p: str(p.relative_to(REPO_ROOT))
)
def test_module_does_not_import_interop_dependencies(path):
    offending = _imported_roots(path) & FORBIDDEN_ROOTS
    assert not offending, (
        f"{path.relative_to(REPO_ROOT)} imports {sorted(offending)}. "
        "Layers 2 and 3 and the test suite must run with no 3Brain DLLs "
        "installed. Move hardware access into biocam/interop/."
    )


def test_interop_dependencies_are_not_loaded_at_runtime():
    """Nothing collected so far may have pulled in the interop stack."""
    loaded = FORBIDDEN_ROOTS & set(sys.modules)
    assert not loaded, (
        f"{sorted(loaded)} was imported while running the test suite. "
        "Some first-party module reaches the interop layer transitively."
    )
```

- [ ] **Step 2: Run it and confirm it passes**

Run: `python -m pytest tests/test_no_hardware_imports.py -v`
Expected: PASS — one parametrised case per file found, plus the runtime check.

This test passes immediately rather than starting red, because it asserts an invariant that currently holds. Step 3 proves it can actually fail, which is the part that matters.

- [ ] **Step 3: Prove the guard detects a violation**

Temporarily append to `biocam/data/__init__.py`:

```python
import clr  # TEMPORARY - proving the guard works
```

Run: `python -m pytest tests/test_no_hardware_imports.py -v`
Expected: FAIL on `biocam/data/__init__.py` with the message about moving hardware access into `biocam/interop/`.

**Now remove that line again** and re-run to confirm it passes. A guard never seen to fail is not known to work.

- [ ] **Step 4: Commit**

```bash
git add tests/test_no_hardware_imports.py
git commit -m "Add guard test enforcing the hardware-free layer split"
```

---

### Task 5: Fixture integrity test

**Files:**
- Create: `tests/test_fixture_integrity.py`

**Interfaces:**
- Consumes: fixtures from Task 3.
- Produces: `FIXTURES` list and `load_fixture(name)` — later phases reuse this loader rather than reimplementing it.

- [ ] **Step 1: Write the failing test**

```python
"""The fixtures must agree with their own metadata.

If a .raw file and its _meta.json disagree about channel count or sample size,
every future test built on that fixture measures the wrong thing while appearing
to pass. Cheap to check once; expensive to discover later.
"""

import json
from pathlib import Path

import numpy as np
import pytest

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"
FIXTURES = ["sample_32ch_2s", "sample_full_100frames"]

DTYPE_BY_BYTE_SIZE = {1: np.uint8, 2: np.uint16, 4: np.uint32}


def load_fixture(name):
    """Return (data, meta) for a committed fixture.

    data has shape (n_frames, total_channels) in raw ADC counts.
    """
    meta = json.loads((FIXTURE_DIR / f"{name}_meta.json").read_text())
    dtype = DTYPE_BY_BYTE_SIZE[meta["ch_sample_byte_size"]]
    raw = np.fromfile(FIXTURE_DIR / f"{name}.raw", dtype=dtype)
    n_ch = meta["total_channels"]
    return raw.reshape(-1, n_ch), meta


@pytest.mark.parametrize("name", FIXTURES)
def test_file_size_is_a_whole_number_of_frames(name):
    meta = json.loads((FIXTURE_DIR / f"{name}_meta.json").read_text())
    size = (FIXTURE_DIR / f"{name}.raw").stat().st_size
    bytes_per_frame = meta["total_channels"] * meta["ch_sample_byte_size"]
    assert size % bytes_per_frame == 0, (
        f"{name}.raw is {size} bytes, not a multiple of {bytes_per_frame}"
    )


@pytest.mark.parametrize("name", FIXTURES)
def test_frame_count_matches_metadata(name):
    data, meta = load_fixture(name)
    assert data.shape[0] == meta["n_frames_total"]


@pytest.mark.parametrize("name", FIXTURES)
def test_samples_are_within_the_declared_digital_range(name):
    data, meta = load_fixture(name)
    assert data.min() >= meta["min_digital_value"]
    assert data.max() <= meta["max_digital_value"]


@pytest.mark.parametrize("name", FIXTURES)
def test_duration_matches_frame_count_and_rate(name):
    data, meta = load_fixture(name)
    expected = data.shape[0] / meta["frame_rate_hz"]
    assert meta["duration_sec"] == pytest.approx(expected, rel=1e-9)


def test_full_width_fixture_really_is_full_width():
    _, meta = load_fixture("sample_full_100frames")
    assert meta["total_channels"] == 4096


def test_subset_fixture_records_which_channels_it_kept():
    _, meta = load_fixture("sample_32ch_2s")
    assert len(meta["source_channels"]) == meta["total_channels"] == 32
```

- [ ] **Step 2: Run the tests**

Run: `python -m pytest tests/test_fixture_integrity.py -v`
Expected: all PASS. A failure here means Task 3's generator wrote inconsistent output — fix the generator and regenerate rather than relaxing the assertion.

- [ ] **Step 3: Commit**

```bash
git add tests/test_fixture_integrity.py
git commit -m "Add fixture integrity tests"
```

---

### Task 6: Environment preflight

**Files:**
- Create: `biocam/preflight.py`, `tests/test_preflight.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `check_environment(dll_dir: Path) -> list[CheckResult]` and `CheckResult(name: str, ok: bool, detail: str)`. Phase 1 extends this with device detection by appending further `CheckResult`s.

**Scope.** This checks the environment only — Python version, required packages, DLL presence. Device detection is Layer 1 and lands in Phase 1. Keeping the environment half separate means the colleague can diagnose a broken install without a working instrument, which is the common case.

- [ ] **Step 1: Write the failing test**

```python
import sys
from pathlib import Path

from biocam.preflight import CheckResult, REQUIRED_DLLS, check_environment, format_report


def test_reports_a_result_for_every_required_dll(tmp_path):
    results = check_environment(tmp_path)
    dll_checks = [r for r in results if r.name.endswith(".dll")]
    assert len(dll_checks) == len(REQUIRED_DLLS)


def test_missing_dlls_are_reported_as_failures(tmp_path):
    results = check_environment(tmp_path)
    assert all(not r.ok for r in results if r.name.endswith(".dll"))


def test_present_dlls_are_reported_as_passing(tmp_path):
    for name in REQUIRED_DLLS:
        (tmp_path / name).write_bytes(b"x")
    results = check_environment(tmp_path)
    assert all(r.ok for r in results if r.name.endswith(".dll"))


def test_python_version_check_passes_on_the_running_interpreter():
    results = check_environment(Path("."))
    version_check = next(r for r in results if r.name == "Python version")
    assert version_check.ok is (sys.version_info[:2] >= (3, 12))


def test_report_marks_failures_visibly():
    report = format_report([
        CheckResult("thing that worked", True, "fine"),
        CheckResult("thing that failed", False, "missing"),
    ])
    assert "PASS" in report
    assert "FAIL" in report
    assert "thing that failed" in report


def test_report_ends_with_an_overall_verdict():
    passing = format_report([CheckResult("a", True, "")])
    failing = format_report([CheckResult("a", False, "")])
    assert passing.strip().endswith("ALL CHECKS PASSED")
    assert "1 CHECK FAILED" in failing
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python -m pytest tests/test_preflight.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'biocam.preflight'`

- [ ] **Step 3: Write the implementation**

```python
"""Environment preflight checks.

Run before an experiment to confirm the machine is set up correctly. Checks only
things that can be verified without the instrument: interpreter version,
required packages, and presence of the 3Brain DLLs.

Device detection is Layer 1 and is added in Phase 1.

Usage:
    python -m biocam.preflight
"""

import importlib
import sys
from dataclasses import dataclass
from pathlib import Path

MIN_PYTHON = (3, 12)

REQUIRED_DLLS = [
    "3Brain.BioCamDriver.dll",
    "3Brain.Common.dll",
    "3Brain.Deployment.Drivers.dll",
    "3Brain.Diagnostic.dll",
    "3Brain.Processing.Core.dll",
    "3Brain.Processing.Native.dll",
    "Newtonsoft.Json.dll",
]

REQUIRED_PACKAGES = ["numpy"]

DEFAULT_DLL_DIR = Path(__file__).resolve().parent.parent / "BioCam_DupleX_API" / "API"


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    detail: str


def check_environment(dll_dir):
    """Run every hardware-free check and return the results in report order."""
    dll_dir = Path(dll_dir)
    results = [_check_python_version()]
    results.extend(_check_package(name) for name in REQUIRED_PACKAGES)
    results.extend(_check_dll(dll_dir, name) for name in REQUIRED_DLLS)
    return results


def _check_python_version():
    actual = ".".join(str(p) for p in sys.version_info[:3])
    required = ".".join(str(p) for p in MIN_PYTHON)
    return CheckResult(
        "Python version",
        sys.version_info[:2] >= MIN_PYTHON,
        f"found {actual}, need >= {required}",
    )


def _check_package(name):
    try:
        module = importlib.import_module(name)
    except ImportError as exc:
        return CheckResult(f"package {name}", False, str(exc))
    version = getattr(module, "__version__", "unknown version")
    return CheckResult(f"package {name}", True, version)


def _check_dll(dll_dir, name):
    path = dll_dir / name
    if not path.is_file():
        return CheckResult(name, False, f"not found in {dll_dir}")
    return CheckResult(name, True, f"{path.stat().st_size} bytes")


def format_report(results):
    """Render results as a human-readable report ending in a verdict."""
    width = max((len(r.name) for r in results), default=0)
    lines = [
        f"[{'PASS' if r.ok else 'FAIL'}] {r.name.ljust(width)}  {r.detail}"
        for r in results
    ]
    failures = sum(1 for r in results if not r.ok)
    lines.append("")
    if failures == 0:
        lines.append("ALL CHECKS PASSED")
    else:
        lines.append(f"{failures} CHECK{'S' if failures != 1 else ''} FAILED")
    return "\n".join(lines)


def main():
    results = check_environment(DEFAULT_DLL_DIR)
    print(format_report(results))
    return 0 if all(r.ok for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_preflight.py -v`
Expected: all PASS

- [ ] **Step 5: Run the preflight for real and keep the output**

Run: `python -m biocam.preflight`

Expected on the development machine: Python and numpy PASS, all seven DLLs PASS (they are present locally though gitignored). Copy the exact output — Task 8's README needs a real sample, not an invented one.

- [ ] **Step 6: Commit**

```bash
git add biocam/preflight.py tests/test_preflight.py
git commit -m "Add environment preflight checks"
```

---

### Task 7: CLAUDE.md, subagents, and permissions

**Files:**
- Create: `CLAUDE.md`, `.claude/agents/biocam-api-verifier.md`, `.claude/agents/realtime-safety-reviewer.md`, `.claude/agents/dsp-implementer.md`, `.claude/settings.json`

**Interfaces:**
- Consumes: commands established in Tasks 1–6.
- Produces: nothing consumed by code. These configure future sessions.

- [ ] **Step 1: Write `CLAUDE.md`**

Keep it short and high-signal. It must not duplicate the README — duplicated facts drift apart. Required content, in this order:

1. **The core constraint, stated first:** the BioCAM is not connected to this machine; no hardware-dependent code can be executed here; never claim such code works — say it is untested and name what must be verified in the lab.
2. **The three-layer rule**, naming `biocam/interop`, `biocam/data`, `biocam/analysis`, and the rule that Layer 2 code is never written without tests, because it is testable and untested Layer 2 code is a choice rather than a constraint.
3. **The callback rule:** `DataReceived` is time-critical — no disk I/O, no printing, no unbounded allocation, no locks. Hand off to a queue and return.
4. **API ground truth:** never guess a .NET member name; verify against `BioCam_DupleX_API/API/3Brain.BioCamDriver.xml` and `BioCam_DupleX_API/SampleApp_BioCamCL/MainForm.cs`. State the stimulator lifecycle `Initialize → Start → Stop → Close` and that `Start()` is currently missing from `connector.py`. Note that `RectangularStimPulse` lives in `_3Brain.Common`, which has **no** XML documentation in this repo.
5. **The two mandatory gates**, copied from setup spec §8: `biocam-api-verifier` before committing Layer 1 changes; `realtime-safety-reviewer` before committing data-path changes; and the five-item pre-lab checklist.
6. **Commands:** `python -m pytest`, `python -m biocam.preflight`, `pip install -r requirements-dev.txt`. Plus the rule that a green suite covers Layers 2–3 only and must never be reported as evidence that instrument code works.
7. **Conventions:** English throughout; never commit DLLs, `.raw` outside `tests/fixtures/`, or `.env`.

- [ ] **Step 2: Write `.claude/agents/biocam-api-verifier.md`**

```markdown
---
name: biocam-api-verifier
description: Verifies .NET interop calls against the 3Brain API documentation. Use before committing any change to biocam/interop/ or any code calling the BioCAM driver.
tools: Read, Grep, Glob
---

You verify Python code that calls the 3Brain BioCamDriver .NET API through
pythonnet. You cannot run this code — no BioCAM is attached — so documentation is
the only ground truth available.

## Sources of truth, in priority order

1. `BioCam_DupleX_API/API/3Brain.BioCamDriver.xml` — the complete API reference.
2. `BioCam_DupleX_API/SampleApp_BioCamCL/MainForm.cs` — 3Brain's own working
   closed-loop sample. When the XML is ambiguous, the sample shows real usage.
3. `3Brain_BioCamDriverAPI_v2.6_Introduction.pdf` — conceptual model and limits.

`RectangularStimPulse` and `StimProperties` belong to `_3Brain.Common`, which has
NO XML in this repository. Never assert their members exist. Report them as
unverifiable and say so explicitly.

## For every .NET call, check

- **Member exists.** Grep the XML for `M:` / `P:` / `T:` entries. A member you
  cannot find is a finding, not an assumption.
- **Argument count, order, and types** match the signature in the XML.
- **`out` parameters** are handled — pythonnet returns them in a tuple, so
  `Send(pulse, pos, neg, out latency)` returns `(bool, latency)` in Python.
- **Call ordering.** The stimulator requires `Initialize → Start → Stop → Close`.
  Streaming requires the device be connected and the plate seated.
- **Documented limits** are respected: <=64 pulse values and <=288 endpoints per
  `Send`; <=1000 endpoints per spatial configuration; <=1000 queued stimuli;
  positive and negative endpoints must never share an electrode column.
- **Event subscriptions** are symmetric — every `+=` has a matching `-=`.

## Output

A list of findings, each with file, line, what is wrong, and the XML or sample
line proving it. If you find nothing, say so plainly.

Report only. Never edit files.
```

- [ ] **Step 3: Write `.claude/agents/realtime-safety-reviewer.md`**

```markdown
---
name: realtime-safety-reviewer
description: Audits code on the DataReceived path for operations that block. Use before committing any change to acquisition or callback code.
tools: Read, Grep, Glob
---

You audit code that runs on the BioCAM data-acquisition path for anything that
could block long enough to drop data.

## Why this matters

The driver invokes the data callback every acquisition period (1-250 ms,
typically 1 ms for closed-loop work). Whatever runs inside must finish before the
next packet arrives. Dropped samples in electrophysiology look like real signal,
not like an error, so the failure is silent and corrupts the recording.

Closed-loop operation targets ~1.5 ms end to end. There is no slack.

## Trace and flag

Start from the callback and follow every function it calls. Flag:

- **File or network I/O** — `open`, `write`, `flush`, `json.dump`, `np.save`.
- **`print` and `logging`** — stdout is slow and can block on a full pipe.
- **Large copies** — `bytes(...)` over a payload, `np.array(x)` where a view
  would do, `.copy()`, `.tolist()`.
- **Locks** — `threading.Lock`, `RLock`, anything that can wait on another thread.
- **Unbounded growth** — `list.append` on a per-packet basis with no drain.
- **Allocation per packet** where a preallocated buffer would serve.
- **Exception handling that does real work** in the hot path.

## The correct pattern

The callback reads the header timestamp, hands the payload to a bounded queue,
and returns. A separate worker thread does everything else. If the queue is full,
that is data loss and must be counted and reported, never silently ignored.

## Output

For each finding: file, line, what it costs, and what to do instead. If the path
is clean, say so plainly.

Report only. Never edit files.
```

- [ ] **Step 4: Write `.claude/agents/dsp-implementer.md`**

```markdown
---
name: dsp-implementer
description: Implements Layer 2 and Layer 3 code test-first. Never touches biocam/interop/.
tools: Read, Write, Edit, Bash, Grep, Glob
---

You implement signal-processing and data-handling code for the BioCAM project,
test-first.

## Scope — hard boundary

You may write:
- `biocam/data/` — Layer 2, pure byte-and-number logic
- `biocam/analysis/` — Layer 3, signal processing
- `tests/`

You may NOT write or modify `biocam/interop/` or anything importing `clr` or
`pythonnet`. That code cannot be tested on this machine, so it is written and
reviewed by hand. If a task seems to require interop changes, stop and say so.

## Method

1. Write the failing test first. Run it. Confirm it fails for the reason you
   expect, not for an unrelated error.
2. Write the minimal code to pass.
3. Run the suite. All of it, not just your new test.
4. Only then move on.

## Testing rules

- **Layer 2 uses synthetic buffers**, not recorded data. You can only assert an
  exact expected output if you constructed the input. Cover the awkward cases
  explicitly: a payload ending mid-frame, a zero-length payload, a timestamp
  discontinuity, values at both ends of the digital range.
- **Layer 3 uses the committed fixtures** in `tests/fixtures/`. Load them with
  `load_fixture` from `tests/test_fixture_integrity.py`.
- **Never import `clr` or `pythonnet`.** `tests/test_no_hardware_imports.py`
  enforces this and must keep passing.

## Reporting

State what you implemented, what the tests cover, and — importantly — what they
do NOT cover. Never describe passing tests as proof that anything works on the
instrument.
```

- [ ] **Step 5: Write `.claude/settings.json`**

```json
{
  "permissions": {
    "allow": [
      "Bash(python -m pytest:*)",
      "Bash(python -m biocam.preflight)",
      "Bash(git status:*)",
      "Bash(git diff:*)",
      "Bash(git log:*)",
      "Read(//c/Users/crist/OneDrive/Desktop/BIOCOMPUTING/API/**)"
    ]
  }
}
```

- [ ] **Step 6: Verify the JSON parses**

Run: `python -c "import json,pathlib; json.loads(pathlib.Path('.claude/settings.json').read_text()); print('ok')"`
Expected: `ok`

- [ ] **Step 7: Commit**

```bash
git add CLAUDE.md .claude/
git commit -m "Add project briefing, verifier subagents, and permissions"
```

---

### Task 8: README and spec correction

**Files:**
- Create: `README.md`
- Modify: `docs/superpowers/specs/2026-08-03-claude-project-setup-design.md`

**Interfaces:**
- Consumes: the real preflight output captured in Task 6 Step 5.
- Produces: the lab manual.

**Audience: the on-site colleague**, who cannot ask questions mid-experiment. Write for someone competent who has never seen this repository.

- [ ] **Step 1: Write the README with these fourteen sections**

1. **What this is** — software to record from, and eventually closed-loop stimulate, a 3Brain BioCAM DupleX. State plainly what works today (recording) and what does not yet exist (stimulation, detection, closed loop).
2. **Before you run an experiment** — a short checklist, placed early because it is the most-reread part: close BrainWave (it holds the device and `TakeBioCamControl` will return nothing), seat the MEA plate, run preflight, check free disk space. **Include the real figure: recording consumes ~152 MB per second — about 9 GB per minute.** A session can exhaust a drive mid-experiment and lose the run.
3. **Hardware and requirements** — BioCAM DupleX, MEA plate, Windows 10/11, .NET Framework 4.7+ (4.8 satisfies this), USB. Windows-only, because the driver targets .NET Framework.
4. **The DLL step** — the seven DLLs from Global Constraints with their exact byte sizes in a table, where they come from (the 3Brain SDK / BrainWave install), that they go in `BioCam_DupleX_API/API/`, and why they are not in the repository (~140 MB, and not ours to redistribute). Give the verification command: `python -m biocam.preflight`.
5. **Environment setup** — Python 3.12; conda and venv paths both; `pip install -r requirements.txt` on the lab machine, `pip install -r requirements-dev.txt` on a development machine with no instrument.
6. **Preflight check** — the real captured output from Task 6 Step 5 as the pass example, and a failure example produced by renaming a DLL. Do not invent output.
7. **Recording** — current commands for `recorder.py`, its options, where files land, how to read one back. Note honestly that this script has known defects listed in the setup spec's Appendix A and is being rebuilt in Phase 1.
8. **Data formats** — the `.raw` layout is frame-major: all 4096 channels for frame 0, then all channels for frame 1, and so on; `uint16`, little-endian, 8192 bytes per frame. List the `_meta.json` fields. Give the conversion explicitly: `microvolts = offset + counts * adc_counts_to_value`, with this recording's values `offset = -4125.0` and `adc_counts_to_value = 2.0146520146520146`. Include a short worked numpy example.
9. **How the instrument works** — condensed from the PDF: data arrives as packets each carrying a hardware timestamp; acquisition time period is configurable 1-250 ms; closed-loop latency is ≈1.15 ms mean and ≈1.52 ms worst case at a 1 ms acquisition period; stimulation uses positive and negative endpoint pairs. State the constraints that are easy to violate: endpoints may not share an electrode column, ≤1000 endpoints, ≤1000 queued stimuli, and chip reconfiguration costs 26 µs + 8.4 µs per additional row.
10. **Troubleshooting** — a symptom → cause → fix table. Seed it with: `TakeBioCamControl` returns `None` (BrainWave or another 3Brain program holds the device — close it); no device found (USB, power); plate not seated (`MeaPlate.IsConnected` false); **no stimulation output despite no error** (`Stimulator.Start()` was never called — a known defect); data-loss warnings (acquisition period too short, or slow work in the callback); `ModuleNotFoundError: clr` (pythonnet not installed — you are on a development machine, use `requirements-dev.txt`).
11. **Project layout** — one line per top-level path, including the three-layer meaning of `biocam/interop`, `biocam/data`, `biocam/analysis`.
12. **Development** — the three-layer model and which layer new code belongs in; `python -m pytest` runs everything with no instrument and no DLLs; the fixtures and what each is for; the callback latency rule; and explicitly, **what a green suite does not prove** — it covers Layers 2-3 only and says nothing about instrument code.
13. **Before handing code to the lab** — the Gate 2 checklist reproduced in full: both verifiers clean across all code, full suite passing, preflight correct, and a written list of everything untested — which, since Layer 1 has no automated coverage, means every Layer 1 change since the last session.
14. **Roadmap and status** — the six phases from `docs/superpowers/specs/2026-08-12-api-roadmap-decomposition.md`, marking which are done.

- [ ] **Step 2: Correct the two spec errors**

In `docs/superpowers/specs/2026-08-03-claude-project-setup-design.md`:

- In §4 "Test fixture", replace the claim that a few seconds costs "a few hundred KB" with the measured reality: 18,557.72 Hz × 4096 channels × 2 bytes = 152 MB/s, so 5 s = 760 MB. State the replacement — two fixtures of ~2.4 MB and ~819 KB — and that Layer 2 decoder tests use synthetic buffers because asserting exact output requires constructed input.
- In §4, add `biocam/preflight.py` to the deliverables table, resolving the gap where §5.5 and Gate 2 item 4 depended on a preflight script that §4 never listed. Note that it covers the environment only and that device detection arrives in Phase 1.

- [ ] **Step 3: Verify every command in the README actually runs**

Run each command block from sections 4, 5, 6, and 12. Any command that fails, or whose output differs from what the README claims, must be corrected in the README rather than explained away. This is the step that stops the document going stale on day one.

- [ ] **Step 4: Run the full suite one final time**

Run: `python -m pytest`
Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add README.md docs/superpowers/specs/2026-08-03-claude-project-setup-design.md
git commit -m "Add lab manual README and correct measured fixture cost in the spec"
```

---

## Not covered by this plan

Setup spec §9 requires the on-site colleague be added as a repository
collaborator. That is blocked on their GitHub username, not on any work here, so
it is not a task. It must be done before they can clone — the README is written
for them and is useless if they cannot read it.

## Done when

- `python -m pytest` passes with no BioCAM and no 3Brain DLLs installed.
- `tests/test_no_hardware_imports.py` has been *seen to fail* when a violation is introduced, then pass when removed.
- `python -m biocam.preflight` reports every check and exits non-zero if any fail.
- `CLAUDE.md`, three agent files, and `.claude/settings.json` exist and parse.
- `README.md` covers all fourteen sections, and every command in it has been run.
- Nothing in `git status` is uncommitted.
