import threading

import pytest

from biocam.control import (
    DEFAULT_CAPACITY,
    StimulationQueue,
    StimulationRequest,
)


def a_queue(**kwargs):
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


def test_a_dispatch_raising_baseexception_is_still_contained():
    q = a_queue()
    q.request("p", "e")

    def explode(request):
        raise KeyboardInterrupt

    assert q.service(explode) is False
    assert q.failed == 1


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
