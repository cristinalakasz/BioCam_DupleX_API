"""Layer 3 - signal processing.

Spike detection, and later spike sorting and closed-loop decision logic.
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
from biocam.analysis.spikes import (
    NoiseEstimator,
    Spike,
    SpikeDetector,
    detect_all,
)

__all__ = [
    "Biquad",
    "HighPass",
    "highpass_coefficients",
    "NoiseEstimator",
    "Spike",
    "SpikeDetector",
    "detect_all",
]
