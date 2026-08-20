"""Layer 1 - packets from the instrument.

The callback does four things and returns: read the header, copy the payload,
hand it off, return. Nothing else. No file I/O, no printing, no allocation
beyond the copy, no locks, and no operation that can block.

Handoff uses a `collections.deque`, not `queue.Queue`. `on_data` (the
DataReceived thread) is the sole producer of real packets, and `__iter__` is
the sole consumer, so that traffic is genuinely single-producer/single-
consumer: CPython's `append`/`popleft` are each a single atomic C operation
under the GIL - no mutex, no condition variable, nothing that can be
preempted mid-hold. `queue.Queue` takes its internal mutex on every
`put_nowait`/`get`/`qsize` call; if Windows preempts the consumer while it
holds that mutex, the callback blocks for a scheduler quantum (up to ~15 ms
on Windows) instead of microseconds.

The STOP sentinel is the one exception to single-producer: it can be
appended by `on_error` (its own driver thread) or by `stop()` (the caller's
thread), each at most once per session, never both in the steady state a
session is designed for. Each of those appends is still individually atomic,
so it never corrupts the deque; what it does *not* guarantee is that its
`len()` capacity check stays consistent with a concurrent check-then-append
happening in `on_data` at the same instant. In that narrow, rare window the
deque can briefly hold one more item than `queue_size` - a bounded, harmless
overshoot (an extra sentinel, not lost or corrupted data), not a lock and not
a crash. It is accepted rather than closed with a lock, because closing it
would mean taking a lock on `on_data`'s hot path to stay consistent with the
rare paths that also touch the deque.

The deque is deliberately unbounded as a structure - capacity is enforced by
checking `len()` before every `append()`, so a full queue drops the *new*
packet and counts it. It must never be a `deque(maxlen=N)`: that silently
evicts the *oldest* entry instead, discarding already-acquired data without
counting it - the reverse of the required behaviour.

Note also that `on_error` sets the stop flag and enqueues STOP without
unsubscribing `on_data` (only `stop()` does that). If DataReceived keeps
firing between an error and the eventual `stop()` call, those packets are
appended after STOP - previously they were never yielded, because `__iter__`
returned the instant it popped STOP, orphaning anything behind it. `__iter__`
now treats STOP as a marker to skip, not a signal to return: it keeps
draining whatever the driver appended after the sentinel, and relies on the
same termination condition it already used when the sentinel is dropped for
being full (see `_try_enqueue_stop`) - queue empty *and* `_stop_event` set.
That pairing always holds when STOP is present, because every caller that
enqueues STOP (`on_error`, `stop()`) sets `_stop_event` first, so this does
not weaken termination; it only stops a sentinel from hiding real,
already-acquired packets behind it. Closing this the other way -
unsubscribing from inside `on_error` - would mean modifying the driver's
event subscription list from within a live driver-raised callback, which is
new interop behaviour this environment cannot verify, so it was not chosen.

A `threading.Event` is still used as the stop flag - `set()`/`is_set()` are
non-waiting and, being uncontended and short in this single-producer use,
do not block the driver's event thread in practice, even though `set()`
itself takes the Event's internal condition lock.

If the queue is full the packet is dropped and counted. Blocking the callback
would stall the driver and lose more than the packet being saved. If the
callback itself raises - `bytes(None)` is a `TypeError`, and the XML documents
a null payload as possible on `BioCamUsbBase.ProcessPayload`'s `data`
parameter for a chunk where nothing was retrieved (it says nothing about
`DataPacketReceivedEventArgs.Payload` itself, so treating that event args
payload as nullable too is our own defensive inference, not documented
behaviour) - the exception is caught and counted rather than allowed to cross
back into the .NET dispatcher, whose behavior on a Python exception is
undocumented and cannot be tested here.

`on_data`, `on_loss`, and `on_error` are assumed here to each run on their
own driver-raised thread. The XML documents this explicitly only for
`DataLossAsync` (as asynchronous); it says nothing about the threading model
of `DataReceived` or `DataStreamingError`, so treating those two as running
on separate threads as well is our own inference, not documented behaviour -
untested here and worth confirming in the lab. Using a separate counter per
handler is correct regardless of whether that inference holds: even if two
of the three turned out to share a thread, `+=` on a shared attribute would
still not be atomic (load-add-store), so keeping the counters apart costs
nothing and loses nothing either way. `callback_errors` sums the three when
read, which is safe because summing ints is not a read-modify-write of any
of the counters themselves.

`start()` also tightens `sys.setswitchinterval` and freezes the current
object graph into gc's permanent generation; `stop()` restores the switch
interval and unfreezes.

HIGH 2/HIGH 3 (Gate 1 final pass): earlier revisions of this module also
disabled *automatic* cyclic collection for the duration of the stream and
ran a manual `gc.collect(1)` from `__iter__` every GC_COLLECT_INTERVAL_
PACKETS, defended at the time by the argument that the manual pass ran on
the consumer thread and therefore never the driver's. That argument is
wrong: `gc.collect()` holds the GIL for the entire traversal it performs,
and the driver's callback thread cannot execute a single bytecode without
the GIL regardless of which Python thread is holding it. Measured on this
codebase: `gc.collect(1)` took 31.8 ms and stalled an unrelated thread for
32.8 ms - at a 1 ms acquisition period that is 32 packets lost every time it
fired, from a mechanism that was supposed to be protecting the callback, not
starving it. Worse, `gc.collect(1)` promotes survivors into generation 2,
and with automatic collection disabled for the whole run generation 2 was
never collected either - so the fix intended to stop reference cycles from
accumulating instead made any cycle that survived one manual pass permanent
for the session, exactly the outcome it was supposed to prevent.

The fix is to do less: `gc.freeze()` is kept, because it is the genuinely
valuable part - it moves everything alive at `start()` out of every future
traversal, which is what keeps a generation-0 collection cheap for the rest
of the run. Automatic collection is simply left enabled. With startup
objects frozen, an automatic generation-0 pass only scans what the run
itself allocated - short, because the hot path allocates one Packet plus one
bytes copy per call and creates no cycles of its own - and generations 1 and
2 still run on their own normal, automatic schedule, so nothing accumulates
in either of them for the length of a session. A manual pause that measured
worse than the automatic passes it replaced cannot be made safe by choosing
which thread calls it; the only fix is not to call it. `gc.unfreeze()` stays
paired with `gc.freeze()` in `stop()`, so the two remain symmetric - see
`_restore_runtime_tuning` below.

Because "we believe pythonnet does not leak cycles" is exactly the kind of
claim this codebase should not make on faith, `start()` and `stop()` each
record `gc.get_count()` (the per-generation allocation counts) and
`len(gc.get_objects())` (the tracked object total) as `gc_counts_at_start`/
`gc_objects_at_start` and `gc_counts_at_stop`/`gc_objects_at_stop`. cli.py
prints the delta in its end-of-run summary, so a real multi-hour lab run
produces a number to report instead of an assumption to repeat.
"""

import collections
import gc
import sys
import time
import threading

from biocam.data.events import QueuePressure
from biocam.data.replay import Packet

STOP = object()

PRESSURE_FRACTION = 0.8

# How long __iter__ sleeps on an empty queue before re-checking the stop
# flag. Paid only while idle, waiting for the next packet - never inside a
# driver callback - so it does not compete with the 1 ms packet budget.
# HIGH: this was 0.1 s. A packet arriving just after the consumer starts
# sleeping waited nearly 100 ms to be picked up - bursty even for recording,
# and fatal against the ~1.5 ms closed-loop target a later phase plans. 1 ms
# trades idle CPU (more frequent wake-ups while the queue is genuinely
# empty) for that latency; it must not be closed instead by having a
# callback signal a threading.Event - that would put a wait/notify
# primitive back on the driver's thread, exactly what this module exists to
# avoid (see the module docstring above on why a deque, not queue.Queue).
POLL_INTERVAL_SEC = 0.001

# sys.getswitchinterval() defaults to 0.005 s - five packet periods at a 1 ms
# acquisition rate. The callback runs on a .NET thread and must acquire the
# GIL to run at all; if the consumer happens to be mid-bytecode-run when a
# packet arrives, the callback can wait up to a full switch interval for the
# GIL. Tightened for the duration of the stream, in start()/stop(), rather
# than left as a global default that would affect unrelated code.
GIL_SWITCH_INTERVAL_SEC = 0.0005


class DriverPacketSource:
    """Turns the driver's DataReceived event into an iterable of packets."""

    def __init__(self, device, queue_size: int = 2000, listener=None):
        self._device = device
        # Unbounded deque; queue_size is enforced explicitly (see module
        # docstring) rather than via deque(maxlen=...), which would evict
        # silently instead of dropping-and-counting the new packet.
        self._queue = collections.deque()
        self._queue_size = queue_size
        self._listener = listener
        self._pressure_reported = False
        self._handler = None
        self._loss_handler = None
        self._error_handler = None
        self._stop_event = threading.Event()
        self.queue_overflows = 0
        # driver_loss_events is exposed as a property below - see FIX 2 in
        # the module docstring. _driver_loss_counter holds the driver's own
        # cumulative DataLossEventArgs.Counter (XML: "the number of data
        # losses since the start of the acquisition"); _loss_fallback_events
        # is the event-count fallback used only if Counter is ever
        # unreadable, which is not documented to happen but is guarded
        # against anyway.
        self._driver_loss_counter = None
        self._loss_fallback_events = 0
        # One private error counter per handler thread - see module
        # docstring on why these cannot share a counter.
        self._data_errors = 0
        # Packets whose header disagreed with the payload actually
        # delivered. See on_data: a mismatch desynchronises every later
        # frame, silently, so it is counted rather than assumed absent.
        self._payload_length_mismatches = 0
        self._last_payload_mismatch = None
        self._loss_errors = 0
        self._error_errors = 0
        self._streaming = False
        # MEDIUM: set True immediately before the StartDataStreaming() call
        # in start() - before we know whether it will succeed, raise before
        # engaging, or raise after engaging (the XML documents a separate
        # AssertCanStartStreaming that can throw; whether that happens
        # before or after the hardware is told to start is unanswerable
        # from documentation here - issue #18). `_streaming` alone is only
        # set once StartDataStreaming has already returned, so a start()
        # that raises leaves it False even if the device is now streaming.
        # stop() checks `_streaming or _maybe_streaming` for exactly that
        # reason: attempting StopDataStreaming on a device that was never
        # started is harmless (see stop()); never attempting it on one that
        # may actually be streaming is not.
        self._maybe_streaming = False
        # Set in start(), consumed and cleared in stop()/_restore_runtime_
        # tuning(). None means "nothing to restore" - covers stop() being
        # called without a preceding successful start().
        self._prev_switch_interval = None
        # True once _apply_runtime_tuning() has run for the current
        # start()/stop() cycle - guards _restore_runtime_tuning()'s
        # gc.unfreeze() the same way _prev_switch_interval guards the switch
        # interval restore: so it runs exactly once per successful
        # _apply_runtime_tuning(), and not at all if start() never got that
        # far.
        self._tuning_applied = False
        # HIGH 2/HIGH 3: a measurement, not an assumption - see the module
        # docstring. Captured in start()/stop() respectively; None until the
        # corresponding call has run.
        self.gc_counts_at_start = None
        self.gc_objects_at_start = None
        self.gc_counts_at_stop = None
        self.gc_objects_at_stop = None

    @property
    def stopped(self) -> bool:
        """Whether this source's own stop flag is set.

        Distinct from a caller-supplied stop_event (e.g. cli.py's, tied to
        KeyboardInterrupt): this reflects only `_stop_event`, which this
        class sets itself - from on_error (a driver-reported streaming
        error) or from stop(). record_session's normal (non-drain) loop
        checks this on every packet so a DataStreamingError still ends the
        session promptly even if the driver keeps firing DataReceived
        afterwards - the on_error race documented in the module docstring
        above. Before FIX 3, __iter__ returning the instant it popped STOP
        gave the consumer loop the same effect as a side effect; making
        __iter__ drain past STOP instead (so those later packets are not
        orphaned) removed that side effect, so this property exists to
        restore it deliberately instead of leaving it to chance.
        """
        return self._stop_event.is_set()

    @property
    def driver_loss_events(self) -> int:
        """The driver's cumulative data-loss count when available.

        `DataLossEventArgs.Counter` is documented as cumulative since the
        start of acquisition, so the latest successfully-read value is the
        correct answer - it is stored, not accumulated. Once a Counter has
        been read successfully at least once (`_driver_loss_counter` is no
        longer None), this property reports it and keeps reporting it for
        the rest of the session, even if a later DataLossAsync event's
        Counter is unreadable - that later failure still increments
        `_loss_fallback_events` internally, but no longer changes what this
        property returns, on the reasoning that a cumulative driver-
        reported value seen earlier in the run is a better answer than a
        locally-counted approximation. The event-count fallback is only
        ever what this property reports if Counter has never once been
        successfully read on this instance; it can itself undercount,
        because DataLossAsync is documented as firing asynchronously and
        events may coalesce.
        """
        if self._driver_loss_counter is not None:
            return self._driver_loss_counter
        return self._loss_fallback_events

    def pending_count(self) -> int:
        """Drain the queue and count the real packets found in it.

        A method, not a plain snapshot property, because it consumes the
        queue - popping one item at a time with `popleft()`, not scanning it
        with a persistent iterator. That distinction matters: CPython's
        `deque` invalidates a live iterator (raising `RuntimeError`) if the
        deque is mutated while that iterator is in progress, and this class
        cannot rule out a driver thread still being mid-append when this
        runs - see the "unverified" note on `_unsubscribe`'s effect on an
        in-flight callback in the clear() comment inside start() below.
        `popleft()` in a
        loop has no such iterator to invalidate: each call is the same
        single atomic operation `on_data` already relies on, so a
        concurrent append can race the count by at most one item (the same
        bounded, accepted imprecision as the STOP-overshoot documented in
        the module docstring) instead of raising.

        Meant to be called once, at the very end of a session's life
        (record_session's `finally`, after stop_source() has run) to get an
        honest final count of what is being abandoned - not as a repeatable
        peek at queue depth, since it empties the queue as a side effect.
        Used to count what a drain pass abandons at its deadline (FIX 1) and
        what leaked into the queue in the window before stop() actually took
        effect (FIX 2).
        """
        count = 0
        while True:
            try:
                item = self._queue.popleft()
            except IndexError:
                return count
            if item is not STOP:
                count += 1

    @property
    def last_payload_mismatch(self):
        """(declared, actual) for the most recent mismatch, or None.

        The direction and size, not just the count - which is what would say
        whether PayloadLength is in bytes or in something else. Written and
        never read is how a diagnostic becomes decoration.
        """
        return self._last_payload_mismatch

    def _note_payload_length(self, declared: int, actual: int) -> None:
        """Record one header-versus-payload comparison. Callback thread.

        A method rather than four lines inside the callback closure so that it
        can be tested: the closure is built inside `start()` and is reachable
        only with a live driver, which meant the first test written for this
        reimplemented the logic in its own body and passed against anything.

        Allocates at most once per session, deliberately. If the unit is wrong
        then EVERY packet mismatches - which is precisely the case this exists
        to detect - and building a fresh tuple a thousand times a second on
        the driver's callback thread would break the no-allocation property of
        the hot path at exactly the moment it matters. The first sample
        answers "is it bytes?" as well as the millionth.
        """
        if declared == actual:
            return
        self._payload_length_mismatches += 1
        if self._last_payload_mismatch is None:
            self._last_payload_mismatch = (declared, actual)

    @property
    def payload_length_mismatches(self) -> int:
        """Packets whose DataPacketHeader.PayloadLength disagreed with the
        payload actually delivered.

        Should be zero. Anything else means the frame alignment assumption
        this whole format rests on did not hold for that packet, and the
        recording past it is suspect in a way that looks like signal.
        """
        return self._payload_length_mismatches

    @property
    def callback_errors(self) -> int:
        """Sum of the three handlers' private error counters.

        Each handler runs on its own driver thread and increments only its
        own counter, so this sum is the only point where the three values
        are combined - see the module docstring.
        """
        return self._data_errors + self._loss_errors + self._error_errors

    def _unsubscribe(self, biocam):
        """Detach every handler this instance may have subscribed.

        Safe to call any number of times, including before any start() call:
        a handler that was never subscribed is skipped (still None), and a
        driver-side failure to detach one handler does not stop the others
        from being tried. Used both to clear a stale subscription from a
        previous start() before subscribing again, and to unwind a start()
        that failed partway through.
        """
        if self._handler is not None:
            try:
                biocam.DataReceived -= self._handler
            except Exception:
                pass
        if self._loss_handler is not None:
            try:
                biocam.DataLossAsync -= self._loss_handler
            except Exception:
                pass
        if self._error_handler is not None:
            try:
                biocam.DataStreamingError -= self._error_handler
            except Exception:
                pass

    def _try_enqueue_stop(self) -> None:
        """Push the STOP sentinel unless the queue is already full.

        A full queue must never make the caller (on_error or stop()) wait
        or raise: the consumer will notice via _stop_event on its next
        poll, so the sentinel is a fast path, not the only way out.

        Called from on_error's thread or from stop()'s caller thread, never
        both at once in the session lifecycle this class is designed for.
        Racing this check-then-append against on_data's own check-then-
        append can, in a narrow window, let the deque briefly exceed
        queue_size by one item - see the module docstring. That overshoot
        is bounded and harmless; it is not fixed here because doing so
        would mean a lock on on_data's hot path to stay consistent with
        this rare one.
        """
        if len(self._queue) < self._queue_size:
            self._queue.append(STOP)

    def _apply_runtime_tuning(self) -> None:
        """Tighten the GIL switch interval and freeze GC's current graph.

        Both changes are about keeping work off the driver's callback
        thread for the duration of the stream:

        - A tighter switch interval (FIX 5) shortens the longest the
          driver thread can wait to acquire the GIL from the consumer.
        - gc.freeze() moves everything currently tracked into the
          permanent generation so it is never rescanned by a later
          collection, which keeps every future generation-0 pass scoped to
          only what this run itself allocates.

        HIGH 2/HIGH 3 (Gate 1 final pass): earlier revisions also called
        gc.disable() here and ran a manual gc.collect(1) from __iter__,
        defended as safe because the manual pass ran on the consumer thread
        rather than the driver's. That reasoning does not hold: gc.collect()
        holds the GIL for its entire traversal, so the driver's callback
        cannot run regardless of which thread called collect() - measured at
        31.8 ms per call, stalling an unrelated thread for 32.8 ms, which at
        a 1 ms acquisition period is 32 dropped packets every time it fired.
        Disabling automatic collection made it worse, not better:
        gc.collect(1) promotes survivors into generation 2, and with
        automatic collection off, generation 2 was never collected - so a
        cycle surviving one manual pass became permanent for the session,
        the opposite of what the fix was meant to prevent.

        Automatic collection is therefore left enabled. With the startup
        object graph frozen, an automatic generation-0 pass only scans what
        this run has allocated since - short, because the hot path creates
        one Packet plus one bytes copy per call and no cycles of its own -
        while generations 1 and 2 keep running on their own normal schedule,
        so nothing accumulates in either one for the length of a session. A
        32 ms manual pause was worse than the automatic passes it replaced,
        and no choice of thread can fix that; not calling it is the fix.

        HIGH: gc.freeze() must still be paired with gc.unfreeze() in
        _restore_runtime_tuning() below, or the permanent generation only
        ever grows - measured at 377 -> 20,658 objects after a single
        start/stop cycle on this codebase, larger every cycle after,
        because a second freeze() moves whatever is newly tracked into a
        permanent generation that nothing ever moves back out of.

        The switch interval is restored unconditionally in
        _restore_runtime_tuning(); gc.unfreeze() is called there too,
        guarded by `_tuning_applied` rather than by anything gc.disable()-
        related, since there is no longer an enabled/disabled state to save.
        """
        self._prev_switch_interval = sys.getswitchinterval()
        sys.setswitchinterval(GIL_SWITCH_INTERVAL_SEC)
        gc.freeze()
        self._tuning_applied = True
        # HIGH 2/HIGH 3: measured, not assumed - see the module docstring.
        # Captured last, after freeze(), so the "start" snapshot reflects
        # the state this session actually begins execution in.
        self.gc_counts_at_start = gc.get_count()
        self.gc_objects_at_start = len(gc.get_objects())

    def _restore_runtime_tuning(self) -> None:
        """Undo _apply_runtime_tuning(), restoring the previous state.

        Called from stop()'s `finally` (and from start()'s failure path)
        so it runs even if stop() itself fails or start() never completes -
        matching the unconditional-cleanup pattern already used for handler
        unsubscription. A guard value of None/False means start() never ran
        (or this was already restored), so there is nothing to undo.

        HIGH: gc.unfreeze() is the fix - it moves everything gc.freeze()
        put into the permanent generation back into a normal one, so a
        subsequent collection (automatic - see _apply_runtime_tuning() above
        for why this is no longer manual) can actually reclaim what is now
        unreachable, and so the permanent generation does not simply grow by
        one freeze()'s worth every cycle. Called unconditionally here
        whenever tuning was applied, regardless of whether this session
        actually created any garbage - symmetry with _apply_runtime_tuning()
        is the point, not a conditional optimisation.
        """
        if self._prev_switch_interval is not None:
            try:
                sys.setswitchinterval(self._prev_switch_interval)
            except Exception:
                pass
            self._prev_switch_interval = None
        if self._tuning_applied:
            # HIGH 2/HIGH 3: captured first, before gc.unfreeze() below
            # changes what gc.get_count()/gc.get_objects() would report, so
            # the "stop" snapshot reflects the state this session actually
            # ran with, not the state after cleanup has already begun.
            self.gc_counts_at_stop = gc.get_count()
            self.gc_objects_at_stop = len(gc.get_objects())
            try:
                gc.unfreeze()
            except Exception:
                pass
            self._tuning_applied = False

    def start(self, packet_timespan_ms: int = 1) -> None:
        if self._streaming:
            raise RuntimeError(
                "DriverPacketSource.start() called while already streaming; "
                "call stop() first."
            )
        biocam = self._device.biocam
        if biocam is None:
            # Without this check, the first attribute access below
            # (biocam.DataReceived) raises a plain AttributeError -
            # "'NoneType' object has no attribute 'DataReceived'" - which
            # names the symptom, not the problem: BioCamDevice.__enter__
            # never completed (or __exit__ already ran), so there is no
            # claimed device to stream from. Naming that here means a
            # caller sees the actual mistake - start() called outside a
            # live BioCamDevice context - instead of having to infer it
            # from an opaque attribute error three lines further down.
            raise RuntimeError(
                "DriverPacketSource.start() called with device.biocam is "
                "None; a BioCamDevice must be successfully __enter__()'d "
                "(claiming the device) before starting a packet source."
            )
        self._stop_event.clear()

        # Counters reflect the session about to start, not a running total
        # across however many start()/stop() cycles this instance has been
        # through. Without this reset, a second start() on the same
        # DriverPacketSource (a retry, or any future caller that reuses one
        # instance across sessions) would carry a previous session's
        # loss/overflow/error counts into a new sidecar via session.py's
        # writer.note_driver_loss()/note_queue_overflow()/
        # note_callback_errors() - silently reporting losses from a run
        # that already ended and was already reported on. queue_overflows
        # is a plain int attribute (not behind a property), so it is reset
        # directly like the private counters below it.
        self.queue_overflows = 0
        self._driver_loss_counter = None
        self._loss_fallback_events = 0
        self._data_errors = 0
        # Packets whose header disagreed with the payload actually
        # delivered. See on_data: a mismatch desynchronises every later
        # frame, silently, so it is counted rather than assumed absent.
        self._payload_length_mismatches = 0
        self._last_payload_mismatch = None
        self._loss_errors = 0
        self._error_errors = 0
        self._pressure_reported = False
        # HIGH 2/MEDIUM 3: a stale True from a previous cycle must not
        # survive into this one. stop() always clears _maybe_streaming on
        # every path it can reach (see stop() below), but reset it here too
        # as a second guarantee - one that does not depend on stop() having
        # been called at all (e.g. a caller that starts a fresh source
        # object without ever stopping a previous one).
        self._maybe_streaming = False

        # Clear anything left over from a previous start()/stop() cycle - a
        # stale STOP sentinel, or straggler packets a consumer that broke out
        # of __iter__ early (e.g. FIX 1's drain giving up at its deadline)
        # never popped.
        #
        # Reached only when _streaming is False (the guard above), which
        # only happens after a stop() call has returned - and stop() always
        # calls _unsubscribe() (unconditionally, whenever biocam is not
        # None) before it returns, never after. So by the time this line
        # runs, on the Python side, unsubscription has already happened.
        #
        # What that buys us is a real but bounded guarantee, not an
        # absolute one: whether `biocam.DataReceived -= handler` (or the
        # other two `-=` calls) stops an *already-dispatched, in-flight*
        # invocation of that handler from completing and appending to
        # self._queue afterwards is standard .NET multicast-delegate
        # behaviour (an invocation captures its list at raise time) that
        # this repo has no documentation for either way - the same class of
        # gap the module docstring above already calls out as unverified
        # for on_error's ordering relative to unsubscription. If such a
        # straggler append lands after this clear() and after
        # pending_count() has already read the queue empty, it is neither
        # counted nor recovered: a genuine, if narrow, way this class can
        # still silently lose a packet. Left as a named lab-verification
        # item (see the phase report) rather than closed with a lock, for
        # the same reason the rest of this file avoids locking on_data's
        # hot path: there is no way to test the real driver's behaviour
        # here to justify the cost.
        #
        # Clearing inside stop() instead of here would be strictly worse:
        # on_error can still be mid-append when stop() runs (no unsubscribe
        # has happened yet at that point), so stop() clearing the queue
        # could race a *known-live* producer, not just a hypothetical
        # straggler - the exact defect this class exists to avoid.
        self._queue.clear()

        # Detach any handlers left over from a previous start() - including
        # one that failed after subscribing - before subscribing again.
        # MainForm.cs:143-148 does the same: unsubscribe immediately before
        # subscribing rather than assuming a clean slate.
        self._unsubscribe(biocam)

        def on_data(_sender, args):
            try:
                header = args.Header  # read once - saves a marshaling
                                       # round trip versus reading it twice.
                payload = bytes(args.Payload)
                packet = Packet(
                    timestamp=header.Timestamp,
                    counter=header.PacketCounter,
                    payload=payload,
                )
            except Exception:
                # Covers a null Payload (documented as possible when no data
                # was retrieved) and any other marshaling failure. Never let
                # an exception cross back into the .NET dispatcher.
                self._data_errors += 1
                return

            # The header states its own payload length (Int32 PayloadLength,
            # confirmed by reflection). Nothing in the XML promises that
            # `args.Payload` is exactly that many bytes, and the driver's own
            # lower-level path explicitly works with payloads at an offset
            # inside a larger buffer (XML:103-109, ProcessPayload(header,
            # data, payloadIndex)). If `Payload` were ever a pooled buffer
            # bigger than the payload, every byte after the first packet is
            # misaligned - wrong channel, wrong time, no error, and a .raw
            # that looks like real signal.
            #
            # Deliberately AFTER the packet is built and in its own guard. The
            # first version read PayloadLength inside the try above, which
            # meant a header that would not yield it dropped the packet
            # entirely - trading a real recording for a diagnostic, which is
            # the wrong way round. A check on the data must never cost the
            # data. Counted, never raised: this runs on the acquisition thread
            # and must not throw back into the .NET dispatcher.
            #
            # The cost is measured, not assumed. On this machine, DLLs and no
            # instrument (`python -m biocam.interop.benchmark`): a
            # DataPacketHeader property read is **0.52 us**, against a 6.9 us
            # payload copy in the same callback and a 1000 us budget at a 1 ms
            # period. The fifth marshal this adds is about 7% of the copy it
            # sits beside. An unmeasured per-packet marshal has no business
            # going into a lab session when measuring it is free here.
            try:
                self._note_payload_length(header.PayloadLength, len(payload))
            except Exception:  # noqa: BLE001 - a check is never worth a packet
                pass

            if len(self._queue) >= self._queue_size:
                self.queue_overflows += 1
            else:
                self._queue.append(packet)

        def on_loss(_sender, args):
            try:
                self._driver_loss_counter = int(args.Counter)
            except Exception:
                # Counter is documented as present on every DataLossEventArgs
                # (XML line 3497); this guards the read anyway rather than
                # assume it, and falls back to counting events - which can
                # undercount, since DataLossAsync is documented as firing
                # asynchronously and events may coalesce. This fallback
                # increment is only safe from cross-call races if the
                # driver invokes DataLossAsync serially, never with two
                # calls to this handler overlapping on different threads -
                # undocumented, so unverified; the primary path above
                # (a plain assignment) has no such assumption to make.
                try:
                    self._loss_fallback_events += 1
                except Exception:
                    self._loss_errors += 1

        def on_error(_sender, _args):
            try:
                self._stop_event.set()
                self._try_enqueue_stop()
            except Exception:
                self._error_errors += 1

        self._handler = on_data
        self._loss_handler = on_loss
        self._error_handler = on_error

        # Gate 1, item A: runtime tuning and all three event subscriptions
        # now happen inside this try, not before it. Previously
        # `biocam.DataReceived += ...` etc. and `_apply_runtime_tuning()`
        # ran ahead of the try block below, even though that block's own
        # comment claimed to guard against a failed start leaving handlers
        # subscribed - so an exception raised while attaching a handler (or
        # while tuning) escaped uncaught: GC stayed disabled, the switch
        # interval stayed tightened, and any handler that had already
        # attached stayed subscribed for the rest of the process. Nothing
        # else in the process was positioned to undo it either - cli.py used
        # to call start() outside its own try/finally that calls stop() (see
        # Gate 1, item A there). Moving both inside the guarded region means
        # any partial failure - tuning, any one of the three subscriptions,
        # or StartDataStreaming itself - unsubscribes whatever did attach
        # and restores both runtime settings before the exception
        # propagates.
        try:
            self._apply_runtime_tuning()

            biocam.DataReceived += self._handler
            biocam.DataLossAsync += self._loss_handler
            biocam.DataStreamingError += self._error_handler

            # issue #18: set immediately before the call, not after. The
            # XML documents a separate AssertCanStartStreaming that can
            # throw, but not whether that assertion runs before or after
            # the hardware is actually told to start - unanswerable from
            # documentation here. `_streaming` alone is only set once
            # StartDataStreaming has already returned successfully, so a
            # call that raises (or is interrupted mid-call - see the
            # BaseException note below) after the hardware engaged would
            # otherwise leave no record that stop() still needs to attempt
            # StopDataStreaming().
            self._maybe_streaming = True
            started = biocam.StartDataStreaming(
                dataPacketTimeSpanMs=packet_timespan_ms,
                optimizeDataPacketLatency=True,
            )
            if not started:
                # HIGH 2: a `False` return is the driver's own statement
                # that nothing engaged - the weakest possible case for
                # "may have engaged". Unlike an exception from the call
                # itself (caught below), there is no uncertainty to
                # preserve here, so clear the flag before raising: stop()
                # must not call StopDataStreaming on a device the driver
                # just told us never started. Controller's original
                # directive covered only "set the flag before the call",
                # not this case - see the Gate 1 report.
                self._maybe_streaming = False
                raise RuntimeError("StartDataStreaming failed.")
        except BaseException:
            # BaseException, not Exception: a KeyboardInterrupt landing
            # between attaching a handler and StartDataStreaming returning
            # (or during the call itself) must hit this cleanup too, or it
            # leaves live handlers subscribed and the switch interval/GC
            # tuning applied with no active stream to justify either -
            # exactly the leak this except block exists to prevent, just
            # from an exception type that `except Exception` does not
            # catch. _maybe_streaming is deliberately left as this branch
            # found it (True, set just above): stop() still needs to
            # attempt StopDataStreaming in case the failure happened after
            # the hardware was actually told to start (issue #18).
            self._unsubscribe(biocam)
            self._restore_runtime_tuning()
            raise
        self._streaming = True

    def stop(self) -> None:
        biocam = self._device.biocam
        stopped_ok = True
        stop_error = None
        try:
            try:
                # issue #18: `_maybe_streaming` (set in start(), immediately
                # before StartDataStreaming) covers the gap `_streaming`
                # alone cannot - a start() that raised or was interrupted
                # after the hardware was actually told to start, but before
                # `_streaming` itself was set. Attempting StopDataStreaming
                # on a device that never started is harmless (nothing for
                # the driver to stop); never attempting it on one that may
                # actually be streaming is not - so both flags are checked,
                # not just `_streaming`.
                if self._streaming or self._maybe_streaming:
                    if biocam is not None:
                        try:
                            stopped_ok = biocam.StopDataStreaming()
                        except BaseException as exc:
                            # HIGH 1: BaseException, not Exception. cli.py
                            # reaches stop() FROM a KeyboardInterrupt
                            # handler (a first Ctrl+C), so a second Ctrl+C
                            # arriving while this call is in flight is a
                            # real path, not a hypothetical one - and it
                            # must still be captured here so cleanup below
                            # (unsubscription, stop flag, sentinel) runs,
                            # instead of the exception skipping straight
                            # past all of it. Propagate the original
                            # exception after cleanup, rather than
                            # swallowing it or replacing it with the generic
                            # "failed" error used for a falsy return: a
                            # caller should be able to tell an exception
                            # (including an interrupt) from a refusal.
                            stopped_ok = False
                            stop_error = exc
                        if stopped_ok:
                            self._streaming = False
                            self._maybe_streaming = False
                        # If it failed or raised, leave both flags as they
                        # were so a retried stop() calls StopDataStreaming()
                        # again instead of silently skipping it.
                    else:
                        # biocam is None: BioCamDevice.__exit__ has already
                        # cleared our reference to it (it calls
                        # ReleaseBioCamControl and sets biocam = None; it
                        # does not call StopDataStreaming or detach handlers
                        # itself), so there is nothing left here to call
                        # StopDataStreaming() on, and nothing to retry
                        # either - unlike the stopped_ok=False branch above,
                        # a future stop() call would find biocam still None
                        # and be no more able to call it. Both flags must
                        # still be cleared here, or start()'s re-entrancy
                        # guard blocks every subsequent start() on this
                        # instance permanently. Whether the driver still
                        # considers itself streaming, and whether the
                        # handlers subscribed to the now-unreachable biocam
                        # object are still live on the .NET side, is
                        # unverified - see the lab follow-up note in the
                        # Gate 1 report - but that is a driver-side question
                        # this Python flag cannot answer either way.
                        self._streaming = False
                        self._maybe_streaming = False
                self._stop_event.set()
                self._try_enqueue_stop()
            finally:
                # HIGH 1: unconditional, matching start()'s
                # `except BaseException` cleanup - must run even if
                # something above (StopDataStreaming's own BaseException
                # handling notwithstanding, or stop_event.set()/
                # _try_enqueue_stop() themselves) raised, or a second
                # Ctrl+C during StopDataStreaming leaves handlers
                # subscribed for the rest of the process. Sample
                # unsubscribes unconditionally too (MainForm.cs:219-221).
                if biocam is not None:
                    self._unsubscribe(biocam)
        finally:
            # Unconditional: must run even if something above raised, so
            # the switch interval and GC state never stay tuned past a
            # stream that has ended (or failed to end cleanly).
            self._restore_runtime_tuning()
        if stop_error is not None:
            raise stop_error
        if not stopped_ok:
            raise RuntimeError("StopDataStreaming failed.")

    def __iter__(self):
        threshold = int(self._queue_size * PRESSURE_FRACTION)
        while True:
            try:
                item = self._queue.popleft()
            except IndexError:
                if self._stop_event.is_set():
                    return
                time.sleep(POLL_INTERVAL_SEC)
                continue
            if item is STOP:
                # Skip, don't return - see FIX 3 in the module docstring.
                # Anything the driver appended after this sentinel (the
                # on_error race) is drained below instead of being orphaned;
                # termination still happens, via the IndexError branch above,
                # once the queue is genuinely empty and _stop_event is set -
                # which it always is by the time STOP was enqueued.
                continue
            depth = len(self._queue)
            if depth >= threshold and not self._pressure_reported:
                self._pressure_reported = True
                if self._listener is not None:
                    self._listener(QueuePressure(depth=depth,
                                                 capacity=self._queue_size))
            elif depth < threshold // 2:
                self._pressure_reported = False

            # HIGH 2/HIGH 3: there used to be a manual gc.collect(1) here,
            # defended as safe because it ran on this thread rather than the
            # driver's. That defence does not hold - gc.collect() holds the
            # GIL for its whole traversal regardless of which thread calls
            # it, so it stalled the driver's callback exactly as if it had
            # run there directly (measured: 31.8 ms per call, ~32 dropped
            # packets at a 1 ms acquisition period). See the module
            # docstring and _apply_runtime_tuning() above: automatic
            # collection is left enabled instead, which is both cheap (the
            # frozen startup graph keeps a generation-0 pass scoped to what
            # this run allocates) and correct (generations 1 and 2 keep
            # collecting on schedule, so nothing accumulates the way it did
            # under gc.disable()).

            yield item
