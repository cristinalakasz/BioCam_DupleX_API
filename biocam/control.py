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

# Above this, a dispatch is reported as having put the drain at risk.
#
# This number is a GUESS and only a lab run can validate it. The real budget
# is not the whole acquisition period either: write_packet, the gap tracker
# and the clock all come out of the same per-packet time first, so a dispatch
# at 499 us can already be past the point of no return at --packet-ms 1.
# Issue #26 exists to replace this with a measurement.
SLOW_DISPATCH_US = 500.0

# Consecutive slow dispatches before stimulation is disconnected outright.
# Counting slow dispatches and carrying on is not enough: a send that takes
# 3 ms at a 1 ms period puts the consumer 2 ms further behind on every packet,
# forever, and the only signal would be text read after the run - by which
# time the queue has overflowed and the recording is silently short. This is
# the brake, and it fails towards losing stimuli rather than losing data.
MAX_CONSECUTIVE_SLOW = 3

# Minimum wall-clock spacing between dispatches. One-per-packet bounds the
# count but not the rate: after any stall the consumer drains its backlog far
# faster than 1 kHz, and every one of those packets triggers a dispatch, so
# queued stimuli would fire back-to-back at catch-up speed. That is a tissue
# safety problem, not just a timing one. 1000 us matches the driver's own
# MinDistance (biocam.stim.train.MIN_PERIOD_US).
DEFAULT_MIN_INTERVAL_US = 1000.0

# A request older than this is discarded rather than delivered late. Manual
# stimulation is driven by a human - reaction time is around 200 ms - so a
# request that has been waiting a full second is one the experimenter has
# stopped expecting. Delivering it then is worse than not delivering it.
DEFAULT_MAX_AGE_US = 1_000_000.0


@dataclass(frozen=True)
class StimulationRequest:
    """One thing a control thread would like delivered."""

    plan: object
    pattern: object
    scheduled: bool = False
    label: str = ""
    # time.perf_counter() when the request was made, for staleness. Stamped
    # on the requesting thread, which is where the intent actually happened.
    #
    # perf_counter, NOT monotonic. time.monotonic() on Windows resolves to
    # 15.625 ms (measured: time.get_clock_info('monotonic').resolution), so a
    # 5 ms interval reads as exactly 0.0 and every sub-15 ms rule below -
    # the 1 ms minimum spacing especially - would silently do nothing while
    # appearing to work. perf_counter resolves to 0.1 us on the same machine.
    requested_at: float = 0.0


class StimulationQueue:
    """A bounded hand-off from any thread to the acquisition consumer.

    `request()` is safe from any thread. `take()` and `service()` must be
    called only from the consumer thread, because what they ultimately invoke
    is the driver.
    """

    def __init__(self, capacity: int = DEFAULT_CAPACITY,
                 slow_dispatch_us: float = SLOW_DISPATCH_US,
                 max_consecutive_slow: int = MAX_CONSECUTIVE_SLOW,
                 min_interval_us: float = DEFAULT_MIN_INTERVAL_US,
                 max_age_us: float = DEFAULT_MAX_AGE_US):
        if capacity < 1:
            raise ValueError(f"capacity must be at least 1, got {capacity}")
        self._queue = deque()
        self._capacity = capacity
        self._slow_dispatch_us = slow_dispatch_us
        self._max_consecutive_slow = max_consecutive_slow
        self._min_interval_us = min_interval_us
        self._max_age_us = max_age_us
        self._last_dispatch_at = None
        self._consecutive_slow = 0
        self.suspended_reason = None
        self.stale = 0
        self.failed_dispatch_us = 0.0
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
            plan=plan, pattern=pattern, scheduled=scheduled, label=label,
            requested_at=time.perf_counter(),
        )
        with self._lock:
            if len(self._queue) >= self._capacity:
                self.dropped += 1
                return False
            self._queue.append(item)
            return True

    # -- consumer thread only ---------------------------------------------

    @property
    def suspended(self) -> bool:
        return self.suspended_reason is not None

    def take(self):
        """Remove and return the next request, or None."""
        # Unlocked fast path. _run_service calls this on every packet, so at a
        # 1 ms period an idle queue would otherwise cost ~1000 lock round
        # trips a second on the one thread that must not accumulate cost.
        # deque.__bool__ is atomic under the GIL; a false negative defers one
        # dispatch by a single packet, and a false positive falls through to
        # the locked path, which re-checks.
        if not self._queue:
            return None
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
        if self.suspended:
            return False

        # perf_counter throughout - see StimulationRequest.requested_at for
        # why monotonic() cannot express these intervals on Windows.
        now = time.perf_counter()
        # Rate, not just count. One-per-packet does not bound wall-clock
        # spacing: after a stall the consumer drains its backlog far faster
        # than packets arrive, so queued stimuli would fire back-to-back.
        if (self._last_dispatch_at is not None
                and (now - self._last_dispatch_at) * 1e6 < self._min_interval_us):
            return False

        # Discard anything that has been waiting too long. Delivering a
        # stimulus the experimenter stopped expecting is worse than not
        # delivering it, and counting them separately from `dropped` keeps
        # "the queue was full" distinct from "it waited too long".
        while True:
            request = self.take()
            if request is None:
                return False
            if self._max_age_us and request.requested_at:
                age_us = (now - request.requested_at) * 1e6
                if age_us > self._max_age_us:
                    self.stale += 1
                    continue
            break

        started = now
        failed = False
        try:
            send(request)
        except Exception:  # noqa: BLE001 - a failed stimulus must not stop the run
            failed = True
        # KeyboardInterrupt and SystemExit are deliberately NOT caught. Ctrl+C
        # is how a recording is stopped, and `send` is the longest call in the
        # loop, so it is the widest window for one to land in. Swallowing it
        # would count the operator's stop as a stimulation failure and carry
        # on recording. It propagates through session._run_service - which
        # catches only Exception, correctly - to record_session's
        # `interrupted` path and the CLI's drain retry.
        finally:
            elapsed_us = (time.perf_counter() - started) * 1e6
            self.total_dispatch_us += elapsed_us
            if failed:
                self.failed_dispatch_us += elapsed_us
            if elapsed_us > self.max_dispatch_us:
                self.max_dispatch_us = elapsed_us
            if elapsed_us > self._slow_dispatch_us:
                self.slow_dispatches += 1
                self._consecutive_slow += 1
                if self._consecutive_slow >= self._max_consecutive_slow:
                    self.suspended_reason = (
                        f"{self._consecutive_slow} consecutive dispatches took "
                        f"longer than {self._slow_dispatch_us:g} us on the "
                        f"acquisition thread (slowest "
                        f"{self.max_dispatch_us:.0f} us). Stimulation was "
                        "disconnected to stop it competing with the packet "
                        "drain; the recording continues"
                    )
            else:
                self._consecutive_slow = 0
            self._last_dispatch_at = now

        if failed:
            self.failed += 1
            return False
        self.dispatched += 1
        return True

    # -- reporting ---------------------------------------------------------

    @property
    def mean_dispatch_us(self) -> float:
        """Mean time of dispatches that actually reached the driver.

        Failed attempts are excluded: one that fails fast contributes a
        near-zero sample and would drag down the very number that decides
        whether stimulating from this thread is viable. `max_dispatch_us` and
        `slow_dispatches` do include them, which is the conservative
        direction.
        """
        if not self.dispatched:
            return 0.0
        return (self.total_dispatch_us - self.failed_dispatch_us) / self.dispatched

    def summary(self) -> dict:
        return {
            "dispatched": self.dispatched,
            "failed": self.failed,
            "dropped": self.dropped,
            "stale": self.stale,
            "pending": len(self),
            "suspended": self.suspended,
            "suspended_reason": self.suspended_reason,
            "max_dispatch_us": self.max_dispatch_us,
            "mean_dispatch_us": self.mean_dispatch_us,
            "slow_dispatches": self.slow_dispatches,
            "slow_dispatch_threshold_us": self._slow_dispatch_us,
        }

    def warnings(self) -> list:
        """What a person needs to be told about this session's stimulation.

        Safe from any thread, and normally read after the session ends. The
        counters it reads are plain ints written only by the consumer thread,
        so a concurrent read can be one dispatch out of date but never torn.
        """
        problems = []
        if self.suspended:
            problems.append(self.suspended_reason + ".")
        if self.stale:
            problems.append(
                f"{self.stale} stimulation request(s) were discarded for "
                f"waiting longer than {self._max_age_us / 1000:g} ms. They "
                "were never delivered - a stimulus the experimenter has "
                "stopped expecting is worse arriving late than not at all."
            )
        pending = len(self)
        if pending:
            problems.append(
                f"{pending} stimulation request(s) were still queued when the "
                "recording ended and were never delivered. Stimulation is "
                "deliberately not serviced during shutdown."
            )
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
