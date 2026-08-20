"""When a stimulus happened, relative to the recording.

This is the correspondence that cannot be reconstructed afterwards. The signal
shows a stimulus artefact; the log shows a stimulus; nothing else ties one to
the other. If the acquisition time is missing from the log, a later analysis
has no way to say which artefact belongs to which request - and on a train
with a hole in it, no way to tell a stimulus that evoked nothing from one that
never fired.

The wiring for this existed and no caller reached it: the window built its
`Stimulator` without a clock, so `_read_clock()` returned None for every
stimulus and every record was written with `clock_us: null`.
"""

import json
import pathlib
import tempfile
import time

import pytest

from biocam.data.recording import AcquisitionParameters
from biocam.stim import (
    PulseSpec, StimConstraints, StimPattern, plan as plan_pulse,
)
from biocam.stim.log import StimulusLog
from biocam.ui.controller import SessionController
from biocam.ui.factories import ReplayFactory

PARAMS = AcquisitionParameters(
    frame_rate_hz=1000.0, total_channels=8, ch_sample_byte_size=2,
    bit_depth=12, adc_counts_to_value=1.0, offset=0.0,
    min_digital_value=0, max_digital_value=4095,
)
CONSTRAINTS = StimConstraints(
    time_resolution_us=10, amplitude_resolution=1.0,
    min_amplitude=-1000.0, max_amplitude=1000.0, max_total_ticks=1000,
)


def a_pulse():
    plan = plan_pulse(
        PulseSpec(-150.0, 200.0, 50.0, 150.0, 200.0, name="t"), CONSTRAINTS)
    return plan, StimPattern(positive=((10, 10),), negative=((20, 30),))


def a_recording(tmp_path, n_frames=4000):
    import numpy as np

    raw = tmp_path / "src.raw"
    np.arange(n_frames * 8, dtype=np.uint16).reshape(n_frames, 8).tofile(raw)
    return raw


def run_with_stimuli(tmp_path, count=3, **factory_kw):
    """Record, stimulating a few times mid-recording. Returns the log."""
    plan, pattern = a_pulse()
    factory = ReplayFactory(
        raw_path=a_recording(tmp_path), params=PARAMS,
        output_path=tmp_path / "out.raw", frames_per_packet=40,
        pace_hz=200.0, **factory_kw)
    controller = SessionController()
    controller.start(factory)
    sent = 0
    deadline = time.time() + 30
    while controller.running and sent < count and time.time() < deadline:
        time.sleep(0.02)
        if controller.request_stimulus(plan, pattern, label="manual"):
            sent += 1
    assert controller.join(30), "the recording did not finish"
    return factory.log, controller


# --------------------------------------------------------------------------
# The thing that was broken
# --------------------------------------------------------------------------

def test_every_stimulus_carries_an_acquisition_time(tmp_path):
    log, _ = run_with_stimuli(tmp_path)
    assert log.records, "no stimulus was recorded at all"
    untimed = [r for r in log.records if r.clock_us is None]
    assert not untimed, (
        f"{len(untimed)} of {len(log.records)} stimuli have no acquisition "
        "time, so nothing in the recording can be matched to them"
    )


def test_the_time_names_the_clock_it_came_from(tmp_path):
    # "0.42 s" means different things from the device's own clock and from a
    # frame count, and a reader who cannot tell which cannot judge it.
    log, _ = run_with_stimuli(tmp_path)
    assert all(r.clock_source for r in log.records)


def test_the_times_advance_through_the_recording(tmp_path):
    # Not merely present: monotonic, and inside the recording. A constant or
    # zero time would satisfy "not None" and mean nothing.
    log, controller = run_with_stimuli(tmp_path, count=4)
    times = [r.clock_us for r in log.records]
    assert times == sorted(times), f"times are not monotonic: {times}"
    assert times[0] > 0, "the first stimulus is at time zero, which is a sentinel"
    duration_us = controller.snapshot().frames / PARAMS.frame_rate_hz * 1e6
    assert times[-1] <= duration_us + 1e6, (
        f"a stimulus is timed at {times[-1]:.0f} us, past the end of a "
        f"{duration_us:.0f} us recording"
    )


def test_closed_loop_stimuli_are_timed_too(tmp_path):
    # These are the ones nobody presses a button for, so an untimed one would
    # be noticed last.
    log, _ = run_with_stimuli(
        tmp_path, count=0, detect_channels=(0, 1), close_the_loop=True,
        threshold_sigmas=3.0)
    loop_records = [r for r in log.records if r.clock_us is not None]
    if not log.records:
        pytest.skip("the synthetic recording triggered no closed-loop stimuli")
    assert len(loop_records) == len(log.records), (
        "a closed-loop stimulus was recorded without an acquisition time")


# --------------------------------------------------------------------------
# The wiring itself
# --------------------------------------------------------------------------

def test_the_controller_hands_the_clock_to_the_factory(tmp_path):
    seen = {}

    class Watching(ReplayFactory):
        def attach_clock(self, clock):
            seen["clock"] = clock
            super().attach_clock(clock)

    factory = Watching(raw_path=a_recording(tmp_path), params=PARAMS,
                       output_path=tmp_path / "out.raw", frames_per_packet=40)
    controller = SessionController()
    controller.start(factory)
    assert controller.join(30)
    assert seen.get("clock") is not None, (
        "the session never gave its clock to the thing that sends stimuli")


def test_a_factory_without_attach_clock_still_records(tmp_path):
    # The controller calls it through getattr: a caller with its own factory
    # must not be broken by a method it does not have.
    class Plain(ReplayFactory):
        attach_clock = None

    factory = Plain(raw_path=a_recording(tmp_path), params=PARAMS,
                    output_path=tmp_path / "out.raw", frames_per_packet=40)
    controller = SessionController()
    controller.start(factory)
    assert controller.join(30)
    assert controller.snapshot().verdict == "clean"


def test_an_unreadable_clock_does_not_stop_a_stimulus(tmp_path):
    # A stimulus that is otherwise valid must not be refused because the
    # clock could not say where it was. The absence is recorded as absence.
    factory = ReplayFactory(raw_path=a_recording(tmp_path), params=PARAMS,
                            output_path=tmp_path / "out.raw")

    class Broken:
        def read(self):
            raise RuntimeError("no reading")

    factory.attach_clock(Broken())
    assert factory._reading() is None
    plan, pattern = a_pulse()
    factory.log.immediate(plan, pattern, simulated=True,
                          clock_reading=factory._reading())
    assert factory.log.records[0].clock_us is None


# --------------------------------------------------------------------------
# What the file says
# --------------------------------------------------------------------------

def test_the_written_log_carries_the_times(tmp_path):
    log, _ = run_with_stimuli(tmp_path)
    path = tmp_path / "out_stimuli.json"
    log.write(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    for entry in data["stimuli"]:
        assert entry["clock_us"] is not None
        assert entry["clock_source"]


def test_a_time_without_the_conversion_factor_is_not_called_measured():
    # best_time_us prefers the driver's reported latency, which is a
    # measurement. Without cycles_per_us it cannot convert one, and must not
    # claim the estimate is a measurement.
    log = StimulusLog()
    plan, pattern = a_pulse()

    class Reading:
        acquisition_us = 1234.0
        source = "frames"

    record = log.immediate(plan, pattern, clock_reading=Reading(),
                           latency_cycles=5000)
    assert record.time_is_measured(None) is False
    assert record.best_time_us(None) == 1234.0
    # Given the factor, the driver's own number wins.
    assert record.time_is_measured(10.0) is True
    assert record.best_time_us(10.0) == 500.0
