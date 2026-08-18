"""Spike sorting: the three techniques, and whether to believe them."""

import numpy as np
import pytest

from biocam.analysis.sorting import (
    MIN_WAVEFORMS_TO_FIT,
    WEAK_SEPARATION,
    SORTER_LABELS,
    SORTERS,
    AmplitudeSorter,
    PCAKMeansSorter,
    TemplateSorter,
    make_sorter,
    suggest_n_units,
    waveform_matrix,
)
from biocam.analysis.spikes import Spike

RATE = 18557.720703125
LENGTH = 38


def wave(depth, width=10.0, noise=0.0, seed=0):
    """One synthetic waveform: a negative trough of a given depth and width."""
    rng = np.random.default_rng(seed)
    t = np.arange(LENGTH) - 9.0
    shape = -depth * np.exp(-(t ** 2) / (2 * width))
    if noise:
        shape = shape + rng.normal(0, noise, LENGTH)
    return shape


def spikes_from(waveforms):
    return [Spike(frame=i * 100, channel=0, amplitude=float(w.min()),
                  threshold=-50.0, waveform=w)
            for i, w in enumerate(waveforms)]


def two_units(n=60, seed=0):
    """Two clearly different units: deep-and-narrow, shallow-and-wide."""
    rng = np.random.default_rng(seed)
    waves, truth = [], []
    for i in range(n):
        if i % 2 == 0:
            waves.append(wave(200 * rng.normal(1, 0.05), width=6.0,
                              noise=4.0, seed=i))
            truth.append(0)
        else:
            waves.append(wave(80 * rng.normal(1, 0.05), width=20.0,
                              noise=4.0, seed=i))
            truth.append(1)
    return spikes_from(waves), truth


def only_noise(n=60, seed=1):
    rng = np.random.default_rng(seed)
    return spikes_from([rng.normal(0, 20, LENGTH) for _ in range(n)])


def accuracy(labels, truth) -> float:
    """Best agreement over the two possible label-to-truth mappings."""
    direct = sum(a == b for a, b in zip(labels, truth)) / len(truth)
    flipped = sum((1 - a) == b for a, b in zip(labels, truth)) / len(truth)
    return max(direct, flipped)


# --------------------------------------------------------------------------
# every technique, the same expectations
# --------------------------------------------------------------------------

@pytest.mark.parametrize("technique", sorted(SORTERS))
def test_each_technique_separates_two_real_units(technique):
    spikes, truth = two_units()
    sorter = make_sorter(technique, n_units=2).fit(spikes)
    assert accuracy(sorter.classify_all(spikes), truth) > 0.9


@pytest.mark.parametrize("technique", sorted(SORTERS))
def test_each_technique_reports_low_separation_on_noise(technique):
    """The negative control, and the point of reporting separation at all.

    Every clusterer returns clusters. Ask any of these for two units on pure
    noise and two units come back, looking exactly as convincing as a real
    split. The score is the only thing that distinguishes them.
    """
    sorter = make_sorter(technique, n_units=2).fit(only_noise())
    assert sorter.separation() < WEAK_SEPARATION
    assert any("barely better separated than noise" in w or "artefact" in w
               for w in sorter.warnings())


@pytest.mark.parametrize("technique", sorted(SORTERS))
def test_each_technique_refuses_to_classify_before_fitting(technique):
    sorter = make_sorter(technique)
    with pytest.raises(RuntimeError, match="has not been fitted"):
        sorter.classify(wave(100))


@pytest.mark.parametrize("technique", sorted(SORTERS))
def test_each_technique_refuses_too_few_waveforms(technique):
    spikes, _ = two_units(n=MIN_WAVEFORMS_TO_FIT - 1)
    with pytest.raises(ValueError, match="at least"):
        make_sorter(technique).fit(spikes)


@pytest.mark.parametrize("technique", sorted(SORTERS))
def test_each_technique_is_reproducible(technique):
    spikes, _ = two_units()
    first = make_sorter(technique, n_units=2).fit(spikes).classify_all(spikes)
    second = make_sorter(technique, n_units=2).fit(spikes).classify_all(spikes)
    assert first == second


@pytest.mark.parametrize("technique", sorted(SORTERS))
def test_each_technique_describes_itself_before_and_after_fitting(technique):
    sorter = make_sorter(technique, n_units=2)
    assert "not fitted" in sorter.describe()
    spikes, _ = two_units()
    sorter.fit(spikes)
    assert "separation" in sorter.describe()
    assert str(sorter.n_units) in sorter.describe()


@pytest.mark.parametrize("technique", sorted(SORTERS))
def test_a_clean_two_unit_fit_warns_about_nothing(technique):
    spikes, _ = two_units()
    assert make_sorter(technique, n_units=2).fit(spikes).warnings() == []


# --------------------------------------------------------------------------
# the differences between them
# --------------------------------------------------------------------------

def test_amplitude_orders_units_by_depth():
    # Unit 0 must mean the same thing between runs and recordings, not
    # whatever k-means happened to seed.
    spikes, _ = two_units()
    sorter = AmplitudeSorter(n_units=2).fit(spikes)
    deep = [s for s, u in zip(spikes, sorter.classify_all(spikes)) if u == 0]
    shallow = [s for s, u in zip(spikes, sorter.classify_all(spikes)) if u == 1]
    assert np.mean([s.waveform.min() for s in deep]) < \
           np.mean([s.waveform.min() for s in shallow])


def test_the_raw_silhouette_alone_would_have_been_misleading():
    """Why separation() subtracts a null.

    A silhouette on one-dimensional data scores well almost regardless of the
    data: splitting a single hump at its middle leaves two tidy halves. The
    amplitude technique scored 0.57 on pure noise - a number that reads as
    "convincing" and means nothing.
    """
    sorter = AmplitudeSorter(n_units=2).fit(only_noise())
    assert sorter.raw_separation() > 0.4          # looks convincing
    assert sorter.null_separation() > 0.4         # so does clustering noise
    assert sorter.separation() < WEAK_SEPARATION  # so it is not


def test_a_real_split_beats_its_own_null():
    spikes, _ = two_units()
    sorter = AmplitudeSorter(n_units=2).fit(spikes)
    assert sorter.raw_separation() > sorter.null_separation()
    assert sorter.separation() > WEAK_SEPARATION


def test_the_null_is_reproducible():
    spikes, _ = two_units()
    a = AmplitudeSorter(n_units=2).fit(spikes).null_separation()
    b = AmplitudeSorter(n_units=2).fit(spikes).null_separation()
    assert a == b


def test_amplitude_cannot_separate_units_of_the_same_depth():
    # An honest limitation, and the reason the other two exist: two neurons
    # of equal amplitude but different shape are one unit to this technique.
    rng = np.random.default_rng(2)
    waves = []
    for i in range(60):
        width = 6.0 if i % 2 == 0 else 24.0
        waves.append(wave(150 * rng.normal(1, 0.02), width=width, noise=3.0,
                          seed=i))
    sorter = AmplitudeSorter(n_units=2).fit(spikes_from(waves))
    # PCA, which sees the shape and not only the depth, does better on
    # exactly this data. The comparison is the assertion; an absolute
    # threshold here would be a number chosen to pass.
    assert PCAKMeansSorter(n_units=2).fit(spikes_from(waves)).separation() > \
           sorter.separation()


def test_pca_reports_how_much_variance_it_kept():
    spikes, _ = two_units()
    sorter = PCAKMeansSorter(n_units=2, n_components=3).fit(spikes)
    assert 0.0 < sorter.explained <= 1.0
    assert "variance" in sorter.describe()


def test_pca_projects_new_spikes_onto_the_axes_it_was_fitted_with():
    # Recomputing components from newer data would silently change what a
    # unit label means partway through a recording.
    spikes, _ = two_units()
    sorter = PCAKMeansSorter(n_units=2).fit(spikes)
    components = sorter._components.copy()
    sorter.classify(wave(500, width=2.0))     # nothing like the training set
    assert np.array_equal(sorter._components, components)


def test_pca_needs_at_least_one_component():
    with pytest.raises(ValueError, match="n_components must be at least 1"):
        PCAKMeansSorter(n_components=0)


def test_template_keeps_one_average_waveform_per_unit():
    spikes, _ = two_units()
    sorter = TemplateSorter(n_units=2).fit(spikes)
    assert sorter.templates.shape == (2, LENGTH)


def test_template_can_say_it_does_not_recognise_a_waveform():
    # A sorter that never says "none of these" labels stimulus artefacts as
    # neurons all day.
    spikes, _ = two_units()
    sorter = TemplateSorter(n_units=2, max_distance_sigmas=2.0).fit(spikes)
    assert sorter.classify(spikes[0].waveform) in (0, 1)
    assert sorter.classify(np.full(LENGTH, -5000.0)) == -1


def test_template_without_a_limit_always_picks_a_unit():
    spikes, _ = two_units()
    sorter = TemplateSorter(n_units=2).fit(spikes)
    assert sorter.classify(np.full(LENGTH, -5000.0)) in (0, 1)


# --------------------------------------------------------------------------
# how many units are there
# --------------------------------------------------------------------------

def test_suggest_prefers_the_number_of_units_actually_present():
    spikes, _ = two_units()
    best = suggest_n_units(spikes, technique="pca", candidates=(1, 2, 3, 4))
    assert best[0][0] == 2


def test_suggest_returns_scores_best_first():
    spikes, _ = two_units()
    scores = [s for _, s in suggest_n_units(spikes)]
    assert scores == sorted(scores, reverse=True)


def test_suggest_skips_candidates_that_cannot_be_fitted():
    spikes, _ = two_units(n=MIN_WAVEFORMS_TO_FIT - 1)
    assert suggest_n_units(spikes) == []


# --------------------------------------------------------------------------
# input handling
# --------------------------------------------------------------------------

def test_waveforms_of_different_lengths_are_refused():
    # Two detectors with different windows, mixed. Trimming them silently
    # would sort on that mistake.
    spikes = spikes_from([wave(100), wave(100)[:20]])
    with pytest.raises(ValueError, match="different lengths"):
        waveform_matrix(spikes)


def test_spikes_without_waveforms_are_skipped():
    detected = [Spike(frame=0, channel=0, amplitude=-1.0, threshold=-1.0)]
    assert waveform_matrix(detected).shape == (0, 0)


def test_an_unknown_technique_lists_the_available_ones():
    with pytest.raises(ValueError, match="unknown sorting technique"):
        make_sorter("magic")
    try:
        make_sorter("magic")
    except ValueError as exc:
        for name in SORTERS:
            assert name in str(exc)


def test_every_technique_has_a_label_for_the_ui():
    assert set(SORTER_LABELS) == set(SORTERS)
    assert all(label for label in SORTER_LABELS.values())


def test_n_units_must_be_positive():
    with pytest.raises(ValueError, match="n_units must be at least 1"):
        make_sorter("pca", n_units=0)


# --------------------------------------------------------------------------
# end to end, from a detector
# --------------------------------------------------------------------------

def test_sorting_a_detected_recording():
    from biocam.analysis.spikes import SpikeDetector

    rng = np.random.default_rng(3)
    n = 200_000
    signal = 2048 + rng.normal(0, 8, (n, 1))
    narrow, wide = int(0.0008 * RATE), int(0.0016 * RATE)
    frame = 5000
    while frame < n - 100:
        if rng.random() < 0.5:
            signal[frame:frame + narrow, 0] += -180 * np.hanning(narrow)
        else:
            signal[frame:frame + wide, 0] += -90 * np.hanning(wide)
        frame += int(rng.integers(1500, 4000))

    detector = SpikeDetector(1, RATE, collect_waveforms=True)
    spikes = []
    for start in range(0, n, 512):
        detector.detect(signal[start:start + 512])
        spikes.extend(detector.take_waveforms())

    assert len(spikes) > MIN_WAVEFORMS_TO_FIT
    sorter = make_sorter("pca", n_units=2).fit(spikes)
    # Comfortably above the noise floor. Not a raw silhouette - see
    # WEAK_SEPARATION and the module docstring for why the raw number would
    # have been the wrong thing to assert on.
    assert sorter.separation() > 2 * WEAK_SEPARATION
    assert sorter.warnings() == []
    assert len(sorter.unit_counts()) == 2
