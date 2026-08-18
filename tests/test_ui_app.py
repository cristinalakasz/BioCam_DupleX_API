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


def a_window(root, tmp_path, demo, params=None, **kwargs):
    from biocam.ui.app import BioCamWindow

    kwargs.setdefault("live", False)
    return BioCamWindow(
        root, output_dir=str(tmp_path / "out"), replay_source=str(demo),
        params=params or PARAMS, **kwargs,
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


# --------------------------------------------------------------------------
# what the review found
# --------------------------------------------------------------------------

def test_live_mode_will_not_validate_against_invented_limits(root, tmp_path, demo):
    # Before the instrument is claimed its limits are unknown, and a LIVE
    # window used to fall through to the simulation defaults - showing
    # "Valid: ..." for durations that were not the ones that would fire.
    window = a_window(root, tmp_path, demo, live=True)
    assert str(window.btn_stim["state"]) == "disabled"
    assert "unknown until the instrument is claimed" in window.lbl_stim["text"]


def test_simulation_mode_still_validates_without_an_instrument(root, tmp_path, demo):
    window = a_window(root, tmp_path, demo, live=False)
    assert "balanced" in window.lbl_stim["text"]


def test_warnings_from_other_threads_reach_the_log(root, tmp_path, demo):
    # Tkinter is not thread-safe, so the `warn` handed to the factory and the
    # stimulator must not touch a widget. It queues; the poll renders.
    import threading

    window = a_window(root, tmp_path, demo)
    done = threading.Event()

    def other_thread():
        window._warn_from_any_thread("something happened elsewhere")
        done.set()

    threading.Thread(target=other_thread).start()
    assert done.wait(5.0)
    for _ in range(20):
        root.update()
        if "something happened elsewhere" in window.text.get("1.0", "end"):
            return
        time.sleep(0.05)
    raise AssertionError("the message never reached the log")


def test_the_stimulus_log_is_written_beside_the_recording(root, tmp_path, demo):
    # Without it the latencies and refusals die with the process - the one
    # correspondence a later analysis cannot reconstruct.
    import json

    window = a_window(root, tmp_path, demo)
    window.var_duration.set("2")
    window._on_start()
    root.update()
    window._refresh_stim_validity()
    window._on_stimulate()
    pump(root, window)

    logs = list((tmp_path / "out").glob("*_stimuli.json"))
    assert len(logs) == 1
    payload = json.loads(logs[0].read_text(encoding="utf-8"))
    assert payload["n_delivered"] == 1
    # And it is unmistakably a rehearsal.
    assert payload["simulated"] is True
    assert payload["stimuli"][0]["simulated"] is True


def test_no_stimulus_log_is_written_when_nothing_was_stimulated(
    root, tmp_path, demo
):
    window = a_window(root, tmp_path, demo)
    window.var_duration.set("1")
    window._on_start()
    pump(root, window)
    assert list((tmp_path / "out").glob("*_stimuli.json")) == []


def test_releasing_is_refused_while_a_recording_runs(root, tmp_path, demo):
    # Releasing mid-recording would deactivate the pool underneath the worker,
    # after which source.stop() never calls StopDataStreaming.
    window = a_window(root, tmp_path, demo)
    window._live_stack = object()          # pretend an instrument is held
    window.var_until_stopped.set(True)
    window._on_duration_mode()
    window._on_start()
    root.update()
    window._release_live()
    assert "still running" in window.text.get("1.0", "end")
    assert window._live_stack is not None   # not dropped, so it can be retried
    window.controller.stop()
    window.controller.join(10.0)


# --------------------------------------------------------------------------
# the array: clicking it, and watching it
# --------------------------------------------------------------------------

def test_the_grid_matches_the_recording_channel_count(root, tmp_path, demo):
    # The demo fixture is 4 channels, so a 64x64 grid would render nothing.
    window = a_window(root, tmp_path, demo)
    assert window.n_rows * window.n_cols == PARAMS.total_channels


def test_clicking_the_array_fills_the_text_fields(root, tmp_path, demo):
    # The picture is the source of truth; the fields follow it. Typing
    # "10,10" to address one of 4096 electrodes is not an interface.
    from biocam.ui.arrayview import cell_bounds

    window = a_window(root, tmp_path, demo)
    window.array.clear()
    x0, y0, _, _ = cell_bounds(2, 1, window.array.cell)
    window.array._click(type("E", (), {"x": x0 + 2, "y": y0 + 2})(), "positive")
    root.update()
    assert window.var_positive.get() == "2,1"


def test_editing_the_text_fields_moves_the_array(root, tmp_path, demo):
    # Both directions, so the two views cannot drift apart.
    window = a_window(root, tmp_path, demo)
    window.var_positive.set("1,2")
    window.var_negative.set("2,1")
    root.update()
    assert window.array.positive == [(1, 2)]
    assert window.array.negative == [(2, 1)]


def test_a_half_typed_field_does_not_wipe_the_selection(root, tmp_path, demo):
    window = a_window(root, tmp_path, demo)
    window.var_positive.set("1,2")
    root.update()
    window.var_positive.set("1,")          # mid-keystroke
    root.update()
    assert window.array.positive == [(1, 2)]


def test_the_array_selection_is_what_gets_validated(root, tmp_path, demo):
    from biocam.ui.arrayview import cell_bounds

    window = a_window(root, tmp_path, demo)
    window.array.clear()
    for row, col, which in ((1, 1, "positive"), (2, 2, "negative")):
        x0, y0, _, _ = cell_bounds(row, col, window.array.cell)
        window.array._click(
            type("E", (), {"x": x0 + 2, "y": y0 + 2})(), which)
    root.update()
    plan, pattern = window._build_stimulus()
    assert [(e.row, e.col) for e in pattern.positive] == [(1, 1)]
    assert [(e.row, e.col) for e in pattern.negative] == [(2, 2)]


def test_an_empty_selection_disables_stimulation_with_a_reason(
    root, tmp_path, demo
):
    window = a_window(root, tmp_path, demo)
    window.array.clear()
    root.update()
    assert str(window.btn_stim["state"]) == "disabled"
    assert "electrode" in window.lbl_stim["text"].lower()


def test_the_array_lights_up_during_a_recording(root, tmp_path, demo):
    window = a_window(root, tmp_path, demo)
    window.var_duration.set("2")
    window._on_start()
    pump(root, window)
    activity = window.controller.activity()
    assert activity is not None and activity.has_data
    low, high = activity.range()
    assert high > low
    assert "peak-to-peak" in window.lbl_scale["text"]


def test_hovering_reports_the_electrode_and_its_reading(root, tmp_path, demo):
    window = a_window(root, tmp_path, demo)
    window.var_duration.set("2")
    window._on_start()
    pump(root, window)
    window._on_array_hover((1, 1))
    text = window.lbl_hover["text"]
    assert "(1,1)" in text
    assert "uV" in text


def test_hovering_off_the_array_says_nothing_specific(root, tmp_path, demo):
    window = a_window(root, tmp_path, demo)
    window._on_array_hover(None)
    assert "Hover" in window.lbl_hover["text"]


def test_the_activity_display_never_costs_the_recording(root, tmp_path, demo):
    # The monitor runs on the drain's thread. If it fails, the recording
    # continues and the sidecar is still complete.
    from biocam.data.recording import read_sidecar

    class BrokenMonitor:
        def observe(self, packet):
            raise RuntimeError("the display fell over")

        def snapshot(self):
            return None

        def warnings(self):
            return []

    window = a_window(root, tmp_path, demo)
    window.var_duration.set("1")
    window._on_start()
    # Swap the monitor in mid-flight; the guard is in record_session.
    window.controller._monitor = BrokenMonitor()
    state = pump(root, window)
    assert state.verdict in ("clean", "gaps_detected")
    assert read_sidecar(
        list((tmp_path / "out").glob("*_meta.json"))[0])["status"] == "complete"


# --------------------------------------------------------------------------
# detection, sorting and the closed loop, reachable at last
# --------------------------------------------------------------------------

# A realistic sample rate, because a spike cannot be represented at 1 kHz:
# a 0.8 ms waveform is 0.8 samples and np.hanning(0) is empty. The module's
# PARAMS stays at 1 kHz for the recording tests, where the rate only sets how
# long a "second" is.
SPIKY_PARAMS = AcquisitionParameters(
    frame_rate_hz=18557.720703125, total_channels=4, ch_sample_byte_size=2,
    bit_depth=12, adc_counts_to_value=1.0, offset=0.0,
    min_digital_value=0, max_digital_value=4095,
)


def spiky_demo(tmp_path, n_frames=60000, channels=(1, 2)):
    """A recording with real spikes on known channels."""
    rng = np.random.default_rng(5)
    data = 2048 + rng.normal(0, 6, (n_frames, SPIKY_PARAMS.total_channels))
    narrow = -260.0 * np.hanning(int(0.0008 * SPIKY_PARAMS.frame_rate_hz))
    wide = -190.0 * np.hanning(int(0.0014 * SPIKY_PARAMS.frame_rate_hz))
    for channel in channels:
        frame = 1200
        while frame < n_frames - 80:
            shape = narrow if rng.random() < 0.5 else wide
            data[frame:frame + len(shape), channel] += shape
            frame += int(rng.integers(300, 900))
    raw = tmp_path / "spiky.raw"
    np.clip(data, 0, 4095).astype(np.uint16).tofile(raw)
    return raw


def select(window, *cells, which="positive"):
    from biocam.ui.arrayview import cell_bounds

    for row, col in cells:
        x0, y0, _, _ = cell_bounds(row, col, window.array.cell)
        window.array._click(
            type("E", (), {"x": x0 + 2, "y": y0 + 2})(), which)


def test_every_sorting_technique_is_offered(root, tmp_path, demo):
    from biocam.analysis.sorting import SORTER_LABELS

    window = a_window(root, tmp_path, demo)
    offered = set(window.sort_combo["values"])
    assert "(none)" in offered
    for label in SORTER_LABELS.values():
        assert label in offered


def test_choosing_a_technique_selects_it(root, tmp_path, demo):
    window = a_window(root, tmp_path, demo)
    assert window.sort_technique is None          # "(none)" by default
    window.var_sort.set("PCA + k-means")
    assert window.sort_technique == "pca"
    window.var_sort.set("Template matching")
    assert window.sort_technique == "template"
    window.var_sort.set("Amplitude (trough depth)")
    assert window.sort_technique == "amplitude"


def test_detection_watches_the_electrodes_chosen_on_the_array(root, tmp_path, demo):
    from biocam.ui.arrayview import channel_index

    window = a_window(root, tmp_path, demo)
    window.array.clear()
    select(window, (1, 2))
    select(window, (2, 1), which="negative")
    window.var_detect.set(True)
    settings = window._analysis_settings()
    assert set(settings["detect_channels"]) == {
        channel_index(1, 2, window.n_cols),
        channel_index(2, 1, window.n_cols),
    }


def test_nothing_extra_runs_when_detection_is_off(root, tmp_path, demo):
    window = a_window(root, tmp_path, demo)
    select(window, (1, 1))
    window.var_detect.set(False)
    assert window._analysis_settings() == {}


def test_detection_with_no_electrodes_selected_says_so(root, tmp_path, demo):
    window = a_window(root, tmp_path, demo)
    window.array.clear()
    window.var_detect.set(True)
    window._refresh_analysis()
    assert "No electrodes selected" in window.lbl_analysis["text"]
    assert window._analysis_settings() == {}


def test_the_loop_needs_detection_and_says_so(root, tmp_path, demo):
    window = a_window(root, tmp_path, demo)
    select(window, (1, 1))
    window.var_detect.set(False)
    window.var_loop.set(True)
    window._refresh_analysis()
    assert "needs detection on" in window.lbl_analysis["text"]


def test_simulation_says_no_stimulus_leaves_the_machine(root, tmp_path, demo):
    window = a_window(root, tmp_path, demo)
    select(window, (1, 1))
    window.var_detect.set(True)
    window.var_loop.set(True)
    window._refresh_analysis()
    assert "nothing is delivered anywhere" in window.lbl_analysis["text"]


def test_spikes_are_detected_through_the_window(root, tmp_path):
    raw = spiky_demo(tmp_path, channels=(1, 2))
    window = a_window(root, tmp_path, raw, params=SPIKY_PARAMS)
    window.array.clear()
    # channel 1 is (1,2) and channel 2 is (1,3) in a 1-based 2x2 grid.
    select(window, (1, 2))                      # channel 1
    select(window, (2, 1), which="negative")    # channel 2
    window.var_detect.set(True)
    window.var_duration.set("2")
    window._on_start()
    state = pump(root, window, timeout=60.0)

    assert state.spikes_detected > 10
    assert state.spike_rate_hz > 0
    assert window.controller.watched_channels() == [1, 2]


def test_sorting_runs_through_the_window(root, tmp_path):
    raw = spiky_demo(tmp_path, channels=(1, 2))
    window = a_window(root, tmp_path, raw, params=SPIKY_PARAMS)
    window.array.clear()
    select(window, (1, 2))                      # channel 1
    select(window, (2, 1), which="negative")    # channel 2
    window.var_detect.set(True)
    window.var_sort.set("PCA + k-means")
    window.var_duration.set("2")
    window._on_start()
    pump(root, window, timeout=60.0)

    assert len(window.controller.spikes_with_waveforms()) > 20
    window._on_sort()
    text = window.text.get("1.0", "end")
    assert "Sorted" in text
    assert "separation" in text


def test_sorting_refuses_rather_than_fitting_badly(root, tmp_path, demo):
    # Nine waveforms is not a unit. An under-fitted sorter is worse than
    # none, because it still returns labels.
    window = a_window(root, tmp_path, demo)
    window.var_sort.set("PCA + k-means")
    window._on_sort()
    assert "Sorted" not in window.text.get("1.0", "end")


def test_the_closed_loop_runs_and_its_limits_hold(root, tmp_path):
    raw = spiky_demo(tmp_path, channels=(1, 2))
    window = a_window(root, tmp_path, raw, params=SPIKY_PARAMS)
    window.array.clear()
    select(window, (1, 2))                      # channel 1
    select(window, (2, 1), which="negative")    # channel 2
    window.var_detect.set(True)
    window.var_loop.set(True)
    window.var_min_interval.set("50")
    window.var_max_rate.set("10")
    window.var_duration.set("2")
    window._on_start()
    state = pump(root, window, timeout=60.0)

    assert state.spikes_detected > 10
    assert state.loop_stimuli > 0
    # A busy culture asks far more often than the limits allow. That the
    # limits held is the thing worth asserting.
    assert state.loop_refused > 0
    assert state.loop_stimuli <= 25          # ~2 s at 10 Hz, plus slack
    assert not state.loop_suspended


def test_a_half_typed_threshold_does_not_stop_a_recording(root, tmp_path, demo):
    window = a_window(root, tmp_path, demo)
    select(window, (1, 1))
    window.var_detect.set(True)
    window.var_sigmas.set("")               # mid-keystroke
    settings = window._analysis_settings()
    assert settings["threshold_sigmas"] == 5.0
