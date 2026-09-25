"""Environment preflight checks.

Run before an experiment to confirm the machine is set up correctly. Checks only
things that can be verified without the instrument: interpreter version,
required packages, presence of the 3Brain DLLs, and that those DLLs load into
the .NET runtime. Loading an assembly makes no USB call, so this is safe to
run while BrainWave or another process holds the BioCAM.

It does not detect the device or the MEA plate: both need the instrument
claimed, which preflight deliberately never does.

Usage:
    python -m biocam.preflight
"""

import importlib
import importlib.util
import shutil
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

# Everything requirements.txt installs. pythonnet fails on a development
# machine, correctly: that machine cannot drive the instrument.
REQUIRED_PACKAGES = ["numpy", "h5py", "pythonnet"]

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
    dlls = [_check_dll(dll_dir, name) for name in REQUIRED_DLLS]
    results.extend(dlls)
    results.append(_check_assemblies_load(dll_dir, all(r.ok for r in dlls)))
    return results


def _pythonnet_available() -> bool:
    return importlib.util.find_spec("pythonnet") is not None


def _check_assemblies_load(dll_dir, dlls_present):
    """Load the 3Brain assemblies into .NET, exactly as a recording would.

    Present on disk is not the same as loadable: a missing .NET Framework, a
    32/64-bit mismatch, or DLLs Windows has blocked as downloaded from the
    internet all pass the file checks above and fail here. Loading makes no
    USB call; nothing is claimed.
    """
    name = "3Brain assemblies load"
    if not dlls_present:
        return CheckResult(name, False, "not attempted: DLLs missing (above)")
    if not _pythonnet_available():
        return CheckResult(name, False, "not attempted: pythonnet missing (above)")
    from biocam.interop import device

    try:
        device.load_assemblies(dll_dir)
    except Exception as exc:  # noqa: BLE001 - reported, the point of the check
        return CheckResult(name, False, f"{type(exc).__name__}: {exc}")
    return CheckResult(name, True, ", ".join(device.ASSEMBLIES))


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


def bytes_per_second(total_channels: int, ch_sample_byte_size: int,
                     frame_rate_hz: float) -> float:
    """Raw data rate of a recording, in bytes per second."""
    return total_channels * ch_sample_byte_size * frame_rate_hz


def check_disk_space(directory, planned_seconds: float,
                     bytes_per_sec: float) -> CheckResult:
    """Whether the drive holds a recording of the planned length.

    Losing the final hour of an experiment to a full disk is entirely
    preventable, and this is where it is prevented. See
    docs/lab/storage-setup.md for the arithmetic behind the rate.
    """
    directory = Path(directory)
    required = int(planned_seconds * bytes_per_sec)
    free = shutil.disk_usage(directory).free
    return CheckResult(
        f"disk space for {planned_seconds:g}s",
        free >= required,
        f"{free:,} bytes free in {directory}, {required:,} required",
    )


def main():
    results = check_environment(DEFAULT_DLL_DIR)
    print(format_report(results))
    return 0 if all(r.ok for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
