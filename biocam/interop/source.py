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

`start()` also tightens `sys.setswitchinterval` and disables cyclic garbage
collection for the duration of the stream, restoring both, unconditionally,
in `stop()`. Both changes exist to keep the driver's thread from stalling
waiting for the GIL or being the one to run a collection - see the
docstrings on `_apply_runtime_tuning`/`_restore_runtime_tuning` below.
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
POLL_INTERVAL_SEC = 0.1

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
        self._loss_errors = 0
        self._error_errors = 0
        self._streaming = False
        # Set in start(), consumed and cleared in stop()/_restore_runtime_
        # tuning(). None means "nothing to restore" - covers stop() being
        # called without a preceding successful start().
        self._prev_switch_interval = None
        self._prev_gc_enabled = None

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
        start of acquisition, so the latest value read is the correct
        answer - it is stored, not accumulated. Falls back to a count of
        DataLossAsync events if Counter was ever unreadable on this
        instance; that fallback can undercount, because DataLossAsync is
        documented as firing asynchronously and events may coalesce.
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
        """Tighten the GIL switch interval and freeze/disable GC.

        Both changes are about keeping work off the driver's callback
        thread for the duration of the stream:

        - A tighter switch interval (FIX 5) shortens the longest the
          driver thread can wait to acquire the GIL from the consumer.
        - gc.freeze() moves everything currently tracked into the
          permanent generation so it is never rescanned, then
          gc.disable() (FIX 6) stops further automatic collections. The
          hot path allocates a Packet plus a bytes copy per call and
          creates no reference cycles, so automatic cyclic collection buys
          nothing there and only risks running - a generation-2 pass scans
          every tracked object in the process - on whichever thread
          happens to cross the threshold, usually the driver's.

        Both are restored unconditionally in _restore_runtime_tuning().
        """
        self._prev_switch_interval = sys.getswitchinterval()
        sys.setswitchinterval(GIL_SWITCH_INTERVAL_SEC)
        self._prev_gc_enabled = gc.isenabled()
        gc.freeze()
        gc.disable()

    def _restore_runtime_tuning(self) -> None:
        """Undo _apply_runtime_tuning(), restoring the previous state.

        Called from stop()'s `finally` (and from start()'s failure path)
        so it runs even if stop() itself fails or start() never completes -
        matching the unconditional-cleanup pattern already used for handler
        unsubscription. A None sentinel means start() never ran (or already
        restored), so there is nothing to undo.
        """
        if self._prev_switch_interval is not None:
            try:
                sys.setswitchinterval(self._prev_switch_interval)
            except Exception:
                pass
            self._prev_switch_interval = None
        if self._prev_gc_enabled is not None:
            try:
                if self._prev_gc_enabled:
                    gc.enable()
            except Exception:
                pass
            self._prev_gc_enabled = None

    def start(self, packet_timespan_ms: int = 1) -> None:
        if self._streaming:
            raise RuntimeError(
                "DriverPacketSource.start() called while already streaming; "
                "call stop() first."
            )
        biocam = self._device.biocam
        self._stop_event.clear()

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
                packet = Packet(
                    timestamp=header.Timestamp,
                    counter=header.PacketCounter,
                    payload=bytes(args.Payload),
                )
            except Exception:
                # Covers a null Payload (documented as possible when no data
                # was retrieved) and any other marshaling failure. Never let
                # an exception cross back into the .NET dispatcher.
                self._data_errors += 1
                return
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

            started = biocam.StartDataStreaming(
                dataPacketTimeSpanMs=packet_timespan_ms,
                optimizeDataPacketLatency=True,
            )
            if not started:
                raise RuntimeError("StartDataStreaming failed.")
        except Exception:
            # A failed start must not leave any live handlers subscribed:
            # that leaks a Python closure into the driver for the rest of
            # the process, and a second start() call would leak another set
            # on top of it. Nor should it leave the switch interval/GC
            # tuning applied with no active stream to justify it.
            self._unsubscribe(biocam)
            self._restore_runtime_tuning()
            raise
        self._streaming = True

    def stop(self) -> None:
        biocam = self._device.biocam
        stopped_ok = True
        stop_error = None
        try:
            if self._streaming:
                if biocam is not None:
                    try:
                        stopped_ok = biocam.StopDataStreaming()
                    except Exception as exc:
                        # Propagate the original exception after cleanup
                        # below, rather than swallowing it or replacing it
                        # with the generic "failed" error used for a falsy
                        # return: a caller should be able to tell an
                        # exception from a refusal.
                        stopped_ok = False
                        stop_error = exc
                    if stopped_ok:
                        self._streaming = False
                    # If it failed or raised, leave _streaming True so a
                    # retried stop() calls StopDataStreaming() again instead
                    # of silently skipping it.
                else:
                    # biocam is None: BioCamDevice.__exit__ has already
                    # cleared our reference to it (it calls
                    # ReleaseBioCamControl and sets biocam = None; it does
                    # not call StopDataStreaming or detach handlers itself),
                    # so there is nothing left here to call
                    # StopDataStreaming() on, and nothing to retry either -
                    # unlike the stopped_ok=False branch above, a future
                    # stop() call would find biocam still None and be no
                    # more able to call it. _streaming must still be
                    # cleared here, or start()'s re-entrancy guard blocks
                    # every subsequent start() on this instance permanently.
                    # Whether the driver still considers itself streaming,
                    # and whether the handlers subscribed to the now-
                    # unreachable biocam object are still live on the .NET
                    # side, is unverified - see the lab follow-up note in
                    # the Gate 1 report - but that is a driver-side question
                    # this Python flag cannot answer either way.
                    self._streaming = False
            if biocam is not None:
                self._unsubscribe(biocam)
            self._stop_event.set()
            self._try_enqueue_stop()
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
            yield item
