"""Converting a raw recording to HDF5.

Runs after a session, never during one. HDF5's chunking and index updates are
variable-latency work, which is exactly what must not happen on the acquisition
path - so recording writes flat bytes and conversion happens here, with the
experiment already over and the whole machine available.

No .brw compatibility is attempted: whether that schema is documented is an open
question with 3Brain (docs/vendor/3brain-correspondence.md). Producing files
that look compatible but are not would be worse than producing files that are
plainly ours.

Usage:
    python -m biocam.convert recording.raw recording_meta.json recording.h5
"""

import sys
from pathlib import Path

import h5py
import numpy as np

from biocam.data.frames import DTYPE_BY_BYTE_SIZE
from biocam.data.recording import integrity_verdict, read_sidecar

GAP_COLUMNS = ("after_frame", "missing_frames", "duration_ms")


def _load_raw(raw_path, meta):
    dtype = DTYPE_BY_BYTE_SIZE[meta["ch_sample_byte_size"]]
    n_channels = meta["total_channels"]
    flat = np.fromfile(raw_path, dtype=dtype)
    n_frames = len(flat) // n_channels
    return flat[: n_frames * n_channels].reshape(n_frames, n_channels)


def convert(raw_path, meta_path, out_path, compression: str = "gzip",
            level: int = 4) -> dict:
    """Write a raw recording and its sidecar into one HDF5 file."""
    raw_path, meta_path, out_path = Path(raw_path), Path(meta_path), Path(out_path)
    meta = read_sidecar(meta_path)
    data = _load_raw(raw_path, meta)
    verdict = integrity_verdict(meta)

    gaps = meta.get("integrity", {}).get("gaps", [])
    gap_rows = np.array(
        [[g["after_frame"], g["missing_frames"], g["duration_ms"]] for g in gaps],
        dtype=np.float64,
    ).reshape(-1, len(GAP_COLUMNS))

    with h5py.File(out_path, "w") as handle:
        handle.create_dataset(
            "data", data=data,
            chunks=(min(4096, data.shape[0]) or 1, data.shape[1]),
            compression=compression, compression_opts=level,
        )
        gap_set = handle.create_dataset("gaps", data=gap_rows)
        gap_set.attrs["columns"] = list(GAP_COLUMNS)

        for key, value in meta.items():
            if key == "integrity":
                continue
            if value is None:
                continue
            handle.attrs[key] = value
        handle.attrs["integrity_verdict"] = verdict
        for key in ("n_frames_missing", "gaps_truncated", "driver_loss_events",
                    "queue_overflows", "callback_errors", "discarded_at_stop"):
            handle.attrs[key] = meta.get("integrity", {}).get(key, -1)

    return {
        "n_frames": int(data.shape[0]),
        "raw_bytes": raw_path.stat().st_size,
        "output_bytes": out_path.stat().st_size,
        "verdict": verdict,
    }


def verify(out_path, raw_path, meta_path) -> bool:
    """True if the HDF5 file reproduces the raw bytes exactly.

    Comparing `_load_raw()` against `_load_raw()` only proves the two calls
    agree with each other - both floor-divide to a whole number of frames, so
    a raw file with a trailing partial frame gets truncated identically on
    both sides and the discrepancy cancels out. That is not the same claim as
    "the HDF5 file reproduces the raw data exactly": it must be checked
    against what the raw file actually contains on disk, not against a second
    reading of the same truncation.
    """
    meta = read_sidecar(meta_path)
    expected = _load_raw(raw_path, meta)
    with h5py.File(out_path, "r") as handle:
        restored = handle["data"][:]
    if restored.shape != expected.shape or not np.array_equal(restored, expected):
        return False

    bytes_per_frame = meta["total_channels"] * meta["ch_sample_byte_size"]
    raw_bytes = Path(raw_path).stat().st_size
    converted_bytes = expected.shape[0] * bytes_per_frame
    remainder = raw_bytes - converted_bytes
    if remainder:
        print(f"VERIFY: {remainder} trailing byte(s) in {raw_path} were not "
              "a whole frame and are not represented in the HDF5 output.")
        return False
    return True


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 3:
        print(__doc__)
        return 1
    raw_path, meta_path, out_path = argv
    report = convert(raw_path, meta_path, out_path)
    ratio = report["raw_bytes"] / report["output_bytes"] if report["output_bytes"] else 0
    print(f"{report['n_frames']:,} frames   "
          f"{report['raw_bytes']:,} -> {report['output_bytes']:,} bytes "
          f"({ratio:.2f}x)   integrity: {report['verdict']}")
    if not verify(out_path, raw_path, meta_path):
        print("VERIFICATION FAILED - the HDF5 file does not match the raw data")
        return 1
    print("Verified: the HDF5 file reproduces the raw data exactly.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
