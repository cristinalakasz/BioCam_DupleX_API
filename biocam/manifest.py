"""Layer 2 - what experiment produced this recording.

A closed-loop session leaves three files behind: the `.raw` signal, the
acquisition sidecar, and the stimulus log. Between them they say what was
acquired and what fired. **None of them says what was being asked.**

Which electrodes were watched. At what threshold. Which policy decided.
What the safety limits were. Whether the loop was armed at all, or whether
every stimulus in that log was pressed by hand. Whether the run was live or
simulated.

Without those, a recording from a lab day six weeks ago is uninterpretable in
exactly the way that matters: two sessions that look identical in the signal
may have been driven by completely different rules, and nothing on disk
distinguishes them. On a shared instrument, where the person analysing the
data is often not the person who ran it, that is not a small gap.

So this is written beside the recording, at the end of the session, and holds
the whole configuration and the whole outcome. It is a record, not a
mechanism: nothing reads it back to drive anything, so it cannot silently
disagree with what actually ran - every field is captured from the objects
that did the running.

Deliberately plain JSON with no schema library, for the same reason the
sidecar is: a file that cannot be read without this package is a file that
cannot be read.
"""

import json
import os
import platform
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

SCHEMA_VERSION = 1


def _clean(value):
    """JSON-safe, and never a numpy scalar pretending to be a float."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, dict):
        return {str(k): _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_clean(v) for v in value]
    item = getattr(value, "item", None)      # numpy scalars
    if callable(item):
        try:
            return item()
        except Exception:  # noqa: BLE001
            pass
    return str(value)


@dataclass
class SessionManifest:
    """Everything about one session that is not in the signal itself."""

    # -- what kind of run this was ---------------------------------------
    live: bool = False
    source_name: str = ""
    started_utc: str = ""
    finished_utc: str = ""

    # -- files -------------------------------------------------------------
    raw_path: str = ""
    meta_path: str = ""
    stimulus_log_path: str = ""

    # -- what was asked for ------------------------------------------------
    requested_duration_sec: object = None
    detection: dict = field(default_factory=dict)
    closed_loop: dict = field(default_factory=dict)
    traces: dict = field(default_factory=dict)
    stimulus: dict = field(default_factory=dict)

    # -- what happened -----------------------------------------------------
    outcome: dict = field(default_factory=dict)
    warnings: tuple = ()

    # -- where it ran ------------------------------------------------------
    environment: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return _clean({
            "schema_version": SCHEMA_VERSION,
            "live": self.live,
            "source_name": self.source_name,
            "started_utc": self.started_utc,
            "finished_utc": self.finished_utc,
            "raw_path": self.raw_path,
            "meta_path": self.meta_path,
            "stimulus_log_path": self.stimulus_log_path,
            "requested_duration_sec": self.requested_duration_sec,
            "detection": self.detection,
            "closed_loop": self.closed_loop,
            "traces": self.traces,
            "stimulus": self.stimulus,
            "outcome": self.outcome,
            "warnings": list(self.warnings),
            "environment": self.environment,
        })

    def write(self, path) -> None:
        """Write as JSON, atomically.

        The same replace-a-temporary the sidecar and the stimulus log use: a
        half-written manifest is worse than none, because it looks complete.
        """
        path = Path(path)
        payload = json.dumps(self.to_dict(), indent=2)
        handle, temporary = tempfile.mkstemp(
            dir=str(path.parent), prefix=path.name, suffix=".tmp")
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
        """One line, for the session log."""
        bits = ["live" if self.live else "SIMULATED"]
        detect = self.detection.get("channels") or []
        if detect:
            bits.append(f"{len(detect)} electrode(s) watched")
        if self.closed_loop.get("armed"):
            bits.append(f"loop armed ({self.closed_loop.get('policy')})")
        elif self.closed_loop.get("configured"):
            bits.append("loop configured but not armed")
        return ", ".join(bits)


def describe_environment() -> dict:
    """Where this ran. Not decoration - a lab machine and a dev machine
    produce different numbers, and a report that omits which is which invites
    the two to be compared as though they were the same."""
    return {
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "machine": platform.node(),
    }


def _frames_to_ms(frames, rate_hz):
    if not frames or not rate_hz:
        return None
    return round(frames / rate_hz * 1e3, 4)


def detection_settings(loop) -> dict:
    """What the detector was doing, read off the detector itself.

    Read from the live objects rather than from whatever the UI believed it
    had configured, so the manifest cannot drift from what actually ran.
    """
    if loop is None:
        return {"enabled": False}
    inner = getattr(loop, "loop", loop)
    detector = getattr(inner, "detector", None)
    if detector is None:
        return {"enabled": False}
    high_pass = getattr(detector, "filter", None)
    return {
        "enabled": True,
        "channels": [int(c) for c in getattr(loop, "channels", [])],
        "threshold_sigmas": getattr(detector, "threshold_sigmas", None),
        # Stored as frames, reported as milliseconds - the unit it was set in,
        # and the unit anyone comparing two sessions will want.
        "refractory_ms": _frames_to_ms(
            getattr(detector, "refractory_frames", None),
            getattr(detector, "frame_rate_hz", None)),
        "high_pass_hz": getattr(high_pass, "cutoff_hz", None),
        "frame_rate_hz": getattr(detector, "frame_rate_hz", None),
        "collect_waveforms": getattr(detector, "collect_waveforms", None),
    }


def closed_loop_settings(loop) -> dict:
    """The policy and, more importantly, the limits it could not override."""
    if loop is None:
        return {"configured": False, "armed": False}
    inner = getattr(loop, "loop", loop)
    policy = getattr(inner, "policy", None)
    envelope = getattr(inner, "envelope", None)
    settings = {
        "configured": True,
        # Armed means a send was actually wired up. A loop that decided all
        # session and had nowhere to send is a very different experiment from
        # one that stimulated, and the two are otherwise hard to tell apart.
        "armed": getattr(inner, "send", None) is not None,
        "policy": getattr(policy, "name", None),
    }
    for attribute in ("target_hz", "trigger_channels"):
        if hasattr(policy, attribute):
            settings[attribute] = getattr(policy, attribute)
    if envelope is not None:
        settings["limits"] = {
            "min_interval_ms": getattr(envelope, "min_interval_ms", None),
            "max_rate_hz": getattr(envelope, "max_rate_hz", None),
            "max_charge_per_second_pc": getattr(
                envelope, "max_charge_per_second_pc", None),
            "max_stimuli": getattr(envelope, "max_stimuli", None),
        }
    return settings


def _electrodes(items) -> list:
    """[row, col] pairs, whatever shape the caller's electrodes have.

    `Electrode` is a dataclass with `row`/`col`, not a tuple - it is not
    iterable, and assuming it was is what made the first version of this
    module fail at the end of a session, which is the worst possible moment
    for a record-keeping bug to surface.
    """
    pairs = []
    for item in items or ():
        row = getattr(item, "row", None)
        if row is not None:
            pairs.append([int(row), int(item.col)])
        else:
            pairs.append([int(v) for v in item])
    return pairs


def stimulus_settings(plan, pattern) -> dict:
    """The pulse and the electrodes it was delivered through."""
    if plan is None:
        return {"configured": False}
    settings = {
        "configured": True,
        "description": plan.describe() if hasattr(plan, "describe") else None,
        "net_charge_pc": getattr(plan, "net_charge_pc", None),
        "total_us": getattr(plan, "total_us", None),
    }
    if pattern is not None:
        settings["positive"] = _electrodes(getattr(pattern, "positive", ()))
        settings["negative"] = _electrodes(getattr(pattern, "negative", ()))
    return settings


def outcome_from(snapshot, loop=None) -> dict:
    """What the session actually did, from the final snapshot."""
    outcome = {
        "frames": getattr(snapshot, "frames", None),
        "acquisition_sec": getattr(snapshot, "acquisition_sec", None),
        "clock_source": getattr(snapshot, "clock_source", None),
        "frames_missing": getattr(snapshot, "frames_missing", None),
        "verdict": getattr(snapshot, "verdict", None),
        "stop_reason": getattr(snapshot, "stop_reason", None),
        "stimuli_delivered": getattr(snapshot, "stimuli_delivered", None),
        "stimuli_failed": getattr(snapshot, "stimuli_failed", None),
        "spikes_detected": getattr(snapshot, "spikes_detected", None),
        "spike_rate_hz": getattr(snapshot, "spike_rate_hz", None),
        "loop_stimuli": getattr(snapshot, "loop_stimuli", None),
        "loop_refused": getattr(snapshot, "loop_refused", None),
        "loop_suspended": getattr(snapshot, "loop_suspended", None),
    }
    if loop is not None:
        inner = getattr(loop, "loop", loop)
        envelope = getattr(inner, "envelope", None)
        if envelope is not None and hasattr(envelope, "summary"):
            # Which limit did the refusing, not just how many were refused.
            # "the loop wanted to fire more often than allowed" and "the
            # charge budget was spent" are different experimental facts.
            outcome["envelope"] = envelope.summary()
        outcome["max_decision_us"] = getattr(inner, "max_decision_us", None)
        outcome["slow_decisions"] = getattr(inner, "slow_decisions", None)
        detector = getattr(inner, "detector", None)
        if detector is not None:
            outcome["frames_analysed"] = getattr(detector, "frames_analysed", None)
            outcome["frames_skipped"] = getattr(detector, "frames_skipped", None)
    return outcome
