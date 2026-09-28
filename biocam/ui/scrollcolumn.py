"""A column that scrolls vertically when its content is taller than its pane.

The operator window promises that the reason a button is greyed out is always
written beneath it. On a short screen, or a Windows PC at 125-150% display
scaling, a column's content can be taller than the space it gets - and then
that text is simply below the bottom edge, with nothing to say it exists. A
scrollbar that appears only when needed keeps it reachable without costing
anything on a screen where everything fits.

UI thread only, like everything else in biocam.ui.
"""


class ScrollableColumn:
    """Canvas + inner frame + a scrollbar that shows only when needed.

    Build the column's widgets into `inner`, exactly as into a plain frame.
    """

    def __init__(self, parent, tk, ttk):
        self.canvas = tk.Canvas(parent, highlightthickness=0, borderwidth=0)
        self.scrollbar = ttk.Scrollbar(parent, orient="vertical",
                                       command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self._on_view_changed)
        # The frame the column's widgets go into. Same background as a ttk
        # frame, so the canvas behind it does not show as a different colour.
        self.inner = ttk.Frame(self.canvas)
        background = ttk.Style().lookup("TFrame", "background")
        if background:
            self.canvas.configure(background=background)
        self._window = self.canvas.create_window(
            0, 0, anchor="nw", window=self.inner)

        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(0, weight=1)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.scrollbar.grid(row=0, column=1, sticky="ns")
        self.scrollbar.grid_remove()

        self.inner.bind("<Configure>", self._on_content_resized)
        self.canvas.bind("<Configure>", self._on_canvas_resized)

    @property
    def scrollable(self) -> bool:
        """Whether the content is taller than what is shown."""
        first, last = self.canvas.yview()
        return first > 0.0 or last < 1.0

    def scroll(self, units: int) -> None:
        if self.scrollable:
            self.canvas.yview_scroll(units, "units")

    # -- geometry ----------------------------------------------------------

    def _on_content_resized(self, _event):
        # Ask for the content's natural size, so the pane starts wide and
        # tall enough where the screen allows; scroll only what does not fit.
        self.canvas.configure(
            scrollregion=(0, 0, self.inner.winfo_reqwidth(),
                          self.inner.winfo_reqheight()),
            width=self.inner.winfo_reqwidth(),
            height=self.inner.winfo_reqheight())

    def _on_canvas_resized(self, event):
        # The content is as wide as the column, so labels wrap to the column
        # rather than running under the scrollbar.
        self.canvas.itemconfigure(self._window, width=event.width)

    def _on_view_changed(self, first, last):
        self.scrollbar.set(first, last)
        if float(first) <= 0.0 and float(last) >= 1.0:
            self.scrollbar.grid_remove()
        else:
            self.scrollbar.grid()


def wheel_units(delta: int) -> int:
    """Mouse-wheel delta to scroll units, on Windows and on macOS.

    Windows reports multiples of 120 per notch; macOS reports small integers.
    Positive delta is scrolling up, which is a negative scroll.
    """
    if delta == 0:
        return 0
    if abs(delta) >= 120:
        return -int(delta / 120)
    return -1 if delta > 0 else 1


def column_under(widget, columns):
    """The ScrollableColumn containing `widget`, or None."""
    canvases = {str(c.canvas): c for c in columns}
    while widget is not None:
        found = canvases.get(str(widget))
        if found is not None:
            return found
        widget = widget.master
    return None
