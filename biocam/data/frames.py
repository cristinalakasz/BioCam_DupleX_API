"""Turning payload bytes into frames.

A frame holds one sample from every channel. Payloads do not necessarily end on
a frame boundary, so a decoder that discards the remainder desynchronises
everything after it - that is Appendix A defect 5. FrameDecoder carries the
remainder into the next call instead.

The recorder does NOT use this. It appends payload bytes untouched, which is
why the defect cannot occur there at all. This is for reading files and for
Phase 5's online path, where packets genuinely are decoded as they arrive.
"""

import numpy as np

DTYPE_BY_BYTE_SIZE = {1: np.uint8, 2: np.uint16, 4: np.uint32}


class FrameDecoder:
    """Decodes payload bytes into whole frames, carrying any partial frame."""

    def __init__(self, total_channels: int, ch_sample_byte_size: int):
        if ch_sample_byte_size not in DTYPE_BY_BYTE_SIZE:
            raise ValueError(
                f"ch_sample_byte_size={ch_sample_byte_size} is not supported; "
                f"expected one of {sorted(DTYPE_BY_BYTE_SIZE)}"
            )
        self._total_channels = total_channels
        self._dtype = DTYPE_BY_BYTE_SIZE[ch_sample_byte_size]
        self._bytes_per_frame = total_channels * ch_sample_byte_size
        self._pending = b""

    def decode(self, payload: bytes) -> np.ndarray:
        """Return the whole frames available, holding any remainder back."""
        buffer = self._pending + bytes(payload)
        n_frames = len(buffer) // self._bytes_per_frame
        used = n_frames * self._bytes_per_frame
        self._pending = buffer[used:]
        if n_frames == 0:
            return np.empty((0, self._total_channels), dtype=self._dtype)
        return np.frombuffer(buffer[:used], dtype=self._dtype).reshape(
            n_frames, self._total_channels
        )

    @property
    def pending_bytes(self) -> int:
        """Bytes of an incomplete frame held for the next call."""
        return len(self._pending)


def to_microvolts(counts, offset: float, adc_counts_to_value: float) -> np.ndarray:
    """Convert raw ADC counts to microvolts."""
    return offset + np.asarray(counts, dtype=np.float64) * adc_counts_to_value
