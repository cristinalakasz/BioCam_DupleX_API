"""Layer 2 - handing stimulation requests to the acquisition thread.

Recording and stimulating at once raises a question this repository cannot
answer from documentation: may `Send` be called from a thread other than the
one the data callback wakes? Nothing in the XML says. 3Brain's own sample only
ever calls it from that thread (`MainForm.cs:383`, inside the closed loop its
data callback drives).

So rather than guess, this takes the sample's arrangement: **stimulation
happens on the consumer thread**, between packets. That sidesteps the
thread-safety question entirely, and it is the arrangement Phase 6's closed
loop needs anyway.

It buys that at a price, and the price has to be respected. The consumer
thread is the only thing draining the packet queue; if it stalls, the queue
fills and the driver's callback starts dropping packets silently - a recording
that looks like real signal rather than an error. `Send` is a driver call of
unknown duration, so putting it on that thread spends part of the drain's
budget on it.

Three things follow, and they are the whole design:

**Requesting never blocks.** A control thread - a UI, a keypress handler, a
protocol runner - puts a request in a bounded queue and returns immediately.
When the queue is full the request is dropped and counted, never queued
without limit and never made to wait.

**At most one stimulus is dispatched per packet.** A backlog of requests
cannot turn into an unbounded burst of driver calls between two packets.

**Time spent is measured, not assumed.** Every dispatch is timed on the
consumer thread, and the slowest is kept. That number is the one that says
whether this arrangement is viable at a given acquisition period, and it can
only be obtained on the instrument.

Nothing here imports the driver, so all of it is testable.
"""

import time
from collections import deque
from dataclasses import dataclass
from threading import Lock

# How many requests may wait at once. Small on purpose: a control thread that
# has fallen this far behind is not going to catch up by queueing more, and a
# stimulus delivered long after it was asked for is usually worse than one
# that was refused - the experimenter has moved on.
DEFAULT_CAPACITY = 16

# Above this, a dispatch is reported as having put the drain at risk. The
# acquisition period is the real budget (packets arrive every `--packet-ms`
# milliseconds and the consumer must keep up), so this is deliberately well
# below the smallest documented period of 1 ms.
SLOW_DISPATCH_US = 500.0


@dataclass(frozen=True)
class StimulationRequest:
    """One thing a control thread would like delivered."""

    plan: object
    pattern: object
    scheduled: bool = False
    label: str = ""


class StimulationQueue:
    """A bounded hand-off from any thread to the acquisition consumer.

    `request()` is safe from any thread. `take()` and `service()` must be
    called only from the consumer thread, because what they ultimately invoke
    is the driver.
    """

    def __init__(self, capacity: int = DEFAULT_CAPACITY,
                 slow_dispatch_us: float = SLOW_DISPATCH_US):
        if capacity < 1:
            raise ValueError(f"capacity must be at least 1, got {capacity}")
        self._queue = deque()
        self._capacity = capacity
        self._slow_dispatch_us = slow_dispatch_us
        # A lock held for a deque append or popleft and nothing else - never
        # across a driver call. CLAUDE.md forbids locks in the DataReceived
        # callback; this is the consumer thread, not that callback, and the
        # hold time is a few tens of nanoseconds either side.
        self._lock = Lock()
        self.dropped = 0
        self.dispatched = 0
        self.failed = 0
        self.slow_dispatches = 0
        self.max_dispatch_us = 0.0
        self.total_dispatch_us = 0.0

    def __len__(self) -> int:
        with self._lock:
            return len(self._queue)

    # -- from any thread --------------------------------------------------

    def request(self, plan, pattern, *, scheduled: bool = False,
                label: str = "") -> bool:
        """Ask for a stimulus. Returns False if the queue was full.

        Never blocks and never grows without bound. A caller that cares
        whether the request was accepted must check the return value; a UI
        button that ignores it will silently do nothing under load, which is
        why `dropped` is counted and reported.
        """
        item = StimulationRequest(
            plan=plan, pattern=pattern, scheduled=scheduled, label=label
        )
        with self._lock:
            if len(self._queue) >= self._capacity:
                self.dropped += 1
                return False
            self._queue.append(item)
            return True

    # -- consumer thread only ---------------------------------------------

    def take(self):
        """Remove and return the next request, or None."""
        with self._lock:
            if not self._queue:
                return None
            return self._queue.popleft()

    def service(self, send) -> bool:
        """Deliver at most one pending stimulus. Returns whether one was sent.

        `send(request)` is supplied by Layer 1 and does the driver call.

        Called between packets on the consumer thread, so it must return
        quickly and must not raise: an exception here would propagate out of
        the packet loop and abandon whatever is still buffered. A failing
        stimulus is counted and the recording continues - the stimulator's own
        log is where the failure is described.

        One request per call, deliberately. A backlog must not become a burst
        of driver calls between two packets.
        """
        request = self.take()
        if request is None:
            return False

        started = time.perf_counter()
        try:
            send(request)
        except BaseException:  # noqa: BLE001 - the recording outranks the stimulus
            self.failed += 1
            return False
        finally:
            elapsed_us = (time.perf_counter() - started) * 1e6
            self.total_dispatch_us += elapsed_us
            if elapsed_us > self.max_dispatch_us:
                self.max_dispatch_us = elapsed_us
            if elapsed_us > self._slow_dispatch_us:
                self.slow_dispatches += 1
        self.dispatched += 1
        return True

    # -- reporting ---------------------------------------------------------

    @property
    def mean_dispatch_us(self) -> float:
        attempts = self.dispatched + self.failed
        if not attempts:
            return 0.0
        return self.total_dispatch_us / attempts

    def summary(self) -> dict:
        return {
            "dispatched": self.dispatched,
            "failed": self.failed,
            "dropped": self.dropped,
            "pending": len(self),
            "max_dispatch_us": self.max_dispatch_us,
            "mean_dispatch_us": self.mean_dispatch_us,
            "slow_dispatches": self.slow_dispatches,
            "slow_dispatch_threshold_us": self._slow_dispatch_us,
        }

    def warnings(self) -> list:
        """What a person needs to be told about this session's stimulation."""
        problems = []
        if self.dropped:
            problems.append(
                f"{self.dropped} stimulation request(s) were dropped because "
                f"the queue was full (capacity {self._capacity}). They were "
                "never delivered. A control thread asking faster than the "
                "acquisition thread can dispatch is the usual cause."
            )
        if self.failed:
            problems.append(
                f"{self.failed} stimulation request(s) failed during "
                "dispatch. The stimulus log records why each one failed."
            )
        if self.slow_dispatches:
            problems.append(
                f"{self.slow_dispatches} dispatch(es) took longer than "
                f"{self._slow_dispatch_us:g} us on the acquisition thread "
                f"(slowest {self.max_dispatch_us:.0f} us). That time is taken "
                "from the packet queue's drain. If it approaches the "
                "acquisition period, stimulation is competing with recording "
                "and packets will start being dropped."
            )
        return problems
