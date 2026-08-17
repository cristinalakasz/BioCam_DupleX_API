"""The electrode array widget.

The geometry and colour functions are pure and run anywhere. The widget
itself needs a Tk display, so those tests skip rather than fail on a headless
box.
"""

import numpy as np
import pytest

from biocam.ui.arrayview import (
    cell_bounds,
    channel_index,
    colour_for,
    electrode_at,
    ppm_bytes,
)

tk = pytest.importorskip("tkinter")


# --------------------------------------------------------------------------
# geometry: the picture and the click must never disagree
# --------------------------------------------------------------------------

def test_the_first_electrode_is_one_one_not_zero_zero():
    # ChCoord is 1-based. An off-by-one here does not raise - it stimulates
    # the wrong electrode, with a picture that agrees with the mistake.
    assert electrode_at(0, 0, cell=10, n_rows=64, n_cols=64) == (1, 1)


def test_the_last_electrode_is_at_the_far_corner():
    assert electrode_at(639, 639, cell=10, n_rows=64, n_cols=64) == (64, 64)


def test_x_is_the_column_and_y_is_the_row():
    # Getting these the wrong way round is a transposed array, which looks
    # plausible and is wrong everywhere.
    assert electrode_at(x=25, y=5, cell=10, n_rows=64, n_cols=64) == (1, 3)
    assert electrode_at(x=5, y=25, cell=10, n_rows=64, n_cols=64) == (3, 1)


def test_a_click_outside_the_array_selects_nothing():
    assert electrode_at(640, 10, cell=10, n_rows=64, n_cols=64) is None
    assert electrode_at(10, 640, cell=10, n_rows=64, n_cols=64) is None
    assert electrode_at(-1, 10, cell=10, n_rows=64, n_cols=64) is None
    assert electrode_at(10, -1, cell=10, n_rows=64, n_cols=64) is None


def test_cell_bounds_is_the_inverse_of_electrode_at():
    # The property that matters: whatever is drawn at a place is what a click
    # there selects.
    for row in (1, 7, 32, 64):
        for col in (1, 2, 33, 64):
            x0, y0, x1, y1 = cell_bounds(row, col, cell=9)
            assert electrode_at(x0, y0, 9, 64, 64) == (row, col)
            assert electrode_at(x1 - 1, y1 - 1, 9, 64, 64) == (row, col)


def test_channel_index_is_row_major():
    assert channel_index(1, 1, n_cols=64) == 0
    assert channel_index(1, 64, n_cols=64) == 63
    assert channel_index(2, 1, n_cols=64) == 64
    assert channel_index(64, 64, n_cols=64) == 4095


# --------------------------------------------------------------------------
# colour
# --------------------------------------------------------------------------

def test_the_ramp_runs_dark_to_bright():
    low = colour_for(0.0, 0.0, 100.0)
    high = colour_for(100.0, 0.0, 100.0)
    assert sum(high) > sum(low)


def test_values_outside_the_range_are_clamped_not_wrapped():
    assert colour_for(-50.0, 0.0, 100.0) == colour_for(0.0, 0.0, 100.0)
    assert colour_for(500.0, 0.0, 100.0) == colour_for(100.0, 0.0, 100.0)


def test_a_zero_width_range_does_not_divide_by_zero():
    assert colour_for(5.0, 5.0, 5.0) == colour_for(0.0, 5.0, 5.0)


def test_the_ramp_is_monotonic_in_brightness():
    previous = -1
    for i in range(0, 101):
        brightness = sum(colour_for(i, 0.0, 100.0))
        assert brightness >= previous - 30   # allowed a little hue-driven dip
        previous = brightness


def test_every_colour_component_is_a_byte():
    for i in range(0, 101):
        assert all(0 <= c <= 255 for c in colour_for(i, 0.0, 100.0))


# --------------------------------------------------------------------------
# the image handed to Tk
# --------------------------------------------------------------------------

def test_the_ppm_header_matches_the_grid():
    data = ppm_bytes(None, 0.0, 1.0, n_rows=8, n_cols=16)
    assert data.startswith(b"P6\n16 8\n255\n")


def test_the_ppm_body_is_three_bytes_per_electrode():
    grid = np.zeros((8, 16))
    data = ppm_bytes(grid, 0.0, 1.0, n_rows=8, n_cols=16)
    body = data.split(b"255\n", 1)[1]
    assert len(body) == 8 * 16 * 3


def test_a_missing_grid_renders_the_quiet_colour_rather_than_failing():
    data = ppm_bytes(None, 0.0, 1.0, n_rows=4, n_cols=4)
    body = data.split(b"255\n", 1)[1]
    assert len(body) == 4 * 4 * 3
    assert len(set(body[i:i + 3] for i in range(0, len(body), 3))) == 1


def test_a_lively_electrode_is_brighter_in_the_image():
    grid = np.zeros((4, 4))
    grid[2][3] = 100.0
    body = ppm_bytes(grid, 0.0, 100.0, 4, 4).split(b"255\n", 1)[1]
    def pixel(row, col):
        at = (row * 4 + col) * 3
        return sum(body[at:at + 3])
    assert pixel(2, 3) > pixel(0, 0)


# --------------------------------------------------------------------------
# the widget
# --------------------------------------------------------------------------

@pytest.fixture(scope="module")
def _tk_root():
    """One Tk root for the whole module.

    Creating and destroying a root per test exhausts something in Tcl on
    Windows - a later test fails to find auto.tcl and skips, intermittently
    and for reasons that have nothing to do with the code under test. One
    root, a fresh frame per test.
    """
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
def root(_tk_root):
    frame = tk.Frame(_tk_root)
    yield frame
    frame.destroy()


def view(root, **kwargs):
    from biocam.ui.arrayview import ElectrodeArrayView

    kwargs.setdefault("n_rows", 8)
    kwargs.setdefault("n_cols", 8)
    kwargs.setdefault("cell", 10)
    return ElectrodeArrayView(root, **kwargs)


def click(v, row, col, which="positive"):
    x0, y0, _, _ = cell_bounds(row, col, v.cell)
    event = type("Event", (), {"x": x0 + 2, "y": y0 + 2})()
    v._click(event, which)


def test_a_left_click_chooses_a_positive_endpoint(root):
    v = view(root)
    click(v, 3, 4)
    assert v.positive == [(3, 4)]
    assert v.as_text("positive") == "3,4"


def test_a_right_click_chooses_a_negative_endpoint(root):
    v = view(root)
    click(v, 5, 6, "negative")
    assert v.negative == [(5, 6)]


def test_clicking_a_chosen_electrode_again_clears_it(root):
    v = view(root)
    click(v, 3, 4)
    click(v, 3, 4)
    assert v.positive == []


def test_an_electrode_cannot_be_both_polarities(root):
    # validate_pattern would refuse such a pattern, so the picture must not
    # be able to express it.
    v = view(root)
    click(v, 3, 4, "positive")
    click(v, 3, 4, "negative")
    assert v.positive == []
    assert v.negative == [(3, 4)]


def test_several_electrodes_can_be_chosen(root):
    v = view(root)
    for col in (1, 2, 3):
        click(v, 2, col)
    assert v.as_text("positive") == "2,1;2,2;2,3"


def test_dragging_adds_without_toggling_back_off(root):
    v = view(root)
    x0, y0, _, _ = cell_bounds(4, 4, v.cell)
    event = type("Event", (), {"x": x0 + 2, "y": y0 + 2})()
    v._paint(event, "positive")
    v._paint(event, "positive")      # the drag re-enters the same cell
    assert v.positive == [(4, 4)]


def test_a_click_outside_the_array_changes_nothing(root):
    v = view(root)
    event = type("Event", (), {"x": 999, "y": 999})()
    v._click(event, "positive")
    assert v.positive == []


def test_clear_removes_everything(root):
    v = view(root)
    click(v, 1, 1)
    click(v, 2, 2, "negative")
    v.clear()
    assert v.positive == [] and v.negative == []


def test_selection_can_be_set_from_outside(root):
    v = view(root)
    v.set_selection([(1, 2)], [(3, 4)])
    assert v.as_text("positive") == "1,2"
    assert v.as_text("negative") == "3,4"


def test_a_change_notifies_the_window(root):
    changes = []
    v = view(root, on_change=lambda: changes.append(1))
    click(v, 1, 1)
    assert changes == [1]


def test_activity_can_be_read_under_an_electrode(root):
    from biocam.data.monitor import MonitorSnapshot

    v = view(root)
    activity = np.arange(64, dtype=np.float32)
    snapshot = MonitorSnapshot(activity, 8, 8, samples=1, skipped=0,
                               max_observe_us=0.0, unit="uV")
    # Row 2, col 3 is index 1*8 + 2 = 10.
    assert v.activity_at(2, 3, snapshot) == 10.0


def test_activity_is_none_when_there_is_no_snapshot(root):
    assert view(root).activity_at(1, 1, None) is None


def test_setting_activity_repaints_without_raising(root):
    from biocam.data.monitor import MonitorSnapshot

    v = view(root)
    snapshot = MonitorSnapshot(np.arange(64, dtype=np.float32), 8, 8,
                               samples=1, skipped=0, max_observe_us=0.0,
                               unit="uV")
    v.set_activity(snapshot)
    root.update()


def test_an_activity_snapshot_with_no_data_is_ignored(root):
    from biocam.data.monitor import MonitorSnapshot

    v = view(root)
    v.set_activity(MonitorSnapshot(None, 8, 8, 0, 0, 0.0, "uV"))
    root.update()
