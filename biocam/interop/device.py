"""Layer 1 - connecting to the BioCAM.

Nothing here can be executed without the instrument and the 3Brain DLLs. Every
.NET call is verified against API/3Brain.BioCamDriver.xml and the C# reference
sample rather than tested. Keep this module as small as it can be: it is the
only code in the acquisition path with no automated coverage.
"""

import time
from pathlib import Path

DEFAULT_DLL_DIR = (
    Path(__file__).resolve().parent.parent.parent / "BioCam_DupleX_API" / "API"
)

ASSEMBLIES = ("3Brain.Common", "3Brain.BioCamDriver")


def load_assemblies(dll_dir=None) -> None:
    """Load the 3Brain assemblies into the .NET runtime."""
    import os
    import sys

    dll_dir = Path(dll_dir or DEFAULT_DLL_DIR)
    path_str = str(dll_dir)
    # LOW: os.environ["PATH"] raises KeyError if PATH is unset in this
    # process's environment (e.g. a stripped-down launcher or test
    # harness) - unlikely on a normal Windows session, but not impossible,
    # and there is no reason this function should crash on it when
    # os.environ.get("PATH", "") makes the empty case just work.
    current_path = os.environ.get("PATH", "")
    if path_str not in current_path.split(os.pathsep):
        os.environ["PATH"] = path_str + os.pathsep + current_path
    if path_str not in sys.path:
        sys.path.insert(0, path_str)

    import pythonnet
    pythonnet.load("netfx")
    import clr

    for name in ASSEMBLIES:
        path = dll_dir / f"{name}.dll"
        if not path.is_file():
            raise FileNotFoundError(f"assembly not found: {path}")
        clr.AddReference(str(path))


def cycles_per_us_of(device):
    """The instrument's clock cycles per microsecond, or None.

    Derived from `IBioCam.ClockCyclesToMilliseconds(UInt64)` (XML:4667),
    which the sample uses for exactly this (MainForm.cs:272). This lives on
    the device rather than the stimulator because that is where the member
    is: a recording-only session still needs the factor, and without it the
    acquisition clock has to calibrate its own - which makes its cross-check
    incapable of failing.

    Returns None rather than raising. A factor that cannot be read is a
    reason to say the times are unresolved, not to abandon a session.
    """
    try:
        probe = 1_000_000
        milliseconds = float(device.biocam.ClockCyclesToMilliseconds(probe))
    except BaseException:  # noqa: BLE001 - an unreadable factor is not fatal
        return None
    if milliseconds <= 0:
        return None
    return probe / (milliseconds * 1000.0)


class BioCamDevice:
    """Claims a BioCAM for the duration of a with-block."""

    def __init__(self, dll_dir=None, timeout_sec: int = 30):
        self._dll_dir = dll_dir
        self._timeout_sec = timeout_sec
        self._pool = None
        self._slot_index = -1
        self.biocam = None

    def __enter__(self):
        load_assemblies(self._dll_dir)
        from _3Brain.BioCamDriver import BioCamPool

        self._pool = BioCamPool
        # issue #17: the XML documents only Activate(System.Boolean)
        # (param supportBioCamInvalidSerial) - 3Brain's own sample
        # (MainForm.cs:72) calls Activate() with no arguments, relying on
        # a C# default parameter value the XML does not state anywhere in
        # this repo. C# supplies that default automatically; whether
        # pythonnet does the same for a zero-argument call from Python is
        # unverified here - untested, worth confirming in the lab. Try the
        # no-argument form first, mirroring the sample as closely as
        # pythonnet allows; if pythonnet does not fill the default,
        # Activate() raises TypeError (missing required argument) rather
        # than silently doing the wrong thing, and the fallback below
        # supplies an explicit value. We do not know what C#'s actual
        # default is, so `False` here is a guess, not a verified value -
        # chosen only because "do not support invalid-serial BioCAMs"
        # reads as the more conservative default for a flag named
        # supportBioCamInvalidSerial.
        #
        # MEDIUM 5: asking the lab "confirm which path is taken" without
        # ever reporting anything left nobody able to answer it.
        # BioCamPool.SupportBioCamWithInvalidSerial (XML:2514, "Gets
        # whether the BioCAM pool should support BioCAM with non valid
        # serial") reads back exactly what Activate() set, so print the
        # branch taken and that property's value right after the call - a
        # single run then answers issue #17 outright instead of leaving it
        # open. `except TypeError` stays narrow on purpose: if the
        # no-argument form raised something other than a non-binding
        # TypeError (e.g. it partially ran before raising), a second call
        # to Activate() below could re-run work the first call already
        # did - unverified here, since neither Activate() overload's
        # partial-failure behaviour is documented in this repo.
        try:
            BioCamPool.Activate()
            print(
                "BioCamDevice: Activate() succeeded with no arguments "
                "(pythonnet supplied the C# default). "
                "SupportBioCamWithInvalidSerial="
                f"{BioCamPool.SupportBioCamWithInvalidSerial!r}"
            )
        except TypeError:
            BioCamPool.Activate(False)
            print(
                "BioCamDevice: Activate() raised TypeError with no "
                "arguments (pythonnet did not supply the C# default); "
                "fell back to Activate(False). "
                "SupportBioCamWithInvalidSerial="
                f"{BioCamPool.SupportBioCamWithInvalidSerial!r}"
            )

        deadline = time.time() + self._timeout_sec
        # MEDIUM 4: found_slot_index is a plain local, not self._slot_index,
        # until TakeBioCamControl below has actually returned a live
        # handle. Identifying a free slot index is not the same as holding
        # it - the sample only releases a slot it holds. Setting
        # self._slot_index this early meant a TakeBioCamControl failure
        # (or the checks that follow it) still ran __exit__ as if a slot
        # had been taken: it would call ReleaseBioCamControl on a slot
        # never claimed, and a failure from that release call would
        # replace the carefully-worded "close BrainWave" message below
        # with whatever ReleaseBioCamControl raises instead.
        found_slot_index = -1
        while time.time() < deadline:
            free = list(BioCamPool.GetSlotIndexesFreeBioCam())
            if free:
                found_slot_index = free[0]
                break
            time.sleep(0.5)
        else:
            BioCamPool.Deactivate()
            raise TimeoutError(
                "No free BioCAM found. Check USB, power, and that BrainWave "
                "is closed - it holds the device."
            )

        # From here on, any failure - including one that raises instead of
        # returning falsy, which the XML doc does not rule out - must still
        # release the slot and deactivate the pool. Otherwise the BioCAM
        # stays claimed until the process dies, and the next person on the
        # instrument finds it held by nothing.
        try:
            self.biocam = BioCamPool.TakeBioCamControl(found_slot_index)
            if self.biocam is None:
                raise RuntimeError(
                    "TakeBioCamControl returned nothing. Close BrainWave or "
                    "any other 3Brain software and try again."
                )
            # MEDIUM 4: only now, with a live handle in hand, does
            # __exit__'s ReleaseBioCamControl(self._slot_index) become
            # correct to call - so this is the earliest point this is set.
            self._slot_index = found_slot_index
            if not self.biocam.IsConnected:
                raise RuntimeError("BioCAM reports it is not connected.")
            if not self.biocam.MeaPlate.IsConnected:
                raise RuntimeError("The MEA plate is not seated.")
        except Exception:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, exc_type, exc, tb):
        if self._pool is None:
            # __enter__ never got as far as BioCamPool.Activate() (e.g.
            # load_assemblies() itself failed). Nothing was claimed.
            self._slot_index = -1
            self.biocam = None
            return False
        try:
            if self._slot_index >= 0:
                self._pool.ReleaseBioCamControl(self._slot_index)
        finally:
            self._slot_index = -1
            self.biocam = None
            self._pool.Deactivate()
        return False

    @property
    def data_format(self):
        if self.biocam is None:
            # LOW: without this check, the attribute access below raises a
            # plain "'NoneType' object has no attribute 'DataFormat'" -
            # the same class of opaque AttributeError source.start() was
            # already fixed to name explicitly (device.biocam is None
            # because __enter__ never completed, or __exit__ already ran).
            raise RuntimeError(
                "BioCamDevice.data_format accessed with biocam is None; a "
                "BioCamDevice must be successfully __enter__()'d (claiming "
                "the device) before reading its data format."
            )
        return self.biocam.DataFormat
