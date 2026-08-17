"""Layer 2 - stimulation described, validated and planned, without a driver.

Nothing in this package imports `clr` or touches the instrument. It turns what
an experimenter means - amplitudes, durations, a train of pulses - into the
exact tick counts the driver takes, and refuses anything the driver would
silently alter on the way.

The Layer 1 counterpart that actually sends the result is
`biocam.interop.stimulator`.
"""

from biocam.stim.constraints import StimConstraints
from biocam.stim.electrodes import (
    Electrode,
    ElectrodeGrid,
    PatternValidationError,
    StimPattern,
    bipolar_pair,
    validate_pattern,
)
from biocam.stim.pulse import (
    PulsePlan,
    PulseSpec,
    PulseValidationError,
    plan,
    verify_built_pulse,
)
from biocam.stim.train import (
    SequencePlan,
    TrainPlan,
    TrainSpec,
    TrainValidationError,
    plan_sequence,
    plan_train,
    ramp,
)

__all__ = [
    # constraints
    "StimConstraints",
    # pulses
    "PulsePlan",
    "PulseSpec",
    "PulseValidationError",
    "plan",
    "verify_built_pulse",
    # electrodes
    "Electrode",
    "ElectrodeGrid",
    "PatternValidationError",
    "StimPattern",
    "bipolar_pair",
    "validate_pattern",
    # trains and sequences
    "SequencePlan",
    "TrainPlan",
    "TrainSpec",
    "TrainValidationError",
    "plan_sequence",
    "plan_train",
    "ramp",
]
