"""The demo generator: the file the window is learned on."""

import importlib.util
import json
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parent.parent / "tools" / "make_demo_recording.py"
_spec = importlib.util.spec_from_file_location("make_demo_recording", _PATH)
demo = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(demo)


def test_the_length_can_be_chosen(tmp_path):
    # Two seconds replays in under four: over before anyone can click an
    # electrode to trace it.
    stem = tmp_path / "long"
    assert demo.main([str(stem), "--seconds", "0.25"]) == 0
    n_frames = int(demo.FRAME_RATE_HZ * 0.25)
    assert stem.with_suffix(".raw").stat().st_size == n_frames * demo.N_CHANNELS * 2
    meta = json.loads((tmp_path / "long_meta.json").read_text())
    assert meta["total_channels"] == demo.N_CHANNELS


def test_the_meta_note_states_the_real_frame_rate(tmp_path):
    stem = tmp_path / "d"
    demo.main([str(stem), "--seconds", "0.1"])
    meta = json.loads((tmp_path / "d_meta.json").read_text())
    assert "1 kHz" not in meta["note"]
    assert meta["frame_rate_hz"] == demo.FRAME_RATE_HZ


@pytest.mark.parametrize("value", ["0", "-1", "nan", "abc"])
def test_a_nonsense_length_is_refused(tmp_path, value):
    with pytest.raises(SystemExit):
        demo.main([str(tmp_path / "d"), "--seconds", value])


def test_an_unknown_flag_does_not_become_a_filename(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit):
        demo.main(["--bogus"])
    assert not list(tmp_path.iterdir())
