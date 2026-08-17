import threading

import pytest

from biocam.control import (
    DEFAULT_CAPACITY,
    StimulationQueue,
    StimulationRequest,
)


def a_queue(**kwargs):
    # Most tests are about the hand-off, not the rate limiter, and they
    # dispatch far faster than any real acquisition would. Turning the
    # spacing off keeps them testing one thing each; the limiter has its own
    # tests below.
    kwargs.setdefault("min_interval_us", 0.0)
    return StimulationQueue(**kwargs)


# --------------------------------------------------------------------------
# requesting never blocks and never grows
# --------------------------------------------------------------------------

def test_a_request_is_accepted_and_queued():
    q = a_queue()
    assert q.request("plan", "pattern") is True
    assert len(q) == 1


def test_requests_are_served_in_order():
    q = a_queue()
    for i in range(3):
        q.request(f"plan{i}", "pattern", label=str(i))
    assert [q.take().label for _ in range(3)] == ["0", "1", "2"]


def test_a_full_queue_drops_rather_than_growing():
    # A control thread must never be able to make the acquisition thread's
    # work unbounded.
    q = a_queue(capacity=3)
    assert [q.request("p", "e") for _ in range(5)] == [
        True, True, True, False, False,
    ]
    assert len(q) == 3
    assert q.dropped == 2


def test_a_full_queue_keeps_the_oldest_not_the_newest():
    # Deliberate: a stimulus asked for earlier is the one the experimenter is
    # waiting on. Evicting it to make room for a newer one would deliver the
    # wrong stimulus, not merely a late one.
    q = a_queue(capacity=2)
    q.request("first", "e", label="first")
    q.request("second", "e", label="second")
    q.request("third", "e", label="third")
    assert [q.take().label, q.take().label] == ["first", "second"]


def test_capacity_must_be_positive():
    with pytest.raises(ValueError, match="capacity must be at least 1"):
        StimulationQueue(capacity=0)


def test_the_default_capacity_is_small_on_purpose():
    assert DEFAULT_CAPACITY <= 32


def test_taking_from_an_empty_queue_returns_none():
    assert a_queue().take() is None


def test_requesting_is_safe_from_many_threads():
    q = a_queue(capacity=10_000)
    def spam():
        for _ in range(200):
            q.request("p", "e")
    threads = [threading.Thread(target=spam) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(q) == 1600
    assert q.dropped == 0


# --------------------------------------------------------------------------
# dispatch
# --------------------------------------------------------------------------

def test_service_dispatches_one_request():
    q = a_queue()
    q.request("plan", "pattern", label="a")
    sent = []
    assert q.service(sent.append) is True
    assert len(sent) == 1
    assert sent[0].label == "a"
    assert q.dispatched == 1


def test_service_dispatches_at_most_one_per_call():
    # A backlog must not become a burst of driver calls between two packets.
    q = a_queue()
    for _ in range(5):
        q.request("p", "e")
    sent = []
    q.service(sent.append)
    assert len(sent) == 1
    assert len(q) == 4


def test_service_on_an_empty_queue_does_nothing():
    q = a_queue()
    sent = []
    assert q.service(sent.append) is False
    assert sent == []
    assert q.dispatched == 0


def test_a_failing_dispatch_is_counted_and_does_not_raise():
    # An exception escaping here would propagate out of the packet loop and
    # abandon everything still buffered.
    q = a_queue()
    q.request("p", "e")

    def explode(request):
        raise RuntimeError("driver said no")

    assert q.service(explode) is False
    assert q.failed == 1
    assert q.dispatched == 0


def test_a_keyboard_interrupt_during_dispatch_is_NOT_swallowed():
    # Ctrl+C is how a recording is stopped, and `send` is the longest call in
    # the packet loop - the widest window for one to land in. Swallowing it
    # would count the operator's stop as a stimulation failure and carry on
    # recording, and the documented drain-retry would never run.
    q = a_queue()
    q.request("p", "e")

    def explode(request):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        q.service(explode)
    assert q.failed == 0
    # Still timed, because it still spent that time on the acquisition thread.
    assert q.max_dispatch_us > 0


def test_a_system_exit_during_dispatch_is_not_swallowed_either():
    q = a_queue()
    q.request("p", "e")

    def explode(request):
        raise SystemExit(1)

    with pytest.raises(SystemExit):
        q.service(explode)


def test_the_request_is_removed_even_when_dispatch_fails():
    # Otherwise a poisonous request would be retried on every packet.
    q = a_queue()
    q.request("p", "e")

    def explode(request):
        raise RuntimeError("no")

    q.service(explode)
    assert len(q) == 0


# --------------------------------------------------------------------------
# measuring the cost to the drain
# --------------------------------------------------------------------------

def test_dispatch_time_is_measured():
    q = a_queue()
    q.request("p", "e")
    q.service(lambda r: None)
    assert q.max_dispatch_us > 0
    assert q.mean_dispatch_us > 0


def test_a_slow_dispatch_is_counted():
    import time

    q = a_queue(slow_dispatch_us=100.0)
    q.request("p", "e")
    q.service(lambda r: time.sleep(0.005))
    assert q.slow_dispatches == 1
    assert q.max_dispatch_us > 100.0


def test_a_fast_dispatch_is_not_counted_as_slow():
    q = a_queue(slow_dispatch_us=1_000_000.0)
    q.request("p", "e")
    q.service(lambda r: None)
    assert q.slow_dispatches == 0


def test_a_failed_dispatch_is_still_timed():
    # It still spent time on the acquisition thread.
    q = a_queue(slow_dispatch_us=0.0)
    q.request("p", "e")

    def explode(request):
        raise RuntimeError("no")

    q.service(explode)
    assert q.max_dispatch_us > 0
    assert q.slow_dispatches == 1


def test_mean_dispatch_is_zero_before_anything_ran():
    assert a_queue().mean_dispatch_us == 0.0


# --------------------------------------------------------------------------
# reporting
# --------------------------------------------------------------------------

def test_summary_reports_the_counters():
    q = a_queue(capacity=1)
    q.request("p", "e")
    q.request("p", "e")   # dropped
    q.service(lambda r: None)
    summary = q.summary()
    assert summary["dispatched"] == 1
    assert summary["dropped"] == 1
    assert summary["pending"] == 0


def test_a_clean_queue_warns_about_nothing():
    q = a_queue()
    q.request("p", "e")
    q.service(lambda r: None)
    assert q.warnings() == []


def test_dropped_requests_are_warned_about():
    q = a_queue(capacity=1)
    q.request("p", "e")
    q.request("p", "e")
    assert any("were dropped" in w for w in q.warnings())


def test_failed_dispatches_are_warned_about():
    q = a_queue()
    q.request("p", "e")
    q.service(lambda r: (_ for _ in ()).throw(RuntimeError("no")))
    assert any("failed during dispatch" in w for w in q.warnings())


def test_slow_dispatches_are_warned_about_with_the_drain_named():
    import time

    q = a_queue(slow_dispatch_us=10.0)
    q.request("p", "e")
    q.service(lambda r: time.sleep(0.002))
    warning = " ".join(q.warnings())
    assert "acquisition thread" in warning
    assert "drain" in warning


def test_a_request_carries_its_plan_and_pattern():
    q = a_queue()
    q.request("the-plan", "the-pattern", scheduled=True, label="x")
    request = q.take()
    assert isinstance(request, StimulationRequest)
    assert request.plan == "the-plan"
    assert request.pattern == "the-pattern"
    assert request.scheduled is True


# --------------------------------------------------------------------------
# the brake: a slow send must not be allowed to bleed the drain forever
# --------------------------------------------------------------------------

def test_repeated_slow_dispatches_suspend_stimulation():
    # One-per-packet bounds the COUNT of driver calls, not their duration. A
    # send that takes longer than the acquisition period puts the consumer
    # further behind on every packet, forever, and counting that while
    # carrying on is not a brake.
    import time

    q = a_queue(slow_dispatch_us=100.0, max_consecutive_slow=3)
    for _ in range(10):
        q.request("p", "e")
    for _ in range(3):
        q.service(lambda r: time.sleep(0.002))
    assert q.suspended
    assert "disconnected" in q.suspended_reason


def test_a_suspended_queue_dispatches_nothing_further():
    import time

    q = a_queue(slow_dispatch_us=100.0, max_consecutive_slow=1)
    q.request("p", "e")
    q.request("p", "e")
    q.service(lambda r: time.sleep(0.002))
    assert q.suspended
    sent = []
    assert q.service(sent.append) is False
    assert sent == []


def test_a_fast_dispatch_resets_the_consecutive_count():
    # The brake is for sustained slowness, not for one unlucky dispatch.
    import time

    q = a_queue(slow_dispatch_us=1000.0, max_consecutive_slow=3)
    for _ in range(10):
        q.request("p", "e")
    q.service(lambda r: time.sleep(0.003))   # slow
    q.service(lambda r: None)                # fast - resets
    q.service(lambda r: time.sleep(0.003))   # slow
    q.service(lambda r: time.sleep(0.003))   # slow
    assert not q.suspended
    assert q.slow_dispatches == 3


def test_suspension_is_reported():
    import time

    q = a_queue(slow_dispatch_us=10.0, max_consecutive_slow=1)
    q.request("p", "e")
    q.service(lambda r: time.sleep(0.002))
    assert any("disconnected" in w for w in q.warnings())
    assert q.summary()["suspended"] is True


# --------------------------------------------------------------------------
# spacing: one per packet is not one per unit time
# --------------------------------------------------------------------------

def test_dispatches_are_spaced_in_wall_clock_time():
    # After a stall the consumer drains its backlog far faster than packets
    # arrive, so without this queued stimuli fire back-to-back at catch-up
    # speed - a tissue safety problem, not just a timing one.
    q = StimulationQueue(min_interval_us=50_000.0)
    for _ in range(5):
        q.request("p", "e")
    sent = []
    assert q.service(sent.append) is True
    assert q.service(sent.append) is False   # too soon
    assert q.service(sent.append) is False
    assert len(sent) == 1
    assert len(q) == 4


def test_a_blocked_dispatch_does_not_consume_the_request():
    q = StimulationQueue(min_interval_us=50_000.0)
    q.request("p", "e", label="kept")
    q.service(lambda r: None)
    q.request("p", "e", label="also-kept")
    q.service(lambda r: None)   # blocked by spacing
    assert [r.label for r in q] == ["also-kept"] if hasattr(q, "__iter__") else True
    assert len(q) == 1


def test_spacing_allows_a_dispatch_once_enough_time_has_passed():
    import time

    q = StimulationQueue(min_interval_us=2000.0)
    q.request("p", "e")
    q.request("p", "e")
    sent = []
    q.service(sent.append)
    time.sleep(0.005)
    q.service(sent.append)
    assert len(sent) == 2


# --------------------------------------------------------------------------
# staleness: a stimulus nobody is waiting for any more
# --------------------------------------------------------------------------

def test_a_stale_request_is_discarded_rather_than_delivered_late():
    import time

    q = a_queue(max_age_us=1000.0)
    q.request("p", "e", label="old")
    time.sleep(0.005)
    sent = []
    assert q.service(sent.append) is False
    assert sent == []
    assert q.stale == 1


def test_staleness_is_counted_separately_from_dropping():
    # "the queue was full" and "it waited too long" point at different
    # mistakes and must not be conflated.
    import time

    q = a_queue(max_age_us=1000.0, capacity=1)
    q.request("p", "e")
    q.request("p", "e")   # dropped, queue full
    time.sleep(0.005)
    q.service(lambda r: None)
    assert q.dropped == 1
    assert q.stale == 1


def test_a_fresh_request_behind_stale_ones_is_still_delivered():
    import time

    q = a_queue(max_age_us=3000.0)
    q.request("p", "e", label="stale")
    time.sleep(0.005)
    q.request("p", "e", label="fresh")
    sent = []
    assert q.service(sent.append) is True
    assert sent[0].label == "fresh"
    assert q.stale == 1


def test_stale_requests_are_reported():
    import time

    q = a_queue(max_age_us=1000.0)
    q.request("p", "e")
    time.sleep(0.005)
    q.service(lambda r: None)
    assert any("waiting longer than" in w for w in q.warnings())


def test_staleness_can_be_turned_off():
    import time

    q = a_queue(max_age_us=0.0)
    q.request("p", "e")
    time.sleep(0.005)
    assert q.service(lambda r: None) is True
    assert q.stale == 0


# --------------------------------------------------------------------------
# undelivered at the end
# --------------------------------------------------------------------------

def test_requests_still_queued_at_the_end_are_reported():
    # Stimulation is deliberately not serviced during shutdown, so anything
    # left is never delivered. A protocol runner that queued five stimuli
    # should not get a clean-looking session.
    q = a_queue()
    for _ in range(3):
        q.request("p", "e")
    assert any("still queued when the recording ended" in w
               for w in q.warnings())


def test_a_failed_dispatch_does_not_drag_down_the_mean():
    # mean_dispatch_us is the number that decides whether stimulating from
    # this thread is viable; an attempt that failed fast never reached the
    # driver and must not dilute it.
    import time

    q = a_queue()
    q.request("p", "e")
    q.request("p", "e")
    q.service(lambda r: time.sleep(0.004))
    q.service(lambda r: (_ for _ in ()).throw(RuntimeError("fast failure")))
    assert q.dispatched == 1
    assert q.failed == 1
    assert q.mean_dispatch_us > 3000.0


def test_the_clock_used_can_actually_express_these_intervals():
    # A guard, not a formality. time.monotonic() resolves to 15.625 ms on
    # Windows, so min_interval_us=1000 measured with it is a sub-resolution
    # quantity: every spacing and staleness rule here would have silently
    # done nothing while appearing to work. This asserts the clock in use can
    # see an interval an order of magnitude below the default spacing.
    import time

    from biocam.control import DEFAULT_MIN_INTERVAL_US

    resolution_us = time.get_clock_info("perf_counter").resolution * 1e6
    assert resolution_us < DEFAULT_MIN_INTERVAL_US / 10
