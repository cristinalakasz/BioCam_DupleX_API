"""Detecting lost data from the instrument's own packet counter.

DataPacketHeader carries a UInt16 PacketCounter alongside the timestamp,
confirmed from its constructor signature in 3Brain.BioCamDriver.xml:
    #ctor(Byte, BioCamUsbComSignalType, Int32, UInt64, UInt16)
being (Reserved, SignalType, PayloadLength, Timestamp, PacketCounter).

That makes loss detection exact rather than inferred: no clock arithmetic, no
tolerance threshold. The counter wraps at 65536, which is handled explicitly -
that wrap is the kind of edge case that works for hours and then does not.
"""

from dataclasses import dataclass
from typing import List, Optional

COUNTER_MODULUS = 65536


@dataclass(frozen=True)
class Gap:
    """A run of packets that never arrived."""
    after_frame: int
    missing_frames: int
    duration_ms: float


def packets_lost(previous_counter: int, counter: int) -> int:
    """How many packets are missing between two counter values.

    Returns 0 for consecutive counters and for a repeated counter. A repeat is
    treated as a duplicate rather than as a full 65536-packet wrap, because a
    duplicate is plausible and losing exactly one modulus is not.
    """
    delta = (counter - previous_counter) % COUNTER_MODULUS
    if delta == 0:
        return 0
    return delta - 1


class GapTracker:
    """Accumulates gaps across a recording.

    frames_in_packet is taken from the packet being observed. Packet size is
    fixed for the duration of a session, so the packets that went missing are
    assumed to have carried the same number of frames as the one that followed
    them.
    """

    def __init__(self, frame_rate_hz: float):
        self._frame_rate_hz = frame_rate_hz
        self._previous_counter: Optional[int] = None
        self._gaps: List[Gap] = []
        self._n_frames_missing = 0

    def observe(self, counter: int, frames_in_packet: int,
                frames_written: int) -> Optional[Gap]:
        """Record a packet. Returns a Gap if packets were lost before it."""
        previous = self._previous_counter
        self._previous_counter = counter
        if previous is None:
            return None

        lost = packets_lost(previous, counter)
        if lost == 0:
            return None

        missing_frames = lost * frames_in_packet
        gap = Gap(
            after_frame=frames_written,
            missing_frames=missing_frames,
            duration_ms=missing_frames / self._frame_rate_hz * 1000.0,
        )
        self._gaps.append(gap)
        self._n_frames_missing += missing_frames
        return gap

    @property
    def gaps(self) -> List[Gap]:
        return list(self._gaps)

    @property
    def n_frames_missing(self) -> int:
        return self._n_frames_missing
