"""Layer 3 - spike sorting: which unit did this spike come from?

Detection says *something crossed a threshold on this electrode*. An
electrode usually hears more than one neuron, so sorting is the step that
splits those crossings into units by the shape of their waveforms.

Three techniques, because they fail differently and no one of them is
correct for every preparation:

- **Amplitude** - split on trough depth alone. Crude, transparent, and
  robust: it cannot invent structure, and when it is wrong it is obviously
  wrong. A reasonable first look and a sanity check on the others.
- **PCA + k-means** - the classic. Project waveforms onto their principal
  components and cluster there. Finds structure the amplitude misses, and
  will happily find structure that is not there, which is why the quality
  measures below matter.
- **Template matching** - learn a template per unit from a training period,
  then assign each new spike to the nearest. The only one of the three that
  is genuinely usable online at low latency, because classifying is one
  distance computation.

**All three must be fitted before they can classify.** That is not an
implementation detail: online sorting means fitting on a stretch of
recording and then classifying what comes after, so the units are only as
good as that training period. A sorter fitted on two minutes of a quiet
culture will confidently mislabel a burst.

## The thing to be suspicious of

Every clusterer returns clusters. Ask k-means for three units and it returns
three, whether or not three exist - and the result looks exactly as
convincing either way.

So each sorter reports `separation()`. It is **not** a raw silhouette, and
the difference matters: a silhouette on one-dimensional data is high almost
regardless of the data, because splitting any single hump at its middle
leaves two tidy-looking halves. Measured here, the amplitude technique scored
0.61 on pure noise - a number that reads as "these units are convincing" and
means nothing at all.

`separation()` is therefore the silhouette **minus what the same clustering
scores on structureless surrogate data of the same shape and spread**. Zero
means "no better than clustering noise", whatever the raw score looked like.
`raw_separation()` and `null_separation()` are both available for anyone who
wants to see the subtraction.

`suggest_n_units` exists for the same reason: it fits several and reports
which separates best, rather than letting a number typed into a box decide
how many neurons a preparation has.

None of this replaces looking at the waveforms.
"""

import numpy as np

# Enough spikes that a template is an average rather than an anecdote. Below
# this a fit is refused: an under-fitted sorter is worse than none, because
# it still returns labels.
MIN_WAVEFORMS_TO_FIT = 20

# Reproducibility. Sorting the same recording twice must give the same units,
# or two analyses of one experiment disagree for no reason anybody can see.
DEFAULT_SEED = 20260818

# Surrogate datasets used to work out what this clustering scores on data
# with no structure in it. Five is enough to place the null within a few
# hundredths and cheap enough to run inside a fit.
NULL_SURROGATES = 5

# Below this much separation above the null, the units are not to be trusted.
WEAK_SEPARATION = 0.10


def waveform_matrix(spikes):
    """Stack the waveforms of a list of spikes into (n_spikes, n_samples).

    Refuses ragged input rather than truncating: waveforms of different
    lengths mean two detectors with different windows were mixed, and
    silently trimming them would sort on an artefact of that mistake.
    """
    shapes = [s.waveform for s in spikes if s.waveform is not None]
    if not shapes:
        return np.empty((0, 0), dtype=np.float64)
    lengths = {len(w) for w in shapes}
    if len(lengths) != 1:
        raise ValueError(
            f"waveforms have different lengths ({sorted(lengths)}). They were "
            "probably collected by detectors with different windows; sorting "
            "them together would sort on that difference."
        )
    return np.asarray(shapes, dtype=np.float64)


def _silhouette(features, labels) -> float:
    """Mean silhouette score: how well-separated the clusters are, in [-1, 1].

    Above ~0.5 the clusters are convincing; near 0 the boundaries are
    arbitrary; below 0 the labels are worse than not splitting at all.

    Written out rather than imported because this project depends on numpy
    alone, and it is a dozen lines.
    """
    labels = np.asarray(labels)
    units = np.unique(labels)
    if len(units) < 2 or len(labels) < 3:
        return 0.0
    # Pairwise distances once, reused for every point.
    diff = features[:, None, :] - features[None, :, :]
    distance = np.sqrt((diff * diff).sum(axis=2))
    scores = []
    for i in range(len(labels)):
        same = labels == labels[i]
        same[i] = False
        if not same.any():
            continue                       # a unit of one has no cohesion
        a = distance[i, same].mean()
        b = np.inf
        for unit in units:
            if unit == labels[i]:
                continue
            other = labels == unit
            if other.any():
                b = min(b, distance[i, other].mean())
        if not np.isfinite(b):
            continue
        denominator = max(a, b)
        if denominator > 0:
            scores.append((b - a) / denominator)
    return float(np.mean(scores)) if scores else 0.0


def _kmeans(features, k: int, seed: int = DEFAULT_SEED, iterations: int = 100):
    """Lloyd's algorithm with k-means++ seeding. Returns (labels, centres).

    Seeded, so the same data sorts the same way twice. k-means++ rather than
    random starts because random starts on a small number of waveforms
    routinely produce an empty cluster, and an empty cluster is a unit that
    exists in the labels and nowhere in the data.
    """
    rng = np.random.default_rng(seed)
    n = features.shape[0]
    if k >= n:
        return np.arange(n), features.copy()

    centres = [features[rng.integers(n)]]
    for _ in range(1, k):
        d = np.min(
            [((features - c) ** 2).sum(axis=1) for c in centres], axis=0)
        total = d.sum()
        if total <= 0:
            centres.append(features[rng.integers(n)])
            continue
        centres.append(features[rng.choice(n, p=d / total)])
    centres = np.asarray(centres, dtype=np.float64)

    labels = np.zeros(n, dtype=np.intp)
    for _ in range(iterations):
        distance = ((features[:, None, :] - centres[None, :, :]) ** 2).sum(axis=2)
        new_labels = distance.argmin(axis=1)
        if np.array_equal(new_labels, labels):
            break
        labels = new_labels
        for j in range(k):
            member = labels == j
            if member.any():
                centres[j] = features[member].mean(axis=0)
    return labels, centres


class Sorter:
    """What every technique has in common."""

    name = "sorter"
    label = "sorter"

    def __init__(self, n_units: int = 2, seed: int = DEFAULT_SEED):
        if n_units < 1:
            raise ValueError(f"n_units must be at least 1, got {n_units}")
        self.n_units = n_units
        self.seed = seed
        self.fitted = False
        self.n_fitted = 0
        self._features = None
        self._labels = None
        self._raw_separation = None
        self._null_separation = None

    def fit(self, spikes) -> "Sorter":
        waveforms = waveform_matrix(spikes)
        if waveforms.shape[0] < MIN_WAVEFORMS_TO_FIT:
            raise ValueError(
                f"only {waveforms.shape[0]} waveforms; at least "
                f"{MIN_WAVEFORMS_TO_FIT} are needed to fit. An under-fitted "
                "sorter is worse than none, because it still returns labels."
            )
        self._fit(waveforms)
        self.fitted = True
        self.n_fitted = waveforms.shape[0]
        return self

    def _fit(self, waveforms):
        raise NotImplementedError

    def _null_score(self) -> float:
        """What this clustering scores on data with no structure.

        Surrogates are Gaussian with the same per-feature mean and standard
        deviation as the real features, so they have the same shape and
        spread and no clusters. Whatever silhouette k-means extracts from
        them is the floor that a real result has to beat.
        """
        if self._features is None or self._features.shape[0] < 3:
            return 0.0
        rng = np.random.default_rng(self.seed + 1)
        mean = self._features.mean(axis=0)
        spread = self._features.std(axis=0)
        scores = []
        for i in range(NULL_SURROGATES):
            surrogate = rng.normal(
                mean, np.where(spread > 0, spread, 1.0),
                size=self._features.shape)
            labels, _ = _kmeans(surrogate, self.n_units, self.seed + 100 + i)
            scores.append(_silhouette(surrogate, labels))
        return float(np.mean(scores)) if scores else 0.0

    def classify(self, waveform) -> int:
        """The unit a single waveform belongs to."""
        if not self.fitted:
            raise RuntimeError(
                f"the {self.name} sorter has not been fitted. Sorting means "
                "fitting on a stretch of recording and classifying what comes "
                "after; there is nothing to classify against yet."
            )
        return self._classify(np.asarray(waveform, dtype=np.float64))

    def _classify(self, waveform) -> int:
        raise NotImplementedError

    def classify_all(self, spikes) -> list:
        return [self.classify(s.waveform) for s in spikes
                if s.waveform is not None]

    def raw_separation(self) -> float:
        """Silhouette score of the fit, before the null is subtracted.

        On its own this is not evidence: a one-dimensional split scores well
        on anything. Use `separation()`.
        """
        if not self.fitted or self._features is None:
            return 0.0
        if self._raw_separation is None:
            self._raw_separation = _silhouette(self._features, self._labels)
        return self._raw_separation

    def null_separation(self) -> float:
        """What the same clustering scores on structureless data."""
        if not self.fitted:
            return 0.0
        if self._null_separation is None:
            self._null_separation = self._null_score()
        return self._null_separation

    def separation(self) -> float:
        """How much better than clustering noise this fit is.

        Zero means no better. See the module docstring for why the raw
        silhouette is not the thing to report.
        """
        if not self.fitted:
            return 0.0
        return self.raw_separation() - self.null_separation()

    def unit_counts(self) -> dict:
        if self._labels is None:
            return {}
        units, counts = np.unique(self._labels, return_counts=True)
        return {int(u): int(c) for u, c in zip(units, counts)}

    def describe(self) -> str:
        if not self.fitted:
            return f"{self.label} (not fitted)"
        counts = self.unit_counts()
        spread = ", ".join(f"unit {u}: {c}" for u, c in sorted(counts.items()))
        return (f"{self.label}, {self.n_units} units from {self.n_fitted} "
                f"waveforms ({spread}); separation {self.separation():.2f} "
                f"(silhouette {self.raw_separation():.2f} against a noise "
                f"floor of {self.null_separation():.2f})")

    def warnings(self) -> list:
        problems = []
        if not self.fitted:
            return problems
        score = self.separation()
        if score < WEAK_SEPARATION:
            problems.append(
                f"the units are barely better separated than noise "
                f"(separation {score:.2f}: silhouette {self.raw_separation():.2f} "
                f"against a noise floor of {self.null_separation():.2f}). "
                "Every clusterer returns clusters; this one has probably "
                "partitioned noise rather than found neurons. Treat the unit "
                "labels as unreliable, and look at the waveforms."
            )
        counts = self.unit_counts()
        if counts and min(counts.values()) < 0.05 * self.n_fitted:
            smallest = min(counts, key=counts.get)
            problems.append(
                f"unit {smallest} holds only {counts[smallest]} of "
                f"{self.n_fitted} waveforms. A unit that small is usually an "
                "artefact or a handful of outliers rather than a neuron."
            )
        return problems


class AmplitudeSorter(Sorter):
    """Split on trough depth alone.

    The crudest technique here and the most trustworthy in one specific way:
    it cannot invent structure that is not in the amplitudes, and when it is
    wrong that is visible rather than hidden behind a projection. Worth
    running alongside the others as a check - if PCA finds three units and
    amplitude finds the same split, that is evidence; if only PCA does, the
    separation score is the thing to look at.
    """

    name = "amplitude"
    label = "Amplitude (trough depth)"

    def _fit(self, waveforms):
        self._features = waveforms.min(axis=1).reshape(-1, 1)
        self._labels, self._centres = _kmeans(
            self._features, self.n_units, self.seed)
        # Order units by depth, so "unit 0" means the same thing between runs
        # and between recordings rather than being whatever k-means seeded.
        order = np.argsort(self._centres[:, 0])
        remap = np.empty(len(order), dtype=np.intp)
        remap[order] = np.arange(len(order))
        self._labels = remap[self._labels]
        self._centres = self._centres[order]

    def _classify(self, waveform) -> int:
        trough = float(np.min(waveform))
        return int(np.argmin(np.abs(self._centres[:, 0] - trough)))


class PCAKMeansSorter(Sorter):
    """Project onto principal components, then cluster.

    The standard approach. The components are computed once during the fit
    and reused for every classification, so a spike classified later is
    projected onto the same axes the units were defined in - not onto axes
    recomputed from newer data, which would silently change what the unit
    labels mean partway through a recording.
    """

    name = "pca"
    label = "PCA + k-means"

    def __init__(self, n_units: int = 2, seed: int = DEFAULT_SEED,
                 n_components: int = 3):
        super().__init__(n_units=n_units, seed=seed)
        if n_components < 1:
            raise ValueError(
                f"n_components must be at least 1, got {n_components}")
        self.n_components = n_components
        self._mean = None
        self._components = None

    def _fit(self, waveforms):
        self._mean = waveforms.mean(axis=0)
        centred = waveforms - self._mean
        # SVD rather than an eigendecomposition of the covariance: it is
        # numerically better behaved and numpy has it.
        _, singular, vt = np.linalg.svd(centred, full_matrices=False)
        keep = min(self.n_components, vt.shape[0])
        self._components = vt[:keep]
        self.explained = (
            (singular[:keep] ** 2).sum() / (singular ** 2).sum()
            if (singular ** 2).sum() > 0 else 0.0
        )
        self._features = centred @ self._components.T
        self._labels, self._centres = _kmeans(
            self._features, self.n_units, self.seed)

    def _classify(self, waveform) -> int:
        projected = (waveform - self._mean) @ self._components.T
        return int(np.argmin(((self._centres - projected) ** 2).sum(axis=1)))

    def describe(self) -> str:
        base = super().describe()
        if self.fitted:
            return (f"{base}; {self.n_components} components explaining "
                    f"{self.explained * 100:.0f}% of the variance")
        return base


class TemplateSorter(Sorter):
    """Learn one average waveform per unit, then assign by nearest template.

    The one of the three built for online use: classifying is a distance to
    each template, which is a few microseconds for a handful of units. The
    templates come from a k-means fit in waveform space, so the fit is as
    expensive as PCA's - it is only the *classifying* that is cheap, which is
    exactly the split online sorting needs.

    `max_distance` rejects a waveform too far from every template rather than
    forcing it into the nearest unit. Returning -1 for "none of these" is the
    honest answer for an artefact, and a sorter that never says it does not
    know will label stimulus artefacts as neurons all day.
    """

    name = "template"
    label = "Template matching"

    def __init__(self, n_units: int = 2, seed: int = DEFAULT_SEED,
                 max_distance_sigmas: float = None):
        super().__init__(n_units=n_units, seed=seed)
        self.max_distance_sigmas = max_distance_sigmas
        self.templates = None
        self._reject_above = None

    def _fit(self, waveforms):
        self._features = waveforms
        self._labels, _ = _kmeans(waveforms, self.n_units, self.seed)
        templates = []
        for unit in range(self.n_units):
            member = self._labels == unit
            templates.append(waveforms[member].mean(axis=0) if member.any()
                             else np.zeros(waveforms.shape[1]))
        self.templates = np.asarray(templates)

        distances = np.sqrt(
            ((waveforms[:, None, :] - self.templates[None, :, :]) ** 2).sum(axis=2))
        nearest = distances.min(axis=1)
        if self.max_distance_sigmas is not None:
            self._reject_above = (
                nearest.mean() + self.max_distance_sigmas * nearest.std())

    def _classify(self, waveform) -> int:
        distances = np.sqrt(
            ((self.templates - waveform) ** 2).sum(axis=1))
        best = int(np.argmin(distances))
        if self._reject_above is not None and distances[best] > self._reject_above:
            return -1          # not any of these
        return best

    def describe(self) -> str:
        base = super().describe()
        if self.fitted and self._reject_above is not None:
            return f"{base}; rejecting waveforms beyond {self._reject_above:.0f}"
        return base


# The techniques a UI should offer, in the order they are worth trying.
SORTERS = {
    AmplitudeSorter.name: AmplitudeSorter,
    PCAKMeansSorter.name: PCAKMeansSorter,
    TemplateSorter.name: TemplateSorter,
}

SORTER_LABELS = {name: cls.label for name, cls in SORTERS.items()}


def make_sorter(technique: str, **kwargs) -> Sorter:
    """Build a sorter by name. Raises with the list if the name is unknown."""
    try:
        cls = SORTERS[technique]
    except KeyError:
        raise ValueError(
            f"unknown sorting technique {technique!r}. Available: "
            f"{', '.join(sorted(SORTERS))}"
        ) from None
    return cls(**kwargs)


def suggest_n_units(spikes, technique: str = "pca", candidates=(1, 2, 3, 4),
                    **kwargs):
    """Fit several unit counts and report how well each separates.

    Exists because asking for three units always returns three, and the
    result looks equally convincing whether or not three neurons are there.
    Returns [(n_units, separation)], best first.

    It is a suggestion and nothing more. Silhouette rewards compact,
    well-spaced clusters, which is not the same as rewarding correct ones,
    and no score settles how many neurons an electrode can hear.
    """
    results = []
    for n in candidates:
        if n < 1:
            continue
        try:
            sorter = make_sorter(technique, n_units=n, **kwargs).fit(spikes)
        except ValueError:
            continue
        results.append((n, sorter.separation()))
    return sorted(results, key=lambda pair: pair[1], reverse=True)
