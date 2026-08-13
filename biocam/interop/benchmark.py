"""Measure the cost of copying a packet payload out of .NET.

The acquisition callback must copy each payload and return. At 152 MB/s with
1 ms packets that is roughly 152 KB every millisecond, so the copy strategy
matters. This measures the candidates.

Requires the 3Brain DLLs on disk. Does NOT require a BioCAM — creating a .NET
byte[] and copying from it involves no hardware.

Usage:
    python -m biocam.interop.benchmark
"""

import time
from pathlib import Path

DLL_DIR = Path(__file__).resolve().parent.parent.parent / "BioCam_DupleX_API" / "API"

PAYLOAD_BYTES = 152_000      # ~1 ms at the reference rate
REPEATS = 2000


def _load_runtime():
    import pythonnet
    pythonnet.load("netfx")
    import clr
    clr.AddReference(str(DLL_DIR / "3Brain.Common.dll"))
    clr.AddReference(str(DLL_DIR / "3Brain.BioCamDriver.dll"))


def _time(label, fn, payload):
    fn(payload)                                  # warm up
    start = time.perf_counter()
    for _ in range(REPEATS):
        fn(payload)
    elapsed = time.perf_counter() - start
    per_call_us = elapsed / REPEATS * 1e6
    mb_per_s = (PAYLOAD_BYTES * REPEATS / 1e6) / elapsed
    print(f"{label:<34} {per_call_us:8.1f} us/packet   {mb_per_s:8.0f} MB/s")
    return per_call_us


def main():
    _load_runtime()
    import System
    from System.Runtime.InteropServices import Marshal

    payload = System.Array.CreateInstance(System.Byte, PAYLOAD_BYTES)

    print(f"payload {PAYLOAD_BYTES:,} bytes, {REPEATS:,} repeats\n")

    _time("bytes(payload)", lambda p: bytes(p), payload)

    buffer = bytearray(PAYLOAD_BYTES)
    def marshal_copy(p):
        from ctypes import addressof, c_char
        target = (c_char * PAYLOAD_BYTES).from_buffer(buffer)
        Marshal.Copy(p, 0, System.IntPtr(addressof(target)), PAYLOAD_BYTES)
    _time("Marshal.Copy into bytearray", marshal_copy, payload)

    budget_us = 1000.0
    print(f"\nBudget is {budget_us:.0f} us per packet at a 1 ms acquisition period.")
    print("A strategy above that cannot keep up and must not be used in the callback.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
