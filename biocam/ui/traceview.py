"""The rolling trace strip: what a few chosen electrodes actually look like.

The array view answers "which electrodes are alive". This answers "and what is
this one doing" - is it spiking, is it saturated, is it hum, did the stimulus
artefact swamp it. Those are different questions and they need different
pictures.

Each channel gets its own lane with its own vertical scale, because one shared
scale means a single saturated electrode flattens every other trace to a line.
The scale is printed on the lane, since a trace without one is decoration.

Drawn from a `TraceSnapshot`, which is a copy - the consumer thread keeps
writing while this draws.
"""

LANE_COLOURS = ["#1a4d8b", "#8b1a1a", "#1a6b2a", "#8a5a00",
                "#5a1a8b", "#1a7a7a", "#8b4a1a", "#4a4a4a"]

BACKGROUND = "#fbfbfb"
GRID = "#e4e4e4"
LABEL = "#333333"
QUIET = "#999999"

# A lane thinner than this cannot be read, so the strip scrolls rather than
# squeezing more in.
MIN_LANE_PX = 44


class TraceStripView:
    """A stack of trace lanes on a Tk canvas."""

    def __init__(self, parent, tk, *, width: int = 620, height: int = 260):
        self.tk = tk
        self.width = width
        self.height = height
        self.canvas = tk.Canvas(parent, width=width, height=height,
                                background=BACKGROUND, highlightthickness=1,
                                highlightbackground="#cccccc")
        self._snapshot = None
        self._message = "No electrodes selected for tracing."

    # -- drawing ----------------------------------------------------------

    def set_message(self, text: str) -> None:
        """Show text instead of traces - no selection, or nothing yet."""
        self._snapshot = None
        self._message = text
        self._redraw()

    def set_snapshot(self, snapshot) -> None:
        self._snapshot = snapshot
        self._redraw()

    def _redraw(self) -> None:
        c = self.canvas
        c.delete("all")
        snapshot = self._snapshot
        if snapshot is None or not snapshot.has_data:
            c.create_text(self.width // 2, self.height // 2,
                          text=self._message, fill=QUIET,
                          font=("Segoe UI", 9))
            return

        n = len(snapshot.channels)
        lane_h = max(MIN_LANE_PX, self.height // max(1, n))
        needed = lane_h * n
        if needed != int(c.cget("height")):
            # Grow the canvas rather than squeezing lanes below legibility.
            c.configure(height=needed, scrollregion=(0, 0, self.width, needed))

        columns = snapshot.columns
        for i, channel in enumerate(snapshot.channels):
            top = i * lane_h
            self._draw_lane(i, channel, top, lane_h, columns, snapshot)

    def _draw_lane(self, index, channel, top, lane_h, columns, snapshot):
        c = self.canvas
        colour = LANE_COLOURS[index % len(LANE_COLOURS)]
        pad = 9
        plot_top, plot_bottom = top + pad, top + lane_h - pad
        mid = (plot_top + plot_bottom) / 2.0

        c.create_line(0, top, self.width, top, fill=GRID)

        low, high = snapshot.range_of(index)
        span = high - low
        if span <= 0:
            span = 1.0
            low, high = mid - 0.5, mid + 0.5
        # A little headroom, so a spike touching the peak is not clipped
        # against the lane edge and made to look like saturation.
        span *= 1.08
        centre = (low + high) / 2.0
        scale = (plot_bottom - plot_top) / span

        def y_of(value):
            return mid - (value - centre) * scale

        c.create_line(0, mid, self.width, mid, fill=GRID, dash=(2, 4))

        # One vertical segment per column, from that bin's minimum to its
        # maximum. This is what makes a one-sample spike visible: the segment
        # is as tall as the spike, wherever in the bin it happened to fall.
        step = self.width / max(1, columns)
        minima, maxima = snapshot.minima[index], snapshot.maxima[index]
        coords = []
        for col in range(columns):
            x = col * step
            coords.append((x, y_of(minima[col]), x, y_of(maxima[col])))
        for x0, y0, x1, y1 in coords:
            c.create_line(x0, y0, x1, y1, fill=colour)

        c.create_text(4, plot_top - 2, anchor="nw", fill=LABEL,
                      font=("Segoe UI", 8, "bold"), text=f"ch {channel}")
        c.create_text(self.width - 4, plot_top - 2, anchor="ne", fill=QUIET,
                      font=("Segoe UI", 8),
                      text=f"{low:.0f} to {high:.0f} {snapshot.value_unit}")

    # -- geometry ---------------------------------------------------------

    def resize(self, width: int, height: int) -> None:
        self.width, self.height = max(120, int(width)), max(60, int(height))
        self.canvas.configure(width=self.width, height=self.height)
        self._redraw()
