"""Layer 2 - the stimulator's limits, as plain numbers.

Mirrors `_3Brain.Common.StimProperties`. Reading that object is ordinary
attribute access, so the conversion lives here rather than in Layer 1 and is
tested against a stand-in rather than the driver.

Every field is a limit the driver enforces **silently**. See
`docs/api/stimulation-reference.md` for the measurements behind that claim; the
short version is that an out-of-range amplitude is clamped and an over-long
pulse has its later phases shortened, both without raising. This module exists
so that those limits can be checked *before* a pulse reaches .NET, where a
violation stops being an error and becomes a wrong stimulus.
"""

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class StimConstraints:
    """What the stimulator will accept, in its own units.

    `time_resolution_us` is the stimulator clock period; every duration the
    driver takes is an integer count of these ticks, not a time. Durations in
    this package are given in microseconds and converted here, so that the tick
    domain never leaks into calling code.

    `max_total_ticks` bounds `width1 + inter_width + width2` for one pulse -
    not each phase separately. That is measured, not documented: see the
    truncation table in the reference document.
    """

    time_resolution_us: int
    amplitude_resolution: float
    min_amplitude: float
    max_amplitude: float
    max_total_ticks: int
    unit: str = "uA"
    is_current: bool = True

    def __post_init__(self):
        if self.time_resolution_us <= 0:
            raise ValueError(
                f"time_resolution_us must be positive, got "
                f"{self.time_resolution_us}"
            )
        if self.amplitude_resolution <= 0:
            raise ValueError(
                f"amplitude_resolution must be positive, got "
                f"{self.amplitude_resolution}"
            )
        if self.max_total_ticks <= 0:
            raise ValueError(
                f"max_total_ticks must be positive, got {self.max_total_ticks}"
            )
        if self.min_amplitude > self.max_amplitude:
            raise ValueError(
                f"min_amplitude ({self.min_amplitude}) exceeds max_amplitude "
                f"({self.max_amplitude})"
            )

    @property
    def max_total_us(self) -> int:
        """The longest single pulse, in microseconds."""
        return self.max_total_ticks * self.time_resolution_us

    def ticks_for(self, duration_us) -> int:
        """Convert microseconds to clock ticks, refusing inexact conversions.

        A duration that is not a whole number of ticks would be snapped by the
        driver without saying so, so it is rejected here instead.
        """
        quotient = Decimal(str(duration_us)) / Decimal(self.time_resolution_us)
        if quotient != quotient.to_integral_value():
            raise ValueError(
                f"{duration_us} us is not a whole number of "
                f"{self.time_resolution_us} us ticks "
                f"(it is {quotient} ticks). Choose a multiple of "
                f"{self.time_resolution_us} us."
            )
        return int(quotient)

    def is_on_amplitude_grid(self, amplitude) -> bool:
        """Whether an amplitude can be represented exactly."""
        quotient = Decimal(str(amplitude)) / Decimal(str(self.amplitude_resolution))
        return quotient == quotient.to_integral_value()

    def snap_amplitude(self, amplitude) -> float:
        """Round an amplitude onto the grid, ties away from zero.

        This matches the driver's own rounding, measured in the reference
        document (7.5 -> 8.0 at a resolution of 1.0). Python's built-in
        `round` uses banker's rounding and would disagree on exact halves, so
        it is deliberately not used.
        """
        from decimal import ROUND_HALF_UP

        step = Decimal(str(self.amplitude_resolution))
        steps = (Decimal(str(amplitude)) / step).quantize(
            Decimal(1), rounding=ROUND_HALF_UP
        )
        return float(steps * step)

    @classmethod
    def from_stim_properties(cls, properties) -> "StimConstraints":
        """Build from a live `StimProperties`.

        Pass `biocam.Stim.Properties` - the constraints the *device* reports.

        Do **not** pass `StimProperties.Default`. That static is a placeholder
        carrying `TimeResolutionMicroSec = 1` where the DupleX stimulator's
        real resolution is coarser, and zeroes in the fields it does not set.
        Building pulses against it yields durations wrong by the ratio of the
        two resolutions - silently, because every value involved is legal.
        """
        return cls(
            time_resolution_us=int(properties.TimeResolutionMicroSec),
            amplitude_resolution=float(properties.AmplitudeResolution),
            min_amplitude=float(properties.MinAmplitude),
            max_amplitude=float(properties.MaxAmplitude),
            max_total_ticks=int(properties.MaxPulseDuration),
            unit=str(properties.UnitMeasureString),
            is_current=bool(properties.IsCurrentStimulator),
        )
