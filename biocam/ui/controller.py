"""Layer 2 - owning the recording thread and the queues either side of it.

The UI cannot call into the recording loop and the recording loop cannot call
into the UI. Three separate threads are involved and each has a rule:

- **The driver's callback thread** puts packets in a bounded queue and returns.
  Untouched by anything here.
- **The consumer thread** drains that queue into the writer, and is the only
  thing standing between a full queue and silently dropped packets. It must
  never block. It is also where stimulation is dispatched, between packets -
  see `biocam.control`.
- **The UI thread** owns every widget. Tkinter is not thread-safe, so nothing
  off this thread may touch one.

So the two directions are queues, not calls:

    UI  --StimulationQueue-->  consumer thread
    UI  <---event ring-------  consumer thread

The event ring is bounded and drops the oldest on overflow, counting what it
dropped. That is the opposite choice from the packet queue, which drops the
newest - and deliberately: a packet dropped is data lost forever, while an
event dropped is one line of console history, and the *latest* state is what a
person watching needs. Dropping is never optional: a listener that blocks
because a UI stopped polling would stall the drain, which is the one failure
this whole arrangement exists to prevent.

Nothing here imports the driver or Tkinter, so all of it is testable.
"""

import threading
import time
from collections import deque
from dataclasses import dataclass, field

# Events kept for the UI to collect. Roughly a screenful of history; the UI
# polls several times a second, so reaching this means it has stopped polling
# rather than merely fallen behind.
EVENT_RING_CAPACITY = 512

# Waveforms kept for sorting. A sort needs a representative sample, not every
# spike ever seen, and this runs in the same process that is drawing the
# array. Past the cap the oldest are dropped and counted - the recording on
# disk still holds everything.
MAX_RETAINED_WAVEFORMS = 20_000


@dataclass(frozen=True)
class SessionSnapshot:
    """Everything the UI needs to render, taken atomically enough to be safe.

    Every field is a plain int, float, str or bool read from the controller,
    so a snapshot can never expose a half-built object to the UI thread.
    """

    running: bool = False
    finished: bool = False
    source_name: str = ""
    output_path: str = ""
    frames: int = 0
    elapsed_sec: float = 0.0
    acquisition_sec: float = 0.0
    clock_source: str = ""
    frames_missing: int = 0
    verdict: str = ""
    stop_reason: str = ""
    error: str = ""
    stimuli_delivered: int = 0
    stimuli_failed: int = 0
    stimuli_pending: int = 0
    stimulation_suspended: bool = False
    events_dropped: int = 0
    spikes_detected: int = 0
    spike_rate_hz: float = 0.0
    loop_stimuli: int = 0
    loop_refused: int = 0
    loop_suspended: bool = False
    warnings: tuple = ()

    @property
    def healthy(self) -> bool:
        return not self.error and not self.warnings


@dataclass
class _EventRing:
    """Bounded, thread-safe, drop-oldest. Written by the consumer thread."""

    capacity: int = EVENT_RING_CAPACITY
    _items: deque = field(default_factory=deque)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    dropped: int = 0

    def put(self, item) -> None:
        # Called from the consumer thread, so: no allocation beyond the
        # append, no blocking, and never an exception the caller has to handle
        # - RecordingWriter._emit guards listeners, but relying on that to
        # swallow our own bugs would be the wrong way round.
        with self._lock:
            if len(self._items) >= self.capacity:
                self._items.popleft()
                self.dropped += 1
            self._items.append(item)

    def drain(self) -> list:
        with self._lock:
            items = list(self._items)
            self._items.clear()
            return items


class _DrainingLoop:
    """A PacketLoop that also hands completed waveforms to the controller.

    Everything it adds after `observe` is bounded work on the consumer
    thread: one list splice per packet, against a capped deque.
    """

    def __init__(self, loop, on_packet):
        self._inner = loop
        self._on_packet = on_packet

    @property
    def loop(self):
        return self._inner.loop

    @property
    def channels(self):
        return self._inner.channels

    def observe(self, packet):
        decision = self._inner.observe(packet)
        self._on_packet()
        return decision

    def warnings(self):
        return self._inner.warnings()

    def summary(self):
        return self._inner.summary()


class SessionController:
    """Runs a recording on a worker thread and mediates between it and a UI.

    `start()` takes a factory rather than a source, because building one is
    the only step that differs between a replayed file and the instrument -
    and keeping that difference behind a callable is what lets the whole UI
    run, and be tested, with no BioCAM present.
    """

    def __init__(self, stim_queue=None, event_capacity: int = EVENT_RING_CAPACITY):
        from biocam.control import StimulationQueue

        self.stim_queue = stim_queue if stim_queue is not None else StimulationQueue()
        self._events = _EventRing(capacity=event_capacity)
        self._waveforms = deque()
        self._waveforms_dropped = 0
        self._thread = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._state = SessionSnapshot()
        self._started_at = None
        self._factory = None
        self._clock = None
        self._monitor = None
        self._loop = None

    # -- lifecycle -------------------------------------------------------

    @property
    def running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    def start(self, factory) -> None:
        """Begin recording. `factory` supplies the source, writer and clock.

        Returns as soon as the worker thread is running; everything after
        that is reported through `snapshot()` and `drain_events()`.
        """
        if self.running:
            raise RuntimeError(
                "a recording is already running. Stop it before starting "
                "another - two recordings would compete for the instrument "
                "and for the disk."
            )
        self._factory = factory
        self._stop.clear()
        self._events.drain()
        self._waveforms.clear()
        self._waveforms_dropped = 0
        self._started_at = time.perf_counter()
        with self._lock:
            self._state = SessionSnapshot(
                running=True,
                source_name=getattr(factory, "name", "unknown"),
                output_path=str(getattr(factory, "output_path", "")),
            )
        self._thread = threading.Thread(
            target=self._run, name="biocam-recording", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        """Ask the recording to finish. Returns immediately."""
        self._stop.set()

    def join(self, timeout: float = None) -> bool:
        """Wait for the recording thread. Returns whether it finished."""
        thread = self._thread
        if thread is None:
            return True
        thread.join(timeout)
        return not thread.is_alive()

    # -- the worker thread -----------------------------------------------

    def _run(self) -> None:
        from biocam.session import record_session

        factory = self._factory
        clock = None
        try:
            clock = factory.make_clock()
            # Held so snapshot() can read progress live from the UI thread.
            # AcquisitionClock is written only by the consumer thread and read
            # only here; its fields are plain scalars, so a read taken
            # mid-observe can be one packet stale but never torn. A lock would
            # be a lock the drain has to take.
            self._clock = clock
            # Built by the factory, because only it knows the acquisition
            # parameters and the array geometry.
            self._monitor = factory.make_monitor()
            # Optional, and built by the factory because only it knows the
            # acquisition parameters. None when the operator has not asked
            # for detection - in which case nothing extra runs on the
            # acquisition thread at all.
            self._loop = factory.make_loop()
            if self._loop is not None:
                # Waveforms are drained on the consumer thread, right after
                # the loop has seen the packet, so a sort can be run at any
                # moment without re-reading the recording. Wrapping rather
                # than editing PacketLoop keeps biocam.loop unaware that a UI
                # is watching.
                self._loop = _DrainingLoop(self._loop, self._drain_waveforms)
            with factory.make_writer(listener=self._events.put) as writer:
                source = factory.make_source()
                # start_source is INSIDE the try whose finally stops it.
                # It performs two driver calls now - StartDataStreaming and
                # then the stimulator's Start - and the second can raise. With
                # it outside, that raise skipped stop_source_safely entirely
                # and left the device streaming, handlers still subscribed,
                # with nothing holding a reference to the source. cli.py makes
                # exactly this fix for the same reason.
                try:
                    factory.start_source(source)
                    result = record_session(
                        source,
                        writer,
                        duration_sec=factory.duration_sec,
                        stop_event=self._stop,
                        counters=factory.counters(source),
                        stop_source=factory.stop_source(source),
                        clock=clock,
                        monitor=self._monitor,
                        loop=self._loop,
                        service=lambda: self.stim_queue.service(factory.send),
                    )
                finally:
                    # Mirrors cli.py's safety net: by now stop_source has
                    # normally already run, but an exception before that leaves
                    # the instrument streaming into a queue nothing drains.
                    factory.stop_source_safely(source)
                self._finish(result, writer, clock)
        except BaseException as exc:  # noqa: BLE001 - reported, never swallowed
            # The UI is the only place this can surface. A worker thread that
            # dies silently leaves a window saying "recording" forever, which
            # is worse than any error message.
            with self._lock:
                self._state = self._replace(
                    running=False, finished=True,
                    error=f"{type(exc).__name__}: {exc}",
                    warnings=self._collect_warnings(clock),
                )
        finally:
            self._started_at = None

    def _finish(self, result, writer, clock) -> None:
        # The clock's final reading is captured here rather than left to
        # snapshot(), which only reads it while running. Without this a
        # finished recording reported "acquisition time 0.000 s" - a specific
        # and wrong claim, and exactly the number an operator would write down
        # before scheduling anything against it.
        final = self._live_progress()
        with self._lock:
            self._state = self._replace(
                running=False,
                finished=True,
                frames=result.n_frames,
                verdict=result.verdict,
                stop_reason=result.stop_reason,
                output_path=str(result.raw_path),
                frames_missing=writer.n_frames_missing,
                acquisition_sec=final.get("acquisition_sec"),
                clock_source=final.get("clock_source"),
                elapsed_sec=(time.perf_counter() - self._started_at
                             if self._started_at else None),
                warnings=self._collect_warnings(clock),
            )

    # -- for the UI thread -----------------------------------------------

    def spikes_with_waveforms(self) -> list:
        """Every spike whose shape has been collected. UI thread only.

        Drained from the detector as the recording runs and kept here, so a
        sort can be run at any point without re-reading the recording. The
        list is capped: sorting needs a sample of waveforms, not all of them,
        and a long session on a busy culture would otherwise grow without
        limit on the one machine that also has to keep drawing.
        """
        return list(self._waveforms)

    def watched_channels(self) -> list:
        """The electrode numbers detection is watching, in detector order."""
        loop = self._loop
        if loop is None:
            return []
        return [int(c) for c in loop.channels]

    def activity(self):
        """The latest picture of the array, or None. UI thread only."""
        monitor = self._monitor
        if monitor is None:
            return None
        return monitor.snapshot()

    @property
    def listener(self):
        """The callable a writer or source should emit events to.

        Public because the factory needs it: reaching into `_events` from
        outside was the only private access left on this path.
        """
        return self._events.put

    def drain_events(self) -> list:
        """Take every event since the last call. UI thread only."""
        return self._events.drain()

    def snapshot(self) -> SessionSnapshot:
        """The current state. Safe to call from the UI thread at any rate."""
        with self._lock:
            state = self._state
        if not state.running:
            return self._replace_on(state, events_dropped=self._events.dropped,
                                    **self._stim_counts(),
                                    **self._loop_counts())
        started = self._started_at
        return self._replace_on(
            state,
            elapsed_sec=(time.perf_counter() - started) if started else 0.0,
            events_dropped=self._events.dropped,
            **self._live_progress(),
            **self._stim_counts(),
            **self._loop_counts(),
        )

    def request_stimulus(self, plan, pattern, *, label: str = "") -> bool:
        """Ask for a stimulus. Returns False if it was not accepted.

        Never blocks - the UI thread must stay responsive, and a control
        thread must never be able to make the acquisition thread's work
        unbounded. A False here means the queue was full; the count is in the
        snapshot.
        """
        return self.stim_queue.request(plan, pattern, label=label)

    # -- internals -------------------------------------------------------

    def _live_progress(self) -> dict:
        clock = getattr(self, "_clock", None)
        if clock is None:
            return {}
        try:
            reading = clock.read()
        except Exception:  # noqa: BLE001 - a progress line is not worth raising
            return {}
        return {
            "acquisition_sec": reading.acquisition_us / 1e6,
            "clock_source": reading.source,
            "frames": reading.frames_seen,
            "frames_missing": reading.frames_lost,
        }

    def _drain_waveforms(self) -> None:
        """Move completed waveforms out of the detector. Consumer thread.

        Called from the packet loop via the factory's loop hook, so it must
        stay cheap and must not grow: past the cap, new waveforms replace the
        oldest rather than accumulating. A sort wants a representative sample,
        and the recording on disk remains the complete record either way.
        """
        loop = self._loop
        if loop is None:
            return
        for spike in loop.loop.detector.take_waveforms():
            if len(self._waveforms) >= MAX_RETAINED_WAVEFORMS:
                self._waveforms.popleft()
                self._waveforms_dropped += 1
            self._waveforms.append(spike)

    def _loop_counts(self) -> dict:
        loop = self._loop
        if loop is None:
            return {}
        inner = loop.loop
        elapsed = max(inner.detector._frames_seen, 1) / inner.detector.frame_rate_hz
        return {
            "spikes_detected": inner.spikes_seen,
            "spike_rate_hz": inner.spikes_seen / elapsed,
            "loop_stimuli": inner.stimuli_sent,
            "loop_refused": inner.envelope.refused,
            "loop_suspended": inner.suspended,
        }

    def _stim_counts(self) -> dict:
        queue = self.stim_queue
        return {
            "stimuli_delivered": queue.dispatched,
            "stimuli_failed": queue.failed + queue.dropped + queue.stale,
            "stimuli_pending": len(queue),
            "stimulation_suspended": queue.suspended,
        }

    def _collect_warnings(self, clock) -> tuple:
        problems = []
        if clock is not None:
            try:
                problems.extend(clock.warnings())
            except Exception:  # noqa: BLE001
                pass
        if self._monitor is not None:
            problems.extend(self._monitor.warnings())
        if self._loop is not None:
            problems.extend(self._loop.warnings())
        if self._waveforms_dropped:
            problems.append(
                f"{self._waveforms_dropped} spike waveform(s) were dropped to "
                f"keep the sorting sample at {MAX_RETAINED_WAVEFORMS}. Sorting "
                "used a sample rather than everything; the recording on disk "
                "is unaffected and holds every spike."
            )
        problems.extend(self.stim_queue.warnings())
        if self._events.dropped:
            problems.append(
                f"{self._events.dropped} status message(s) were dropped "
                "because the window was not collecting them. The recording "
                "itself was unaffected - the sidecar is the record."
            )
        return tuple(problems)

    def _replace(self, **fields) -> SessionSnapshot:
        return self._replace_on(self._state, **fields)

    @staticmethod
    def _replace_on(state: SessionSnapshot, **fields) -> SessionSnapshot:
        from dataclasses import replace

        return replace(state, **{k: v for k, v in fields.items() if v is not None})
