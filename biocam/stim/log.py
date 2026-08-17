"""Layer 2 - the record of what was stimulated, and when.

A recording of 4096 channels is not interpretable on its own. Every later
analysis - did this stimulus evoke a response, at what latency, on which
electrodes - needs to know exactly when each stimulus was delivered relative
to the signal. That correspondence is the experiment; losing it turns a
recording into 152 MB/s of unlabelled noise.

So the log is written for the person analysing the data months later, not for
the person watching the console. Two things follow from that.

**It records what was asked for AND what happened.** A stimulus that was
refused, or that the driver rejected, is as much a part of the record as one
that fired - a gap in a stimulus train that nobody wrote down looks, in the
data, exactly like a stimulus that failed to evoke anything.

**It is explicit about which time is authoritative.** For an immediate
stimulus there are two different answers to "when":

- `clock_us` - what `AcquisitionClock` said at the moment of sending. This is
  the acquisition time of the last packet *processed*, so it is a lower bound:
  the stimulus certainly went out at or after it, by an unknown margin.
- `latency_cycles` - what the driver reported through the `out UInt64` of
  `Send`, documented as clock cycles "relative to the beginning of the
  acquisition" and accounting for the time to program all endpoints. This is
  the instrument's own answer and is the one to trust.

They are both kept. Recording only the first would silently substitute an
estimate for a measurement; recording only the second would leave nothing at
all when the driver does not report one.

Nothing here touches the driver, so all of it is testable.
"""

import json
from dataclasses import asdict, dataclass, field

SCHEMA_VERSION = 1

# A cap, for the same reason GapTracker has one (MAX_RETAINED_GAPS): this list
# is the only unbounded structure on the stimulation path, and Phase 6's
# closed loop stimulates from a loop. Nothing is silently dropped - the counts
# and the accumulated charge keep including truncated entries, and
# `records_truncated` says how many are missing from the detail.
MAX_RETAINED_RECORDS = 100_000

# Outcomes. "refused" means this software declined to send it; "rejected"
# means the driver did. They are kept apart because they point at different
# mistakes - a protocol that was never valid, versus one the instrument would
# not take.
SENT = "sent"
REFUSED = "refused"
REJECTED = "rejected"


@dataclass(frozen=True)
class StimulusRecord:
    """One stimulation attempt, successful or not."""

    index: int
    kind: str                      # "immediate" or "scheduled"
    outcome: str                   # SENT / REFUSED / REJECTED
    clock_us: float = None         # AcquisitionClock at the moment of sending
    clock_source: str = None       # "device" or "frames"
    latency_cycles: int = None     # the driver's own answer, when it gave one
    requested_timestamps_us: tuple = ()   # scheduled only
    pulse: str = None
    positive: tuple = ()
    negative: tuple = ()
    net_charge_pc: float = None
    detail: str = None             # why it was refused or rejected
    # True when nothing was actually delivered because there was no
    # instrument - a simulated run. Without this a simulated record is
    # structurally identical to a real delivery whose clock reading happened
    # to be absent, and CLAUDE.md is explicit that a run which looks real and
    # was not is worse than no run.
    simulated: bool = False

    @property
    def delivered(self) -> bool:
        return self.outcome == SENT

    def best_time_us(self, cycles_per_us: float = None):
        """The most trustworthy delivery time available, in acquisition µs.

        Prefers the driver's reported latency, which is a measurement, over
        the clock reading, which is a lower bound. Returns None when neither
        is usable - never a plausible-looking zero.
        """
        if self.latency_cycles is not None and cycles_per_us:
            return self.latency_cycles / cycles_per_us
        if self.kind == "scheduled" and self.requested_timestamps_us:
            return self.requested_timestamps_us[0]
        return self.clock_us

    def time_is_measured(self, cycles_per_us: float = None) -> bool:
        """Whether `best_time_us` is the instrument's answer or our estimate."""
        return self.latency_cycles is not None and bool(cycles_per_us)


@dataclass
class StimulusLog:
    """Every stimulation attempt in one session, in order.

    Counts and accumulated charge are maintained incrementally rather than
    recomputed. `to_dict` reads all three, so rebuilding a full list per
    property made writing a large log quadratic in its own length.
    """

    records: list = field(default_factory=list)
    n_attempted: int = 0
    n_delivered: int = 0
    n_simulated: int = 0
    records_truncated: int = 0
    _charge_pc: float = 0.0

    def __len__(self) -> int:
        return self.n_attempted

    def __iter__(self):
        return iter(self.records)

    @property
    def delivered(self) -> list:
        return [r for r in self.records if r.delivered]

    @property
    def failed(self) -> list:
        return [r for r in self.records if not r.delivered]

    @property
    def n_failed(self) -> int:
        return self.n_attempted - self.n_delivered

    @property
    def net_charge_pc(self) -> float:
        """Total charge actually delivered.

        Only successful stimuli count: a refused one injected nothing. Over a
        long session this is the number that says whether the electrodes have
        been accumulating a DC offset - so it keeps counting past the
        retention cap, where the individual records stop being kept.
        """
        return self._charge_pc

    # -- recording -------------------------------------------------------

    def _add(self, **fields) -> StimulusRecord:
        record = StimulusRecord(index=self.n_attempted, **fields)
        self.n_attempted += 1
        if record.simulated:
            self.n_simulated += 1
        if record.delivered:
            self.n_delivered += 1
            self._charge_pc += record.net_charge_pc or 0.0
        if len(self.records) < MAX_RETAINED_RECORDS:
            self.records.append(record)
        else:
            self.records_truncated += 1
        return record

    def immediate(
        self, plan, pattern, *, clock_reading=None, latency_cycles=None,
        simulated: bool = False,
    ) -> StimulusRecord:
        """Record a delivered single pulse."""
        return self._add(
            kind="immediate",
            outcome=SENT,
            clock_us=getattr(clock_reading, "acquisition_us", None),
            clock_source=getattr(clock_reading, "source", None),
            latency_cycles=latency_cycles,
            simulated=simulated,
            **_describe(plan, pattern),
        )

    def scheduled(self, plan, pattern, *, clock_reading=None,
                  simulated: bool = False) -> StimulusRecord:
        """Record a queued train or sequence."""
        return self._add(
            kind="scheduled",
            outcome=SENT,
            clock_us=getattr(clock_reading, "acquisition_us", None),
            clock_source=getattr(clock_reading, "source", None),
            requested_timestamps_us=tuple(plan.timestamps_us),
            simulated=simulated,
            **_describe(plan, pattern),
        )

    def failure(
        self, kind, detail, *, plan=None, pattern=None, clock_reading=None,
        rejected_by_driver=False,
    ) -> StimulusRecord:
        """Record an attempt that did not deliver.

        Kept in the same log as the successes, deliberately: a stimulus train
        with a hole in it looks identical, in the recorded signal, to one
        whose stimuli evoked nothing.
        """
        fields = _describe(plan, pattern) if plan is not None else {}
        # The times that were asked for are the most useful thing about a
        # refused train, and they were being dropped.
        requested = tuple(getattr(plan, "timestamps_us", ()) or ())
        return self._add(
            kind=kind,
            outcome=REJECTED if rejected_by_driver else REFUSED,
            clock_us=getattr(clock_reading, "acquisition_us", None),
            clock_source=getattr(clock_reading, "source", None),
            requested_timestamps_us=requested,
            detail=str(detail),
            **fields,
        )

    # -- output ----------------------------------------------------------

    def to_dict(self, cycles_per_us: float = None) -> dict:
        """The whole log, ready to serialise beside a recording."""
        entries = []
        for record in self.records:
            entry = asdict(record)
            entry["delivered"] = record.delivered
            entry["best_time_us"] = record.best_time_us(cycles_per_us)
            entry["time_is_measured"] = record.time_is_measured(cycles_per_us)
            entries.append(entry)
        return {
            "schema_version": SCHEMA_VERSION,
            "cycles_per_us": cycles_per_us,
            "n_attempted": self.n_attempted,
            "n_delivered": self.n_delivered,
            "n_failed": self.n_failed,
            # Non-zero means some or all of this log is a rehearsal.
            "n_simulated": self.n_simulated,
            "simulated": self.n_simulated == self.n_attempted > 0,
            "net_charge_pc": self.net_charge_pc,
            # Non-zero means `stimuli` below is not the whole story: the
            # counts and the charge above still are.
            "records_truncated": self.records_truncated,
            "stimuli": entries,
        }

    def write(self, path, cycles_per_us: float = None) -> None:
        """Write the log as JSON.

        Written whole rather than appended to, and only when a session ends,
        because this runs alongside a recording that must not be interrupted
        by disk work. A session long enough for that to matter is a session
        whose log belongs in a database, not a file.
        """
        import os
        import tempfile
        from pathlib import Path

        path = Path(path)
        payload = json.dumps(self.to_dict(cycles_per_us), indent=2)
        # Same atomic write the recording sidecar uses: a half-written log is
        # worse than none, because it looks complete.
        handle, temporary = tempfile.mkstemp(
            dir=str(path.parent), prefix=path.name, suffix=".tmp"
        )
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as file:
                file.write(payload)
            os.replace(temporary, path)
        except BaseException:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise

    def describe(self) -> str:
        if not self.n_attempted:
            return "no stimuli attempted"
        text = (
            f"{self.n_delivered} of {self.n_attempted} stimuli delivered"
        )
        if self.n_failed:
            text += f", {self.n_failed} not"
        if self.records_truncated:
            text += (f"; {self.records_truncated} individual records dropped "
                     f"past the {MAX_RETAINED_RECORDS} retention cap (counts "
                     "and charge above still include them)")
        if self.n_simulated:
            text += (f" - {self.n_simulated} SIMULATED (no instrument; "
                     "nothing was delivered)")
        charge = self.net_charge_pc
        if charge:
            text += f"; net charge delivered {charge:+g} pC"
        return text


def _describe(plan, pattern) -> dict:
    """Pull the recordable fields off a plan and a pattern."""
    pulse_plan = getattr(plan, "pulse_plan", None)
    if pulse_plan is None:
        pulse_plans = getattr(plan, "pulse_plans", None)
        pulse_plan = pulse_plans[0] if pulse_plans else plan
    return {
        "pulse": pulse_plan.describe(),
        "positive": tuple(str(e) for e in getattr(pattern, "positive", ())),
        "negative": tuple(str(e) for e in getattr(pattern, "negative", ())),
        "net_charge_pc": getattr(plan, "net_charge_pc", None),
    }
