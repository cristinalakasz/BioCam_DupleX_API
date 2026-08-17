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

        root.title(
            "BioCAM DupleX — LIVE INSTRUMENT" if live
            else "BioCAM DupleX — SIMULATION (no instrument)"
        )
        root.geometry("1080x720")
        root.protocol("WM_DELETE_WINDOW", self._on_close)

        self._build()
        self._refresh_stim_validity()
        self.root.after(POLL_MS, self._poll)

    # -- construction ----------------------------------------------------

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

        body = ttk.Frame(self.root, padding=10)
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1, minsize=320)
        body.columnconfigure(1, weight=1, minsize=320)
        body.rowconfigure(1, weight=1)

        self._build_recording(ttk.LabelFrame(body, text="Recording", padding=10))
        self._build_stimulation(
            ttk.LabelFrame(body, text="Stimulation", padding=10))
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

    def _build_stimulation(self, frame):
        tk, ttk = self.tk, self.ttk
        frame.grid(row=0, column=1, sticky="nsew")

        self.var_amplitude = tk.StringVar(value="100")
        self.var_phase = tk.StringVar(value="200")
        self.var_gap = tk.StringVar(value="100")
        self.var_positive = tk.StringVar(value="10,10")
        self.var_negative = tk.StringVar(value="20,30")
        self.var_resolution = tk.StringVar(value="10")

        fields = [
            ("Amplitude (µA)", self.var_amplitude),
            ("Phase duration (µs)", self.var_phase),
            ("Inter-phase gap (µs)", self.var_gap),
            ("Positive electrode(s)", self.var_positive),
            ("Negative electrode(s)", self.var_negative),
        ]
        row = 0
        for label, var in fields:
            ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w")
            entry = ttk.Entry(frame, textvariable=var, width=18)
            entry.grid(row=row, column=1, sticky="w", pady=2)
            var.trace_add("write", lambda *_: self._refresh_stim_validity())
            row += 1

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

        self.lbl_stim = tk.Label(frame, text="", fg=COLOURS["idle"],
                                 wraplength=320, justify="left",
                                 font=("Segoe UI", 9))
        self.lbl_stim.grid(row=row, column=0, columnspan=2, sticky="w")

    def _build_log(self, frame):
        tk = self.tk
        frame.grid(row=1, column=0, columnspan=2, sticky="nsew", pady=(10, 0))
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
            PulseSpec, StimConstraints, StimPattern, plan as plan_pulse,
            validate_pattern,
        )

        amplitude = float(self.var_amplitude.get())
        phase_us = float(self.var_phase.get())
        gap_us = float(self.var_gap.get())
        pattern = StimPattern(
            positive=self._electrodes(self.var_positive.get()),
            negative=self._electrodes(self.var_negative.get()),
        )
        validate_pattern(pattern)

        constraints = self._constraints()
        spec = PulseSpec(
            amplitude1=amplitude, phase1_us=phase_us, inter_us=gap_us,
            amplitude2=-amplitude, phase2_us=phase_us, name="ui-pulse",
        )
        return plan_pulse(spec, constraints), pattern

    def _constraints(self):
        from biocam.stim import StimConstraints

        if self.live and getattr(self._factory, "stimulator", None) is not None:
            return self._factory.stimulator.constraints
        return StimConstraints(
            time_resolution_us=int(self.var_resolution.get()),
            amplitude_resolution=1.0,
            min_amplitude=-1000.0, max_amplitude=1000.0,
            max_total_ticks=1000,
        )

    def _refresh_stim_validity(self, *_):
        """Validate continuously, and say why the button is unavailable.

        A greyed-out control with no explanation is the thing an operator
        cannot debug alone in the middle of an experiment.
        """
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
            self._release_live()
            self._log(f"Cannot start: {exc}", "bad")
            return
        self.controller.start(self._factory)
        self.btn_start.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        # No "recording to ..." line here: RecordingStarted arrives through
        # the event stream a moment later and says the same thing with the
        # channel count and rate attached.
        self._refresh_stim_validity()

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
        )

    def _make_live_factory(self, output, duration):
        # Imported here, never at module scope: this module must stay
        # importable on a machine with no 3Brain DLLs.
        from biocam.interop.device import BioCamDevice
        from biocam.interop.stimulator import Stimulator
        from biocam.stim import StimulusLog
        from biocam.ui.factories import LiveFactory

        warn = lambda message: self._log(message, "warn")  # noqa: E731
        stack = ExitStack()
        try:
            device = stack.enter_context(BioCamDevice())
            # The stimulator is entered here rather than per-stimulus: its
            # lifecycle is Initialize -> Start -> Stop -> Close, and cycling
            # that for every pulse would be both slow and wrong. Note it
            # warns rather than refuses when the device is not yet streaming;
            # the recording starts moments later.
            stimulator = stack.enter_context(
                Stimulator(device, log=StimulusLog(), warn=warn))
        except BaseException:
            # Whatever was entered must come back out, or the instrument
            # stays claimed by a window that failed to open a session.
            stack.close()
            raise
        self._live_stack = stack
        return LiveFactory(
            output_path=output, duration_sec=duration,
            device=device, stimulator=stimulator, log=stimulator.log,
            warn=warn,
        )

    def _release_live(self):
        """Release the instrument. Safe to call when nothing is held."""
        stack, self._live_stack = self._live_stack, None
        if stack is None:
            return
        try:
            stack.close()
            self._log("Instrument released.", "ok")
        except Exception as exc:  # noqa: BLE001 - reported, never raised at a UI
            self._log(f"Releasing the instrument failed: {exc}", "bad")

    # -- polling ----------------------------------------------------------

    def _poll(self):
        try:
            for event in self.controller.drain_events():
                self._log(describe(event), self._severity(event))
            self._render(self.controller.snapshot())
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
            # The session is over, so the device goes back before anything
            # else. Held any longer and a second Start would find it claimed
            # by this same process.
            self._release_live()
            self.btn_start.configure(state="normal")
            self.btn_stop.configure(state="disabled")
            self._log(f"Finished: {state.stop_reason}, {state.frames:,} frames, "
                      f"verdict {state.verdict}",
                      "ok" if state.verdict == "clean" else "warn")
            self._refresh_stim_validity()

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
        if self.controller.running:
            self.controller.stop()
            self.controller.join(5.0)
        self._release_live()
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
