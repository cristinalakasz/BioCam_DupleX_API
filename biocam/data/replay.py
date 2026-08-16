"""Replaying a recorded file as if it were arriving from the instrument.

This is what makes a whole recording session testable without hardware. The
session consumes an iterable of packets; the driver provides one and this
provides another, reading a .raw file and chopping it into packets. Losses can
be injected to exercise the gap detection.

The counter advances for dropped packets, exactly as the instrument's would -
that is precisely how a gap becomes detectable.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Sequence

from biocam.data.integrity import COUNTER_MODULUS
from biocam.data.recording import AcquisitionParameters


@dataclass(frozen=True, slots=True)
class Packet:
    """One packet's worth of acquired data.

    LOW: `slots=True` (added to `dataclass` in 3.10, so requires nothing
    beyond the MIN_PYTHON floor in biocam/preflight.py) removes the
    per-instance `__dict__` a plain dataclass otherwise carries. One Packet
    is allocated per callback on the driver's own thread (see
    biocam/interop/source.py's on_data), so the saving is free there and
    costs nothing anywhere else - a frozen dataclass never gains attributes
    dynamically, so nothing here relied on `__dict__` existing.
    """

    timestamp: int
    counter: int
    payload: bytes


class ReplayPacketSource:
    """Emits a .raw file as a sequence of packets."""

    def __init__(self, raw_path, params: AcquisitionParameters,
                 frames_per_packet: int = 20,
                 drop_packets: Sequence[int] = ()):
        self._raw_path = Path(raw_path)
        self._params = params
        self._frames_per_packet = frames_per_packet
        self._drop = set(drop_packets)

    def __iter__(self) -> Iterator[Packet]:
        chunk_bytes = self._frames_per_packet * self._params.bytes_per_frame
        timestamp = 0
        counter = 0
        with open(self._raw_path, "rb") as handle:
            while True:
                payload = handle.read(chunk_bytes)
                if not payload:
                    return
                index = counter
                counter = (counter + 1) % COUNTER_MODULUS
                timestamp += self._frames_per_packet
                if index in self._drop:
                    continue
                yield Packet(timestamp=timestamp, counter=index, payload=payload)
