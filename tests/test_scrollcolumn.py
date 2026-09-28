"""The scrolling column's pure parts: wheel arithmetic on both platforms."""

from biocam.ui.scrollcolumn import column_under, wheel_units


def test_a_windows_notch_is_one_unit_the_right_way():
    # Windows: +120 per notch away from the user, which scrolls up.
    assert wheel_units(120) == -1
    assert wheel_units(-240) == 2


def test_a_mac_delta_is_one_unit_the_right_way():
    assert wheel_units(3) == -1
    assert wheel_units(-1) == 1


def test_no_movement_scrolls_nothing():
    assert wheel_units(0) == 0


def test_the_column_is_found_from_any_widget_inside_it():
    class W:
        def __init__(self, name, master=None):
            self.name, self.master = name, master

        def __str__(self):
            return self.name

    canvas = W(".col.canvas")
    entry = W(".col.canvas.inner.entry", W(".col.canvas.inner", canvas))
    column = type("C", (), {"canvas": canvas})()
    assert column_under(entry, [column]) is column
    assert column_under(W(".log.text"), [column]) is None
