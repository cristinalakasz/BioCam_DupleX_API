"""Detecting lost data from the instrument's own packet counter.

DataPacketHeader carries a UInt16 PacketCounter alongside the timestamp.
The XML gives it only through a constructor signature:
    #ctor(Byte, BioCamUsbComSignalType, Int32, UInt64, UInt16)
being (Reserved, SignalType, PayloadLength, Timestamp, PacketCounter) - which
is an inference about a *parameter*, not a statement about the property.

Reflection settles it directly (DLLs, no instrument):
    UInt16 PacketCounter  (get/set)
so COUNTER_MODULUS below is a read fact rather than a deduction. Reproduce
with `python -m biocam.interop.reflect DataPacketHeader`.

That makes loss detection exact rather than inferred: no clock arithmetic, no
tolerance threshold. The counter wraps at 65536, which is handled explicitly -
that wrap is the kind of edge case that works for hours and then does not.
"""

from dataclasses import dataclass
from typing import List, Optional

COUNTER_MODULUS = 65536
COUNTER_ANOMALY_THRESHOLD = COUNTER_MODULUS // 2

# Gate 1, item H: caps how many Gap objects GapTracker retains in memory.
# Under persistent loss, one Gap is recorded per packet - roughly 14 million
# objects over four hours at a 1 ms packet period - and serialising a list
# that size at finalise() (json.dumps of that many dicts) would risk a
# MemoryError raised straight into the failure path, right when the run
# most needs an honest sidecar. 100,000 retained gaps is already far more
# detail than any operator will read, and small enough (a Gap is three
# scalar fields) that the retained list stays a rounding error in memory and
# fast to serialise. Anything beyond the cap is not lost information: it is
# still counted (gaps_truncated) and still contributes to n_frames_missing
# and to any GapDetected/GapSummary emitted to a listener (see
# RecordingWriter) - only the *retained list* is bounded.
MAX_RETAINED_GAPS = 100_000
# Deltas exceeding this threshold likely represent out-of-order packets, device
# resets, or anomalous steps rather than genuine loss. A delta > 32768 means
# more than half the counter space was traversed in a single jump — implausible
# at normal packet rates (1ms intervals would require ~32 seconds of consecutive
# loss). We treat such steps as counter anomalies, not loss.


@dataclass(frozen=True)
class Gap:
    """A run of packets that never arrived."""
    after_frame: int
    missing_frames: int
    duration_ms: float


def packets_lost(previous_counter: int, counter: int) -> int:
    """How many packets are missing between two counter values.

    Returns 0 for consecutive counters, repeated counters, and anomalous steps.
    A repeated counter is treated as a duplicate rather than as a full 65536-packet
    wrap, because a duplicate is plausible and losing exactly one modulus is not.
    An anomalous step (delta > COUNTER_ANOMALY_THRESHOLD) is likely an out-of-order
    packet, device reset, or transient issue; we return 0 and let GapTracker record
    the anomaly separately.
    """
    delta = (counter - previous_counter) % COUNTER_MODULUS
    if delta == 0:
        return 0
    if delta > COUNTER_ANOMALY_THRESHOLD:
        return 0
    return delta - 1


class GapTracker:
    """Accumulates gaps across a recording.

    frames_in_packet is taken from the packet being observed. Packet size is
    fixed for the duration of a session, so the packets that went missing are
    assumed to have carried the same number of frames as the one that followed
    them.
    """

    def __init__(self, frame_rate_hz: float,
                 max_retained_gaps: int = MAX_RETAINED_GAPS):
        self._frame_rate_hz = frame_rate_hz
        self._previous_counter: Optional[int] = None
        self._gaps: List[Gap] = []
        self._max_retained_gaps = max_retained_gaps
        self._gaps_truncated = 0
        self._n_frames_missing = 0
        self._counter_anomalies = 0

    def observe(self, counter: int, frames_in_packet: int,
                frames_written: int) -> Optional[Gap]:
        """Record a packet. Returns a Gap if packets were lost before it.

        The returned Gap is always the real one detected, even once the
        retained list is full - only what gets appended to self._gaps is
        capped (item H); the caller (RecordingWriter) still sees, and can
        still emit, every gap as it happens.
        """
        previous = self._previous_counter
        self._previous_counter = counter
        if previous is None:
            return None

        delta = (counter - previous) % COUNTER_MODULUS
        if delta > COUNTER_ANOMALY_THRESHOLD:
            self._counter_anomalies += 1
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
        if len(self._gaps) < self._max_retained_gaps:
            self._gaps.append(gap)
        else:
            self._gaps_truncated += 1
        self._n_frames_missing += missing_frames
        return gap

    @property
    def gaps(self) -> List[Gap]:
        return list(self._gaps)

    @property
    def has_gaps(self) -> bool:
        """Whether any gap has been observed - O(1), unlike `bool(gaps)`.

        Must also account for gaps_truncated: a gap that was detected but
        not retained in the list (because the cap was already full) is
        still a real gap that happened, so this must not read False just
        because the retained list itself is empty in some hypothetical
        max_retained_gaps=0 configuration.
        """
        return bool(self._gaps) or self._gaps_truncated > 0

    @property
    def n_gaps(self) -> int:
        """Total gaps observed, retained or not - O(1), unlike `len(gaps)`."""
        return len(self._gaps) + self._gaps_truncated

    @property
    def gaps_truncated(self) -> int:
        """Gaps detected after the retained list reached its cap.

        Real, counted loss that did not make it into `gaps` - see
        MAX_RETAINED_GAPS above. Always 0 while len(gaps) < max_retained_gaps.
        """
        return self._gaps_truncated

    @property
    def n_frames_missing(self) -> int:
        return self._n_frames_missing

    @property
    def counter_anomalies(self) -> int:
        return self._counter_anomalies
