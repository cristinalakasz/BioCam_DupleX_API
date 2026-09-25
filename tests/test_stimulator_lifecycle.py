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
