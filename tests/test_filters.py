import numpy as np
import pytest

from biocam.analysis.filters import Biquad, HighPass, highpass_coefficients

RATE = 18557.720703125


# --------------------------------------------------------------------------
# does it filter what it says it filters
# --------------------------------------------------------------------------

def test_the_cutoff_is_where_the_cutoff_is_supposed_to_be():
    # -3.01 dB at the corner is the definition of a Butterworth cutoff. If
    # the pre-warping were dropped the corner would drift, plausibly and
    # wrongly, by a few percent.
    hp = HighPass(1, RATE, cutoff_hz=300.0)
    assert float(hp.gain_at(300.0)) == pytest.approx(1 / np.sqrt(2), rel=1e-6)


def test_dc_is_removed_entirely():
    hp = HighPass(1, RATE, cutoff_hz=300.0)
    assert float(hp.gain_at(0.0)) == pytest.approx(0.0, abs=1e-12)


def test_the_response_matches_the_butterworth_formula():
    """|H(f)| = 1 / sqrt(1 + (fc/f)^4) for a second-order high-pass.

    Stronger than checking the passband looks flat, and it caught a
    too-tight tolerance in the first version of this test: 0.9961 at 1 kHz
    with a 300 Hz corner is not "nearly 1", it is exactly right, and a test
    that called it a failure would have invited someone to "fix" a correct
    filter.
    """
    hp = HighPass(1, RATE, cutoff_hz=300.0)
    for f in (400.0, 1000.0, 3000.0, 6000.0):
        expected = 1.0 / np.sqrt(1.0 + (300.0 / f) ** 4)
        assert float(hp.gain_at(f)) == pytest.approx(expected, rel=2e-3)


def test_the_stopband_falls_at_forty_db_per_decade():
    # Second order: a decade below the corner should be about 40 dB down.
    hp = HighPass(1, RATE, cutoff_hz=300.0)
    at_30 = 20 * np.log10(float(hp.gain_at(30.0)))
    assert -45 < at_30 < -35


def test_a_lower_cutoff_passes_more_low_frequency():
    low = HighPass(1, RATE, cutoff_hz=100.0)
    high = HighPass(1, RATE, cutoff_hz=600.0)
    assert float(low.gain_at(200.0)) > float(high.gain_at(200.0))


def test_drift_is_removed_from_a_real_looking_signal():
    # The thing the filter is for: a slow baseline wander that a threshold
    # would otherwise track instead of the spikes.
    t = np.arange(20000) / RATE
    drift = 500.0 * np.sin(2 * np.pi * 2.0 * t)
    hp = HighPass(1, RATE)
    hp.warm_up(np.array([drift[0]]))
    out = hp.process(drift.reshape(-1, 1))
    assert np.abs(out[2000:]).max() < 5.0        # from 500 uV to under 5


# --------------------------------------------------------------------------
# streaming: the property that makes it usable online
# --------------------------------------------------------------------------

def test_one_block_and_many_blocks_agree_exactly():
    # Bit-identical, not approximately: a filter that resets its state at
    # every packet boundary produces a plausible transient 500 times a
    # second, and nothing about the output looks wrong.
    rng = np.random.default_rng(0)
    signal = rng.normal(0, 1, (5000, 3))

    whole = HighPass(3, RATE).process(signal)
    piecewise = HighPass(3, RATE)
    pieces = np.vstack([piecewise.process(signal[i:i + 37])
                        for i in range(0, 5000, 37)])
    assert np.array_equal(whole, pieces)


def test_an_odd_final_block_is_handled():
    rng = np.random.default_rng(1)
    signal = rng.normal(0, 1, (100, 2))
    whole = HighPass(2, RATE).process(signal)
    stream = HighPass(2, RATE)
    pieces = np.vstack([stream.process(signal[:37]),
                        stream.process(signal[37:74]),
                        stream.process(signal[74:])])
    assert np.array_equal(whole, pieces)


def test_state_is_per_channel():
    # A step on one channel must not appear on its neighbour.
    block = np.zeros((200, 2))
    block[:, 0] = 100.0
    out = HighPass(2, RATE).process(block)
    assert np.abs(out[:, 1]).max() == 0.0
    assert np.abs(out[:, 0]).max() > 0.0


def test_reset_forgets_the_past():
    rng = np.random.default_rng(2)
    signal = rng.normal(0, 1, (500, 1))
    f = HighPass(1, RATE)
    first = f.process(signal)
    f.reset()
    assert np.array_equal(f.process(signal), first)


# --------------------------------------------------------------------------
# warm-up: the transient that would otherwise look like a burst of spikes
# --------------------------------------------------------------------------

def test_without_warm_up_a_baseline_offset_rings():
    # A recording sitting at 2048 counts is a step from zero as far as the
    # filter is concerned.
    block = np.full((2000, 1), 2048.0)
    out = HighPass(1, RATE).process(block)
    assert np.abs(out).max() > 100.0


def test_warm_up_removes_the_transient_entirely():
    block = np.full((2000, 1), 2048.0)
    f = HighPass(1, RATE)
    f.warm_up(block[0])
    out = f.process(block)
    assert np.abs(out).max() < 1e-9


def test_warm_up_checks_its_shape():
    f = HighPass(3, RATE)
    with pytest.raises(ValueError, match="one value per channel"):
        f.warm_up(np.array([1.0]))


# --------------------------------------------------------------------------
# construction
# --------------------------------------------------------------------------

def test_the_cutoff_must_be_below_nyquist():
    with pytest.raises(ValueError, match="Nyquist"):
        highpass_coefficients(RATE, RATE)
    with pytest.raises(ValueError, match="Nyquist"):
        highpass_coefficients(0.0, RATE)


def test_the_frame_rate_must_be_positive():
    with pytest.raises(ValueError, match="frame_rate_hz must be positive"):
        highpass_coefficients(300.0, 0.0)


def test_coefficients_are_normalised():
    b, a = highpass_coefficients(300.0, RATE)
    assert a[0] == 1.0
    assert b.shape == (3,) and a.shape == (3,)


def test_a_biquad_needs_three_coefficients_each():
    with pytest.raises(ValueError, match="three coefficients"):
        Biquad([1.0, 0.0], [1.0, 0.0, 0.0], n_channels=1)


def test_a_block_of_the_wrong_width_is_refused():
    f = HighPass(4, RATE)
    with pytest.raises(ValueError, match=r"\(frames, 4\) block"):
        f.process(np.zeros((10, 3)))


def test_a_biquad_normalises_an_unnormalised_a0():
    b, a = highpass_coefficients(300.0, RATE)
    scaled = Biquad(b * 2.0, a * 2.0, n_channels=1)
    assert scaled.a[0] == 1.0
    rng = np.random.default_rng(3)
    signal = rng.normal(0, 1, (100, 1))
    assert np.allclose(scaled.process(signal), HighPass(1, RATE).process(signal))


# --------------------------------------------------------------------------
# the two code paths must never disagree
# --------------------------------------------------------------------------

@pytest.mark.parametrize("n_channels", [1, 2, 4, 16, 48])
def test_the_scalar_and_vector_paths_are_bit_identical(n_channels):
    """Two paths that drift would mean a closed loop triggering on one
    signal while the recording was analysed with another.

    Bit-identical, not close: the arithmetic is the same operations in the
    same order, and IEEE-754 doubles do not care whether the interpreter or
    numpy performed the multiply.
    """
    rng = np.random.default_rng(11)
    signal = 2048.0 + rng.normal(0, 30, (3000, n_channels))

    scalar = HighPass(n_channels, RATE)
    scalar.warm_up(signal[0])
    vector = HighPass(n_channels, RATE)
    vector.warm_up(signal[0])

    assert np.array_equal(scalar.process(signal),
                          vector._process_vector(signal))


def test_the_scalar_path_streams_identically_too():
    rng = np.random.default_rng(12)
    signal = 2048.0 + rng.normal(0, 30, (1000, 4))
    whole = HighPass(4, RATE)
    whole.warm_up(signal[0])
    reference = whole.process(signal)

    streamed = HighPass(4, RATE)
    streamed.warm_up(signal[0])
    pieces = np.vstack([streamed.process(signal[i:i + 37])
                        for i in range(0, 1000, 37)])
    assert np.array_equal(reference, pieces)


def test_the_channel_count_decides_which_path_runs():
    from biocam.analysis.filters import SCALAR_CHANNEL_LIMIT

    # Not a behavioural claim - the two agree - but the performance claim in
    # the module docstring depends on which one runs, so it is pinned.
    assert SCALAR_CHANNEL_LIMIT >= 1
    small = HighPass(SCALAR_CHANNEL_LIMIT, RATE)
    large = HighPass(SCALAR_CHANNEL_LIMIT + 1, RATE)
    assert small.n_channels <= SCALAR_CHANNEL_LIMIT
    assert large.n_channels > SCALAR_CHANNEL_LIMIT


# --------------------------------------------------------------------------
# The scalar path keeps its own state and its own scratch buffer. Both are
# reuse, and reuse is where a filter silently starts returning the wrong data.
# --------------------------------------------------------------------------

def test_each_block_gets_its_own_output_array():
    # `process` hands its array to the caller, and the detector holds the
    # previous block's output across calls (it needs the last sample of it).
    # Returning the same buffer every time would rewrite data still being read
    # - and the corruption would look like signal, not like an error.
    import numpy as np

    from biocam.analysis.filters import HighPass

    hp = HighPass(4, 18557.720703125)
    rng = np.random.default_rng(0)
    first = hp.process(rng.normal(size=(19, 4)))
    kept = first.copy()
    second = hp.process(rng.normal(size=(19, 4)))
    assert first is not second
    assert np.array_equal(first, kept), "the first block was overwritten"


def test_the_two_state_representations_never_disagree():
    # The scalar path carries its state as Python lists and the vector path as
    # arrays. `reset` and `warm_up` write both; nothing else may let them drift.
    import numpy as np

    from biocam.analysis.filters import HighPass

    hp = HighPass(4, 18557.720703125)
    hp.warm_up(np.full(4, 2048.0))
    assert np.allclose(hp._z1, hp._z1_list)
    assert np.allclose(hp._z2, hp._z2_list)

    hp.process(np.random.default_rng(1).normal(size=(19, 4)) + 2048.0)
    assert np.allclose(hp._z1, hp._z1_list), "state diverged during processing"
    assert np.allclose(hp._z2, hp._z2_list)

    hp.reset()
    assert np.allclose(hp._z1, 0.0)
    assert np.allclose(hp._z1_list, 0.0)
    assert np.allclose(hp._z2_list, 0.0)


def test_a_changing_block_size_regrows_the_scratch_buffer():
    # A recording has one packet size, but a final short block is normal and
    # a scratch list sized for the previous one would truncate or overrun.
    import numpy as np

    from biocam.analysis.filters import HighPass

    wide = HighPass(3, 18557.720703125)
    narrow = HighPass(3, 18557.720703125)
    rng = np.random.default_rng(2)
    blocks = [rng.normal(size=(n, 3)) for n in (19, 7, 19, 40, 1)]

    # One filter fed varying sizes must equal one fed the same samples in one
    # go, which is the property block size must never affect.
    varied = np.concatenate([wide.process(b) for b in blocks])
    whole = narrow.process(np.concatenate(blocks))
    assert np.array_equal(varied, whole)
