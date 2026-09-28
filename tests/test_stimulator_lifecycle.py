"""Fake-driver tests for the Stimulator's lifecycle control flow.

Like tests/test_device.py: the .NET calls themselves are Layer 1 and cannot be
exercised here. What can is the Python-side order - which of Initialize,
Start, Stop and Close run, in what order, and which still run when one of
them fails. Fake `_3Brain.BioCamDriver` in sys.modules; no DLLs, no clr.
"""

import sys
import types

import pytest

from biocam.interop.stimulator import Stimulator, StimulatorError


class FakeStim:
    """Records every lifecycle call. Each can be made to fail."""

    def __init__(self):
        self.calls = []
        self.IsInitialized = False
        self.IsStimulating = False
        self.start_raises = None
        self.start_returns = True

    def IsAvailable(self):
        return True

    def Initialize(self, protocol_type):
        self.calls.append(("Initialize", protocol_type))
        return True

    def Start(self):
        self.calls.append("Start")
        if self.start_raises is not None:
            raise self.start_raises
        return self.start_returns

    def Stop(self):
        self.calls.append("Stop")
        return True

    def Close(self):
        self.calls.append("Close")
        return True


@pytest.fixture
def stim(monkeypatch):
    fake_pkg = types.ModuleType("_3Brain")
    fake_mod = types.ModuleType("_3Brain.BioCamDriver")
    fake_mod.StimProtocolType = types.SimpleNamespace(RealTime="RealTime")
    monkeypatch.setitem(sys.modules, "_3Brain", fake_pkg)
    monkeypatch.setitem(sys.modules, "_3Brain.BioCamDriver", fake_mod)
    fake = FakeStim()
    device = types.SimpleNamespace(
        biocam=types.SimpleNamespace(Stimulator=fake, IsStreaming=True))
    return fake, device


def test_the_full_lifecycle_runs_in_the_samples_order(stim):
    fake, device = stim
    with Stimulator(device) as s:
        with s.stimulating():
            pass
    assert fake.calls == [("Initialize", "RealTime"), "Start", "Stop", "Close"]


def test_a_start_that_raises_is_still_stopped(stim):
    # Start() may have engaged the stimulator before raising. Without a
    # Stop(), a window that keeps the Stimulator open would call Start()
    # again on the next recording - and the XML documents that as throwing
    # "already started", on every retry, until the window closes.
    fake, device = stim
    fake.start_raises = RuntimeError("driver-side failure")
    s = Stimulator(device).__enter__()
    with pytest.raises(StimulatorError, match="driver-side failure"):
        with s.stimulating():
            pass
    assert fake.calls[-1] == "Stop"
    assert not s.is_stimulating

    fake.start_raises = None
    with s.stimulating():
        pass
    s.__exit__(None, None, None)
    assert fake.calls[-3:] == ["Start", "Stop", "Close"]


def test_a_start_that_returns_false_is_not_treated_as_started(stim):
    fake, device = stim
    fake.start_returns = False
    with Stimulator(device) as s:
        with pytest.raises(StimulatorError, match="returned false"):
            s.start()
        assert not s.is_stimulating
    assert fake.calls[-1] == "Close"


def test_an_already_initialized_stimulator_is_refused_untouched(stim):
    fake, device = stim
    fake.IsInitialized = True
    with pytest.raises(StimulatorError, match="already initialized"):
        Stimulator(device).__enter__()
    assert fake.calls == []


def test_attach_log_replaces_the_log_for_the_next_session(stim):
    from biocam.stim import StimulusLog

    _, device = stim
    s = Stimulator(device, log=StimulusLog())
    fresh = StimulusLog()
    s.attach_log(fresh)
    assert s._log is fresh


# --------------------------------------------------------------------------
# endpoints: only valid, internal ones reach Send (Gate 2)
# --------------------------------------------------------------------------

@pytest.fixture
def endpoint_modules(monkeypatch):
    system = types.ModuleType("System")
    system.Array = {object: list}
    system.Array = type("A", (), {"__class_getitem__": classmethod(
        lambda cls, t: list)})
    common = types.ModuleType("_3Brain.Common")
    common.ChCoord = lambda r, c: (r, c)
    driver = sys.modules.get("_3Brain.BioCamDriver") or types.ModuleType(
        "_3Brain.BioCamDriver")
    driver.StimEndPoint = object
    monkeypatch.setitem(sys.modules, "System", system)
    monkeypatch.setitem(sys.modules, "_3Brain.Common", common)
    monkeypatch.setitem(sys.modules, "_3Brain.BioCamDriver", driver)


def _stimulator_returning(endpoints):
    net = types.SimpleNamespace(
        GetInternalEndPoint=lambda coord: endpoints[coord])
    s = Stimulator(types.SimpleNamespace(biocam=types.SimpleNamespace()))
    s._stimulator = net
    return s


def _endpoint(valid=True, internal=True):
    return types.SimpleNamespace(IsValid=valid, IsInternal=internal)


def test_valid_internal_endpoints_are_passed_through(stim, endpoint_modules):
    from biocam.stim import Electrode

    s = _stimulator_returning({(1, 2): _endpoint(), (3, 4): _endpoint()})
    built = s._build_endpoints([Electrode(1, 2), Electrode(3, 4)])
    assert len(built) == 2


@pytest.mark.parametrize("flags", [dict(valid=False), dict(internal=False)])
def test_an_invalid_or_external_endpoint_is_refused_before_send(
        stim, endpoint_modules, flags):
    # The XML documents no null return from GetInternalEndPoint - only
    # StimEndPoint.IsValid and IsInternal. Checking for None alone let an
    # endpoint the driver itself marks invalid go into Send.
    from biocam.stim import Electrode

    s = _stimulator_returning({(1, 2): _endpoint(**flags)})
    with pytest.raises(StimulatorError, match="1,2|\\(1, 2\\)|1, 2"):
        s._build_endpoints([Electrode(1, 2)])


def test_ctrl_c_during_a_driver_read_is_not_turned_into_a_driver_error(stim):
    # _read wraps driver failures as StimulatorError, which callers on the
    # recording thread count and carry on from. Ctrl+C must stay Ctrl+C.
    def interrupted():
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        Stimulator._read("anything", interrupted)
