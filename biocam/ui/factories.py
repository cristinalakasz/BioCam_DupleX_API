"""Layer 2 - the one thing that differs between a replay and the instrument.

`SessionController` needs a source, a writer, a clock and a way to send a
stimulus. Everything else about running a session is identical whether the
packets come from a file or from a BioCAM, so that difference is confined to
these two classes.

`ReplayFactory` needs no driver, no DLLs and no instrument. That is what makes
the whole window openable and testable on a development machine, and it is
what lets the colleague learn the controls before spending instrument time on
them. It is not a mock: it drives the real `RecordingWriter`, the real
`record_session`, the real `AcquisitionClock` and the real gap tracking. Only
the packets and the stimulator are stand-ins.

`LiveFactory` imports `biocam.interop` lazily, inside methods, so importing
this module never pulls `clr` into a process that has no 3Brain assemblies.
"""

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ReplayFactory:
    """Drives a session from a `.raw` file, or from synthetic packets.

    Stimulation is *simulated*: requests are validated and logged exactly as
    they would be, and then not sent anywhere. Each record is marked
    `simulated` so that a simulated log cannot be mistaken for a real one -
    a claim that was in this docstring before it was true, and is now checked
    by a test.
    """

    raw_path: Path
    params: object
    output_path: Path
    duration_sec: float = None
    frames_per_packet: int = 200
    drop_packets: tuple = ()
    name: str = "replay (no instrument)"
    log: object = None
    pace_hz: float = None

    def __post_init__(self):
        from biocam.stim import StimulusLog

        self.raw_path = Path(self.raw_path)
        self.output_path = Path(self.output_path)
        if self.log is None:
            self.log = StimulusLog()

    @property
    def is_live(self) -> bool:
        return False

    @property
    def meta_path(self) -> Path:
        return self.output_path.with_name(self.output_path.stem + "_meta.json")

    def make_clock(self):
        from biocam.data.clock import AcquisitionClock

        return AcquisitionClock(self.params.frame_rate_hz)

    def make_writer(self, listener=None):
        from biocam.data.recording import RecordingWriter

        return RecordingWriter(
            self.output_path, self.meta_path, self.params, listener=listener
        )

    def make_source(self):
        from biocam.data.replay import ReplayPacketSource

        source = ReplayPacketSource(
            self.raw_path,
            self.params,
            frames_per_packet=self.frames_per_packet,
            drop_packets=self.drop_packets,
        )
        if self.pace_hz:
            return _Paced(source, self.pace_hz)
        return source

    # A replay has nothing to start, stop or count.
    def start_source(self, source):
        return None

    def counters(self, source):
        return None

    def stop_source(self, source):
        return None

    def stop_source_safely(self, source):
        return None

    def send(self, request) -> None:
        """Pretend to stimulate, and record that it was a pretence.

        The plan and pattern have already been validated by whoever built
        them, so what this exercises is the whole path either side of the
        driver call: the queue, the dispatch timing, the log, the UI counters.

        `scheduled` is honoured, so a simulated log distinguishes the two the
        way a live one does. Without it, a scheduled request would be recorded
        as a delivered immediate pulse.
        """
        if request.scheduled:
            self.log.scheduled(request.plan, request.pattern, simulated=True)
        else:
            self.log.immediate(request.plan, request.pattern, simulated=True)


@dataclass
class _Paced:
    """Yields a source's packets at roughly a real acquisition rate.

    A replay otherwise runs as fast as the disk allows, which makes a
    ten-second recording finish instantly - fine for tests, useless for
    someone learning what the window does. The delay is deliberately crude:
    this is a teaching aid, not a simulator, and pretending to reproduce the
    instrument's timing would be a worse lie than obviously not doing so.
    """

    source: object
    hz: float

    def __iter__(self):
        import time

        period = 1.0 / self.hz
        next_at = time.perf_counter()
        for packet in self.source:
            next_at += period
            delay = next_at - time.perf_counter()
            if delay > 0:
                time.sleep(delay)
            yield packet


@dataclass
class LiveFactory:
    """Drives a session from the instrument.

    Every import of `biocam.interop` happens inside a method, so importing
    this module on a machine with no 3Brain DLLs is harmless.

    **Untested.** Nothing in this class has run against a BioCAM.
    """

    output_path: Path
    duration_sec: float = None
    packet_ms: int = 2
    name: str = "BioCAM DupleX"
    device: object = None
    stimulator: object = None
    log: object = None
    grid: object = None
    listener: object = None
    warn: object = None
    _params: object = field(default=None, init=False)

    def __post_init__(self):
        from biocam.stim import StimulusLog

        self.output_path = Path(self.output_path)
        if self.log is None:
            self.log = StimulusLog()

    @property
    def is_live(self) -> bool:
        return True

    @property
    def meta_path(self) -> Path:
        return self.output_path.with_name(self.output_path.stem + "_meta.json")

    @property
    def params(self):
        """Acquisition parameters, with the diagnostic the CLI prints.

        Five of the members read here appear nowhere in
        `3Brain.BioCamDriver.xml`. `_probe_data_format` reads each one
        separately so that a missing member is reported by name rather than
        surfacing as whichever `AttributeError` happens to be hit first -
        which, 600 km away, is the difference between a fixable report and an
        unusable one.
        """
        if self._params is None:
            from biocam.cli import _parameters_from, _probe_data_format

            data_format = self.device.data_format
            if self.warn:
                for line in _probe_data_format(data_format):
                    self.warn(f"DataFormat: {line}")
            self._params = _parameters_from(data_format)
        return self._params

    def make_clock(self):
        from biocam.data.clock import AcquisitionClock

        cycles_per_us = None
        if self.stimulator is not None:
            # The authoritative factor, from IBioCam.ClockCyclesToMilliseconds.
            # Supplying it is also what makes the clock's cross-check able to
            # fail at all - calibrating it from the same packets reduces the
            # comparison to an identity.
            cycles_per_us = self.stimulator.cycles_per_us
        return AcquisitionClock(
            self.params.frame_rate_hz, cycles_per_us=cycles_per_us
        )

    def make_writer(self, listener=None):
        from biocam.data.recording import RecordingWriter

        return RecordingWriter(
            self.output_path, self.meta_path, self.params, listener=listener
        )

    def make_source(self):
        """Build the driver source, sized the way the CLI sizes it.

        `DriverPacketSource`'s own default is 2000 packets regardless of the
        acquisition period, which at the full DupleX configuration is several
        seconds of buffering and well past the 512 MiB ceiling `cli.py`
        computes for exactly this reason. The listener matters too: without
        it `QueuePressure` - the warning that precedes an overflow - is
        emitted to nothing, so the window would see the loss but not the
        approach to it.
        """
        from biocam.cli import _bytes_per_packet, _queue_size_for
        from biocam.interop.source import DriverPacketSource

        queue_size = _queue_size_for(
            self.packet_ms, _bytes_per_packet(self.params, self.packet_ms)
        )
        return DriverPacketSource(
            self.device, queue_size=queue_size, listener=self.listener
        )

    def start_source(self, source):
        # Streaming first, then the stimulator - MainForm.cs:186 then :192.
        # The reverse is what the UI used to do, by holding the whole
        # stimulator lifecycle open across the session.
        source.start(packet_timespan_ms=self.packet_ms)
        if self.stimulator is not None:
            self.stimulator.start()

    def counters(self, source):
        return source

    def stop_source(self, source):
        return source.stop

    def stop_source_safely(self, source):
        # Stimulator first, then streaming - MainForm.cs:210 then :213.
        if self.stimulator is not None:
            self.stimulator.stop()          # never raises; warns instead
        try:
            source.stop()
        except Exception as exc:  # noqa: BLE001 - must not mask the real failure
            if self.warn:
                self.warn(f"source.stop() failed during cleanup: {exc}")

    def send(self, request) -> None:
        """Deliver one stimulus. Runs on the consumer thread.

        Raising here is caught and counted by `StimulationQueue.service`; the
        stimulator's own log records why.
        """
        if request.scheduled:
            self.stimulator.send_scheduled(request.plan, request.pattern)
        else:
            self.stimulator.send_now(request.plan, request.pattern)
