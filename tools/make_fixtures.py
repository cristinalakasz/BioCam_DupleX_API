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
