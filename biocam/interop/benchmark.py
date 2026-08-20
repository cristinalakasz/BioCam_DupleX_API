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
    # LOW: this used to reimplement load_assemblies() from scratch, minus
    # its PATH/sys.path setup (see device.py) - a divergence with no
    # reason to exist, since this benchmark loads the exact same two
    # assemblies for the exact same reason (3Brain.Common must be present
    # for 3Brain.BioCamDriver to resolve, even though only
    # System.Array/System.Byte/Marshal.Copy are used directly below).
    # Calling the real thing means a future change to load_assemblies()
    # (e.g. a fix to how the DLL directory is put on PATH) cannot silently
    # stop applying here.
    from biocam.interop.device import load_assemblies
    load_assemblies(DLL_DIR)


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

    _time_header_reads()

    budget_us = 1000.0
    print(f"\nBudget is {budget_us:.0f} us per packet at a 1 ms acquisition period.")
    print("A strategy above that cannot keep up and must not be used in the callback.")
    return 0


def _time_header_reads():
    """What a DataPacketHeader property costs to read from Python.

    The callback reads four header members per packet and, since the
    payload-length check was added, a fifth. That fifth was carried on the
    argument that a single Int32 property get is negligible beside the payload
    copy - almost certainly true, and until now not measured. An unmeasured
    per-packet marshal has no business going into a lab session when the
    measurement is free here.

    Needs the DLLs, not the instrument: DataPacketHeader has a public
    (Int32 payloadLength, UInt16 packetCounter) constructor (XML:1953-1959),
    so a header can be built and read without a BioCAM.
    """
    from _3Brain.BioCamDriver import DataPacketHeader

    header = DataPacketHeader(PAYLOAD_BYTES, 1)
    reads = (
        ("header.PayloadLength", lambda h: h.PayloadLength),
        ("header.Timestamp", lambda h: h.Timestamp),
        ("header.PacketCounter", lambda h: h.PacketCounter),
    )
    print("")
    print("Header property reads (the callback does four; the payload-length")
    print("check added a fifth):")
    for label, read in reads:
        for _ in range(WARMUP):
            read(header)
        start = time.perf_counter()
        for _ in range(REPEATS):
            read(header)
        per_call_us = (time.perf_counter() - start) / REPEATS * 1e6
        print(f"  {label:<26} {per_call_us:8.4f} us per read")


if __name__ == "__main__":
    raise SystemExit(main())
