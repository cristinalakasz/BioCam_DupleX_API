"""Layer 3 - signal processing.

Spike detection, spike sorting, and the decision logic a closed loop needs.
Fully testable here, and tested against the real recordings in
tests/fixtures/ as well as against synthetic signal with known answers.

MUST NOT import `clr` or `pythonnet`, directly or transitively.

Everything here is causal and streaming, because the same code has to serve
two purposes that usually get separate implementations: analysing a recording
afterwards, and deciding what to do about a spike while the recording is
still running. A detector that needs the whole recording cannot do the
second, and two implementations that are supposed to agree eventually will
not.
"""

from biocam.analysis.filters import Biquad, HighPass, highpass_coefficients
from biocam.analysis.sorting import (
    SORTER_LABELS,
    SORTERS,
    AmplitudeSorter,
    PCAKMeansSorter,
    Sorter,
    TemplateSorter,
    make_sorter,
    sort_by_channel,
    suggest_n_units,
)
from biocam.analysis.spikes import (
    NoiseEstimator,
    Spike,
    SpikeDetector,
    detect_all,
)

__all__ = [
    # filtering
    "Biquad",
    "HighPass",
    "highpass_coefficients",
    # detection
    "NoiseEstimator",
    "Spike",
    "SpikeDetector",
    "detect_all",
    # sorting
    "SORTERS",
    "SORTER_LABELS",
    "Sorter",
    "AmplitudeSorter",
    "PCAKMeansSorter",
    "TemplateSorter",
    "make_sorter",
    "sort_by_channel",
    "suggest_n_units",
]
