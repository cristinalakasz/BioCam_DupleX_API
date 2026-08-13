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
    os.environ["PATH"] = str(dll_dir) + os.pathsep + os.environ["PATH"]
    if str(dll_dir) not in sys.path:
        sys.path.insert(0, str(dll_dir))

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
        BioCamPool.Activate()

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

        self.biocam = BioCamPool.TakeBioCamControl(self._slot_index)
        if self.biocam is None:
            BioCamPool.Deactivate()
            raise RuntimeError(
                "TakeBioCamControl returned nothing. Close BrainWave or any "
                "other 3Brain software and try again."
            )
        if not self.biocam.IsConnected:
            self.__exit__(None, None, None)
            raise RuntimeError("BioCAM reports it is not connected.")
        if not self.biocam.MeaPlate.IsConnected:
            self.__exit__(None, None, None)
            raise RuntimeError("The MEA plate is not seated.")
        return self

    def __exit__(self, exc_type, exc, tb):
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
