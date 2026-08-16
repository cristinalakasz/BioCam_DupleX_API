"""Fake-driver tests for the Python-side control flow in BioCamDevice.

`biocam/interop/device.py` is Layer 1 - the only package permitted to import
`clr` - and CLAUDE.md is explicit that Layer 1 "cannot be tested here". That is
true of the .NET calls themselves (TakeBioCamControl's real behaviour,
Activate()'s real default, etc.) and none of that is exercised below.

What *is* Python-side control flow - the order __enter__/__exit__ call things
in, which branch Activate()'s except clause takes, whether a property guards
None - can be exercised the same way tests/test_source.py already exercises
biocam/interop/source.py: by faking the .NET surface device.py touches,
never by loading the real 3Brain assemblies or pythonnet.

The one wrinkle specific to this module: `__enter__` does
`from _3Brain.BioCamDriver import BioCamPool` as a plain Python import
statement, not a duck-typed attribute access. Python resolves that via
sys.modules before ever touching pythonnet/clr, so installing fake `_3Brain`
and `_3Brain.BioCamDriver` modules there - and monkeypatching
`load_assemblies` to a no-op - lets `__enter__`/`__exit__` run to completion
against a fake BioCamPool with no DLLs, no clr, and no pythonnet involved at
any point. That satisfies test_no_hardware_imports.py's guard (this file
imports no `clr`/`pythonnet`/`clr_loader` itself) and CLAUDE.md's constraint
(nothing here claims to know how the *real* BioCamPool behaves).
"""

import sys
import types

import pytest

import biocam.interop.device as device_module
from biocam.interop.device import BioCamDevice


class FakeBioCamPool:
    """Stands in for _3Brain.BioCamDriver.BioCamPool.

    A class (not an instance) because device.py uses BioCamPool as a static
    surface (`from _3Brain.BioCamDriver import BioCamPool`, then calls
    directly on it) - mirrored here with classmethods so `self._pool` in
    device.py is this class itself, matching the real usage shape.
    """

    activate_calls = []
    take_calls = []
    release_calls = []
    deactivate_calls = 0
    free_slots = [0]
    take_result = None
    take_raises = None
    # Read directly as BioCamPool.SupportBioCamWithInvalidSerial in
    # device.py - a plain class attribute, not a property, since device.py
    # accesses it on the class itself, never on an instance.
    SupportBioCamWithInvalidSerial = False
    no_arg_activate_raises_type_error = False

    @classmethod
    def reset(cls):
        cls.activate_calls = []
        cls.take_calls = []
        cls.release_calls = []
        cls.deactivate_calls = 0
        cls.free_slots = [0]
        cls.take_result = _FakeBioCam()
        cls.take_raises = None
        cls.SupportBioCamWithInvalidSerial = False
        cls.no_arg_activate_raises_type_error = False

    @classmethod
    def Activate(cls, *args):
        cls.activate_calls.append(args)
        if not args and cls.no_arg_activate_raises_type_error:
            raise TypeError("missing required argument")

    @classmethod
    def Deactivate(cls):
        cls.deactivate_calls += 1

    @classmethod
    def GetSlotIndexesFreeBioCam(cls):
        return list(cls.free_slots)

    @classmethod
    def TakeBioCamControl(cls, slot_index):
        cls.take_calls.append(slot_index)
        if cls.take_raises is not None:
            raise cls.take_raises
        return cls.take_result

    @classmethod
    def ReleaseBioCamControl(cls, slot_index):
        cls.release_calls.append(slot_index)


class _FakeBioCam:
    """A minimal stand-in for the object TakeBioCamControl returns."""

    def __init__(self, is_connected=True, mea_plate_connected=True):
        self.IsConnected = is_connected
        self.MeaPlate = types.SimpleNamespace(IsConnected=mea_plate_connected)


@pytest.fixture
def fake_pool(monkeypatch):
    FakeBioCamPool.reset()
    monkeypatch.setattr(device_module, "load_assemblies", lambda dll_dir=None: None)
    # No test here relies on the real 0.5 s poll interval between slot
    # checks (free_slots is either populated up front or empty for the
    # whole timeout test below); removing it keeps the timeout test fast
    # and deterministic instead of depending on a timeout_sec=0 race
    # against wall-clock time.
    monkeypatch.setattr(device_module.time, "sleep", lambda seconds: None)
    fake_pkg = types.ModuleType("_3Brain")
    fake_mod = types.ModuleType("_3Brain.BioCamDriver")
    fake_mod.BioCamPool = FakeBioCamPool
    monkeypatch.setitem(sys.modules, "_3Brain", fake_pkg)
    monkeypatch.setitem(sys.modules, "_3Brain.BioCamDriver", fake_mod)
    return FakeBioCamPool


def test_enter_claims_slot_and_exit_releases_it(fake_pool):
    device = BioCamDevice()
    with device as entered:
        assert entered is device
        assert device.biocam is fake_pool.take_result
        assert fake_pool.take_calls == [0]

    assert fake_pool.release_calls == [0]
    assert fake_pool.deactivate_calls == 1
    assert device.biocam is None


def test_take_biocam_control_returning_none_does_not_release_an_unheld_slot(fake_pool):
    # MEDIUM 4: TakeBioCamControl returning None means no control was ever
    # taken. __exit__'s cleanup must not call ReleaseBioCamControl on the
    # slot that was merely *identified* as free - the sample only releases
    # a slot it holds, and a spurious release call could replace the
    # "close BrainWave" message with whatever ReleaseBioCamControl raises.
    fake_pool.take_result = None

    with pytest.raises(RuntimeError, match="TakeBioCamControl returned nothing"):
        with BioCamDevice():
            pass

    assert fake_pool.take_calls == [0]
    assert fake_pool.release_calls == []
    assert fake_pool.deactivate_calls == 1


def test_take_biocam_control_raising_does_not_release_an_unheld_slot(fake_pool):
    # Same reasoning as above, for the "raises instead of returning falsy"
    # case __enter__'s own comment says the XML does not rule out.
    fake_pool.take_raises = RuntimeError("driver-side failure")

    with pytest.raises(RuntimeError, match="driver-side failure"):
        with BioCamDevice():
            pass

    assert fake_pool.take_calls == [0]
    assert fake_pool.release_calls == []
    assert fake_pool.deactivate_calls == 1


def test_not_connected_after_successful_take_still_releases_the_held_slot(fake_pool):
    # Once TakeBioCamControl has actually returned a live handle, control
    # really was taken - so a later check failing (IsConnected here) must
    # still release it, unlike the two tests above.
    fake_pool.take_result = _FakeBioCam(is_connected=False)

    with pytest.raises(RuntimeError, match="not connected"):
        with BioCamDevice():
            pass

    assert fake_pool.take_calls == [0]
    assert fake_pool.release_calls == [0]
    assert fake_pool.deactivate_calls == 1


def test_mea_plate_not_seated_after_successful_take_still_releases_the_held_slot(fake_pool):
    fake_pool.take_result = _FakeBioCam(mea_plate_connected=False)

    with pytest.raises(RuntimeError, match="MEA plate is not seated"):
        with BioCamDevice():
            pass

    assert fake_pool.take_calls == [0]
    assert fake_pool.release_calls == [0]
    assert fake_pool.deactivate_calls == 1


def test_no_free_slot_times_out_without_ever_calling_take_or_release(fake_pool):
    fake_pool.free_slots = []

    with pytest.raises(TimeoutError, match="No free BioCAM found"):
        BioCamDevice(timeout_sec=0).__enter__()

    assert fake_pool.take_calls == []
    assert fake_pool.release_calls == []
    assert fake_pool.deactivate_calls == 1


def test_activate_reports_the_no_argument_path_and_the_readback(fake_pool, capsys):
    # MEDIUM 5: the no-argument Activate() path must report which branch
    # ran and SupportBioCamWithInvalidSerial's value afterwards - not leave
    # the lab with nothing to answer issue #17 with.
    fake_pool.no_arg_activate_raises_type_error = False
    fake_pool.SupportBioCamWithInvalidSerial = True

    with BioCamDevice():
        pass

    assert fake_pool.activate_calls == [()]
    console = capsys.readouterr().out
    assert "Activate() succeeded with no arguments" in console
    assert "SupportBioCamWithInvalidSerial=True" in console


def test_activate_reports_the_fallback_path_and_the_readback(fake_pool, capsys):
    fake_pool.no_arg_activate_raises_type_error = True
    fake_pool.SupportBioCamWithInvalidSerial = False

    with BioCamDevice():
        pass

    assert fake_pool.activate_calls == [(), (False,)]
    console = capsys.readouterr().out
    assert "fell back to Activate(False)" in console
    assert "SupportBioCamWithInvalidSerial=False" in console


def test_data_format_raises_a_named_error_not_attributeerror_when_unclaimed():
    # LOW: mirrors source.start()'s own fix for the same class of opaque
    # AttributeError - accessing data_format before (or after) a live
    # BioCamDevice context must name the actual mistake.
    device = BioCamDevice()
    with pytest.raises(RuntimeError, match="biocam is None"):
        device.data_format


def test_data_format_reads_through_to_biocam_dataformat_when_claimed(fake_pool):
    fake_pool.take_result = _FakeBioCam()
    fake_pool.take_result.DataFormat = "SENTINEL_FORMAT"

    with BioCamDevice() as device:
        assert device.data_format == "SENTINEL_FORMAT"
