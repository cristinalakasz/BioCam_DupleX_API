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
    if path_str not in os.environ["PATH"].split(os.pathsep):
        os.environ["PATH"] = path_str + os.pathsep + os.environ["PATH"]
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
        # supportBioCamInvalidSerial. Flag for the lab: confirm which path
        # is taken, and if the fallback runs, confirm False matches
        # 3Brain's real default.
        try:
            BioCamPool.Activate()
        except TypeError:
            BioCamPool.Activate(False)

        deadline = time.time() + self._timeout_sec
        while time.time() < deadline:
            free = list(BioCamPool.GetSlotIndexesFreeBioCam())
            if free:
                self._slot_index = free[0]
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
            self.biocam = BioCamPool.TakeBioCamControl(self._slot_index)
            if self.biocam is None:
                raise RuntimeError(
                    "TakeBioCamControl returned nothing. Close BrainWave or "
                    "any other 3Brain software and try again."
                )
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
        return self.biocam.DataFormat
