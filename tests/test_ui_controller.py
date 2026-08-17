"""The UI's session controller, driven through a real recording."""

import time

import numpy as np
import pytest

from biocam.control import StimulationQueue
from biocam.data.events import GapDetected, RecordingStarted
from biocam.data.recording import AcquisitionParameters, read_sidecar
from biocam.stim import (
    Electrode, PulseSpec, StimConstraints, bipolar_pair, plan as plan_pulse,
)
from biocam.ui.controller import SessionController, SessionSnapshot, _EventRing
from biocam.ui.factories import ReplayFactory

DUPLEX = StimConstraints(
    time_resolution_us=10, amplitude_resolution=1.0,
    min_amplitude=-1000.0, max_amplitude=1000.0, max_total_ticks=1000,
)
PULSE = plan_pulse(PulseSpec(100.0, 200.0, 100.0, -100.0, 200.0), DUPLEX)
PATTERN = bipolar_pair(Electrode(10, 10), Electrode(20, 30))

PARAMS = AcquisitionParameters(
    frame_rate_hz=1000.0, total_channels=4, ch_sample_byte_size=2, bit_depth=12,
    adc_counts_to_value=1.0, offset=0.0, min_digital_value=0, max_digital_value=4095,
)


def a_factory(tmp_path, n_frames=1000, **kwargs):
    data = np.arange(n_frames * 4, dtype=np.uint16).reshape(n_frames, 4)
    raw = tmp_path / "src.raw"
    data.tofile(raw)
    kwargs.setdefault("frames_per_packet", 10)
    return ReplayFactory(
        raw_path=raw, params=PARAMS, output_path=tmp_path / "out.raw", **kwargs
    )


def run_to_completion(controller, factory, timeout=10.0):
    controller.start(factory)
    assert controller.join(timeout), "the recording thread did not finish"
    return controller.snapshot()


# --------------------------------------------------------------------------
# the event ring
# --------------------------------------------------------------------------

def test_the_event_ring_drops_the_oldest_and_counts_it():
    # The opposite choice from the packet queue, deliberately: a dropped
    # packet is data lost forever, a dropped event is one line of history,
    # and the latest state is what a person watching needs.
    ring = _EventRing(capacity=3)
    for i in range(5):
        ring.put(i)
    assert ring.drain() == [2, 3, 4]
    assert ring.dropped == 2


def test_draining_the_ring_empties_it():
    ring = _EventRing()
    ring.put("a")
    assert ring.drain() == ["a"]
    assert ring.drain() == []


def test_the_ring_never_blocks_a_writer():
    # A listener that blocked because the UI stopped polling would stall the
    # only thread draining the packet queue.
    ring = _EventRing(capacity=2)
    for i in range(10_000):
        ring.put(i)
    assert len(ring.drain()) == 2


# --------------------------------------------------------------------------
# a whole recording, with no instrument anywhere
# --------------------------------------------------------------------------

def test_a_replayed_recording_runs_to_completion(tmp_path):
    controller = SessionController()
    factory = a_factory(tmp_path)
    state = run_to_completion(controller, factory)

    assert state.finished
    assert not state.running
    assert state.frames == 1000
    assert state.verdict == "clean"
    assert state.stop_reason == "source_exhausted"
    assert not state.error
    # The real writer ran: there is a real sidecar.
    assert read_sidecar(factory.meta_path)["status"] == "complete"


def test_the_recording_writes_the_bytes(tmp_path):
    controller = SessionController()
    factory = a_factory(tmp_path, n_frames=500)
    run_to_completion(controller, factory)
    assert factory.output_path.read_bytes() == factory.raw_path.read_bytes()


def test_events_reach_the_ui(tmp_path):
    controller = SessionController()
    run_to_completion(controller, a_factory(tmp_path))
    events = controller.drain_events()
    assert any(isinstance(e, RecordingStarted) for e in events)


def test_gaps_are_reported_to_the_ui(tmp_path):
    controller = SessionController()
    factory = a_factory(tmp_path, drop_packets=(5, 6, 7))
    state = run_to_completion(controller, factory)
    assert state.verdict == "gaps_detected"
    assert state.frames_missing == 30
    assert any(isinstance(e, GapDetected) for e in controller.drain_events())


def test_a_duration_limit_is_honoured(tmp_path):
    controller = SessionController()
    state = run_to_completion(controller, a_factory(tmp_path, duration_sec=0.1))
    assert state.stop_reason == "duration_reached"
    assert state.frames == 100


def test_stopping_early_finishes_cleanly(tmp_path):
    controller = SessionController()
    # Paced slowly enough that stop() lands mid-recording.
    factory = a_factory(tmp_path, n_frames=100_000, pace_hz=200.0)
    controller.start(factory)
    time.sleep(0.15)
    controller.stop()
    assert controller.join(10.0)
    state = controller.snapshot()
    assert state.finished
    assert state.stop_reason == "user_stopped"
    assert 0 < state.frames < 100_000
    assert read_sidecar(factory.meta_path)["status"] == "complete"


def test_a_session_reports_progress_while_running(tmp_path):
    controller = SessionController()
    factory = a_factory(tmp_path, n_frames=100_000, pace_hz=200.0)
    controller.start(factory)
    time.sleep(0.15)
    running = controller.snapshot()
    controller.stop()
    controller.join(10.0)

    assert running.running
    assert running.elapsed_sec > 0
    assert running.frames > 0
    assert running.acquisition_sec > 0
    assert running.clock_source == "frames"   # a replay has no device clock


def test_two_recordings_cannot_run_at_once(tmp_path):
    controller = SessionController()
    factory = a_factory(tmp_path, n_frames=100_000, pace_hz=500.0)
    controller.start(factory)
    try:
        with pytest.raises(RuntimeError, match="already running"):
            controller.start(a_factory(tmp_path))
    finally:
        controller.stop()
        controller.join(10.0)


def test_a_failing_factory_surfaces_as_an_error_not_a_hang(tmp_path):
    # A worker thread that dies silently leaves a window saying "recording"
    # forever, which is worse than any error message.
    class Broken(ReplayFactory):
        def make_source(self):
            raise RuntimeError("the instrument fell over")

    factory = Broken(
        raw_path=a_factory(tmp_path).raw_path, params=PARAMS,
        output_path=tmp_path / "out.raw",
    )
    controller = SessionController()
    state = run_to_completion(controller, factory)

    assert state.finished
    assert not state.running
    assert "the instrument fell over" in state.error


def test_snapshot_before_anything_started_is_idle():
    state = SessionController().snapshot()
    assert isinstance(state, SessionSnapshot)
    assert not state.running
    assert not state.finished
    assert state.frames == 0


# --------------------------------------------------------------------------
# stimulation through the UI, end to end
# --------------------------------------------------------------------------

def test_a_requested_stimulus_is_dispatched_during_the_recording(tmp_path):
    controller = SessionController(
        stim_queue=StimulationQueue(min_interval_us=0.0)
    )
    factory = a_factory(tmp_path)
    assert controller.request_stimulus(PULSE, PATTERN, label="one")
    state = run_to_completion(controller, factory)

    assert state.stimuli_delivered == 1
    # The simulated stimulator logged it, so the whole path either side of
    # the driver call was exercised.
    assert len(factory.log) == 1


def test_many_stimuli_are_dispatched_one_per_packet(tmp_path):
    controller = SessionController(
        stim_queue=StimulationQueue(capacity=64, min_interval_us=0.0)
    )
    factory = a_factory(tmp_path)
    for i in range(20):
        controller.request_stimulus(PULSE, PATTERN, label=str(i))
    state = run_to_completion(controller, factory)
    assert state.stimuli_delivered == 20


def test_a_full_stimulation_queue_refuses_rather_than_growing(tmp_path):
    controller = SessionController(stim_queue=StimulationQueue(capacity=2))
    assert controller.request_stimulus(PULSE, PATTERN) is True
    assert controller.request_stimulus(PULSE, PATTERN) is True
    assert controller.request_stimulus(PULSE, PATTERN) is False


def test_undelivered_stimuli_are_reported_as_warnings(tmp_path):
    # Stimulation is not serviced during shutdown, so anything still queued
    # was never delivered and the operator must be told.
    controller = SessionController(
        stim_queue=StimulationQueue(capacity=8, min_interval_us=1e9)
    )
    for _ in range(4):
        controller.request_stimulus(PULSE, PATTERN)
    state = run_to_completion(controller, a_factory(tmp_path, n_frames=100))
    assert any("still queued" in w for w in state.warnings)


def test_stimulation_counters_reach_the_snapshot(tmp_path):
    controller = SessionController(
        stim_queue=StimulationQueue(capacity=2, min_interval_us=1e9)
    )
    for _ in range(5):
        controller.request_stimulus(PULSE, PATTERN)
    state = controller.snapshot()
    assert state.stimuli_pending == 2
    assert state.stimuli_failed == 3      # three were refused


def test_clock_warnings_reach_the_snapshot(tmp_path):
    # A replay carries timestamps but no conversion factor, so the clock
    # calibrates its own - and the cross-check then reduces to an identity.
    # The operator must be told that rather than shown a passing check.
    controller = SessionController()
    state = run_to_completion(controller, a_factory(tmp_path))
    assert any("cannot detect anything" in w for w in state.warnings)
