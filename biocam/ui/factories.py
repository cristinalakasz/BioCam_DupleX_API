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


@dataclass(frozen=True)
class _LoopPlan:
    """What a simulated closed-loop stimulus records as its pulse.

    A stand-in, and labelled as one: in simulation nothing is delivered, so
    there is no real pulse to describe. Every such record is marked
    `simulated` in the log, so it cannot be mistaken for a delivery.
    """

    net_charge_pc: float = 0.0

    def describe(self) -> str:
        return "closed-loop stimulus (simulated - nothing was delivered)"


@dataclass(frozen=True)
class _LoopPattern:
    positive: tuple = ()
    negative: tuple = ()


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
    # Detection and the closed loop. No channels means both are off, and
    # nothing extra runs on the acquisition thread at all - which is the
    # right default, since detection is affordable on a handful of channels
    # and unaffordable across the array.
    detect_channels: tuple = ()
    threshold_sigmas: float = 5.0
    # Off unless asked for. Collecting waveforms allocates per spike on
    # the acquisition thread and retains them until someone drains, and
    # the obvious headless script - record_session(loop=f.make_loop()) -
    # never drains. Only the UI, which does, turns this on.
    collect_waveforms: bool = False
    close_the_loop: bool = False
    policy_name: str = "echo"
    target_rate_hz: float = 1.0
    min_interval_ms: float = 20.0
    max_rate_hz: float = 10.0
    # Electrodes whose signal is drawn as a rolling trace. Empty means the
    # trace panel does no work at all on the acquisition thread.
    trace_channels: tuple = ()

    log: object = None
    pace_hz: float = None
    n_rows: int = 64
    n_cols: int = 64

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

    def make_loop(self):
        """Build the closed-loop runner, or None if detection is off.

        The detector watches only `detect_channels`. That is not a UI
        convenience: detection over the whole array costs about three times
        one core, and this runs on the thread that drains the packet queue.
        """
        if not self.detect_channels:
            return None
        from biocam.analysis.spikes import SpikeDetector
        from biocam.loop import (
            ClosedLoop, EchoPolicy, PacketLoop, RatePolicy, SafetyEnvelope,
        )

        rate = self.params.frame_rate_hz
        detector = SpikeDetector(
            len(self.detect_channels), rate,
            threshold_sigmas=self.threshold_sigmas,
            collect_waveforms=self.collect_waveforms,
        )
        if self.policy_name == "rate":
            policy = RatePolicy(rate, target_hz=self.target_rate_hz)
        else:
            policy = EchoPolicy()
        envelope = SafetyEnvelope(
            rate,
            min_interval_ms=self.min_interval_ms,
            max_rate_hz=self.max_rate_hz,
        )
        loop = ClosedLoop(
            detector, policy, envelope,
            send=self.send_loop_stimulus if self.close_the_loop else None,
        )
        # Paid here, before acquisition, rather than on the first packet -
        # see ClosedLoop.warm_up. Ten milliseconds there is about five
        # dropped packets at the start of every recording.
        loop.warm_up()
        return PacketLoop(loop, self.params, self.detect_channels)

    def send_loop_stimulus(self, trigger):
        """A simulated closed-loop stimulus: recorded, never delivered."""
        self.log.immediate(_LoopPlan(), _LoopPattern(), simulated=True)

    def make_monitor(self):
        from biocam.data.monitor import LiveMonitor

        return LiveMonitor(self.params, n_rows=self.n_rows, n_cols=self.n_cols)

    def make_traces(self):
        """Build the rolling trace window, or None if no electrode is chosen.

        None means nothing extra runs on the acquisition thread. Traces are
        not decimated in time - a trace with gaps lies about what the
        electrode did - so what keeps the cost down is watching few channels,
        which `TraceRecorder` enforces rather than trusts.
        """
        if not self.trace_channels:
            return None
        from biocam.data.traces import TraceRecorder

        return TraceRecorder(self.params, self.trace_channels)


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
    listener: object = None
    warn: object = None
    loop_plan: object = None
    loop_pattern: object = None
    n_rows: int = 64
    n_cols: int = 64
    # Detection and the closed loop. No channels means both are off, and
    # nothing extra runs on the acquisition thread at all - which is the
    # right default, since detection is affordable on a handful of channels
    # and unaffordable across the array.
    detect_channels: tuple = ()
    threshold_sigmas: float = 5.0
    # Off unless asked for. Collecting waveforms allocates per spike on
    # the acquisition thread and retains them until someone drains, and
    # the obvious headless script - record_session(loop=f.make_loop()) -
    # never drains. Only the UI, which does, turns this on.
    collect_waveforms: bool = False
    close_the_loop: bool = False
    policy_name: str = "echo"
    target_rate_hz: float = 1.0
    min_interval_ms: float = 20.0
    max_rate_hz: float = 10.0
    # Electrodes whose signal is drawn as a rolling trace. Empty means the
    # trace panel does no work at all on the acquisition thread.
    trace_channels: tuple = ()
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

        # From IBioCam.ClockCyclesToMilliseconds (XML:4667) - the device,
        # not the stimulator - so a recording-only session keeps it. Supplying
        # it is also what makes the clock's cross-check able to fail at all:
        # calibrating the factor from the same packets reduces the comparison
        # to an identity.
        from biocam.interop.device import cycles_per_us_of

        cycles_per_us = cycles_per_us_of(self.device)
        return AcquisitionClock(
            self.params.frame_rate_hz, cycles_per_us=cycles_per_us
        )

    def make_loop(self):
        """Build the closed-loop runner, or None if detection is off.

        The detector watches only `detect_channels`. That is not a UI
        convenience: detection over the whole array costs about three times
        one core, and this runs on the thread that drains the packet queue.
        """
        if not self.detect_channels:
            return None
        from biocam.analysis.spikes import SpikeDetector
        from biocam.loop import (
            ClosedLoop, EchoPolicy, PacketLoop, RatePolicy, SafetyEnvelope,
        )

        rate = self.params.frame_rate_hz
        detector = SpikeDetector(
            len(self.detect_channels), rate,
            threshold_sigmas=self.threshold_sigmas,
            collect_waveforms=self.collect_waveforms,
        )
        if self.policy_name == "rate":
            policy = RatePolicy(rate, target_hz=self.target_rate_hz)
        else:
            policy = EchoPolicy()
        envelope = SafetyEnvelope(
            rate,
            min_interval_ms=self.min_interval_ms,
            max_rate_hz=self.max_rate_hz,
        )
        loop = ClosedLoop(
            detector, policy, envelope,
            send=self.send_loop_stimulus if self.close_the_loop else None,
        )
        # Paid here, before acquisition, rather than on the first packet -
        # see ClosedLoop.warm_up. Ten milliseconds there is about five
        # dropped packets at the start of every recording.
        loop.warm_up()
        return PacketLoop(loop, self.params, self.detect_channels)

    def send_loop_stimulus(self, trigger):
        """Deliver a closed-loop stimulus. Runs on the acquisition thread.

        Raising here is caught and counted by ClosedLoop, which reports it
        rather than stopping the recording. The plan and pattern are fixed
        for the session: re-planning per spike would put pulse arithmetic on
        the latency path for no benefit, since the loop decides *whether* to
        stimulate, not *what with*.
        """
        if self.stimulator is None or self.loop_plan is None:
            raise RuntimeError(
                "the closed loop has no stimulus to send: either the "
                "stimulator is unavailable or no pulse was configured"
            )
        self.stimulator.send_now(self.loop_plan, self.loop_pattern)

    def make_monitor(self):
        from biocam.data.monitor import LiveMonitor

        return LiveMonitor(self.params, n_rows=self.n_rows, n_cols=self.n_cols)

    def make_traces(self):
        """Build the rolling trace window, or None if no electrode is chosen.

        None means nothing extra runs on the acquisition thread. Traces are
        not decimated in time - a trace with gaps lies about what the
        electrode did - so what keeps the cost down is watching few channels,
        which `TraceRecorder` enforces rather than trusts.
        """
        if not self.trace_channels:
            return None
        from biocam.data.traces import TraceRecorder

        return TraceRecorder(self.params, self.trace_channels)


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
        """The callable `record_session` runs the moment its loop ends.

        This - not `stop_source_safely` - is what runs first: `record_session`
        invokes it in its own `finally` (session.py), long before
        `SessionController._run`'s `finally` gets there. Putting the
        stimulator stop only in `stop_source_safely` therefore produced
        StopDataStreaming *then* Stop() on every ordinary session, the exact
        inversion of MainForm.cs:210 then :213.
        """
        def stop():
            if self.stimulator is not None:
                self.stimulator.stop()      # never raises; warns instead
            source.stop()

        return stop

    def stop_source_safely(self, source):
        """The safety net, for paths where `stop_source` never ran.

        Same order for the same reason. Both calls are idempotent: `stop()`
        returns immediately once `_started` is clear, and `source.stop()` is
        deliberately re-callable after a failure.
        """
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
        if self.stimulator is None:
            raise RuntimeError(
                "no stimulator: this session is recording only. The request "
                "was not delivered."
            )
        if request.scheduled:
            self.stimulator.send_scheduled(request.plan, request.pattern)
        else:
            self.stimulator.send_now(request.plan, request.pattern)
