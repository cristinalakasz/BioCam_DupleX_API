"""Layer 2 - the electrode array, drawn and clickable.

Typing `10,10` into a text box to address one of 4096 electrodes is not an
interface for a microelectrode array. This draws the array, colours it by
what each electrode is currently picking up, and lets the electrodes be
chosen by clicking them.

Two things it has to get right, and both are about not lying:

**Coordinates are 1-based, and the picture must agree.** `ChCoord(1, 1)` is
the first electrode. An off-by-one here does not raise - it stimulates the
wrong electrode, and the operator has a picture that agrees with the mistake.
So the mapping between a pixel and an `Electrode` is a pure function, tested
on its own, and the same function is used for drawing and for clicking.

**Row-major is an assumption.** That channel *i* of a frame is
`row = i // n_cols, col = i % n_cols` is how `biocam.convert` and
`FrameDecoder` lay a frame out and matches ChCoord's reading order, but
nothing in the XML states the ordering. If the picture looks transposed on
the instrument, that is this assumption being wrong - which is worth
reporting rather than rotating the image until it looks right.

The colour mapping and the geometry are module-level functions with no Tk in
sight, so they are testable on a machine with no display.
"""

# Low to high. Deliberately not a rainbow: this is read at a glance to answer
# "which electrodes are carrying signal", and a monotonic dark-to-bright ramp
# answers that without the false boundaries a rainbow invents.
_RAMP = (
    (0x10, 0x14, 0x28),   # near-silent
    (0x1f, 0x4e, 0x79),
    (0x2e, 0x8b, 0x8b),
    (0x86, 0xc2, 0x32),
    (0xf2, 0xd0, 0x2f),
    (0xf2, 0x6b, 0x1f),   # loudest
)

POSITIVE_COLOUR = "#ff3b30"
NEGATIVE_COLOUR = "#0a84ff"
GRID_COLOUR = "#000000"


def colour_for(value, low, high):
    """Map a value in [low, high] onto the ramp. Returns (r, g, b)."""
    if high <= low:
        fraction = 0.0
    else:
        fraction = (value - low) / (high - low)
    fraction = 0.0 if fraction < 0.0 else 1.0 if fraction > 1.0 else fraction
    span = len(_RAMP) - 1
    position = fraction * span
    index = int(position)
    if index >= span:
        return _RAMP[span]
    blend = position - index
    a, b = _RAMP[index], _RAMP[index + 1]
    return tuple(int(round(a[i] + (b[i] - a[i]) * blend)) for i in range(3))


def electrode_at(x, y, cell, n_rows, n_cols):
    """The 1-based (row, col) under a pixel, or None if outside the array.

    The inverse of `cell_bounds`, and the reason both exist as functions: the
    picture and the click must not be able to disagree about which electrode
    is which.
    """
    if x < 0 or y < 0:
        return None
    col = int(x // cell) + 1
    row = int(y // cell) + 1
    if not (1 <= row <= n_rows and 1 <= col <= n_cols):
        return None
    return row, col


def cell_bounds(row, col, cell):
    """Pixel rectangle (x0, y0, x1, y1) of a 1-based (row, col)."""
    x0 = (col - 1) * cell
    y0 = (row - 1) * cell
    return x0, y0, x0 + cell, y0 + cell


def channel_index(row, col, n_cols):
    """Row-major channel index for a 1-based (row, col). See the module note."""
    return (row - 1) * n_cols + (col - 1)


def ppm_bytes(grid, low, high, n_rows, n_cols):
    """Render an activity grid as a binary PPM.

    A PPM handed to `tk.PhotoImage(data=...)` is the fastest way to repaint
    4096 cells in plain Tk. Recolouring 4096 individual canvas rectangles ten
    times a second is not - and this runs on the UI thread, where a stall
    freezes the window an operator is using to watch a recording.
    """
    header = f"P6\n{n_cols} {n_rows}\n255\n".encode("ascii")
    if grid is None:
        flat = bytes(_RAMP[0]) * (n_rows * n_cols)
        return header + flat
    out = bytearray(n_rows * n_cols * 3)
    at = 0
    for row in range(n_rows):
        source = grid[row]
        for col in range(n_cols):
            r, g, b = colour_for(source[col], low, high)
            out[at] = r
            out[at + 1] = g
            out[at + 2] = b
            at += 3
    return header + bytes(out)


class ElectrodeArrayView:
    """A clickable, activity-coloured picture of the array.

    Left click chooses a positive endpoint, right click a negative one;
    clicking a chosen electrode again clears it. Dragging paints.
    """

    def __init__(self, parent, *, n_rows: int = 64, n_cols: int = 64,
                 cell: int = 9, on_change=None, on_hover=None):
        import tkinter as tk

        self._tk = tk
        self.n_rows = n_rows
        self.n_cols = n_cols
        self.cell = cell
        self._on_change = on_change
        self._on_hover = on_hover

        self.positive = []
        self.negative = []
        self._image = None
        self._overlay = []
        self._low, self._high = 0.0, 1.0

        self.canvas = tk.Canvas(
            parent, width=n_cols * cell, height=n_rows * cell,
            highlightthickness=1, highlightbackground="#888888",
            background="#101428", cursor="crosshair",
        )
        self.canvas.bind("<Button-1>", lambda e: self._click(e, "positive"))
        self.canvas.bind("<B1-Motion>", lambda e: self._paint(e, "positive"))
        self.canvas.bind("<Button-3>", lambda e: self._click(e, "negative"))
        self.canvas.bind("<B3-Motion>", lambda e: self._paint(e, "negative"))
        self.canvas.bind("<Motion>", self._hover)
        self.canvas.bind("<Leave>", lambda e: self._on_hover and self._on_hover(None))
        self._redraw_image(None)

    # -- activity ---------------------------------------------------------

    def set_activity(self, snapshot):
        """Repaint from a `MonitorSnapshot`. UI thread only."""
        grid = snapshot.as_grid() if snapshot is not None else None
        if grid is None:
            return
        self._low, self._high = snapshot.range()
        self._redraw_image(grid)

    def _redraw_image(self, grid):
        data = ppm_bytes(grid, self._low, self._high, self.n_rows, self.n_cols)
        # Held on the instance: Tk keeps only a weak reference to a
        # PhotoImage, so a local would be collected and the canvas would go
        # blank at an unpredictable moment.
        self._photo = self._tk.PhotoImage(data=data).zoom(self.cell, self.cell)
        if self._image is None:
            self._image = self.canvas.create_image(
                0, 0, anchor="nw", image=self._photo)
            self.canvas.tag_lower(self._image)
        else:
            self.canvas.itemconfigure(self._image, image=self._photo)

    # -- selection --------------------------------------------------------

    def set_selection(self, positive, negative):
        """Replace the selection, e.g. from the text fields."""
        self.positive = [tuple(e) for e in positive]
        self.negative = [tuple(e) for e in negative]
        self._redraw_overlay()

    def clear(self):
        self.positive.clear()
        self.negative.clear()
        self._redraw_overlay()
        self._changed()

    def _click(self, event, which):
        found = electrode_at(
            self.canvas.canvasx(event.x), self.canvas.canvasy(event.y),
            self.cell, self.n_rows, self.n_cols)
        if found is None:
            return
        target = self.positive if which == "positive" else self.negative
        other = self.negative if which == "positive" else self.positive
        if found in target:
            target.remove(found)
        else:
            # An electrode cannot be both polarities, so choosing it as one
            # takes it out of the other rather than producing a pattern that
            # validate_pattern would refuse.
            if found in other:
                other.remove(found)
            target.append(found)
        self._redraw_overlay()
        self._changed()

    def _paint(self, event, which):
        found = electrode_at(
            self.canvas.canvasx(event.x), self.canvas.canvasy(event.y),
            self.cell, self.n_rows, self.n_cols)
        if found is None:
            return
        target = self.positive if which == "positive" else self.negative
        other = self.negative if which == "positive" else self.positive
        if found in target:
            return          # dragging adds; it does not toggle back off
        if found in other:
            other.remove(found)
        target.append(found)
        self._redraw_overlay()
        self._changed()

    def _redraw_overlay(self):
        for item in self._overlay:
            self.canvas.delete(item)
        self._overlay.clear()
        for electrodes, colour in ((self.positive, POSITIVE_COLOUR),
                                   (self.negative, NEGATIVE_COLOUR)):
            for row, col in electrodes:
                x0, y0, x1, y1 = cell_bounds(row, col, self.cell)
                self._overlay.append(self.canvas.create_rectangle(
                    x0, y0, x1 - 1, y1 - 1,
                    outline="#ffffff", width=1, fill=colour))

    def _hover(self, event):
        if self._on_hover is None:
            return
        self._on_hover(electrode_at(
            self.canvas.canvasx(event.x), self.canvas.canvasy(event.y),
            self.cell, self.n_rows, self.n_cols))

    def _changed(self):
        if self._on_change is not None:
            self._on_change()

    # -- for the rest of the window ---------------------------------------

    def as_text(self, which) -> str:
        electrodes = self.positive if which == "positive" else self.negative
        return ";".join(f"{r},{c}" for r, c in electrodes)

    def activity_at(self, row, col, snapshot):
        """The activity reading under an electrode, or None."""
        if snapshot is None or snapshot.activity is None:
            return None
        index = channel_index(row, col, self.n_cols)
        if index >= snapshot.activity.size:
            return None
        return float(snapshot.activity[index])
