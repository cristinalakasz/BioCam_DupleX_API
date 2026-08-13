import numpy as np
import pytest

from biocam.data.frames import DTYPE_BY_BYTE_SIZE, FrameDecoder, to_microvolts


def _payload(values):
    return np.asarray(values, dtype=np.uint16).tobytes()


def test_decodes_whole_frames():
    decoder = FrameDecoder(total_channels=4, ch_sample_byte_size=2)
    out = decoder.decode(_payload([1, 2, 3, 4, 5, 6, 7, 8]))
    assert out.shape == (2, 4)
    assert out.tolist() == [[1, 2, 3, 4], [5, 6, 7, 8]]
    assert decoder.pending_bytes == 0


def test_carries_a_partial_frame_to_the_next_call():
    decoder = FrameDecoder(total_channels=4, ch_sample_byte_size=2)
    first = decoder.decode(_payload([1, 2, 3, 4, 5, 6]))     # 1.5 frames
    assert first.shape == (1, 4)
    assert decoder.pending_bytes == 4                         # 2 samples held back

    second = decoder.decode(_payload([7, 8]))                 # completes frame 2
    assert second.shape == (1, 4)
    assert second.tolist() == [[5, 6, 7, 8]]
    assert decoder.pending_bytes == 0


def test_no_sample_is_ever_dropped_across_many_ragged_packets():
    """Feed deliberately uneven packets; every sample must come back in order.

    Sizes cycle so that packet boundaries land at every possible offset within
    a frame - which is the situation that made the original code lose data.
    """
    import itertools

    decoder = FrameDecoder(total_channels=4, ch_sample_byte_size=2)
    source = np.arange(400, dtype=np.uint16)
    sizes = itertools.cycle([3, 7, 1, 11, 5, 13])
    recovered = []
    cursor = 0
    while cursor < len(source):
        size = next(sizes)
        chunk = source[cursor:cursor + size]
        cursor += size
        block = decoder.decode(chunk.tobytes())
        if block.size:
            recovered.append(block)

    joined = np.concatenate(recovered).reshape(-1)
    assert joined.tolist() == source[:len(joined)].tolist()
    # At most one incomplete frame may remain held back.
    assert len(source) - len(joined) < 4
    assert decoder.pending_bytes == (len(source) - len(joined)) * 2


def test_empty_payload_yields_no_frames():
    decoder = FrameDecoder(total_channels=4, ch_sample_byte_size=2)
    out = decoder.decode(b"")
    assert out.shape == (0, 4)
    assert decoder.pending_bytes == 0


def test_payload_shorter_than_one_frame_yields_nothing_yet():
    decoder = FrameDecoder(total_channels=4, ch_sample_byte_size=2)
    out = decoder.decode(_payload([1, 2]))
    assert out.shape == (0, 4)
    assert decoder.pending_bytes == 4


def test_rejects_unsupported_sample_size():
    with pytest.raises(ValueError):
        FrameDecoder(total_channels=4, ch_sample_byte_size=3)


def test_microvolt_conversion_matches_hand_computation():
    counts = np.array([[686, 3050]], dtype=np.uint16)
    out = to_microvolts(counts, offset=-4125.0, adc_counts_to_value=2.0146520146520146)
    assert out[0, 0] == pytest.approx(-4125.0 + 686 * 2.0146520146520146)
    assert out[0, 1] == pytest.approx(-4125.0 + 3050 * 2.0146520146520146)


def test_microvolt_conversion_returns_float_not_integer():
    out = to_microvolts(np.array([[0]], dtype=np.uint16), offset=-1.5,
                        adc_counts_to_value=0.5)
    assert out.dtype == np.float64
    assert out[0, 0] == pytest.approx(-1.5)


def test_supported_sample_sizes():
    assert set(DTYPE_BY_BYTE_SIZE) == {1, 2, 4}
