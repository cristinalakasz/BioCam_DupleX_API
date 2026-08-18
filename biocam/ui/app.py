"""Layer 2 - the window.

Tkinter, because it ships with Python. The lab machine already depends on a
pythonnet/.NET/DLL arrangement that is delicate enough; adding a GUI toolkit
that needs its own wheel would be one more thing to go wrong at the worst
moment, on a machine 600 km away, for no benefit an operator would notice.

Three rules govern everything here, and they come from who uses it: the
on-site colleague first, the author remotely later. The stricter case governs.

**Nothing off the UI thread touches a widget.** Tkinter is not thread-safe.
The recording runs on a worker thread and communicates only through
`SessionController`'s queues, which this polls with `after()`.

**Simulation is never mistakable for the instrument.** The mode is stated in
the title bar and in a coloured banner, and a simulated stimulus log says so
in every record. A run that looks real and was not is worse than no run.

**Every refusal explains itself.** A greyed-out button with no reason is the
thing an operator cannot debug alone mid-experiment, so the reason a control
is unavailable is always on screen next to it.
"""

import queue
import sys
import time
from contextlib import ExitStack
from pathlib import Path

from biocam.data.events import describe
from biocam.ui.controller import SessionController

POLL_MS = 150
MAX_LOG_LINES = 400

COLOURS = {
    "live": "#8b1a1a",
    "simulation": "#1a4d8b",
    "ok": "#1a6b2a",
    "warn": "#8a5a00",
    "bad": "#8b1a1a",
    "idle": "#555555",
}


class BioCamWindow:
    """The operator's window. Build it, then call `run()`."""

    def __init__(self, root, *, live: bool = False, output_dir: str = "recordings",
                 replay_source: str = None, params=None):
        import tkinter as tk
        from tkinter import ttk

        self.tk = tk
        self.ttk = ttk
        self.root = root
        self.live = live
        self.output_dir = Path(output_dir)
        self.replay_source = replay_source
        self.replay_params = params
        self.controller = SessionController()
        self._factory = None
        self._log_lines = 0
        # Holds the device and the stimulator for a live session. They are
        # context managers, and nothing else in this window would ever exit
        # them - a leaked BioCamDevice stays claimed until the process dies,
        # so the next person to try the instrument finds it held by nothing.
        self._live_stack = None
        self._device = None
        self._stimulator = None
        # Bounds-checks electrodes. Stated rather than read from the device:
        # BioCamDataFormat exposes NChsPerWell and NWells but no row or
        # column count, and the geometry sits behind IMeaPlatePilot.
        self.grid = None
        # The array's dimensions. BioCamDataFormat exposes NChsPerWell and
        # NWells but no row or column count, and the geometry sits behind
        # IMeaPlatePilot - so this is derived from the channel count on the
        # assumption the array is square, which is true of the 4096-electrode
        # DupleX. A demo file with fewer channels gets a smaller grid rather
        # than a blank one.
        self.n_rows, self.n_cols = self._grid_shape(params)
        self._activity = None
        self._syncing = False
        # Messages from other threads. The `warn` callables handed to the
        # factory and the stimulator are invoked on the recording thread
        # and on the consumer thread, and Tkinter is not thread-safe -
        # calling _log() from either would breach this module's own first
        # rule. SimpleQueue.put is thread-safe and never blocks, which
        # also matters because one of those callers is the drain.
        # Bounded, like every other buffer here. Unbounded, a _poll that
        # stopped draining would let it grow without limit.
        self._messages = queue.Queue(maxsize=256)

        root.title(
            "BioCAM DupleX — LIVE INSTRUMENT" if live
            else "BioCAM DupleX — SIMULATION (no instrument)"
        )
        root.geometry("1320x860")
        root.protocol("WM_DELETE_WINDOW", self._on_close)

        self._build()
        self._refresh_stim_validity()
        self.root.after(POLL_MS, self._poll)

    # -- construction ----------------------------------------------------

    @staticmethod
    def _grid_shape(params):
        from biocam.stim import ElectrodeGrid

        channels = getattr(params, "total_channels", None)
        if not channels:
            return 64, 64
        try:
            grid = ElectrodeGrid.square_from_channel_count(channels)
        except ValueError:
            # Not square: show one row per channel rather than guessing a
            # shape, and let the shape be obviously odd instead of quietly
            # wrong.
            return 1, channels
        return grid.n_rows, grid.n_cols

    def _build(self):
        tk, ttk = self.tk, self.ttk

        banner = tk.Label(
            self.root,
            text=("LIVE — stimuli will be delivered to the preparation"
                  if self.live else
                  "SIMULATION — no instrument, no stimulus leaves this machine"),
            bg=COLOURS["live"] if self.live else COLOURS["simulation"],
            fg="white", font=("Segoe UI", 11, "bold"), pady=6,
        )
        banner.pack(fill="x")

        body = ttk.Frame(self.root, padding=8)
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, minsize=260)
        body.columnconfigure(1, weight=1)
        body.columnconfigure(2, minsize=240)
        body.columnconfigure(3, minsize=280)
        body.rowconfigure(1, weight=1)

        self._build_recording(ttk.LabelFrame(body, text="Recording", padding=8))
        self._build_array(
            ttk.LabelFrame(body, text="Electrode array", padding=6))
        self._build_stimulation(
            ttk.LabelFrame(body, text="Stimulus", padding=8))
        self._build_analysis(
            ttk.LabelFrame(body, text="Spikes and closed loop", padding=8))
        self._build_log(ttk.LabelFrame(body, text="Session log", padding=6))

    def _build_recording(self, frame):
        tk, ttk = self.tk, self.ttk
        frame.grid(row=0, column=0, sticky="nsew", padx=(0, 6))

        self.var_duration = tk.StringVar(value="10")
        self.var_until_stopped = tk.BooleanVar(value=False)
        self.var_name = tk.StringVar(value="")

        row = 0
        ttk.Label(frame, text="Output folder").grid(row=row, column=0, sticky="w")
        ttk.Label(frame, text=str(self.output_dir), foreground="#333").grid(
            row=row, column=1, sticky="w")
        row += 1
        ttk.Label(frame, text="Name (optional)").grid(row=row, column=0, sticky="w")
        ttk.Entry(frame, textvariable=self.var_name, width=22).grid(
            row=row, column=1, sticky="w", pady=2)
        row += 1
        ttk.Label(frame, text="Duration (s)").grid(row=row, column=0, sticky="w")
        self.entry_duration = ttk.Entry(
            frame, textvariable=self.var_duration, width=10)
        self.entry_duration.grid(row=row, column=1, sticky="w", pady=2)
        row += 1
        ttk.Checkbutton(
            frame, text="Run until I press Stop",
            variable=self.var_until_stopped, command=self._on_duration_mode,
        ).grid(row=row, column=0, columnspan=2, sticky="w", pady=(0, 6))
        row += 1

        buttons = ttk.Frame(frame)
        buttons.grid(row=row, column=0, columnspan=2, sticky="w", pady=4)
        self.btn_start = ttk.Button(buttons, text="Start recording",
                                    command=self._on_start)
        self.btn_start.pack(side="left")
        self.btn_stop = ttk.Button(buttons, text="Stop", command=self._on_stop,
                                   state="disabled")
        self.btn_stop.pack(side="left", padx=6)
        if self.live:
            # The instrument is claimed on the first Start and held for the
            # window's lifetime, so there has to be a way to give it back
            # without closing - BrainWave cannot open the device while this
            # holds it.
            ttk.Button(buttons, text="Release instrument",
                       command=self._release_live).pack(side="left", padx=6)
        row += 1

        self.status_vars = {}
        for label in ("Status", "Elapsed", "Acquisition time", "Frames",
                      "Frames missing", "Verdict", "Stimuli delivered"):
            ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w")
            var = tk.StringVar(value="—")
            self.status_vars[label] = var
            ttk.Label(frame, textvariable=var, font=("Consolas", 10)).grid(
                row=row, column=1, sticky="w")
            row += 1

        self.lbl_health = tk.Label(frame, text="Idle", fg=COLOURS["idle"],
                                   font=("Segoe UI", 10, "bold"),
                                   wraplength=300, justify="left")
        self.lbl_health.grid(row=row, column=0, columnspan=2, sticky="w",
                             pady=(8, 0))

    def _build_array(self, frame):
        """The array: what it is picking up, and which electrodes are chosen.

        Both at once, deliberately. Choosing an electrode without seeing what
        it reads is how a dead one gets stimulated all afternoon, and watching
        activity without being able to act on it is a screensaver.
        """
        tk, ttk = self.tk, self.ttk
        from biocam.ui.arrayview import (
            NEGATIVE_COLOUR, POSITIVE_COLOUR, ElectrodeArrayView,
        )

        frame.grid(row=0, column=1, sticky="nsew", padx=6)

        self.array = ElectrodeArrayView(
            frame, n_rows=self.n_rows, n_cols=self.n_cols, cell=9,
            on_change=self._on_array_selection, on_hover=self._on_array_hover,
        )
        self.array.canvas.grid(row=0, column=0, columnspan=3)

        from biocam.ui.traceview import TraceStripView

        # Directly under the array, because the electrodes it draws are the
        # ones just clicked on it. Two panels apart would make the connection
        # something the operator has to remember rather than see.
        self.traces = TraceStripView(
            frame, tk, width=self.n_cols * self.array.cell, height=200)
        self.traces.canvas.grid(row=3, column=0, columnspan=3,
                                sticky="ew", pady=(8, 0))

        legend = ttk.Frame(frame)
        legend.grid(row=1, column=0, columnspan=3, sticky="w", pady=(6, 0))
        tk.Label(legend, text="  ", bg=POSITIVE_COLOUR,
                 relief="solid", borderwidth=1).pack(side="left")
        ttk.Label(legend, text=" left-click: positive   ").pack(side="left")
        tk.Label(legend, text="  ", bg=NEGATIVE_COLOUR,
                 relief="solid", borderwidth=1).pack(side="left")
        ttk.Label(legend, text=" right-click: negative  ").pack(side="left")
        ttk.Button(legend, text="Clear", width=7,
                   command=self.array.clear).pack(side="left", padx=6)

        self.lbl_hover = tk.Label(
            frame, text="Hover an electrode to read it.", anchor="w",
            font=("Consolas", 9), fg="#333333")
        self.lbl_hover.grid(row=2, column=0, columnspan=3, sticky="ew",
                            pady=(4, 0))
        self.lbl_scale = tk.Label(
            frame, text="No signal yet - start a recording.", anchor="w",
            font=("Segoe UI", 9), fg=COLOURS["idle"])
        self.lbl_scale.grid(row=3, column=0, columnspan=3, sticky="ew")

    def _on_array_selection(self):
        """The picture is the source of truth; the text fields follow it."""
        self._syncing = True
        try:
            self.var_positive.set(self.array.as_text("positive"))
            self.var_negative.set(self.array.as_text("negative"))
        finally:
            self._syncing = False
        self._refresh_stim_validity()
        if hasattr(self, "lbl_analysis"):
            self._refresh_analysis()

    def _on_array_hover(self, found):
        if found is None:
            self.lbl_hover.configure(text="Hover an electrode to read it.")
            return
        row, col = found
        reading = self.array.activity_at(row, col, self._activity)
        text = f"electrode ({row},{col})"
        if reading is not None:
            text += f"   {reading:7.0f} uV peak-to-peak"
        self.lbl_hover.configure(text=text)

    def _sync_array_from_text(self):
        """Follow the text fields when they are edited directly."""
        if getattr(self, "_syncing", False):
            return
        try:
            positive = self._electrodes(self.var_positive.get())
            negative = self._electrodes(self.var_negative.get())
        except Exception:  # noqa: BLE001 - a half-typed field is not an error
            return
        self.array.set_selection(
            [(e.row, e.col) for e in positive],
            [(e.row, e.col) for e in negative],
        )

    def _build_stimulation(self, frame):
        tk, ttk = self.tk, self.ttk
        frame.grid(row=0, column=2, sticky="nsew")

        self.var_amplitude = tk.StringVar(value="100")
        self.var_phase = tk.StringVar(value="200")
        self.var_gap = tk.StringVar(value="100")
        # The second phase, independently settable. Mirroring is the default
        # because it is charge-balanced by construction, but a short
        # high-amplitude phase followed by a long low-amplitude recovery is a
        # standard configuration and was previously not expressible at all.
        self.var_mirror = tk.BooleanVar(value=True)
        self.var_amplitude2 = tk.StringVar(value="-100")
        self.var_phase2 = tk.StringVar(value="200")
        self.var_positive = tk.StringVar(value="10,10")
        self.var_negative = tk.StringVar(value="20,30")
        self.var_resolution = tk.StringVar(value="10")

        fields = [
            ("Amplitude (µA)", self.var_amplitude),
            ("Phase duration (µs)", self.var_phase),
            ("Inter-phase gap (µs)", self.var_gap),
        ]
        row = 0
        for label, var in fields:
            ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w")
            entry = ttk.Entry(frame, textvariable=var, width=18)
            entry.grid(row=row, column=1, sticky="w", pady=2)
            var.trace_add("write", lambda *_: self._on_field_edited())
            row += 1

        ttk.Checkbutton(
            frame, text="Second phase mirrors the first",
            variable=self.var_mirror, command=self._on_mirror_toggled,
        ).grid(row=row, column=0, columnspan=2, sticky="w", pady=(4, 0))
        row += 1
        self.second_phase_entries = []
        for label, var in (("Second amplitude (µA)", self.var_amplitude2),
                           ("Second duration (µs)", self.var_phase2)):
            ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w")
            entry = ttk.Entry(frame, textvariable=var, width=18)
            entry.grid(row=row, column=1, sticky="w", pady=2)
            var.trace_add("write", lambda *_: self._on_field_edited())
            self.second_phase_entries.append(entry)
            row += 1

        for label, var in (("Positive electrode(s)", self.var_positive),
                           ("Negative electrode(s)", self.var_negative)):
            ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w")
            entry = ttk.Entry(frame, textvariable=var, width=18)
            entry.grid(row=row, column=1, sticky="w", pady=2)
            var.trace_add("write", lambda *_: self._on_field_edited())
            row += 1
        self._on_mirror_toggled()

        if not self.live:
            ttk.Label(frame, text="Clock resolution (µs)").grid(
                row=row, column=0, sticky="w")
            ttk.Entry(frame, textvariable=self.var_resolution, width=18).grid(
                row=row, column=1, sticky="w", pady=2)
            self.var_resolution.trace_add(
                "write", lambda *_: self._refresh_stim_validity())
            row += 1
            ttk.Label(
                frame, foreground="#8a5a00", wraplength=300, justify="left",
                text=("Simulation only. On the instrument this is read from "
                      "the device — guessing it rescales every duration."),
            ).grid(row=row, column=0, columnspan=2, sticky="w", pady=(0, 6))
            row += 1

        self.btn_stim = ttk.Button(frame, text="Stimulate now",
                                   command=self._on_stimulate, state="disabled")
        self.btn_stim.grid(row=row, column=0, columnspan=2, sticky="w", pady=6)
        row += 1

        ttk.Separator(frame, orient="horizontal").grid(
            row=row, column=0, columnspan=2, sticky="ew", pady=4)
        row += 1
        ttk.Label(frame, text="Train", font=("Segoe UI", 9, "bold")).grid(
            row=row, column=0, sticky="w")
        row += 1

        self.var_train_count = tk.StringVar(value="10")
        self.var_train_rate = tk.StringVar(value="10")
        self.var_train_delay = tk.StringVar(value="100")
        for label_text, var in (("Pulses", self.var_train_count),
                                ("Rate (Hz)", self.var_train_rate),
                                ("Starts in (ms)", self.var_train_delay)):
            ttk.Label(frame, text=label_text).grid(row=row, column=0, sticky="w")
            entry = ttk.Entry(frame, textvariable=var, width=18)
            entry.grid(row=row, column=1, sticky="w", pady=2)
            var.trace_add("write", self._on_train_edited)
            row += 1

        self.btn_train = ttk.Button(frame, text="Send train",
                                    command=self._on_train, state="disabled")
        self.btn_train.grid(row=row, column=0, columnspan=2, sticky="w", pady=6)
        row += 1

        self.lbl_train = tk.Label(frame, text="", fg=COLOURS["idle"],
                                  wraplength=320, justify="left",
                                  font=("Segoe UI", 9))
        self.lbl_train.grid(row=row, column=0, columnspan=2, sticky="w")
        row += 1

        self.lbl_stim = tk.Label(frame, text="", fg=COLOURS["idle"],
                                 wraplength=320, justify="left",
                                 font=("Segoe UI", 9))
        self.lbl_stim.grid(row=row, column=0, columnspan=2, sticky="w")

    def _build_analysis(self, frame):
        """Detection, sorting and the closed loop - the things that were
        built and then had no switch.

        The channels come from the array: whatever is selected for
        stimulation is what gets watched, because those are the electrodes
        the experiment is about. That keeps the watched set small, which is
        not a UI convenience - detection over the whole array costs about
        three times a core, and this runs on the thread that drains the
        packet queue.
        """
        tk, ttk = self.tk, self.ttk
        from biocam.analysis.sorting import SORTER_LABELS

        frame.grid(row=0, column=3, sticky="nsew", padx=(6, 0))

        self.var_detect = tk.BooleanVar(value=False)
        self.var_traces = tk.BooleanVar(value=True)
        self.var_sigmas = tk.StringVar(value="5")
        self.var_sort = tk.StringVar(value="(none)")
        self.var_units = tk.StringVar(value="2")
        self.var_loop = tk.BooleanVar(value=False)
        self.var_policy = tk.StringVar(value="echo")
        self.var_min_interval = tk.StringVar(value="20")
        self.var_max_rate = tk.StringVar(value="10")

        row = 0
        ttk.Checkbutton(
            frame, text="Detect spikes on the selected electrodes",
            variable=self.var_detect, command=self._refresh_analysis,
        ).grid(row=row, column=0, columnspan=2, sticky="w")
        row += 1
        ttk.Checkbutton(
            frame, text="Draw traces for the selected electrodes",
            variable=self.var_traces, command=self._refresh_analysis,
        ).grid(row=row, column=0, columnspan=2, sticky="w")
        row += 1
        ttk.Label(frame, text="Threshold (sigmas)").grid(
            row=row, column=0, sticky="w")
        ttk.Entry(frame, textvariable=self.var_sigmas, width=8).grid(
            row=row, column=1, sticky="w", pady=2)
        row += 1

        ttk.Separator(frame, orient="horizontal").grid(
            row=row, column=0, columnspan=2, sticky="ew", pady=6)
        row += 1

        ttk.Label(frame, text="Sorting technique").grid(
            row=row, column=0, sticky="w")
        # The techniques, by their own labels. "(none)" first, because
        # detection without sorting is a complete and often sufficient answer.
        self.sort_choices = ["(none)"] + list(SORTER_LABELS)
        combo = ttk.Combobox(
            frame, textvariable=self.var_sort, width=14, state="readonly",
            values=[
                "(none)" if name == "(none)" else SORTER_LABELS[name]
                for name in self.sort_choices
            ],
        )
        combo.grid(row=row, column=1, sticky="w", pady=2)
        combo.bind("<<ComboboxSelected>>", lambda _: self._refresh_analysis())
        self.sort_combo = combo
        row += 1
        ttk.Label(frame, text="Units per electrode").grid(
            row=row, column=0, sticky="w")
        ttk.Entry(frame, textvariable=self.var_units, width=8).grid(
            row=row, column=1, sticky="w", pady=2)
        row += 1
        self.btn_sort = ttk.Button(
            frame, text="Sort spikes so far", command=self._on_sort,
            state="disabled")
        self.btn_sort.grid(row=row, column=0, columnspan=2, sticky="w", pady=4)
        row += 1

        ttk.Separator(frame, orient="horizontal").grid(
            row=row, column=0, columnspan=2, sticky="ew", pady=6)
        row += 1

        ttk.Checkbutton(
            frame, text="Close the loop (stimulate on a spike)",
            variable=self.var_loop, command=self._refresh_analysis,
        ).grid(row=row, column=0, columnspan=2, sticky="w")
        row += 1
        ttk.Label(frame, text="Policy").grid(row=row, column=0, sticky="w")
        policy = ttk.Combobox(frame, textvariable=self.var_policy, width=14,
                              state="readonly", values=["echo", "rate"])
        policy.grid(row=row, column=1, sticky="w", pady=2)
        row += 1
        ttk.Label(frame, text="Min interval (ms)").grid(
            row=row, column=0, sticky="w")
        ttk.Entry(frame, textvariable=self.var_min_interval, width=8).grid(
            row=row, column=1, sticky="w", pady=2)
        row += 1
        ttk.Label(frame, text="Max rate (Hz)").grid(row=row, column=0, sticky="w")
        ttk.Entry(frame, textvariable=self.var_max_rate, width=8).grid(
            row=row, column=1, sticky="w", pady=2)
        row += 1

        self.lbl_analysis = tk.Label(
            frame, text="", anchor="w", justify="left", wraplength=270,
            font=("Segoe UI", 9), fg=COLOURS["idle"])
        self.lbl_analysis.grid(row=row, column=0, columnspan=2, sticky="ew",
                               pady=(8, 0))
        self._refresh_analysis()

    @property
    def sort_technique(self):
        """The selected technique's internal name, or None."""
        label = self.var_sort.get()
        if label in ("", "(none)"):
            return None
        from biocam.analysis.sorting import SORTER_LABELS

        for name, text in SORTER_LABELS.items():
            if text == label or name == label:
                return name
        return None

    def _refresh_analysis(self, *_):
        """Say what will happen, and what will not, before anything runs."""
        lines = []
        selected = len(self.array.positive) + len(self.array.negative)
        if self.var_detect.get():
            if not selected:
                lines.append(
                    "No electrodes selected. Detection watches the electrodes "
                    "chosen on the array - click some.")
            else:
                lines.append(
                    f"Detecting on {selected} electrode(s) at "
                    f"{self.var_sigmas.get()} sigma.")
        else:
            lines.append("Detection off.")

        if self.var_traces.get():
            from biocam.data.traces import MAX_TRACE_CHANNELS

            if not selected:
                lines.append(
                    "Traces on, but no electrodes are selected - click some.")
            elif selected > MAX_TRACE_CHANNELS:
                lines.append(
                    f"Traces: the first {MAX_TRACE_CHANNELS} of {selected} "
                    "selected. A trace is for looking at closely, and this "
                    "costs time on the thread draining the packet queue.")
            else:
                lines.append(
                    f"Traces: {selected} electrode(s), peak-preserving so a "
                    "spike cannot fall between samples.")

        technique = self.sort_technique
        if technique:
            lines.append(
                f"Sorting: {technique}, {self.var_units.get()} units per "
                "electrode. Sorting happens per electrode - a unit is a "
                "neuron as heard by one site.")
        if self.var_loop.get():
            if not self.var_detect.get():
                lines.append(
                    "The closed loop needs detection on: it triggers on "
                    "spikes.")
            elif self.live:
                lines.append(
                    f"LOOP ARMED: {self.var_policy.get()} policy, at most one "
                    f"stimulus per {self.var_min_interval.get()} ms and "
                    f"{self.var_max_rate.get()} Hz sustained. The limits are "
                    "enforced after the policy decides and cannot be "
                    "overridden by it.")
            else:
                lines.append(
                    f"Loop armed ({self.var_policy.get()}), but this is "
                    "simulation - decisions are made and logged, and nothing "
                    "is delivered anywhere.")

        if self.controller.running:
            lines.append("Settings apply to the NEXT recording.")
        self.lbl_analysis.configure(text="\n\n".join(lines))
        if self.controller.running:
            # Sorting is not a background job. It fits k-means and a silhouette
            # on the same thread that draws, in the same process whose
            # consumer thread is the only thing draining the packet queue -
            # and a stalled consumer is dropped packets, silently, in a
            # recording that afterwards looks like real signal. It waits.
            #
            # This guard is also load-bearing for thread safety, not only for
            # CPU contention: `spikes_with_waveforms()` does `list(deque)` on
            # the UI thread while the consumer thread appends to it. That is
            # safe on CPython because the whole copy runs inside one bytecode,
            # but `deque` documents `RuntimeError` on mutation during
            # iteration and this guard is what keeps the two threads from
            # overlapping at all. Do not relax it without replacing the copy.
            self.btn_sort.configure(state="disabled")
            return
        self.btn_sort.configure(
            state="normal" if (technique and self._sortable()) else "disabled")

    def _sortable(self) -> bool:
        return self.controller.n_waveforms() > 0

    def _on_sort(self):
        from biocam.analysis.sorting import sort_by_channel

        if self.controller.running:
            self._log(
                "Sorting waits until the recording stops - it would compete "
                "with the thread draining the packet queue, and losing that "
                "race costs packets.", "warn")
            return
        technique = self.sort_technique
        spikes = self.controller.spikes_with_waveforms()
        if not technique or not spikes:
            return
        try:
            units = int(self.var_units.get())
        except ValueError:
            self._log("Units per electrode must be a whole number.", "bad")
            return
        try:
            sorters = sort_by_channel(spikes, technique=technique,
                                      n_units=units)
        except ValueError as exc:
            self._log(f"Sorting failed: {exc}", "bad")
            return
        if not sorters:
            self._log(
                f"No electrode had enough spikes to sort ({len(spikes)} in "
                "total). An under-fitted sorter is worse than none, because "
                "it still returns labels.", "warn")
            return

        watched = self.controller.watched_channels()
        self._log(f"Sorted {len(sorters)} electrode(s) with {technique}:", "ok")
        for index, sorter in sorted(sorters.items()):
            name = watched[index] if index < len(watched) else index
            self._log(f"   electrode {name}: {sorter.describe()}")
            for warning in sorter.warnings():
                self._log(f"      {warning}", "warn")

    def _build_log(self, frame):
        tk = self.tk
        frame.grid(row=1, column=0, columnspan=4, sticky="nsew", pady=(8, 0))
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)

        self.text = tk.Text(frame, height=12, wrap="word",
                            font=("Consolas", 9), state="disabled")
        self.text.grid(row=0, column=0, sticky="nsew")
        scroll = self.ttk.Scrollbar(frame, command=self.text.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.text.configure(yscrollcommand=scroll.set)
        for tag, colour in (("warn", COLOURS["warn"]), ("bad", COLOURS["bad"]),
                            ("ok", COLOURS["ok"])):
            self.text.tag_configure(tag, foreground=colour)

    # -- building a stimulus from the fields ------------------------------

    def _electrodes(self, text):
        from biocam.stim import Electrode

        out = []
        for chunk in text.split(";"):
            chunk = chunk.strip()
            if not chunk:
                continue
            parts = chunk.split(",")
            if len(parts) != 2:
                raise ValueError(f"{chunk!r} is not a row,col pair")
            out.append(Electrode(int(parts[0]), int(parts[1])))
        if not out:
            raise ValueError("no electrodes given")
        return tuple(out)

    def _build_stimulus(self):
        """Return (plan, pattern), or raise with a message fit for a label."""
        from biocam.stim import (
            StimPattern, plan as plan_pulse, validate_pattern,
        )

        pattern = StimPattern(
            positive=self._electrodes(self.var_positive.get()),
            negative=self._electrodes(self.var_negative.get()),
        )
        # The same grid the stimulator will use, so the window and the
        # instrument cannot disagree about what is inside the array.
        validate_pattern(pattern, self.grid)
        return plan_pulse(self._pulse_spec(), self._constraints()), pattern

    def _on_mirror_toggled(self):
        """Grey the second-phase fields when they are being derived."""
        if not hasattr(self, "second_phase_entries"):
            return
        state = "disabled" if self.var_mirror.get() else "normal"
        for entry in self.second_phase_entries:
            entry.configure(state=state)
        if self.var_mirror.get():
            # Keep them showing what will actually be delivered, so the
            # displayed numbers never contradict the pulse.
            self.var_amplitude2.set(str(-_as_float(self.var_amplitude, 0.0)))
            self.var_phase2.set(self.var_phase.get())
        # Guarded: this also runs while the panel is still being built, before
        # the button whose state validation sets exists.
        self._on_field_edited()

    def _pulse_spec(self):
        """The pulse the fields describe, before planning.

        Shared by the single-pulse and train paths so the two can never
        describe different pulses - a train that quietly used a different
        amplitude from the one shown would be very hard to notice and very
        easy to write.

        Mirroring is charge-balanced by construction. Unmirrored, it is not,
        so `plan()` still enforces balance and refuses anything that injects
        net DC - the fields let you shape the two phases, not escape the
        constraint.
        """
        from biocam.stim import PulseSpec

        amplitude = float(self.var_amplitude.get())
        phase_us = float(self.var_phase.get())
        if self.var_mirror.get():
            amplitude2, phase2_us = -amplitude, phase_us
        else:
            amplitude2 = float(self.var_amplitude2.get())
            phase2_us = float(self.var_phase2.get())
        return PulseSpec(
            amplitude1=amplitude, phase1_us=phase_us,
            inter_us=float(self.var_gap.get()),
            amplitude2=amplitude2, phase2_us=phase2_us, name="ui-pulse",
        )

    def _constraints(self):
        """The stimulator's limits: the device's in live mode, always.

        This used to fall through to the simulation defaults whenever the
        instrument was not yet claimed, so before Start was pressed a LIVE
        window would display "Valid: ..." for a pulse validated against
        invented limits - durations the operator would read and write down,
        and not the ones that would fire.
        """
        from biocam.stim import StimConstraints

        if self.live:
            if self._stimulator is None:
                if self._live_stack is not None:
                    # Claimed, but the stimulator would not initialize. Start
                    # will never change that, so saying "press Start" would
                    # send the operator round a loop.
                    raise RuntimeError(
                        "this session is recording only: the stimulator did "
                        "not initialize when the instrument was claimed. "
                        "Stimulation is unavailable until the instrument is "
                        "released and re-claimed - the session log has the "
                        "reason it gave."
                    )
                raise RuntimeError(
                    "the stimulator's limits are unknown until the instrument "
                    "is claimed. Press Start; the pulse is validated against "
                    "the device's own limits before anything is delivered."
                )
            return self._stimulator.constraints
        return StimConstraints(
            time_resolution_us=int(self.var_resolution.get()),
            amplitude_resolution=1.0,
            min_amplitude=-1000.0, max_amplitude=1000.0,
            max_total_ticks=1000,
        )

    def _on_field_edited(self, *_):
        """A text field changed: mirror it into the picture, then validate."""
        self._sync_array_from_text()
        self._refresh_stim_validity()

    def _refresh_stim_validity(self, *_):
        """Validate continuously, and say why the button is unavailable.

        A greyed-out control with no explanation is the thing an operator
        cannot debug alone in the middle of an experiment.
        """
        # Field traces fire while the panel is still being built - setting the
        # mirrored second-phase fields triggers their own write callbacks - so
        # this runs before the widgets it configures exist. Tkinter prints a
        # callback exception and carries on, which is exactly why 54 passing
        # UI tests did not notice: the window threw on every edit during
        # construction and the tests only ever saw the end state.
        if not hasattr(self, "btn_stim"):
            return

        try:
            plan, pattern = self._build_stimulus()
        except Exception as exc:  # noqa: BLE001 - every failure is a message
            self.btn_stim.configure(state="disabled")
            self.lbl_stim.configure(
                text=str(exc).replace("\n", " ")[:400], fg=COLOURS["bad"])
            return

        if not self.controller.running:
            self.btn_stim.configure(state="disabled")
            self.lbl_stim.configure(
                text=("Valid: " + plan.describe()
                      + "\n\nStart a recording to enable stimulation — "
                        "a stimulus with nothing recording it leaves no "
                        "evidence of what it did."),
                fg=COLOURS["idle"])
            return

        self.btn_stim.configure(state="normal")
        self.lbl_stim.configure(text=plan.describe(), fg=COLOURS["ok"])

    # -- actions ----------------------------------------------------------

    def _on_duration_mode(self):
        self.entry_duration.configure(
            state="disabled" if self.var_until_stopped.get() else "normal")

    def _on_start(self):
        if self.controller.running:
            return
        try:
            self._factory = self._make_factory()
        except Exception as exc:  # noqa: BLE001 - shown, never swallowed
            # Deliberately NOT releasing the instrument here. It is held for
            # the window's lifetime, and a typo in the Duration field should
            # not force the next Start to re-Activate a deactivated pool.
            # _ensure_live_instrument cleans up after itself if the claim is
            # what failed.
            self._log(f"Cannot start: {exc}", "bad")
            return
        try:
            self.controller.start(self._factory)
        except Exception as exc:  # noqa: BLE001 - the instrument is already held
            self._log(f"Could not start the recording: {exc}", "bad")
            return
        self.btn_start.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        # No "recording to ..." line here: RecordingStarted arrives through
        # the event stream a moment later and says the same thing with the
        # channel count and rate attached.
        self._refresh_stim_validity()
        self._refresh_analysis()   # greys out Sort for the recording's duration
        self._on_train_edited()    # and un-greys Send train

    def _on_stop(self):
        self.controller.stop()
        self.btn_stop.configure(state="disabled")
        self._log("Stopping — draining whatever is still buffered…")

    def _on_stimulate(self):
        try:
            plan, pattern = self._build_stimulus()
        except Exception as exc:  # noqa: BLE001
            self._log(f"Stimulus refused: {exc}", "bad")
            return
        if self.controller.request_stimulus(plan, pattern, label="manual"):
            self._log(f"Requested: {plan.describe()}")
        else:
            self._log(
                "Stimulus NOT queued: the stimulation queue is full. It was "
                "not delivered.", "bad")

    def _build_train(self):
        """Return a TrainPlan positioned in acquisition time, or raise.

        The positioning is the part worth reading. Stimulation timestamps are
        counted **from the beginning of the acquisition**, not from now
        (`Stimulator.send_scheduled`, and issue #24). A train built with
        `delay_us=0` and sent ten minutes into a recording therefore has every
        timestamp ten minutes in the past, and what the instrument does with a
        past timestamp is untested. So the plan is shifted by the acquisition
        clock's current reading before it goes anywhere, and if that reading
        is unavailable the train is refused rather than sent to an unknown
        point in time.
        """
        from biocam.stim import TrainSpec, plan_train

        pulse_plan, pattern = self._build_stimulus()

        count = int(self.var_train_count.get())
        rate_hz = float(self.var_train_rate.get())
        delay_ms = float(self.var_train_delay.get())
        if count < 2:
            raise ValueError(
                "a train needs at least two pulses - use \"Stimulate now\" "
                "for a single one")
        if rate_hz <= 0:
            raise ValueError("the train rate must be above zero")
        if delay_ms < 0:
            raise ValueError("a train cannot start in the past")

        spec = TrainSpec.at_rate(
            self._pulse_spec(), count=count, rate_hz=rate_hz,
            delay_us=delay_ms * 1000.0, name="ui-train")
        train = plan_train(spec, self._constraints())
        return train, pattern

    def _positioned_train(self):
        """The train shifted into acquisition time. Raises if that is unknown."""
        train, pattern = self._build_train()
        now_us = self.controller.acquisition_us()
        if now_us is None:
            raise RuntimeError(
                "the acquisition clock has no reading yet, so there is no way "
                "to say when this train should fire. Timestamps are counted "
                "from the beginning of the acquisition, not from now - "
                "sending without shifting them would schedule the train into "
                "the past. Start the recording first.")
        return train.shifted_by(now_us), pattern, train

    def _on_train_edited(self, *_):
        """Say what the train will do, before it does it."""
        if not hasattr(self, "lbl_train"):
            return
        try:
            train, _pattern = self._build_train()
        except Exception as exc:  # noqa: BLE001 - shown, not raised
            self.lbl_train.configure(text=str(exc), fg=COLOURS["bad"])
            self.btn_train.configure(state="disabled")
            return
        self.lbl_train.configure(text=train.describe(), fg=COLOURS["ok"])
        self.btn_train.configure(
            state="normal" if self.controller.running else "disabled")

    def _on_train(self):
        try:
            plan, pattern, train = self._positioned_train()
        except Exception as exc:  # noqa: BLE001
            self._log(f"Train refused: {exc}", "bad")
            return
        if self.controller.request_stimulus(plan, pattern, scheduled=True,
                                            label="train"):
            first = plan.timestamps_us[0] / 1e6
            self._log(
                f"Train queued: {train.describe()}. First pulse at "
                f"{first:.3f} s of acquisition time, which is "
                f"{self.var_train_delay.get()} ms from now.")
            self._log(
                "Scheduled trains are queued by the instrument, not delivered "
                "pulse by pulse from here. Whether it honours the schedule is "
                "untested - issue #24.", "warn")
        else:
            self._log(
                "Train NOT queued: the stimulation queue is full. It was not "
                "delivered.", "bad")

    def _make_factory(self):
        from biocam.ui.factories import ReplayFactory

        name = self.var_name.get().strip() or time.strftime("%Y%m%d_%H%M%S")
        self.output_dir.mkdir(parents=True, exist_ok=True)
        output = self.output_dir / f"{name}.raw"
        duration = (None if self.var_until_stopped.get()
                    else float(self.var_duration.get()))

        if self.live:
            return self._make_live_factory(output, duration)

        if not self.replay_source:
            raise RuntimeError(
                "simulation needs a source recording: pass --replay <file.raw>"
            )
        return ReplayFactory(
            raw_path=self.replay_source, params=self.replay_params,
            output_path=output, duration_sec=duration,
            frames_per_packet=200, pace_hz=50.0,
            **self._analysis_settings(),
        )

    def _ensure_live_instrument(self):
        """Claim the instrument once and keep it until the window closes.

        `BioCamDevice.__enter__` calls `BioCamPool.Activate()` and `__exit__`
        calls `Deactivate()`, so one device per recording would do
        Activate -> Deactivate -> Activate across two runs. Nothing in this
        repository documents whether the pool survives that, and 3Brain's
        sample never tries: it activates once at form load and deactivates
        once at form close (MainForm.cs:72, :85), taking and releasing BioCAM
        control repeatedly in between. The first thing that would have
        exercised re-activation is the second recording of a lab day.
        """
        from biocam.interop.device import BioCamDevice
        from biocam.interop.stimulator import Stimulator
        from biocam.stim import StimulusLog

        if self._live_stack is not None:
            return

        # The same floor record_command enforces. source.POLL_INTERVAL_SEC's
        # 1 ms only resolves from 3.11 on Windows; below that time.sleep()
        # rounds up to the ~15.6 ms system timer, silently restoring the
        # latency that constant exists to remove. The CLI refuses outright,
        # and there is no reason this window should be more permissive about
        # the same driver.
        from biocam.cli import MIN_PYTHON_FOR_POLL_PRECISION

        if sys.version_info < MIN_PYTHON_FOR_POLL_PRECISION:
            need = ".".join(str(n) for n in MIN_PYTHON_FOR_POLL_PRECISION)
            raise RuntimeError(
                f"Python {need}+ is required to drive the instrument; this is "
                f"{sys.version_info.major}.{sys.version_info.minor}. Below "
                f"{need}, time.sleep() on Windows rounds up to ~15.6 ms and "
                "the consumer would poll 15x slower than intended."
            )

        stack = ExitStack()
        try:
            self._device = stack.enter_context(BioCamDevice())
            try:
                # Initialize/Close only. Start/Stop bracket the streaming and
                # happen in LiveFactory.start_source / stop_source_safely.
                self._stimulator = stack.enter_context(
                    Stimulator(self._device, log=StimulusLog(),
                               grid=self.grid,
                               warn=self._warn_from_any_thread))
            except Exception as exc:  # noqa: BLE001 - see below
                # A stimulator that will not initialize must not block
                # recording. It is a separate module that may not be
                # installed, or connector.py may already hold it; refusing
                # the whole session would make a recording-only run
                # impossible on this window.
                self._stimulator = None
                self._log(f"Stimulation unavailable: {exc}", "warn")
                self._log("Recording will still work.", "warn")
        except BaseException:
            stack.close()
            self._device = self._stimulator = None
            raise
        self._live_stack = stack

    def _analysis_settings(self) -> dict:
        """Detection and loop settings, from the panel and the array.

        The watched channels come from the array selection: the electrodes
        chosen for stimulation are the ones the experiment is about, and
        keeping the set small is a requirement rather than a preference -
        detection over the whole array is unaffordable on the thread that
        drains the packet queue.
        """
        from biocam.ui.arrayview import channel_index

        if not self.var_detect.get():
            return {}
        chosen = list(self.array.positive) + list(self.array.negative)
        channels = sorted({channel_index(row, col, self.n_cols)
                           for row, col in chosen})
        if not channels:
            return {}
        return {
            "trace_channels": self._trace_channels(),
            "detect_channels": tuple(channels),
            "threshold_sigmas": _as_float(self.var_sigmas, 5.0),
            "collect_waveforms": self.sort_technique is not None,
            "close_the_loop": bool(self.var_loop.get()),
            "policy_name": self.var_policy.get(),
            "min_interval_ms": _as_float(self.var_min_interval, 20.0),
            "max_rate_hz": _as_float(self.var_max_rate, 10.0),
        }

    def _trace_channels(self) -> tuple:
        """The electrodes whose signal is drawn, in array order.

        The array selection again, capped: a trace is for looking at closely
        and the cost lands on the thread draining the packet queue, so past
        `MAX_TRACE_CHANNELS` the extras are dropped and the operator is told.
        Silently drawing eight of twenty would be worse - it reads as "these
        are the ones you chose".
        """
        from biocam.data.traces import MAX_TRACE_CHANNELS
        from biocam.ui.arrayview import channel_index

        if not self.var_traces.get():
            return ()
        chosen = list(self.array.positive) + list(self.array.negative)
        channels = sorted({channel_index(row, col, self.n_cols)
                           for row, col in chosen})
        if len(channels) > MAX_TRACE_CHANNELS:
            self._log(
                f"{len(channels)} electrodes are selected but only "
                f"{MAX_TRACE_CHANNELS} can be traced at once; tracing the "
                f"first {MAX_TRACE_CHANNELS}. Detection still watches all of "
                "them.", "warn")
            channels = channels[:MAX_TRACE_CHANNELS]
        return tuple(channels)

    def _make_live_factory(self, output, duration):
        # Imported here, never at module scope: this module must stay
        # importable on a machine with no 3Brain DLLs.
        from biocam.ui.factories import LiveFactory

        self._ensure_live_instrument()
        plan = pattern = None
        if self.var_loop.get():
            try:
                plan, pattern = self._build_stimulus()
            except Exception as exc:  # noqa: BLE001 - reported, not raised
                self._log(f"The closed loop has no valid stimulus: {exc}. "
                          "It will detect but not stimulate.", "warn")
        return LiveFactory(
            output_path=output, duration_sec=duration,
            device=self._device, stimulator=self._stimulator,
            log=self._stimulator.log if self._stimulator else None,
            listener=self.controller.listener,
            warn=self._warn_from_any_thread,
            loop_plan=plan, loop_pattern=pattern,
            **self._analysis_settings(),
        )

    def _warn_from_any_thread(self, message):
        """A `warn` callable safe to hand to code running on another thread.

        Never blocks: two of its callers are the recording thread and the
        consumer thread, and the consumer thread is the drain. A full queue
        drops rather than waiting.
        """
        try:
            self._messages.put_nowait(message)
        except queue.Full:
            pass

    def _release_live(self):
        """Release the instrument. Safe to call when nothing is held.

        The stack reference is cleared only once the close succeeds. Dropping
        it first left a failed release unretryable, with the device still
        claimed and nothing able to try again.
        """
        stack = self._live_stack
        if stack is None:
            return
        if self.controller.running:
            # Releasing now would run ReleaseBioCamControl and Deactivate
            # while the worker is still streaming, setting device.biocam to
            # None underneath it - after which source.stop() takes its
            # "nothing to stop" branch and never calls StopDataStreaming nor
            # unsubscribes. The slot would go back to the pool still
            # streaming, with handlers attached.
            self._log("Not releasing the instrument: a recording is still "
                      "running. Stop it first.", "bad")
            return
        try:
            stack.close()
        except Exception as exc:  # noqa: BLE001 - reported, never raised at a UI
            self._log(f"Releasing the instrument failed: {exc}. It may still "
                      "be claimed; try again, or restart.", "bad")
            return
        self._live_stack = None
        self._device = None
        self._stimulator = None
        self._log("Instrument released.", "ok")

    def _write_manifest(self):
        """Persist what this session WAS, beside what it recorded.

        The sidecar says what was acquired and the stimulus log says what
        fired. Neither says which electrodes were watched, at what threshold,
        under which policy, or inside what limits - so two sessions driven by
        completely different rules are indistinguishable on disk. Six weeks
        later, on a shared instrument, that is the difference between data and
        an unlabelled file.

        Written after the stimulus log and guarded the same way: a recording
        is never lost because a record of it could not be saved.
        """
        if self._factory is None:
            return
        output = Path(self._factory.output_path)
        path = output.with_name(output.stem + "_session.json")
        plan = pattern = None
        try:
            plan, pattern = self._build_stimulus()
        except Exception:  # noqa: BLE001 - an invalid pulse is still a fact
            pass                # about the session; record the rest anyway
        try:
            manifest = self.controller.build_manifest(
                live=self.live,
                requested_duration_sec=(
                    None if self.var_until_stopped.get()
                    else _as_float(self.var_duration, None)),
                stimulus_plan=plan, stimulus_pattern=pattern,
                raw_path=output,
                meta_path=output.with_name(output.stem + "_meta.json"),
                stimulus_log_path=output.with_name(output.stem + "_stimuli.json"),
            )
            manifest.write(path)
        except Exception as exc:  # noqa: BLE001 - never at the cost of the UI
            self._log(f"Could not write the session record to {path}: {exc}. "
                      "The recording itself is unaffected.", "bad")
            return
        self._log(f"Session record: {path} ({manifest.describe()})", "ok")

    def _write_stimulus_log(self):
        """Persist what was stimulated, beside the recording it belongs to.

        Without this the latencies, refusals and rejections die with the
        process - the one correspondence a later analysis cannot
        reconstruct. Written at the end of a session rather than during it,
        because this must not do disk work while the drain is running.
        """
        log = getattr(self._factory, "log", None)
        if log is None or not len(log):
            return
        output = Path(self._factory.output_path)
        path = output.with_name(output.stem + "_stimuli.json")
        cycles_per_us = None
        if self._stimulator is not None:
            cycles_per_us = self._stimulator.cycles_per_us
        try:
            log.write(path, cycles_per_us=cycles_per_us)
        except Exception as exc:  # noqa: BLE001 - never at the cost of the UI
            self._log(f"Could not write the stimulus log to {path}: {exc}. "
                      "The recording itself is unaffected.", "bad")
            return
        self._log(f"Stimulus log: {path} ({log.describe()})", "ok")

    # -- polling ----------------------------------------------------------

    def _poll(self):
        try:
            while True:
                try:
                    self._log(self._messages.get_nowait(), "warn")
                except queue.Empty:
                    break
            for event in self.controller.drain_events():
                self._log(describe(event), self._severity(event))
            self._render(self.controller.snapshot())
            self._render_activity()
            self._render_traces()
        finally:
            # Rescheduled in a finally so one bad render cannot stop the UI
            # updating forever - a frozen window during a recording is the
            # worst thing this can do to an operator.
            self.root.after(POLL_MS, self._poll)

    @staticmethod
    def _severity(event):
        from biocam.data.events import (
            DiskLow, DriverDataLoss, GapDetected, GapSummary, QueueOverflow,
            StimulationSuspended,
        )

        if isinstance(event, (QueueOverflow, DiskLow, StimulationSuspended)):
            return "bad"
        if isinstance(event, (GapDetected, GapSummary, DriverDataLoss)):
            return "warn"
        return None

    def _render(self, state):
        set_ = lambda k, v: self.status_vars[k].set(v)  # noqa: E731
        set_("Status", "recording" if state.running
             else "finished" if state.finished else "idle")
        set_("Elapsed", f"{state.elapsed_sec:6.1f} s")
        set_("Acquisition time", f"{state.acquisition_sec:6.3f} s "
                                 f"({state.clock_source or '—'})")
        set_("Frames", f"{state.frames:,}")
        set_("Frames missing", f"{state.frames_missing:,}")
        set_("Verdict", state.verdict or "—")
        set_("Stimuli delivered", f"{state.stimuli_delivered}"
                                  + (f"  ({state.stimuli_failed} not delivered)"
                                     if state.stimuli_failed else ""))

        if state.error:
            self.lbl_health.configure(text=f"ERROR: {state.error}",
                                      fg=COLOURS["bad"])
        elif state.warnings:
            self.lbl_health.configure(
                text="\n\n".join(state.warnings)[:900], fg=COLOURS["warn"])
        elif state.running:
            self.lbl_health.configure(text="Recording.", fg=COLOURS["ok"])
        elif state.finished:
            self.lbl_health.configure(
                text=f"Finished: {state.stop_reason}. Nothing to report.",
                fg=COLOURS["ok"])

        # str(): a ttk widget's option comes back as a Tcl_Obj, and
        # Tcl_Obj == "disabled" is False even when it prints as "disabled".
        # Without the conversion this branch never ran, so the Start button
        # stayed greyed out after the first recording and the only way back
        # was to restart the application.
        if state.finished and str(self.btn_start["state"]) == "disabled":
            # The instrument is deliberately NOT released here. It is held for
            # the window's lifetime so a second recording need not re-Activate
            # the pool; use "Release instrument", or close the window.
            # Buttons first. This branch is guarded on btn_start still
            # being "disabled", so anything that raises in between made
            # _render retry it every 150 ms for the life of the window, with
            # the recording finished and Start greyed out for good.
            self.btn_start.configure(state="normal")
            self._write_stimulus_log()
            self._write_manifest()
            self.btn_stop.configure(state="disabled")
            self._log(f"Finished: {state.stop_reason}, {state.frames:,} frames, "
                      f"verdict {state.verdict}",
                      "ok" if state.verdict == "clean" else "warn")
            self._refresh_stim_validity()
            # The recording has stopped, so sorting is allowed again and a
            # train is not. Both are only ever refreshed by a widget command
            # otherwise, which would leave a button in the wrong state until
            # the operator happened to touch something.
            self._refresh_analysis()
            self._on_train_edited()

    def _render_activity(self):
        """Repaint the array. UI thread only, from a copied snapshot."""
        activity = self.controller.activity()
        if activity is None or not activity.has_data:
            return
        self._activity = activity
        self.array.set_activity(activity)
        low, high = activity.range()
        self.lbl_scale.configure(
            text=(f"peak-to-peak {low:.0f} - {high:.0f} uV   "
                  f"({activity.samples} samples, "
                  f"{activity.max_observe_us:.0f} us slowest)"),
            fg=COLOURS["ok"])

    def _render_traces(self):
        """Repaint the trace strip. UI thread only, from a copied snapshot."""
        if not hasattr(self, "traces"):
            return
        snapshot = self.controller.traces()
        if snapshot is None:
            self.traces.set_message(
                "Traces are off. Tick \"Draw traces\" and select electrodes "
                "on the array.")
            return
        if not snapshot.has_data:
            self.traces.set_message("Waiting for the first packet...")
            return
        self.traces.set_snapshot(snapshot)

    def _log(self, message, tag=None):
        stamp = time.strftime("%H:%M:%S")
        self.text.configure(state="normal")
        self.text.insert("end", f"{stamp}  {message}\n", tag or ())
        self._log_lines += 1
        if self._log_lines > MAX_LOG_LINES:
            # Bounded, like every other buffer here. A window left open for a
            # ten-hour recording must not accumulate a ten-hour text widget.
            self.text.delete("1.0", "2.0")
            self._log_lines -= 1
        self.text.see("end")
        self.text.configure(state="disabled")

    def _on_close(self):
        """Close the window, but never while the instrument is still live.

        `_release_live` refuses while a recording runs - correctly, because
        releasing mid-stream leaves the slot back in the pool still streaming.
        But ignoring that refusal and destroying the window anyway is worse
        than the defect it replaced: the instrument is then never released at
        all, and the message saying so is written into a widget that is
        destroyed on the next line.

        So the close waits, generously, and if the worker still will not stop
        it says what is being left behind rather than pretending otherwise.
        """
        if self.controller.running:
            self._log("Stopping the recording before closing...")
            self.controller.stop()
            # Longer than DRAIN_DEADLINE_SEC (5 s), which is only the first
            # of several steps left after stop() is seen - stop_source, the
            # backlog drain and finalise() all follow it.
            if not self.controller.join(20.0):
                self._log(
                    "The recording thread has not stopped. Not closing yet - "
                    "closing now would leave the instrument claimed and "
                    "streaming, with no way to release it but ending the "
                    "process. Wait, or kill the process if it never "
                    "finishes.", "bad")
                self.root.after(1000, self._on_close)
                return
        self._release_live()
        if self._live_stack is not None:
            self._log("The instrument could not be released; closing anyway. "
                      "It will be freed when this process exits.", "bad")
        self.root.destroy()

    def run(self):
        self.root.mainloop()


def main(argv=None) -> int:
    import argparse
    import tkinter as tk

    parser = argparse.ArgumentParser(
        prog="python -m biocam.ui",
        description=("The BioCAM operator window. Without --live it runs in "
                     "simulation against a recorded file, which needs no "
                     "instrument and no 3Brain DLLs."),
    )
    parser.add_argument("--live", action="store_true",
                        help="drive the real instrument (lab machine only)")
    parser.add_argument("--replay", type=str, default=None,
                        help="a .raw file to replay in simulation mode")
    parser.add_argument("--meta", type=str, default=None,
                        help="the _meta.json beside --replay, for its "
                             "acquisition parameters")
    parser.add_argument("--output-dir", type=str, default="recordings")
    args = parser.parse_args(argv)

    params = None
    if not args.live:
        if not args.replay or not args.meta:
            print("Simulation mode needs --replay <file.raw> and "
                  "--meta <file_meta.json>. Use tools/make_demo_recording.py "
                  "to make one if you have no recording to hand.",
                  file=sys.stderr)
            return 2
        params = _params_from_meta(args.meta)

    root = tk.Tk()
    BioCamWindow(root, live=args.live, output_dir=args.output_dir,
                 replay_source=args.replay, params=params).run()
    return 0


def _as_float(variable, fallback: float) -> float:
    """Read a Tk entry as a number, falling back rather than raising.

    A half-typed field must not stop a recording from starting; the panel
    already shows what will be used.
    """
    try:
        return float(variable.get())
    except (TypeError, ValueError):
        return fallback


def _params_from_meta(meta_path):
    import json

    from biocam.data.recording import AcquisitionParameters

    meta = json.loads(Path(meta_path).read_text(encoding="utf-8"))
    return AcquisitionParameters(
        frame_rate_hz=meta["frame_rate_hz"],
        total_channels=meta["total_channels"],
        ch_sample_byte_size=meta["ch_sample_byte_size"],
        bit_depth=meta["bit_depth"],
        adc_counts_to_value=meta["adc_counts_to_value"],
        offset=meta["offset"],
        min_digital_value=meta["min_digital_value"],
        max_digital_value=meta["max_digital_value"],
    )


if __name__ == "__main__":
    raise SystemExit(main())
