"""The window itself, built and driven without ever being shown.

These need a Tk display. On the lab machine and on any Windows desktop that is
always true; on a headless CI box it is not, so they skip rather than fail.
"""

import time

import numpy as np
import pytest

from biocam.data.recording import AcquisitionParameters

tk = pytest.importorskip("tkinter")

PARAMS = AcquisitionParameters(
    frame_rate_hz=1000.0, total_channels=4, ch_sample_byte_size=2, bit_depth=12,
    adc_counts_to_value=1.0, offset=0.0, min_digital_value=0, max_digital_value=4095,
)


@pytest.fixture
def root():
    try:
        window = tk.Tk()
    except tk.TclError as exc:  # pragma: no cover - headless only
        pytest.skip(f"no Tk display: {exc}")
    window.withdraw()
    yield window
    try:
        window.destroy()
    except tk.TclError:
        pass


@pytest.fixture
def demo(tmp_path):
    n_frames = 4000
    data = np.arange(n_frames * 4, dtype=np.uint16).reshape(n_frames, 4)
    raw = tmp_path / "demo.raw"
    data.tofile(raw)
    return raw


def a_window(root, tmp_path, demo, **kwargs):
    from biocam.ui.app import BioCamWindow

    kwargs.setdefault("live", False)
    return BioCamWindow(
        root, output_dir=str(tmp_path / "out"), replay_source=str(demo),
        params=PARAMS, **kwargs,
    )


def pump(root, window, timeout=20.0):
    """Run the event loop until the WINDOW has caught up, not the controller.

    Waiting on the controller alone returns before the window's own poll has
    run - a replayed second finishes in well under one poll interval - so
    every assertion about what is on screen would race it.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        root.update()
        if (window.controller.snapshot().finished
                and str(window.btn_start["state"]) == "normal"):
            return window.controller.snapshot()
        time.sleep(0.02)
    raise AssertionError("the window did not report a finished recording")


# --------------------------------------------------------------------------
# it says what it is
# --------------------------------------------------------------------------

def test_simulation_mode_says_so_in_the_title(root, tmp_path, demo):
    window = a_window(root, tmp_path, demo)
    assert "SIMULATION" in root.title()
    assert "no instrument" in root.title()


def test_live_mode_says_so_in_the_title(root, tmp_path, demo):
    # Constructing the window does not touch the instrument; only Start does.
    a_window(root, tmp_path, demo, live=True)
    assert "LIVE" in root.title()


def test_simulation_mode_offers_the_clock_resolution_field(root, tmp_path, demo):
    # On the instrument it is read from the device, so the field would be a
    # way to get it wrong.
    window = a_window(root, tmp_path, demo)
    assert hasattr(window, "var_resolution")


# --------------------------------------------------------------------------
# the guardrail on stimulation
# --------------------------------------------------------------------------

def test_stimulation_is_disabled_until_something_is_recording(root, tmp_path, demo):
    # A stimulus with nothing recording it leaves no evidence of what it did.
    window = a_window(root, tmp_path, demo)
    assert str(window.btn_stim["state"]) == "disabled"
    assert "Start a recording" in window.lbl_stim["text"]


def test_an_invalid_pulse_disables_the_button_and_says_why(root, tmp_path, demo):
    window = a_window(root, tmp_path, demo)
    window.var_amplitude.set("5000")          # outside the stimulator's range
    root.update()
    assert str(window.btn_stim["state"]) == "disabled"
    assert "outside the stimulator's range" in window.lbl_stim["text"]


def test_an_unbalanced_request_cannot_be_expressed(root, tmp_path, demo):
    # The window builds the second phase as the mirror of the first, so a
    # charge-unbalanced pulse is not reachable from these controls at all.
    window = a_window(root, tmp_path, demo)
    plan, _ = window._build_stimulus()
    assert plan.is_charge_balanced


def test_a_malformed_electrode_says_which_field(root, tmp_path, demo):
    window = a_window(root, tmp_path, demo)
    window.var_positive.set("10")
    root.update()
    assert str(window.btn_stim["state"]) == "disabled"
    assert "row,col pair" in window.lbl_stim["text"]


def test_an_out_of_array_electrode_is_refused(root, tmp_path, demo):
    window = a_window(root, tmp_path, demo)
    window.var_positive.set("99,99")
    root.update()
    assert "outside the 64x64 array" in window.lbl_stim["text"]


def test_a_shared_column_is_refused(root, tmp_path, demo):
    window = a_window(root, tmp_path, demo)
    window.var_negative.set("20,10")          # same column as 10,10
    root.update()
    assert "share column" in window.lbl_stim["text"]


def test_a_valid_pulse_is_described_before_it_is_sent(root, tmp_path, demo):
    window = a_window(root, tmp_path, demo)
    assert "uA" in window.lbl_stim["text"]
    assert "balanced" in window.lbl_stim["text"]


# --------------------------------------------------------------------------
# a whole session through the window
# --------------------------------------------------------------------------

def test_a_recording_runs_and_reports(root, tmp_path, demo):
    window = a_window(root, tmp_path, demo)
    window.var_duration.set("1")
    window._on_start()
    state = pump(root, window)

    assert state.finished
    assert state.frames == 1000
    assert state.verdict == "clean"
    assert not state.error
    assert state.acquisition_sec > 0        # not the 0.000 s a finished run
    assert state.clock_source                # once reported


def test_the_buttons_swap_over_while_recording(root, tmp_path, demo):
    window = a_window(root, tmp_path, demo)
    window.var_duration.set("1")
    window._on_start()
    assert str(window.btn_start["state"]) == "disabled"
    assert str(window.btn_stop["state"]) == "normal"
    pump(root, window)
    assert str(window.btn_start["state"]) == "normal"
    assert str(window.btn_stop["state"]) == "disabled"


def test_stimulation_becomes_available_once_recording(root, tmp_path, demo):
    window = a_window(root, tmp_path, demo)
    window.var_duration.set("2")
    window._on_start()
    root.update()
    window._refresh_stim_validity()
    assert str(window.btn_stim["state"]) == "normal"
    window._on_stimulate()
    state = pump(root, window)
    assert state.stimuli_delivered == 1


def test_the_log_records_what_happened(root, tmp_path, demo):
    window = a_window(root, tmp_path, demo)
    window.var_duration.set("1")
    window._on_start()
    pump(root, window)
    text = window.text.get("1.0", "end")
    assert "Recording to" in text
    assert "Finished" in text


def test_the_log_is_bounded(root, tmp_path, demo):
    from biocam.ui.app import MAX_LOG_LINES

    window = a_window(root, tmp_path, demo)
    for i in range(MAX_LOG_LINES + 50):
        window._log(f"line {i}")
    lines = window.text.get("1.0", "end").strip().splitlines()
    assert len(lines) <= MAX_LOG_LINES + 1


def test_starting_without_a_replay_source_reports_rather_than_crashes(
    root, tmp_path
):
    from biocam.ui.app import BioCamWindow

    window = BioCamWindow(root, live=False, output_dir=str(tmp_path),
                          replay_source=None, params=None)
    window._on_start()
    assert "Cannot start" in window.text.get("1.0", "end")
    assert not window.controller.running


def test_closing_stops_a_running_recording(root, tmp_path, demo):
    window = a_window(root, tmp_path, demo)
    window.var_until_stopped.set(True)
    window._on_duration_mode()
    window._on_start()
    root.update()
    assert window.controller.running
    window._on_close()
    assert not window.controller.running


def test_releasing_the_instrument_is_safe_when_nothing_is_held(
    root, tmp_path, demo
):
    window = a_window(root, tmp_path, demo)
    window._release_live()
    window._release_live()
