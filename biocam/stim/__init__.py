"""Layer 2 - stimulation described, validated and planned, without a driver.

Nothing in this package imports `clr` or touches the instrument. It turns what
an experimenter means - amplitudes, durations, a train of pulses - into the
exact tick counts the driver takes, and refuses anything the driver would
silently alter on the way.

The Layer 1 counterpart that actually sends the result is
`biocam.interop.stimulator`.
"""

from biocam.stim.constraints import StimConstraints
from biocam.stim.pulse import (
    PulsePlan,
    PulseSpec,
    PulseValidationError,
    plan,
    verify_built_pulse,
)

__all__ = [
    "StimConstraints",
    "PulsePlan",
    "PulseSpec",
    "PulseValidationError",
    "plan",
    "verify_built_pulse",
]
