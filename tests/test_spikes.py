import json
from pathlib import Path

import numpy as np
import pytest

from biocam.analysis.spikes import (
    DEFAULT_WARMUP_SECONDS,
    NoiseEstimator,
    Spike,
    SpikeDetector,
    detect_all,
)

RATE = 18557.720703125
FIXTURES = Path(__file__).parent / "fixtures"


def noisy(n_frames, n_channels=4, sigma=10.0, baseline=2048.0, seed=0):
    rng = np.random.default_rng(seed)
    return baseline + rng.normal(0, sigma, (n_frames, n_channels))


def plant(signal, frame, channel, amplitude=-120.0, width_ms=1.0):
    """Add one negative-going spike of a realistic width."""
    width = int(width_ms * 1e-3 * RATE)
    signal[frame:frame + width, channel] += amplitude * np.hanning(width)
    return signal


# --------------------------------------------------------------------------
# the noise estimate
# --------------------------------------------------------------------------

def test_the_noise_estimate_recovers_the_true_sigma():
    estimator = NoiseEstimator(3, RATE)
    rng = np.random.default_rng(1)
    for _ in range(50):
        estimator.update(rng.normal(0, 25.0, (512, 3)))
    assert estimator.sigma == pytest.approx(25.0, rel=0.1)


def test_spikes_do_not_inflate_the_noise_estimate():
    # The reason for a median rather than a standard deviation. If spikes
    # raised the estimate, the threshold would climb until they stopped being
    # detected - which looks exactly like a preparation going quiet.
    rng = np.random.default_rng(2)
    clean = rng.normal(0, 20.0, (8000, 1))
    spiky = clean.copy()
    for frame in range(200, 8000, 400):
        plant(spiky, frame, 0, amplitude=-400.0)

    a, b = NoiseEstimator(1, RATE), NoiseEstimator(1, RATE)
    for start in range(0, 8000, 512):
        a.update(clean[start:start + 512])
        b.update(spiky[start:start + 512])
    assert b.sigma[0] == pytest.approx(a.sigma[0], rel=0.15)


def test_the_estimate_is_the_same_however_the_data_is_blocked():
    # A per-block smoothing factor made the estimator's memory a hundred
    # times longer when the same data arrived in bigger chunks.
    rng = np.random.default_rng(3)
    signal = rng.normal(0, 15.0, (20000, 1))
    results = []
    for block in (37, 512, 4096):
        estimator = NoiseEstimator(1, RATE)
        for start in range(0, 20000, block):
            estimator.update(signal[start:start + block])
        results.append(estimator.sigma[0])
    assert max(results) - min(results) < 0.1 * np.mean(results)


def test_warm_up_is_measured_in_time_not_in_blocks():
    fast = NoiseEstimator(1, RATE, warmup_seconds=0.05)
    # One big block already covers the warm-up period.
    fast.update(np.zeros((int(0.05 * RATE) + 1, 1)))
    assert fast.ready

    slow = NoiseEstimator(1, RATE, warmup_seconds=0.05)
    for _ in range(5):
        slow.update(np.zeros((37, 1)))       # 185 frames, ~10 ms
    assert not slow.ready


def test_the_threshold_is_negative_and_scales_with_sigma():
    estimator = NoiseEstimator(2, RATE)
    estimator.update(np.array([[10.0, 20.0]] * 100))
    thresholds = estimator.thresholds(5.0)
    assert (thresholds < 0).all()
    assert thresholds[1] == pytest.approx(2 * thresholds[0])


def test_the_estimator_validates_its_arguments():
    with pytest.raises(ValueError, match="frame_rate_hz must be positive"):
        NoiseEstimator(1, 0.0)
    with pytest.raises(ValueError, match="tau_seconds must be positive"):
        NoiseEstimator(1, RATE, tau_seconds=0.0)


def test_an_empty_block_changes_nothing():
    estimator = NoiseEstimator(1, RATE)
    estimator.update(np.zeros((0, 1)))
    assert estimator.frames == 0


# --------------------------------------------------------------------------
# detection
# --------------------------------------------------------------------------

def test_a_planted_spike_is_found_within_a_millisecond():
    signal = noisy(40000)
    plant(signal, 20000, 2)
    spikes, _ = detect_all(signal, RATE)
    found = [s for s in spikes if s.channel == 2]
    assert len(found) == 1
    assert abs(found[0].frame - 20000) < RATE * 0.001


def test_pure_noise_produces_no_spikes_at_five_sigma():
    # Five sigma on Gaussian noise is about one sample in 3.5 million; over
    # 40000 frames x 4 channels the expectation is well under one.
    spikes, _ = detect_all(noisy(40000, seed=5), RATE, threshold_sigmas=5.0)
    assert spikes == []


def test_a_lower_threshold_finds_more():
    signal = noisy(40000, seed=6)
    at_four = len(detect_all(signal, RATE, threshold_sigmas=4.0)[0])
    at_six = len(detect_all(signal, RATE, threshold_sigmas=6.0)[0])
    assert at_four >= at_six


def test_one_spike_is_one_detection_not_eighteen():
    # A 1 ms spike at 18.5 kHz is about eighteen samples below the line.
    signal = noisy(40000)
    plant(signal, 20000, 0, amplitude=-300.0)
    spikes, _ = detect_all(signal, RATE)
    assert len([s for s in spikes if s.channel == 0]) == 1


def test_two_spikes_inside_the_refractory_period_are_one_event():
    signal = noisy(40000)
    plant(signal, 20000, 0)
    plant(signal, 20000 + int(0.0004 * RATE), 0)     # 0.4 ms later
    spikes, _ = detect_all(signal, RATE, refractory_ms=1.0)
    assert len([s for s in spikes if s.channel == 0]) == 1


def test_two_spikes_outside_the_refractory_period_are_two_events():
    signal = noisy(40000)
    plant(signal, 20000, 0)
    plant(signal, 20000 + int(0.003 * RATE), 0)      # 3 ms later
    spikes, _ = detect_all(signal, RATE, refractory_ms=1.0)
    assert len([s for s in spikes if s.channel == 0]) == 2


def test_a_spike_on_a_block_boundary_is_not_missed():
    """The case a naive per-block detector loses ~500 times a second.

    The crossing sample is the first frame of a block, so the sample it must
    be compared against lives in the previous block.
    """
    block = 512
    signal = noisy(40000)
    plant(signal, block * 20, 1)          # exactly on a boundary
    spikes, _ = detect_all(signal, RATE, block_frames=block)
    assert len([s for s in spikes if s.channel == 1]) == 1


def test_the_result_barely_depends_on_the_block_size():
    signal = noisy(60000, seed=7)
    for frame in (10000, 20000, 30000, 40000, 50000):
        plant(signal, frame, 1)
    counts = {}
    for block in (37, 128, 512, 4096, 60000):
        spikes, _ = detect_all(signal, RATE, block_frames=block)
        counts[block] = [s.frame for s in spikes]
    # Same number of spikes, and each within a sample or two - the estimator
    # measures its median over whatever window it is given, so the threshold
    # is not bit-identical across blockings. The filter is; this is not.
    lengths = {len(v) for v in counts.values()}
    assert lengths == {5}
    reference = counts[512]
    for frames in counts.values():
        assert all(abs(a - b) <= 3 for a, b in zip(frames, reference))


def test_nothing_is_detected_during_warm_up():
    # A threshold from an unsettled estimator is arbitrary, and a detector
    # that reports arbitrary spikes for its first moments is worse than one
    # that reports none - the first moments still look like data.
    signal = noisy(40000)
    plant(signal, 100, 0, amplitude=-500.0)     # inside the warm-up
    spikes, detector = detect_all(signal, RATE)
    assert detector.noise.warmup_frames == int(round(DEFAULT_WARMUP_SECONDS * RATE))
    assert all(s.frame > detector.noise.warmup_frames for s in spikes)


def test_a_detector_reports_whether_it_is_ready():
    detector = SpikeDetector(1, RATE)
    assert not detector.ready
    detector.detect(noisy(int(0.1 * RATE), 1))
    assert detector.ready


def test_spikes_come_back_in_frame_order():
    signal = noisy(60000, n_channels=3, seed=8)
    for frame, channel in ((15000, 0), (25000, 2), (35000, 1), (45000, 0)):
        plant(signal, frame, channel)
    spikes, _ = detect_all(signal, RATE)
    frames = [s.frame for s in spikes]
    assert frames == sorted(frames)


def test_a_spike_records_what_it_had_to_beat():
    signal = noisy(40000)
    plant(signal, 20000, 0, amplitude=-300.0)
    spikes, _ = detect_all(signal, RATE)
    spike = next(s for s in spikes if s.channel == 0)
    assert isinstance(spike, Spike)
    assert spike.threshold < 0
    assert spike.amplitude <= spike.threshold      # it crossed
    assert spike.sigma >= 5.0


def test_the_detection_rate_is_reported():
    signal = noisy(int(2 * RATE), n_channels=1, seed=9)
    for frame in range(2000, int(2 * RATE) - 2000, int(0.1 * RATE)):
        plant(signal, frame, 0)
    spikes, detector = detect_all(signal, RATE)
    # Roughly one every 100 ms over two seconds: about 10 Hz.
    assert 8.0 < detector.rates_hz() < 12.0


def test_channels_are_independent():
    signal = noisy(40000, n_channels=3)
    plant(signal, 20000, 1, amplitude=-400.0)
    spikes, _ = detect_all(signal, RATE)
    assert {s.channel for s in spikes} == {1}


# --------------------------------------------------------------------------
# construction and shapes
# --------------------------------------------------------------------------

def test_the_detector_validates_its_arguments():
    with pytest.raises(ValueError, match="n_channels must be at least 1"):
        SpikeDetector(0, RATE)
    with pytest.raises(ValueError, match="threshold_sigmas must be positive"):
        SpikeDetector(1, RATE, threshold_sigmas=0.0)


def test_a_block_of_the_wrong_width_is_refused():
    with pytest.raises(ValueError, match=r"\(frames, 2\) block"):
        SpikeDetector(2, RATE).detect(np.zeros((10, 3)))


def test_an_empty_block_yields_nothing():
    assert SpikeDetector(2, RATE).detect(np.zeros((0, 2))) == []


def test_detect_all_refuses_one_dimensional_data():
    with pytest.raises(ValueError, match=r"\(frames, channels\)"):
        detect_all(np.zeros(100), RATE)


# --------------------------------------------------------------------------
# against real signal from the instrument
# --------------------------------------------------------------------------

def test_it_runs_on_the_recorded_fixture():
    """Two seconds of real 32-channel signal, recorded on the BioCAM.

    This does not assert a spike count: nobody has told us what is in this
    recording, and inventing an expected number would be inventing a result.
    What it checks is that the detector survives real data and produces
    something physically sane.
    """
    meta = json.loads(
        (FIXTURES / "sample_32ch_2s_meta.json").read_text(encoding="utf-8"))
    raw = np.fromfile(FIXTURES / "sample_32ch_2s.raw", dtype=np.uint16)
    data = raw.reshape(-1, meta["total_channels"]).astype(np.float64)
    data = meta["offset"] + data * meta["adc_counts_to_value"]

    spikes, detector = detect_all(
        data, meta["frame_rate_hz"], block_frames=512)

    assert detector.ready
    # A real electrode's noise is microvolts, not zero and not millivolts.
    assert (detector.noise.sigma > 0.5).all()
    assert (detector.noise.sigma < 200.0).all()
    # Every spike must be locatable in the file it came from.
    for spike in spikes:
        assert 0 <= spike.frame < data.shape[0]
        assert 0 <= spike.channel < meta["total_channels"]


def test_a_higher_threshold_finds_fewer_on_the_real_fixture():
    meta = json.loads(
        (FIXTURES / "sample_32ch_2s_meta.json").read_text(encoding="utf-8"))
    raw = np.fromfile(FIXTURES / "sample_32ch_2s.raw", dtype=np.uint16)
    data = raw.reshape(-1, meta["total_channels"]).astype(np.float64)
    data = meta["offset"] + data * meta["adc_counts_to_value"]

    counts = [
        len(detect_all(data, meta["frame_rate_hz"], threshold_sigmas=s,
                       block_frames=512)[0])
        for s in (4.0, 5.0, 6.0)
    ]
    assert counts[0] > counts[1] > counts[2]
