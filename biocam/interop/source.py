"""Layer 1 - packets from the instrument.

The callback does four things and returns: read the header, copy the payload,
put it on a bounded queue, return. Nothing else. No file I/O, no printing, no
allocation beyond the copy, and no operation that can block: `put_nowait`
only, never `put`. A `threading.Event` is used as a stop flag - `set()` and
`is_set()` are non-waiting, not lock-free: `Event.set()` takes the internal
condition lock and `queue.Queue.put_nowait` takes the queue's mutex, both
uncontended and short in this single-producer use, so they do not block the
driver's event thread in practice.

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
"""

import queue
import threading

from biocam.data.events import QueuePressure
from biocam.data.replay import Packet

STOP = object()

PRESSURE_FRACTION = 0.8

# How long __iter__ waits on an empty queue before re-checking the stop
# flag. Paid only while idle, waiting for the next packet - never inside a
# driver callback - so it does not compete with the 1 ms packet budget.
POLL_INTERVAL_SEC = 0.1


class DriverPacketSource:
    """Turns the driver's DataReceived event into an iterable of packets."""

    def __init__(self, device, queue_size: int = 2000, listener=None):
        self._device = device
        self._queue = queue.Queue(maxsize=queue_size)
        self._queue_size = queue_size
        self._listener = listener
        self._pressure_reported = False
        self._handler = None
        self._loss_handler = None
        self._error_handler = None
        self._stop_event = threading.Event()
        self.queue_overflows = 0
        self.driver_loss_events = 0
        self.callback_errors = 0
        self._streaming = False

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

    def start(self, packet_timespan_ms: int = 1) -> None:
        if self._streaming:
            raise RuntimeError(
                "DriverPacketSource.start() called while already streaming; "
                "call stop() first."
            )
        biocam = self._device.biocam
        self._stop_event.clear()

        # Detach any handlers left over from a previous start() - including
        # one that failed after subscribing - before subscribing again.
        # MainForm.cs:143-148 does the same: unsubscribe immediately before
        # subscribing rather than assuming a clean slate.
        self._unsubscribe(biocam)

        def on_data(_sender, args):
            try:
                packet = Packet(
                    timestamp=args.Header.Timestamp,
                    counter=args.Header.PacketCounter,
                    payload=bytes(args.Payload),
                )
            except Exception:
                # Covers a null Payload (documented as possible when no data
                # was retrieved) and any other marshaling failure. Never let
                # an exception cross back into the .NET dispatcher.
                self.callback_errors += 1
                return
            try:
                self._queue.put_nowait(packet)
            except queue.Full:
                self.queue_overflows += 1

        def on_loss(_sender, args):
            try:
                self.driver_loss_events += 1
            except Exception:
                self.callback_errors += 1

        def on_error(_sender, _args):
            try:
                self._stop_event.set()
                try:
                    self._queue.put_nowait(STOP)
                except queue.Full:
                    # The consumer will notice via _stop_event on its next
                    # poll; a full queue must never make this handler wait -
                    # DataStreamingError fires exactly when the queue is
                    # most likely to be full and nothing may be draining it.
                    pass
            except Exception:
                self.callback_errors += 1

        self._handler = on_data
        self._loss_handler = on_loss
        self._error_handler = on_error

        biocam.DataReceived += self._handler
        biocam.DataLossAsync += self._loss_handler
        biocam.DataStreamingError += self._error_handler

        try:
            started = biocam.StartDataStreaming(
                dataPacketTimeSpanMs=packet_timespan_ms,
                optimizeDataPacketLatency=True,
            )
            if not started:
                raise RuntimeError("StartDataStreaming failed.")
        except Exception:
            # A failed start must not leave three live handlers subscribed:
            # that leaks a Python closure into the driver for the rest of
            # the process, and a second start() call would leak another set
            # on top of it.
            self._unsubscribe(biocam)
            raise
        self._streaming = True

    def stop(self) -> None:
        biocam = self._device.biocam
        stopped_ok = True
        stop_error = None
        if self._streaming:
            if biocam is not None:
                try:
                    stopped_ok = biocam.StopDataStreaming()
                except Exception as exc:
                    # Propagate the original exception after cleanup below,
                    # rather than swallowing it or replacing it with the
                    # generic "failed" error used for a falsy return: a
                    # caller should be able to tell an exception from a
                    # refusal.
                    stopped_ok = False
                    stop_error = exc
                if stopped_ok:
                    self._streaming = False
                # If it failed or raised, leave _streaming True so a
                # retried stop() calls StopDataStreaming() again instead of
                # silently skipping it.
            # If biocam is None, BioCamDevice.__exit__ has already cleared
            # our reference to it (it calls ReleaseBioCamControl and sets
            # biocam = None; it does not call StopDataStreaming or detach
            # handlers itself), so there is nothing left here to call
            # StopDataStreaming() on. Fall through to the Python-side
            # cleanup below instead of raising AttributeError. Whether the
            # driver still considers itself streaming, and whether the
            # handlers subscribed to the now-unreachable biocam object are
            # still live on the .NET side, is unverified - see the lab
            # follow-up note in the Gate 1 report.
        if biocam is not None:
            self._unsubscribe(biocam)
        self._stop_event.set()
        try:
            self._queue.put_nowait(STOP)
        except queue.Full:
            # __iter__ still returns: it re-checks _stop_event on every
            # empty-queue poll, so the sentinel is a fast path, not the
            # only way out.
            pass
        if stop_error is not None:
            raise stop_error
        if not stopped_ok:
            raise RuntimeError("StopDataStreaming failed.")

    def __iter__(self):
        threshold = int(self._queue_size * PRESSURE_FRACTION)
        while True:
            try:
                item = self._queue.get(timeout=POLL_INTERVAL_SEC)
            except queue.Empty:
                if self._stop_event.is_set():
                    return
                continue
            if item is STOP:
                return
            depth = self._queue.qsize()
            if depth >= threshold and not self._pressure_reported:
                self._pressure_reported = True
                if self._listener is not None:
                    self._listener(QueuePressure(depth=depth,
                                                 capacity=self._queue_size))
            elif depth < threshold // 2:
                self._pressure_reported = False
            yield item
