"""Layer 1 - packets from the instrument.

The callback does four things and returns: read the header, copy the payload,
put it on a bounded queue, return. Nothing else. No file I/O, no printing, no
allocation beyond the copy, no locks.

If the queue is full the packet is dropped and counted. Blocking the callback
would stall the driver and lose more than the packet being saved.
"""

import queue

from biocam.data.events import QueuePressure
from biocam.data.replay import Packet

STOP = object()

PRESSURE_FRACTION = 0.8


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
        self.queue_overflows = 0
        self.driver_loss_events = 0
        self._streaming = False

    def start(self, packet_timespan_ms: int = 1) -> None:
        biocam = self._device.biocam

        def on_data(_sender, args):
            try:
                self._queue.put_nowait(Packet(
                    timestamp=args.Header.Timestamp,
                    counter=args.Header.PacketCounter,
                    payload=bytes(args.Payload),
                ))
            except queue.Full:
                self.queue_overflows += 1

        def on_loss(_sender, args):
            self.driver_loss_events += 1

        def on_error(_sender, _args):
            self._queue.put(STOP)

        self._handler = on_data
        self._loss_handler = on_loss
        self._error_handler = on_error

        biocam.DataReceived += self._handler
        biocam.DataLossAsync += self._loss_handler
        biocam.DataStreamingError += self._error_handler

        started = biocam.StartDataStreaming(
            dataPacketTimeSpanMs=packet_timespan_ms,
            optimizeDataPacketLatency=True,
        )
        if not started:
            raise RuntimeError("StartDataStreaming failed.")
        self._streaming = True

    def stop(self) -> None:
        biocam = self._device.biocam
        stopped_ok = True
        if self._streaming:
            stopped_ok = biocam.StopDataStreaming()
            self._streaming = False
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
        self._queue.put(STOP)
        if not stopped_ok:
            raise RuntimeError("StopDataStreaming failed.")

    def __iter__(self):
        threshold = int(self._queue_size * PRESSURE_FRACTION)
        while True:
            item = self._queue.get()
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
