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
    size = path.stat().st_size
    if size == 0:
        return CheckResult(name, False, f"0 bytes (empty file) in {dll_dir}")
    return CheckResult(name, True, f"{size} bytes")


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
