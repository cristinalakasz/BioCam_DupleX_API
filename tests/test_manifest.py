"""The record of what a session was.

The point of this file is that a recording six weeks old must be
interpretable: which electrodes were watched, at what threshold, under which
policy, inside what limits, and whether anything was actually delivered. The
tests are mostly about what would be *missing* if it were absent.
"""

import json

import numpy as np
import pytest

from biocam.analysis.spikes import SpikeDetector
from biocam.loop import ClosedLoop, EchoPolicy, RatePolicy, SafetyEnvelope
from biocam.manifest import (
    SessionManifest, closed_loop_settings, describe_environment,
    detection_settings, outcome_from, stimulus_settings,
)

RATE = 18557.720703125


class Wrapped:
    """Stands in for PacketLoop: a loop plus the channels it watches."""

    def __init__(self, loop, channels):
        self.loop = loop
        self.channels = channels


def a_loop(*, armed=True, policy=None, channels=(300, 301), **envelope_kw):
    detector = SpikeDetector(len(channels), RATE, threshold_sigmas=6.0,
                             collect_waveforms=True)
    loop = ClosedLoop(detector, policy or EchoPolicy(),
                      SafetyEnvelope(RATE, **envelope_kw),
                      send=(lambda t: None) if armed else None)
    return Wrapped(loop, list(channels))


# --------------------------------------------------------------------------
# Detection
# --------------------------------------------------------------------------

def test_the_watched_electrodes_are_recorded():
    # Without this, a recording does not say what it was listening to - and
    # the spike counts in it cannot be attributed to anything.
    settings = detection_settings(a_loop(channels=(300, 301, 332)))
    assert settings["enabled"] is True
    assert settings["channels"] == [300, 301, 332]


def test_the_threshold_and_filter_are_recorded():
    settings = detection_settings(a_loop())
    assert settings["threshold_sigmas"] == 6.0
    assert settings["high_pass_hz"] == 300.0
    # Stored internally as frames; reported in the unit it was set in.
    assert settings["refractory_ms"] == pytest.approx(1.0, abs=0.05)


def test_no_detection_says_so_rather_than_omitting_it():
    # An absent key reads as "unknown". False reads as "this did not happen",
    # which is the fact worth recording.
    assert detection_settings(None) == {"enabled": False}


# --------------------------------------------------------------------------
# The closed loop, and the distinction that matters most
# --------------------------------------------------------------------------

def test_a_configured_but_unarmed_loop_is_not_recorded_as_armed():
    # A loop that decided all session with nowhere to send is a completely
    # different experiment from one that stimulated. Nothing else on disk
    # distinguishes them: the stimulus log of an unarmed session is empty,
    # and so is the log of an armed session that never triggered.
    assert closed_loop_settings(a_loop(armed=False))["armed"] is False
    assert closed_loop_settings(a_loop(armed=True))["armed"] is True


def test_the_safety_limits_are_recorded():
    settings = closed_loop_settings(
        a_loop(min_interval_ms=50.0, max_rate_hz=4.0, max_stimuli=100))
    limits = settings["limits"]
    assert limits["min_interval_ms"] == pytest.approx(50.0, abs=0.1)
    assert limits["max_rate_hz"] == 4.0
    assert limits["max_stimuli"] == 100


def test_the_policy_is_named_and_its_setting_kept():
    echo = closed_loop_settings(a_loop())
    assert echo["policy"] == "echo"
    rate = closed_loop_settings(
        a_loop(policy=RatePolicy(RATE, target_hz=2.5)))
    assert rate["policy"] == "rate"
    assert rate["target_hz"] == 2.5


def test_no_loop_says_so():
    assert closed_loop_settings(None) == {"configured": False, "armed": False}


# --------------------------------------------------------------------------
# The stimulus
# --------------------------------------------------------------------------

def a_pulse():
    from biocam.stim import (
        PulseSpec, StimConstraints, StimPattern, plan as plan_pulse,
    )

    constraints = StimConstraints(
        time_resolution_us=10, amplitude_resolution=1.0,
        min_amplitude=-1000.0, max_amplitude=1000.0, max_total_ticks=1000)
    plan = plan_pulse(
        PulseSpec(-150.0, 200.0, 50.0, 150.0, 200.0, name="p"), constraints)
    return plan, StimPattern(positive=((10, 10),), negative=((20, 30),))


def test_the_pulse_and_its_electrodes_are_recorded():
    plan, pattern = a_pulse()
    settings = stimulus_settings(plan, pattern)
    assert settings["configured"] is True
    assert "150" in settings["description"]
    assert settings["net_charge_pc"] == 0.0
    assert settings["positive"] == [[10, 10]]
    assert settings["negative"] == [[20, 30]]


def test_no_stimulus_says_so():
    assert stimulus_settings(None, None) == {"configured": False}


# --------------------------------------------------------------------------
# Outcome
# --------------------------------------------------------------------------

class Snap:
    frames = 18600
    acquisition_sec = 1.002
    clock_source = "frames"
    frames_missing = 0
    verdict = "clean"
    stop_reason = "duration_reached"
    stimuli_delivered = 3
    stimuli_failed = 0
    spikes_detected = 96
    spike_rate_hz = 96.0
    loop_stimuli = 10
    loop_refused = 52
    loop_suspended = False
    warnings = ()


def test_the_outcome_records_which_limit_refused():
    # "52 refused" is not enough. Refused by the interval floor and refused by
    # the charge budget are different experimental facts.
    wrapped = a_loop()
    wrapped.loop.envelope.record(0)
    _, _, which = wrapped.loop.envelope.check(10)
    wrapped.loop.envelope.note_refusal(which)
    outcome = outcome_from(Snap(), wrapped)
    assert outcome["envelope"]["refused_interval"] == 1
    assert outcome["loop_refused"] == 52


def test_the_outcome_records_the_decision_timing():
    outcome = outcome_from(Snap(), a_loop())
    assert "max_decision_us" in outcome
    assert "slow_decisions" in outcome


def test_the_outcome_records_frames_never_analysed():
    # A gap between what was recorded and what detection saw is the difference
    # between "the culture was quiet" and "we stopped listening".
    wrapped = a_loop()
    wrapped.loop.detector.skip_frames(500)
    outcome = outcome_from(Snap(), wrapped)
    assert outcome["frames_skipped"] == 500


def test_an_outcome_without_a_loop_still_works():
    outcome = outcome_from(Snap(), None)
    assert outcome["frames"] == 18600
    assert "envelope" not in outcome


# --------------------------------------------------------------------------
# The file
# --------------------------------------------------------------------------

def test_it_round_trips_through_json(tmp_path):
    wrapped = a_loop()
    plan, pattern = a_pulse()
    manifest = SessionManifest(
        live=False, source_name="replay",
        detection=detection_settings(wrapped),
        closed_loop=closed_loop_settings(wrapped),
        stimulus=stimulus_settings(plan, pattern),
        outcome=outcome_from(Snap(), wrapped),
        environment=describe_environment(),
    )
    path = tmp_path / "session.json"
    manifest.write(path)
    loaded = json.loads(path.read_text(encoding="utf-8"))
    assert loaded["schema_version"] == 1
    assert loaded["detection"]["channels"] == [300, 301]
    assert loaded["closed_loop"]["limits"]["max_rate_hz"] == 10.0
    assert loaded["outcome"]["verdict"] == "clean"


def test_numpy_values_do_not_break_the_write(tmp_path):
    # Channels and thresholds arrive from numpy, and json.dumps refuses a
    # numpy scalar. A manifest that cannot be written is worse than no
    # manifest, because the failure surfaces at the end of a lab session.
    manifest = SessionManifest(
        detection={"channels": [np.intp(300)], "threshold_sigmas": np.float64(5.0)},
        outcome={"frames": np.int64(1000)},
    )
    path = tmp_path / "session.json"
    manifest.write(path)
    loaded = json.loads(path.read_text(encoding="utf-8"))
    assert loaded["detection"]["channels"] == [300]
    assert loaded["outcome"]["frames"] == 1000


def test_a_simulated_run_is_labelled_as_one(tmp_path):
    # The single most important field. A simulated session mistaken for a real
    # one is worse than no session at all.
    assert "SIMULATED" in SessionManifest(live=False).describe()
    assert "live" in SessionManifest(live=True).describe()


def test_a_failed_write_leaves_no_half_file(tmp_path):
    manifest = SessionManifest()
    target = tmp_path / "sub" / "session.json"   # parent does not exist
    with pytest.raises(Exception):
        manifest.write(target)
    assert not list(tmp_path.glob("**/*.tmp"))
