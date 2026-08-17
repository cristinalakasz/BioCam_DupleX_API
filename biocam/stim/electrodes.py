"""Layer 2 - choosing which electrodes a stimulus flows between.

A stimulus needs a positive set and a negative set: current leaves one and
returns through the other. The driver models each as a `StimEndPoint`, built
from a `ChCoord(row, col)`.

Two measured facts shape this module.

**Coordinates are 1-based.** `ChCoord(1, 1)` is the first electrode and reports
`IsValid == True`; `ChCoord(0, 0)` reports `IsNone == True` and raises from
`ToIdentifier()`. An off-by-one here does not fail - it stimulates the wrong
electrode, or none.

**`ChCoord.IsValid` does not bound-check the array.** `ChCoord(65, 65)` on a
64x64 plate reports `IsValid == True`. The type knows nothing about how large
the MEA actually is, so the bound has to be checked here, against a grid the
caller states explicitly.

The remaining limits come from the API introduction PDF (p. 10-11) rather than
from the XML or the assembly, and are marked as such. They are enforced
because exceeding them is documented to fail *silently* - the PDF states that
on buffer overflow the next invocation's values are ignored.
"""

from dataclasses import dataclass

# Source: 3Brain BioCamDriver API introduction PDF, p. 10-11. Not present in
# 3Brain.BioCamDriver.xml and not reachable by reflection - these are runtime
# limits, not type metadata. Unverified against the instrument.
MAX_ENDPOINTS_PER_CONFIGURATION = 1000
MAX_ENDPOINT_VALUES_PER_SEND = 288
MAX_PULSE_VALUES_PER_SEND = 64

# The BioCAM DupleX is 4096 electrodes in a square array.
DEFAULT_GRID_ROWS = 64
DEFAULT_GRID_COLS = 64


class PatternValidationError(ValueError):
    """An electrode pattern the instrument would reject or mis-execute."""

    def __init__(self, problems):
        self.problems = list(problems)
        super().__init__(
            "electrode pattern is not valid:\n  - " + "\n  - ".join(self.problems)
        )


@dataclass(frozen=True, order=True)
class Electrode:
    """One electrode, in the driver's 1-based (row, col) convention."""

    row: int
    col: int

    def __str__(self) -> str:
        return f"({self.row},{self.col})"

    @property
    def caption(self) -> str:
        """The form `ChCoord.Caption` produces, for cross-checking."""
        return f"{self.row},{self.col}"


@dataclass(frozen=True)
class ElectrodeGrid:
    """The extent of the MEA, so coordinates can be bounds-checked.

    Stated explicitly rather than read from the driver: `BioCamDataFormat`
    exposes `NChsPerWell` and `NWells` but no row/column count, and the plate
    geometry sits behind `IMeaPlatePilot`, which needs the instrument. Rather
    than guess a member name, the caller states the grid.
    """

    n_rows: int = DEFAULT_GRID_ROWS
    n_cols: int = DEFAULT_GRID_COLS

    def __post_init__(self):
        if self.n_rows <= 0 or self.n_cols <= 0:
            raise ValueError(
                f"grid must be positive in both dimensions, got "
                f"{self.n_rows}x{self.n_cols}"
            )

    @property
    def n_electrodes(self) -> int:
        return self.n_rows * self.n_cols

    def contains(self, electrode: Electrode) -> bool:
        return (
            1 <= electrode.row <= self.n_rows
            and 1 <= electrode.col <= self.n_cols
        )

    @classmethod
    def square_from_channel_count(cls, n_channels: int) -> "ElectrodeGrid":
        """Assume a square array of `n_channels` electrodes.

        True of the DupleX (4096 = 64x64). Raises rather than rounding if the
        count is not a perfect square, because a silently wrong grid would
        bounds-check against the wrong limit.
        """
        side = int(round(n_channels ** 0.5))
        if side * side != n_channels:
            raise ValueError(
                f"{n_channels} channels is not a square array; state the grid "
                "explicitly with ElectrodeGrid(n_rows, n_cols)"
            )
        return cls(side, side)


@dataclass(frozen=True)
class StimPattern:
    """Where a stimulus enters the culture and where it returns.

    `positive` and `negative` are the two endpoint sets. Both must be
    non-empty: current has to have a return path.
    """

    positive: tuple
    negative: tuple
    name: str = "pattern"

    def __post_init__(self):
        # Accept any iterable but store tuples, so a pattern cannot be mutated
        # after it has been validated.
        object.__setattr__(self, "positive", tuple(self.positive))
        object.__setattr__(self, "negative", tuple(self.negative))

    @property
    def n_endpoints(self) -> int:
        return len(self.positive) + len(self.negative)

    @property
    def columns_used(self) -> set:
        return {e.col for e in self.positive} | {e.col for e in self.negative}


def validate_pattern(
    pattern: StimPattern,
    grid: ElectrodeGrid = None,
    *,
    enforce_column_rule: bool = True,
) -> None:
    """Check a pattern against the array and the documented endpoint limits.

    Raises `PatternValidationError` listing every problem found.

    `enforce_column_rule=False` disables the positive/negative column
    separation check. That rule comes from the API introduction PDF and has
    not been confirmed against the instrument; the override exists so that a
    lab finding it does not apply is not blocked by this code. Leave it on
    until someone has checked.
    """
    grid = grid or ElectrodeGrid()
    problems = []

    if not pattern.positive:
        problems.append("no positive endpoints; current needs somewhere to go")
    if not pattern.negative:
        problems.append("no negative endpoints; current needs a return path")

    for label, electrodes in (
        ("positive", pattern.positive),
        ("negative", pattern.negative),
    ):
        for electrode in electrodes:
            if not grid.contains(electrode):
                problems.append(
                    f"{label} endpoint {electrode} is outside the "
                    f"{grid.n_rows}x{grid.n_cols} array (rows and columns are "
                    "1-based). ChCoord would report this as valid - it does "
                    "not know the array size - so nothing downstream catches it."
                )
        duplicates = _duplicates(electrodes)
        if duplicates:
            problems.append(
                f"{label} endpoints repeat: "
                f"{', '.join(str(e) for e in duplicates)}"
            )

    shared = set(pattern.positive) & set(pattern.negative)
    if shared:
        problems.append(
            "these electrodes are both positive and negative: "
            f"{', '.join(str(e) for e in sorted(shared))}"
        )

    if enforce_column_rule:
        positive_columns = {e.col for e in pattern.positive}
        negative_columns = {e.col for e in pattern.negative}
        overlap = positive_columns & negative_columns
        if overlap:
            problems.append(
                f"positive and negative endpoints share column(s) "
                f"{sorted(overlap)}. The API introduction PDF states positive "
                "and negative endpoints may never share an electrode column. "
                "Pass enforce_column_rule=False to override if the lab "
                "establishes otherwise."
            )

    if pattern.n_endpoints > MAX_ENDPOINTS_PER_CONFIGURATION:
        problems.append(
            f"{pattern.n_endpoints} endpoints exceeds the documented maximum "
            f"of {MAX_ENDPOINTS_PER_CONFIGURATION} per spatial configuration"
        )
    if pattern.n_endpoints > MAX_ENDPOINT_VALUES_PER_SEND:
        problems.append(
            f"{pattern.n_endpoints} endpoints exceeds the documented "
            f"{MAX_ENDPOINT_VALUES_PER_SEND} endpoint values per Send. The PDF "
            "states buffer overflow is silent: the next invocation's values "
            "are ignored rather than an error being raised."
        )

    if problems:
        raise PatternValidationError(problems)


def _duplicates(electrodes):
    seen, repeated = set(), []
    for electrode in electrodes:
        if electrode in seen and electrode not in repeated:
            repeated.append(electrode)
        seen.add(electrode)
    return repeated


def bipolar_pair(
    positive: Electrode, negative: Electrode, name: str = "bipolar"
) -> StimPattern:
    """The simplest useful pattern: one electrode against one other."""
    return StimPattern(positive=(positive,), negative=(negative,), name=name)
