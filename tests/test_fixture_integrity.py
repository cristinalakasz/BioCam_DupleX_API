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
