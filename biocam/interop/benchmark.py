"""Measure the cost of copying a packet payload out of .NET.

The acquisition callback must copy each payload and return. At 152 MB/s with
1 ms packets that is roughly 152 KB every millisecond, so the copy strategy
matters. This measures the candidates.

Requires the 3Brain DLLs on disk. Does NOT require a BioCAM — creating a .NET
byte[] and copying from it involves no hardware.

Usage:
    python -m biocam.interop.benchmark
"""

import statistics
import time
from ctypes import addressof, c_char
from pathlib import Path

DLL_DIR = Path(__file__).resolve().parent.parent.parent / "BioCam_DupleX_API" / "API"

PAYLOAD_BYTES = 152_000      # ~1 ms at the reference rate
REPEATS = 2000
WARMUP = 3


def _load_runtime():
    import pythonnet
    pythonnet.load("netfx")
    import clr
    clr.AddReference(str(DLL_DIR / "3Brain.Common.dll"))
    clr.AddReference(str(DLL_DIR / "3Brain.BioCamDriver.dll"))


def _expected_pattern():
    """A recognisable, non-zero byte pattern. A strategy that returns a
    correctly-sized block of zeros (or garbage) must still fail verification,
    so every value in range 1..251 is used and the sequence repeats with a
    period that does not evenly divide PAYLOAD_BYTES-aligned chunks."""
    return bytes((i % 251) + 1 for i in range(PAYLOAD_BYTES))


def _verify(label, fn, payload, expected):
    """Run the strategy once outside the timed loop and confirm it actually
    produced PAYLOAD_BYTES of the expected content. Without this, a strategy
    that silently short-circuits or copies the wrong bytes would still get a
    (meaningless) timing number."""
    result = bytes(fn(payload))
    if len(result) != PAYLOAD_BYTES:
        raise AssertionError(
            f"{label}: expected {PAYLOAD_BYTES:,} bytes, got {len(result):,}"
        )
    if result != expected:
        raise AssertionError(f"{label}: copied content does not match the expected pattern")
    print(f"{label:<34} verified: {len(result):,} bytes match expected pattern")


def _time(label, fn, payload):
    for _ in range(WARMUP):
        fn(payload)
    samples = []
    start = time.perf_counter()
    for _ in range(REPEATS):
        call_start = time.perf_counter()
        fn(payload)
        samples.append(time.perf_counter() - call_start)
    elapsed = time.perf_counter() - start
    mean_us = elapsed / REPEATS * 1e6
    median_us = statistics.median(samples) * 1e6
    mb_per_s = (PAYLOAD_BYTES * REPEATS / 1e6) / elapsed
    print(
        f"{label:<34} mean {mean_us:7.1f} us   median {median_us:7.1f} us   {mb_per_s:8.0f} MB/s"
    )
    return mean_us, median_us


def main():
    _load_runtime()
    import System
    from System.Runtime.InteropServices import Marshal

    expected = _expected_pattern()
    payload = System.Array[System.Byte](expected)

    print(f"payload {PAYLOAD_BYTES:,} bytes, {REPEATS:,} repeats, {WARMUP} warm-up calls\n")

    print("Verification (run once, outside the timed loop):")
    _verify("bytes(payload)", lambda p: bytes(p), payload, expected)

    # All ctypes machinery is built exactly once, outside the timed closure,
    # so the timed call does nothing but Marshal.Copy itself.
    buffer = bytearray(PAYLOAD_BYTES)
    view = (c_char * PAYLOAD_BYTES).from_buffer(buffer)
    dest_ptr = System.IntPtr(addressof(view))

    def marshal_copy(p):
        Marshal.Copy(p, 0, dest_ptr, PAYLOAD_BYTES)
        return buffer

    _verify("Marshal.Copy into bytearray", marshal_copy, payload, expected)
    print()

    _time("bytes(payload)", lambda p: bytes(p), payload)
    _time("Marshal.Copy into bytearray", marshal_copy, payload)

    budget_us = 1000.0
    print(f"\nBudget is {budget_us:.0f} us per packet at a 1 ms acquisition period.")
    print("A strategy above that cannot keep up and must not be used in the callback.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
