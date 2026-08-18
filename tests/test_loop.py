"""The closed loop, and the limits that stand between a policy and a culture."""

import numpy as np
import pytest

from biocam.analysis.spikes import Spike, SpikeDetector
from biocam.loop import (
    ClosedLoop,
    Decision,
    EchoPolicy,
    RatePolicy,
    SafetyEnvelope,
)

RATE = 18557.720703125


def envelope(**kwargs):
    return SafetyEnvelope(RATE, **kwargs)


def ms(milliseconds):
    """Frames in a number of milliseconds."""
    return int(round(milliseconds * 1e-3 * RATE))


def spike(frame, channel=0):
    return Spike(frame=frame, channel=channel, amplitude=-100.0,
                 threshold=-50.0)


# --------------------------------------------------------------------------
# the envelope: the part a policy cannot argue with
# --------------------------------------------------------------------------

def test_the_first_stimulus_is_allowed():
    allowed, reason, which = envelope().check(0)
    assert allowed and which is None


def test_a_second_stimulus_too_soon_is_refused():
    env = envelope(min_interval_ms=20.0)
    env.record(0)
    allowed, reason, which = env.check(ms(5))
    assert not allowed
    assert which == "interval"
    assert "5.0 ms since the last stimulus" in reason


def test_a_second_stimulus_after_the_floor_is_allowed():
    env = envelope(min_interval_ms=20.0)
    env.record(0)
    assert env.check(ms(21))[0]


def test_the_sustained_rate_ceiling_holds_where_the_floor_does_not():
    # Fifty stimuli 20 ms apart is a legal second under the floor alone.
    # This is the limit that catches a burst.
    env = envelope(min_interval_ms=20.0, max_rate_hz=10.0)
    at = 0
    delivered = 0
    for _ in range(50):
        allowed, _, which = env.check(at)
        if allowed:
            env.record(at)
            delivered += 1
        at += ms(20)
    assert delivered == 10           # not 50


def test_the_rate_window_slides():
    env = envelope(min_interval_ms=1.0, max_rate_hz=3.0)
    for i in range(3):
        env.record(i * ms(2))
    assert not env.check(ms(10))[0]
    # A second and a bit later the window has emptied.
    assert env.check(int(RATE * 1.2))[0]


def test_the_charge_budget_refuses_an_accumulating_offset():
    env = envelope(min_interval_ms=1.0, max_rate_hz=1000.0,
                   max_charge_per_second_pc=1000.0)
    env.record(0, charge_pc=600.0)
    allowed, reason, which = env.check(ms(5), charge_pc=600.0)
    assert not allowed
    assert which == "charge"
    assert "pC/s" in reason


def test_a_balanced_pulse_never_touches_the_charge_budget():
    # Net zero contributes nothing, so the budget only bites on the pulses
    # whose charge actually accumulates.
    env = envelope(min_interval_ms=1.0, max_rate_hz=10_000.0,
                   max_charge_per_second_pc=1.0)
    at = 0
    for _ in range(100):
        assert env.check(at, charge_pc=0.0)[0]
        env.record(at, charge_pc=0.0)
        at += ms(1)


def test_a_session_limit_stops_the_loop_for_good():
    env = envelope(min_interval_ms=1.0, max_stimuli=2)
    env.record(0)
    env.record(ms(2))
    allowed, reason, which = env.check(ms(100))
    assert not allowed
    assert which == "session"
    assert "session limit of 2" in reason


def test_checking_does_not_consume_budget():
    # A caller that asks and then does not stimulate must not have spent
    # anything - otherwise a policy that changes its mind starves itself.
    env = envelope(min_interval_ms=20.0)
    for _ in range(10):
        assert env.check(0)[0]
    assert env.delivered == 0


def test_refusals_are_counted_by_reason():
    # "The loop delivered nothing" and "the loop wanted four hundred and was
    # stopped" look identical in a recording and mean different things.
    env = envelope(min_interval_ms=20.0, max_rate_hz=2.0)
    env.record(0)
    env.note_refusal(env.check(ms(1))[2])
    env.record(ms(30))
    env.note_refusal(env.check(ms(60))[2])
    assert env.refused_interval == 1
    assert env.refused_rate == 1
    assert env.refused == 2


def test_pressing_against_the_limits_is_reported():
    env = envelope(min_interval_ms=20.0)
    env.record(0)
    for _ in range(5):
        env.note_refusal(env.check(ms(1))[2])
    assert any("more often than the limits allow" in w for w in env.warnings())


def test_a_clean_envelope_warns_about_nothing():
    env = envelope()
    env.record(0)
    assert env.warnings() == []


def test_the_envelope_refuses_nonsense_limits():
    with pytest.raises(ValueError, match="min_interval_ms must be positive"):
        SafetyEnvelope(RATE, min_interval_ms=0.0)
    with pytest.raises(ValueError, match="max_rate_hz must be positive"):
        SafetyEnvelope(RATE, max_rate_hz=0.0)
    with pytest.raises(ValueError, match="frame_rate_hz must be positive"):
        SafetyEnvelope(0.0)


def test_the_recent_window_cannot_grow_with_the_recording():
    env = envelope(min_interval_ms=1.0, max_rate_hz=5.0)
    for i in range(10_000):
        at = i * ms(2)
        if env.check(at)[0]:
            env.record(at)
    assert len(env._recent) <= 6


# --------------------------------------------------------------------------
# policies
# --------------------------------------------------------------------------

def test_echo_fires_on_a_spike():
    assert EchoPolicy().decide([spike(100)], 200) is not None


def test_echo_does_nothing_without_a_spike():
    assert EchoPolicy().decide([], 200) is None


def test_echo_can_watch_only_some_channels():
    policy = EchoPolicy(trigger_channels=[3])
    assert policy.decide([spike(100, channel=0)], 200) is None
    assert policy.decide([spike(100, channel=3)], 200) is not None


def test_rate_stimulates_while_the_preparation_is_quiet():
    policy = RatePolicy(RATE, target_hz=5.0, window_seconds=1.0)
    assert policy.decide([], int(RATE)) is not None


def test_rate_stops_once_the_target_is_met():
    policy = RatePolicy(RATE, target_hz=5.0, window_seconds=1.0)
    spikes = [spike(int(i * RATE / 10)) for i in range(10)]
    policy.decide(spikes, int(RATE))
    assert policy.rate_hz >= 5.0
    assert policy.decide([], int(RATE)) is None


def test_rate_forgets_spikes_outside_its_window():
    policy = RatePolicy(RATE, target_hz=5.0, window_seconds=1.0)
    policy.decide([spike(i * 100) for i in range(20)], int(RATE))
    # Ten seconds later, none of those are in the window.
    policy.decide([], int(RATE * 10))
    assert policy.rate_hz == 0.0


def test_rate_refuses_a_nonsense_window():
    with pytest.raises(ValueError, match="window_seconds must be positive"):
        RatePolicy(RATE, target_hz=1.0, window_seconds=0.0)


def test_both_policies_describe_themselves():
    assert "echo" in EchoPolicy().describe()
    assert "rate" in RatePolicy(RATE, 5.0).describe()


# --------------------------------------------------------------------------
# the loop, end to end
# --------------------------------------------------------------------------

def signal(n_frames=60000, n_channels=2, seed=0):
    rng = np.random.default_rng(seed)
    return 2048 + rng.normal(0, 10.0, (n_frames, n_channels))


def plant(sig, frame, channel=0, amplitude=-150.0):
    width = int(0.001 * RATE)
    sig[frame:frame + width, channel] += amplitude * np.hanning(width)
    return sig


def a_loop(send=None, **envelope_kwargs):
    detector = SpikeDetector(2, RATE)
    return ClosedLoop(detector, EchoPolicy(), envelope(**envelope_kwargs),
                      send=send)


def drive(loop, sig, block=37):
    for start in range(0, sig.shape[0], block):
        loop.process(sig[start:start + block])
    return loop


def test_a_spike_produces_a_stimulus():
    sent = []
    sig = plant(signal(), 30000)
    loop = drive(a_loop(send=lambda t: sent.append(t.frame)), sig)
    assert loop.stimuli_sent == 1
    assert len(sent) == 1


def test_a_quiet_recording_produces_nothing():
    loop = drive(a_loop(send=lambda t: None), signal(seed=3))
    assert loop.stimuli_sent == 0


def test_a_burst_is_bounded_by_the_envelope_not_the_policy():
    """The case the envelope exists for.

    An echo policy on a bursting culture asks to fire on every spike. Nothing
    about the policy stops it; the envelope does, and says how often it had
    to.
    """
    sig = signal()
    for i in range(200):
        plant(sig, 20000 + i * int(0.003 * RATE))
    loop = drive(a_loop(send=lambda t: None, min_interval_ms=20.0,
                        max_rate_hz=10.0), sig)

    assert loop.spikes_seen > 50            # the policy had plenty to ask for
    assert loop.stimuli_sent <= 12          # a second or so of recording
    assert loop.envelope.refused > 0
    assert any("more often than the limits allow" in w
               for w in loop.warnings())


def test_budget_is_consumed_only_by_a_stimulus_that_went_out():
    def refuse(trigger):
        raise RuntimeError("the driver said no")

    sig = plant(signal(), 30000)
    loop = drive(a_loop(send=refuse), sig)
    assert loop.send_failures == 1
    assert loop.stimuli_sent == 0
    assert loop.envelope.delivered == 0     # nothing was spent


def test_a_failed_send_is_reported_but_does_not_stop_the_loop():
    def refuse(trigger):
        raise RuntimeError("no")

    sig = signal()
    for frame in (20000, 30000, 40000):
        plant(sig, frame)
    loop = drive(a_loop(send=refuse), sig)
    assert loop.send_failures >= 1
    assert not loop.suspended
    assert any("not delivered" in w for w in loop.warnings())


def test_a_broken_detector_suspends_the_loop_and_never_raises():
    # An exception on the packet loop skips the backlog drain and finalise()
    # and stamps an intact recording failed. A loop that stops working must
    # stop the loop, not the recording.
    class Exploding:
        n_channels = 2

        def detect(self, block):
            raise RuntimeError("the detector fell over")

    loop = ClosedLoop(Exploding(), EchoPolicy(), envelope(),
                      send=lambda t: None)
    decision = loop.process(np.zeros((37, 2)))
    assert isinstance(decision, Decision)
    assert not decision.stimulate
    assert loop.suspended
    assert "fell over" in loop.suspended_reason


def test_a_suspended_loop_stays_suspended_and_stays_quiet():
    class Exploding:
        n_channels = 2

        def detect(self, block):
            raise RuntimeError("no")

    loop = ClosedLoop(Exploding(), EchoPolicy(), envelope(),
                      send=lambda t: None)
    for _ in range(10):
        loop.process(np.zeros((37, 2)))
    assert loop.errors == 1              # not ten: it stopped trying
    assert loop.stimuli_sent == 0


def test_every_decision_is_timed():
    loop = drive(a_loop(send=lambda t: None), signal(n_frames=10000))
    assert loop.max_decision_us > 0
    assert loop.mean_decision_us > 0


def test_the_summary_reports_what_happened():
    sig = plant(signal(), 30000)
    loop = drive(a_loop(send=lambda t: None), sig)
    summary = loop.summary()
    assert summary["stimuli_sent"] == 1
    assert summary["spikes_seen"] >= 1
    assert "echo" in summary["policy"]
    assert summary["envelope"]["delivered"] == 1


# --------------------------------------------------------------------------
# warm-up: the ten milliseconds that would land on the first packet
# --------------------------------------------------------------------------

def test_warm_up_leaves_no_trace_of_itself():
    loop = a_loop(send=lambda t: None)
    loop.warm_up()
    assert loop.blocks == 0
    assert loop.spikes_seen == 0
    assert loop.stimuli_sent == 0
    assert loop.envelope.delivered == 0
    assert not loop.detector.ready          # the noise estimate was reset too
    assert loop.detector._frames_seen == 0


def test_warm_up_does_not_stimulate():
    sent = []
    loop = a_loop(send=lambda t: sent.append(t))
    loop.warm_up()
    assert sent == []


def test_warm_up_reports_what_it_absorbed():
    loop = a_loop(send=lambda t: None)
    assert loop.warm_up() >= 0.0


def test_a_warmed_loop_still_detects_normally():
    # The reset must not have broken anything.
    sent = []
    loop = a_loop(send=lambda t: sent.append(t.frame))
    loop.warm_up()
    drive(loop, plant(signal(), 30000))
    assert loop.stimuli_sent == 1


def test_the_detector_can_be_reset():
    detector = SpikeDetector(2, RATE)
    detector.detect(signal(n_frames=5000))
    assert detector.ready
    detector.reset()
    assert not detector.ready
    assert detector._frames_seen == 0
    assert detector.spikes_detected == 0


# --------------------------------------------------------------------------
# driven from real packets, through a real recording
# --------------------------------------------------------------------------

PACKET_PARAMS = None


def _params():
    from biocam.data.recording import AcquisitionParameters

    return AcquisitionParameters(
        frame_rate_hz=RATE, total_channels=8, ch_sample_byte_size=2,
        bit_depth=12, adc_counts_to_value=1.0, offset=0.0,
        min_digital_value=0, max_digital_value=4095,
    )


def a_packet(frames=37, n_channels=8, spike_on=None, seed=0):
    from biocam.data.replay import Packet

    rng = np.random.default_rng(seed)
    data = 2048 + rng.normal(0, 5, (frames, n_channels))
    if spike_on is not None:
        data[10:28, spike_on] -= 300
    return Packet(timestamp=0, counter=0,
                  payload=np.clip(data, 0, 4095).astype(np.uint16).tobytes())


def a_packet_loop(channels=(2, 5), send=None):
    from biocam.loop import PacketLoop

    detector = SpikeDetector(len(channels), RATE)
    loop = ClosedLoop(detector, EchoPolicy(), envelope(), send=send)
    return PacketLoop(loop, _params(), channels)


def test_only_the_watched_channels_are_decoded():
    runner = a_packet_loop(channels=(2, 5))
    assert runner.loop.detector.n_channels == 2
    assert runner.channels.tolist() == [2, 5]


def test_a_packet_loop_detects_on_a_watched_channel():
    sent = []
    runner = a_packet_loop(channels=(2, 5), send=lambda t: sent.append(t))
    for i in range(400):
        runner.observe(a_packet(seed=i))          # settle the noise estimate
    runner.observe(a_packet(spike_on=2, seed=999))
    assert runner.loop.spikes_seen >= 1


def test_a_spike_on_an_unwatched_channel_is_invisible():
    runner = a_packet_loop(channels=(2, 5))
    for i in range(400):
        runner.observe(a_packet(seed=i))
    before = runner.loop.spikes_seen
    runner.observe(a_packet(spike_on=7, seed=999))   # channel 7 is not watched
    assert runner.loop.spikes_seen == before


def test_a_malformed_packet_is_counted_and_never_raises():
    from biocam.data.replay import Packet

    runner = a_packet_loop()
    assert runner.observe(Packet(timestamp=0, counter=0, payload=b"\x01")) is None
    assert runner.decode_errors == 0        # too short is skipped, not an error
    assert not runner.loop.suspended


def test_the_channel_list_is_validated():
    from biocam.loop import PacketLoop

    detector = SpikeDetector(2, RATE)
    loop = ClosedLoop(detector, EchoPolicy(), envelope())
    with pytest.raises(ValueError, match="at least one channel"):
        PacketLoop(loop, _params(), [])
    with pytest.raises(ValueError, match="built for 2 channels"):
        PacketLoop(loop, _params(), [1, 2, 3])
    with pytest.raises(ValueError, match="outside the 8-channel array"):
        PacketLoop(loop, _params(), [0, 99])


def test_a_broken_loop_does_not_cost_the_recording(tmp_path):
    """The guard that matters: an exception on the packet loop would skip the
    backlog drain and finalise(), and stamp an intact recording failed."""
    from biocam.data.recording import RecordingWriter, read_sidecar
    from biocam.data.replay import ReplayPacketSource
    from biocam.session import record_session

    params = _params()
    n_frames = 4000
    data = np.arange(n_frames * 8, dtype=np.uint16).reshape(n_frames, 8)
    raw = tmp_path / "src.raw"
    data.tofile(raw)

    class Exploding:
        def observe(self, packet):
            raise RuntimeError("the loop fell over")

    source = ReplayPacketSource(raw, params, frames_per_packet=37)
    out, meta = tmp_path / "out.raw", tmp_path / "out_meta.json"
    with RecordingWriter(out, meta, params) as writer:
        result = record_session(source, writer, loop=Exploding())

    assert result.n_frames == n_frames
    assert result.verdict == "clean"
    assert read_sidecar(meta)["status"] == "complete"
    assert out.read_bytes() == raw.read_bytes()


def test_a_loop_runs_through_a_whole_recording(tmp_path):
    from biocam.data.recording import RecordingWriter
    from biocam.data.replay import ReplayPacketSource
    from biocam.session import record_session

    params = _params()
    n_frames = 60000
    rng = np.random.default_rng(4)
    data = 2048 + rng.normal(0, 8, (n_frames, 8))
    for frame in range(20000, 50000, 6000):
        data[frame:frame + 18, 2] -= 400
    raw = tmp_path / "src.raw"
    np.clip(data, 0, 4095).astype(np.uint16).tofile(raw)

    sent = []
    runner = a_packet_loop(channels=(2, 5), send=lambda t: sent.append(t.frame))
    runner.loop.warm_up()

    source = ReplayPacketSource(raw, params, frames_per_packet=37)
    with RecordingWriter(tmp_path / "o.raw", tmp_path / "o_meta.json",
                         params) as writer:
        result = record_session(source, writer, loop=runner)

    assert result.verdict == "clean"
    assert runner.loop.spikes_seen >= 4
    assert runner.loop.stimuli_sent >= 1
    assert len(sent) == runner.loop.stimuli_sent
