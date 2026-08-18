"""What the real-time review found, turned into tests that would catch it again.

Every finding here is Layer 2: testable on this machine, so an untested fix
would be a choice rather than a limitation. The three that mattered most were
each a version of the same thing - a bound that existed in a comment but not
in the code.
"""

import numpy as np

from biocam.analysis.sorting import SILHOUETTE_SAMPLE, _silhouette
from biocam.analysis.spikes import SpikeDetector
from biocam.loop import ClosedLoop, EchoPolicy, SafetyEnvelope

RATE = 18557.720703125


def detector(n=2, **kw):
    return SpikeDetector(n, RATE, **kw)


# --------------------------------------------------------------------------
# HIGH 1: a warm-up that stopped short of the code it exists to warm
# --------------------------------------------------------------------------

def test_warm_up_runs_long_enough_to_get_past_the_noise_estimator():
    # The first version fed 512 frames against an estimator needing 928, so
    # detect() took its not-ready early return every time and the expensive
    # half - crossings, waveform windows, the envelope - was never touched.
    d = detector()
    loop = ClosedLoop(d, EchoPolicy(), SafetyEnvelope(RATE), send=None)
    original, seen = d.detect, []

    def spy(block):
        spikes = original(block)
        seen.append(d.ready)
        return spikes

    d.detect = spy
    loop.warm_up()
    assert any(seen), (
        "warm_up never got the detector past its noise estimate, so the "
        "detection path was not warmed at all"
    )


def test_warm_up_exercises_the_envelope_including_a_refusal():
    # Delivery and refusal are different branches, and a busy culture takes
    # the refusal one far more often. Both should cost their first touch here.
    d = detector()
    env = SafetyEnvelope(RATE, min_interval_ms=20.0)
    original, checks = env.check, []

    def spy(frame, charge_pc=0.0):
        result = original(frame, charge_pc)
        checks.append(result[0])
        return result

    env.check = spy
    ClosedLoop(d, EchoPolicy(), env, send=None).warm_up()
    assert checks, "the envelope was never consulted during warm-up"
    assert True in checks, "warm-up never exercised an allowed stimulus"
    assert False in checks, "warm-up never exercised a refusal"


def test_warm_up_leaves_nothing_behind():
    d = detector()
    env = SafetyEnvelope(RATE)
    loop = ClosedLoop(d, EchoPolicy(), env, send=None)
    loop.warm_up()
    assert d._frames_seen == 0
    assert d.spikes_detected == 0
    assert d.waveforms_dropped == 0
    assert env.delivered == 0
    assert env.refused == 0
    assert loop.spikes_seen == 0
    assert loop.blocks == 0


def test_warm_up_scales_with_the_estimator_rather_than_assuming():
    # A slower frame rate needs a longer warm-up in frames. Hard-coding the
    # block count is what put the first version below the threshold.
    slow = SpikeDetector(1, 1000.0)
    fast = SpikeDetector(1, 40_000.0)
    assert fast.noise.warmup_frames > slow.noise.warmup_frames
    for d in (slow, fast):
        loop = ClosedLoop(d, EchoPolicy(),
                          SafetyEnvelope(d.frame_rate_hz), send=None)
        original, seen = d.detect, []
        d.detect = lambda b, _o=original, _s=seen, _d=d: (
            _s.append(_d.ready) or _o(b))
        loop.warm_up()
        assert any(seen), f"warm-up too short at {d.frame_rate_hz} Hz"


# --------------------------------------------------------------------------
# HIGH 3: an O(N^2) silhouette one click away from a live recording
# --------------------------------------------------------------------------

def test_the_silhouette_is_sampled_rather_than_quadratic():
    # Unsampled this allocates n * n * f * 8 bytes, which at the numbers below
    # is 121 GB. If the call returns at all, the cap is doing its job - a
    # regression exhausts memory rather than failing an assertion about an
    # implementation detail.
    n, f = 20_000, 38
    rng = np.random.default_rng(0)
    features = rng.normal(size=(n, f))
    labels = np.zeros(n, dtype=int)
    labels[n // 2:] = 1
    assert -1.0 <= _silhouette(features, labels) <= 1.0


def _full_silhouette(features, labels):
    """The uncapped computation, written out independently.

    Deliberately not the implementation under test: comparing the sampled
    answer against a re-import of the same code would confirm nothing.
    """
    total = 0.0
    for i in range(len(labels)):
        same = labels == labels[i]
        same[i] = False
        if not same.any():
            continue
        d = np.linalg.norm(features - features[i], axis=1)
        a = d[same].mean()
        b = min(d[labels == other].mean()
                for other in set(labels.tolist()) if other != labels[i])
        total += (b - a) / max(a, b)
    return total / len(labels)


def test_sampling_does_not_change_the_answer():
    # A silhouette is a mean over points, so a sample estimates it. If it did
    # not, capping would be trading a correct number for a fast wrong one -
    # which is the failure this whole score was rebuilt to avoid.
    rng = np.random.default_rng(1)
    n = SILHOUETTE_SAMPLE * 3
    features = np.concatenate([
        rng.normal(-4.0, 1.0, size=(n, 2)),
        rng.normal(+4.0, 1.0, size=(n, 2)),
    ])
    labels = np.array([0] * n + [1] * n)
    sampled = _silhouette(features, labels)
    full = _full_silhouette(features, labels)
    assert abs(sampled - full) < 0.02, (
        f"sampled {sampled:.3f} against full {full:.3f}"
    )


def test_sampling_still_tells_structure_from_noise():
    # The point of the score. A cap that blurred this would be worse than the
    # quadratic version it replaced.
    rng = np.random.default_rng(2)
    n = SILHOUETTE_SAMPLE * 4
    noise = rng.normal(size=(n, 2))
    split = (noise[:, 0] > 0).astype(int)
    apart = np.concatenate([rng.normal(-5.0, 0.5, size=(n // 2, 2)),
                            rng.normal(+5.0, 0.5, size=(n // 2, 2))])
    labels = np.array([0] * (n // 2) + [1] * (n // 2))
    assert _silhouette(apart, labels) > 0.7
    assert _silhouette(noise, split) < 0.6


# --------------------------------------------------------------------------
# MEDIUM 4: the detector bounds its own queue
# --------------------------------------------------------------------------

def test_the_ready_queue_is_bounded_without_anyone_draining_it():
    # Nothing drains this in a headless closed-loop script, which is the
    # obvious thing to write. The bound has to live in the detector.
    d = detector(1, collect_waveforms=True, max_ready=8)
    rng = np.random.default_rng(3)
    for _ in range(400):
        block = rng.normal(0.0, 5.0, (256, 1))
        block[128, 0] = -500.0
        d.detect(block)
    assert len(d._ready) <= 8
    assert d.waveforms_dropped > 0, "waveforms were dropped without counting"


def test_taking_nothing_allocates_nothing():
    d = detector(collect_waveforms=True)
    first, second = d.take_waveforms(), d.take_waveforms()
    assert first == ()
    assert first is second      # a shared empty, not a fresh list per packet


def test_a_completed_waveform_is_always_full_length():
    d = detector(1, collect_waveforms=True)
    rng = np.random.default_rng(4)
    lengths = set()
    for _ in range(200):
        block = rng.normal(0.0, 5.0, (128, 1))
        block[64, 0] = -500.0
        d.detect(block)
        for spike in d.take_waveforms():
            lengths.add(len(spike.waveform))
    assert lengths, "no waveform was ever completed"
    assert lengths == {d.waveform_length}


def test_a_retained_spike_carries_no_instance_dictionary():
    # Thousands of these are held for sorting, and every full collection walks
    # each one while holding the GIL - during which the acquisition callback
    # cannot run a bytecode.
    from biocam.analysis.spikes import Spike

    spike = Spike(0, 0, -1.0, -1.0)
    assert not hasattr(spike, "__dict__")


# --------------------------------------------------------------------------
# MEDIUM 9: frame numbers must stay findable in the raw file
# --------------------------------------------------------------------------

def test_skipping_frames_keeps_spike_numbers_aligned_with_the_recording():
    # Spike.frame promises to locate a spike in the .raw file. Frames that
    # reach the writer but not the detector break that promise silently unless
    # they are counted.
    d = detector(1)
    rng = np.random.default_rng(5)
    for _ in range(20):
        d.detect(rng.normal(0.0, 5.0, (128, 1)))
    before = d._frames_seen
    d.skip_frames(1000)
    assert d._frames_seen == before + 1000
    assert d.frames_skipped == 1000

    block = rng.normal(0.0, 5.0, (128, 1))
    block[64, 0] = -500.0
    spikes = d.detect(block)
    assert spikes, "nothing detected after the skip"
    # The spike sits in the block that followed the gap, not 1000 frames early.
    assert spikes[0].frame >= before + 1000


def test_skipping_drops_pending_waveforms_rather_than_stitching_across_a_gap():
    d = detector(1, collect_waveforms=True)
    rng = np.random.default_rng(6)
    for _ in range(20):
        d.detect(rng.normal(0.0, 5.0, (128, 1)))
    block = rng.normal(0.0, 5.0, (128, 1))
    block[126, 0] = -500.0      # crosses near the end: its window is pending
    d.detect(block)
    pending = len(d._pending)
    assert pending, "expected a spike still waiting for its post-samples"
    d.skip_frames(500)
    assert not d._pending
    assert d.waveforms_dropped >= pending


def test_a_reset_clears_the_skip_bookkeeping():
    d = detector(1)
    d.skip_frames(500)
    d.reset()
    assert d.frames_skipped == 0
    assert d._frames_seen == 0


def test_a_suspended_loop_still_counts_the_frames_going_past():
    d = detector(1)
    loop = ClosedLoop(d, EchoPolicy(), SafetyEnvelope(RATE), send=None)
    loop.suspended_reason = "suspended for this test"
    assert loop.suspended
    loop.process(np.zeros((256, 1)))
    assert d._frames_seen == 256


# --------------------------------------------------------------------------
# MEDIUM 10: counted is not the same as reported
# --------------------------------------------------------------------------

def test_dropped_waveforms_reach_the_warnings():
    d = detector(1, collect_waveforms=True, max_ready=4)
    loop = ClosedLoop(d, EchoPolicy(), SafetyEnvelope(RATE), send=None)
    rng = np.random.default_rng(7)
    for _ in range(300):
        block = rng.normal(0.0, 5.0, (256, 1))
        block[128, 0] = -500.0
        loop.process(block)
    assert d.waveforms_dropped > 0
    assert any("waveform" in w for w in loop.warnings()), (
        "waveforms were dropped and nothing said so"
    )


def test_skipped_frames_reach_the_warnings():
    d = detector(1)
    loop = ClosedLoop(d, EchoPolicy(), SafetyEnvelope(RATE), send=None)
    d.skip_frames(500)
    assert any("never analysed" in w for w in loop.warnings())


# --------------------------------------------------------------------------
# MEDIUM 8: refusal is the common case, so it must not build a string
# --------------------------------------------------------------------------

def test_refusing_returns_a_prebuilt_reason():
    from biocam.loop import REFUSAL_REASONS

    env = SafetyEnvelope(RATE, min_interval_ms=20.0)
    env.record(0)
    _, first, _ = env.check(int(RATE * 0.005))
    _, second, _ = env.check(int(RATE * 0.006))
    # The same object both times: no formatting happened on either call.
    assert first is second
    assert first is REFUSAL_REASONS["interval"]


def test_the_numbers_survive_in_the_summary():
    env = SafetyEnvelope(RATE, min_interval_ms=20.0, max_rate_hz=10.0)
    env.record(0)
    _, _, which = env.check(int(RATE * 0.005))
    env.note_refusal(which)
    assert "20 ms floor" in " ".join(env.warnings())
    assert env.summary()["refused_interval"] == 1


# --------------------------------------------------------------------------
# LOW 17: a drain must not stimulate on seconds-old data - and the frame
# accounting has to survive being called through a wrapper
# --------------------------------------------------------------------------

class _Packet:
    def __init__(self, payload):
        self.payload = payload


def a_packet_loop(n_channels=2, frames=64):
    from biocam.data.recording import AcquisitionParameters
    from biocam.loop import PacketLoop

    params = AcquisitionParameters(
        frame_rate_hz=RATE, total_channels=8, ch_sample_byte_size=2,
        bit_depth=12, adc_counts_to_value=1.0, offset=0.0,
        min_digital_value=0, max_digital_value=4095,
    )
    d = detector(n_channels)
    loop = ClosedLoop(d, EchoPolicy(), SafetyEnvelope(RATE), send=None)
    return PacketLoop(loop, params, list(range(n_channels))), params, d


def test_skipping_a_packet_advances_the_detector():
    loop, params, d = a_packet_loop()
    packet = _Packet(bytes(64 * params.bytes_per_frame))
    loop.skip(packet)
    assert d._frames_seen == 64
    assert d.frames_skipped == 64


def test_skip_survives_the_controllers_wrapper():
    # The first version of this reached in for `loop._bytes_per_frame`, which
    # the wrapper does not have. It raised, its own guard swallowed it, and it
    # silently did nothing - in the one path the window actually uses. A
    # delegated public method is why this now works; the test is why it stays
    # working.
    from biocam.ui.controller import _DrainingLoop

    inner, params, d = a_packet_loop()
    wrapped = _DrainingLoop(inner, lambda: None)
    wrapped.skip(_Packet(bytes(64 * params.bytes_per_frame)))
    assert d._frames_seen == 64, (
        "skipping through the wrapper did nothing, so spike frame numbers "
        "would drift from the recording during every drain"
    )


def test_a_decode_failure_still_counts_its_frames():
    # The writer already wrote these frames. If the detector is not told, every
    # later spike is offset from the raw file by however many were skipped -
    # silently, and with nothing in the output to say so.
    loop, params, d = a_packet_loop()

    class Exploding:
        def __getattr__(self, name):
            raise RuntimeError("decode failed")

    loop._np = Exploding()
    loop.observe(_Packet(bytes(64 * params.bytes_per_frame)))
    assert loop.decode_errors == 1
    assert d.frames_skipped == 64


# --------------------------------------------------------------------------
# MEDIUM 6: the retention count has to be right, not just bounded
# --------------------------------------------------------------------------

def test_the_retained_waveform_drop_count_is_derived_correctly():
    from collections import deque

    from biocam.ui.controller import MAX_RETAINED_WAVEFORMS

    kept = deque(maxlen=MAX_RETAINED_WAVEFORMS)
    seen = 0
    for batch in (10, MAX_RETAINED_WAVEFORMS, 5):
        kept.extend(range(batch))
        seen += batch
    assert len(kept) == MAX_RETAINED_WAVEFORMS
    assert seen - len(kept) == 15          # exactly what fell off the front
    # And the most recent survive, which is what a sorter should see.
    assert kept[-1] == 4


# --------------------------------------------------------------------------
# A loop with no `skip` at all must not cost the recording (HIGH 1, round 2)
# --------------------------------------------------------------------------

def a_replay(tmp_path, n_frames=4000):
    from biocam.data.recording import AcquisitionParameters
    from biocam.data.replay import ReplayPacketSource

    params = AcquisitionParameters(
        frame_rate_hz=1000.0, total_channels=8, ch_sample_byte_size=2,
        bit_depth=12, adc_counts_to_value=1.0, offset=0.0,
        min_digital_value=0, max_digital_value=4095,
    )
    raw = tmp_path / "src.raw"
    np.arange(n_frames * 8, dtype=np.uint16).reshape(n_frames, 8).tofile(raw)
    return ReplayPacketSource(raw, params, frames_per_packet=37), params, raw


def test_a_loop_without_skip_does_not_lose_a_drained_recording(tmp_path):
    # `loop` is duck-typed: the only contract record_session ever enforced is
    # `observe`. An AttributeError on the *lookup* of `skip` is not caught by
    # any guard inside the method it failed to find, and escaping the packet
    # loop skips the backlog drain and finalise() and stamps an intact raw
    # file "failed" - a whole recording lost to a bookkeeping call.
    from biocam.data.recording import RecordingWriter, read_sidecar
    from biocam.session import record_session

    source, params, raw = a_replay(tmp_path)

    class ObserveOnly:
        """Everything record_session historically required, and nothing more."""

        def observe(self, packet):
            return None

    out, meta = tmp_path / "out.raw", tmp_path / "out_meta.json"
    with RecordingWriter(out, meta, params) as writer:
        result = record_session(source, writer, loop=ObserveOnly(), drain=True)

    assert result.n_frames == 4000
    assert result.verdict == "clean"
    assert read_sidecar(meta)["status"] == "complete"
    assert out.read_bytes() == raw.read_bytes()


def test_the_loop_does_not_decide_on_a_drained_backlog(tmp_path):
    # Deciding over a backlog delivers stimuli triggered by data seconds old,
    # after the operator asked to stop. The envelope bounds the rate; nothing
    # bounds staleness.
    from biocam.data.recording import RecordingWriter
    from biocam.session import record_session

    source, params, _ = a_replay(tmp_path)

    class Counting:
        def __init__(self):
            self.observed = self.skipped = 0

        def observe(self, packet):
            self.observed += 1

        def skip(self, packet):
            self.skipped += 1

    loop = Counting()
    out, meta = tmp_path / "out.raw", tmp_path / "out_meta.json"
    with RecordingWriter(out, meta, params) as writer:
        record_session(source, writer, loop=loop, drain=True)

    assert loop.observed == 0, "the loop decided on backlogged packets"
    assert loop.skipped > 0, "the drained frames were never accounted for"


# --------------------------------------------------------------------------
# HIGH 2 (round 2): a warm-up that breaks the loop must say so
# --------------------------------------------------------------------------

def test_a_loop_that_breaks_during_warm_up_stays_broken():
    # Clearing the suspension meant a loop that had just proved it was broken
    # returned a plausible microsecond figure with suspended == False, and the
    # operator saw a normal recording start.
    d = detector(1)
    loop = ClosedLoop(d, EchoPolicy(), SafetyEnvelope(RATE), send=None)

    def explode(spikes, frame_now):
        raise RuntimeError("the policy fell over")

    loop.policy.decide = explode
    loop.warm_up()
    assert loop.suspended, "warm-up broke the loop and then erased the evidence"
    assert "warm-up" in loop.suspended_reason
    assert any("warm-up" in w for w in loop.warnings())


def test_a_clean_warm_up_leaves_no_suspension():
    d = detector(1)
    loop = ClosedLoop(d, EchoPolicy(), SafetyEnvelope(RATE), send=None)
    loop.warm_up()
    assert not loop.suspended
    assert loop.suspended_reason is None


def test_warm_up_refuses_a_zero_length_block():
    d = detector(1)
    loop = ClosedLoop(d, EchoPolicy(), SafetyEnvelope(RATE), send=None)
    try:
        loop.warm_up(block_frames=0)
    except ValueError:
        pass
    else:
        raise AssertionError("block_frames=0 should be refused, not divided by")


def test_warm_up_is_bounded_however_long_the_noise_estimate_is():
    from biocam.loop import MAX_WARM_UP_BLOCKS

    d = SpikeDetector(1, RATE, warmup_seconds=30.0)
    loop = ClosedLoop(d, EchoPolicy(), SafetyEnvelope(RATE), send=None)
    blocks = []
    original = loop.process
    loop.process = lambda b: (blocks.append(1), original(b))[1]
    loop.warm_up()
    assert len(blocks) <= MAX_WARM_UP_BLOCKS


def test_warm_up_tolerates_a_detector_without_a_noise_estimator():
    # warm_up should not be the thing that narrows what counts as a detector.
    class Minimal:
        n_channels = 1
        frame_rate_hz = RATE

        def detect(self, block):
            return []

        def reset(self):
            pass

    loop = ClosedLoop(Minimal(), EchoPolicy(), SafetyEnvelope(RATE), send=None)
    loop.warm_up()          # must not raise AttributeError


# --------------------------------------------------------------------------
# MEDIUM 3/4 (round 2): what a gap must and must not do
# --------------------------------------------------------------------------

def test_a_rate_is_not_diluted_by_frames_nobody_detected_on():
    # Once a loop suspends, skip_frames runs on every packet forever after. A
    # rate divided by _frames_seen decays smoothly towards zero and is shown
    # as a measurement rather than an absence - a detector that has gone deaf
    # looking exactly like a culture that has gone quiet.
    d = detector(1)
    rng = np.random.default_rng(8)
    for _ in range(80):
        block = rng.normal(0.0, 5.0, (256, 1))
        block[128, 0] = -500.0
        d.detect(block)
    before = d.rates_hz()
    assert before > 0
    d.skip_frames(d._frames_seen * 10)
    assert d.rates_hz() == before, (
        "the reported rate decayed because of frames nothing listened to"
    )
    assert d.frames_analysed == d._frames_seen - d.frames_skipped


def test_a_gap_resettles_the_filter_rather_than_ringing_across_it():
    # Left primed on the pre-gap baseline the biquad rings, and if the DC
    # baseline moved across the gap that transient is a run of threshold
    # crossings. False spikes on a closed loop are stimuli.
    d = detector(1)
    rng = np.random.default_rng(9)
    for _ in range(80):
        d.detect(rng.normal(0.0, 5.0, (256, 1)) + 1000.0)   # a high baseline
    assert d.ready
    d.skip_frames(5000)
    assert not d._warmed, "the filter kept its pre-gap state"
    # The baseline moved a long way across the gap. A filter still primed on
    # the old one would ring hard enough to cross threshold repeatedly.
    spurious = 0
    for _ in range(5):
        spurious += len(d.detect(rng.normal(0.0, 5.0, (256, 1)) - 1000.0))
    assert spurious == 0, f"{spurious} false crossings after the gap"


def test_a_gap_does_not_grant_the_noise_estimator_readiness():
    d = detector(1)
    d.skip_frames(10 ** 6)
    assert not d.ready, (
        "skipping time the estimator never observed made it claim readiness"
    )
