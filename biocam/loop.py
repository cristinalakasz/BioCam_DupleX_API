"""Layer 2 - closing the loop: detect a spike, decide, stimulate.

The three trigger modes are one action from three sources. Manual and
scheduled stimulation can route through an ordinary queue, because a human
takes ~200 ms to click and a schedule is known in advance. This one cannot:
to reach the ~1.5 ms the hardware allows, the decision has to happen on the
thread the data callback wakes, between packets - which is exactly where
3Brain's own sample calls `Send`.

That puts a loop with an opinion about stimulation on the one thread that
must never stall. Two consequences run through everything here.

**Nothing in this module may raise.** An exception on the packet loop skips
the backlog drain and `finalise()` and stamps an intact recording `failed`.
A loop that stops working must stop the loop, not the recording.

**The limits are not the policy's to negotiate.** `SafetyEnvelope` is checked
after the policy has decided, and cannot be overridden by it. A policy is a
research idea that will be changed often, frequently in a hurry, sometimes by
someone who did not write it. The envelope is what stands between a mistake
in that idea and a preparation stimulated hundreds of times a second. Keeping
the two apart is the point, not an accident of layering.

Runaway matters here in a way it does not elsewhere in this repository. An
echo policy on a bursting culture will try to fire on every spike, and a
burst is hundreds of spikes in a few hundred milliseconds. The defaults are
chosen so the failure reads "the loop refused, and said so" rather than "the
electrode was driven until it corroded".
"""

import time
from collections import deque
from dataclasses import dataclass

# Hard floor between two stimuli, whatever the policy wants. 20 ms is well
# below anything a protocol is likely to ask for and far above the chip
# reconfiguration cost the API introduction PDF documents (26 us + 8.4 us per
# row), so it constrains runaway rather than experiments.
DEFAULT_MIN_INTERVAL_MS = 20.0

# Sustained ceiling, over a sliding second. The floor bounds the instantaneous
# rate; this bounds the average, which is what a burst defeats - fifty stimuli
# 20 ms apart is a legal second under the floor alone.
DEFAULT_MAX_RATE_HZ = 10.0

# Net charge per second, in picocoulombs. A charge-balanced pulse contributes
# nothing, so this only bites on deliberately unbalanced ones - which is the
# case that needs a budget, since that charge accumulates rather than
# cancelling. Generous for research use, and still finite.
DEFAULT_MAX_CHARGE_PER_SECOND_PC = 500_000.0

# How long one decision may take on the acquisition thread before it is
# reported. Same reasoning as biocam.control's threshold, and the same caveat:
# a guess until a lab run measures it.
SLOW_DECISION_US = 400.0


@dataclass(frozen=True)
class Decision:
    """What the loop concluded about one block of samples."""

    stimulate: bool
    reason: str
    frame: int = None            # the spike that triggered it, if any
    channel: int = None
    refused_by: str = None       # which limit said no, when one did

    @property
    def refused(self) -> bool:
        return not self.stimulate and self.refused_by is not None


class SafetyEnvelope:
    """Hard limits on stimulation, enforced after the policy has decided.

    Deliberately not configurable by the policy: a policy proposes, this
    disposes. Every refusal is counted by reason, because "the loop delivered
    nothing" and "the loop wanted to deliver four hundred stimuli and was
    stopped" look identical in a recording and mean entirely different things.
    """

    def __init__(self, frame_rate_hz: float, *,
                 min_interval_ms: float = DEFAULT_MIN_INTERVAL_MS,
                 max_rate_hz: float = DEFAULT_MAX_RATE_HZ,
                 max_charge_per_second_pc: float = DEFAULT_MAX_CHARGE_PER_SECOND_PC,
                 max_stimuli: int = None):
        if frame_rate_hz <= 0:
            raise ValueError(f"frame_rate_hz must be positive, got {frame_rate_hz}")
        if min_interval_ms <= 0:
            raise ValueError(
                f"min_interval_ms must be positive, got {min_interval_ms}. A "
                "loop with no floor between stimuli is not one with a fast "
                "floor; it is one with none."
            )
        if max_rate_hz <= 0:
            raise ValueError(f"max_rate_hz must be positive, got {max_rate_hz}")

        self.frame_rate_hz = frame_rate_hz
        self.min_interval_frames = max(
            1, int(round(min_interval_ms * 1e-3 * frame_rate_hz)))
        self.max_rate_hz = max_rate_hz
        self.max_charge_per_second_pc = max_charge_per_second_pc
        self.max_stimuli = max_stimuli
        self.window_frames = int(round(frame_rate_hz))     # one second

        self._last_frame = None
        # (frame, charge) over the last second. Bounded by max_rate_hz, so a
        # few tens of entries at most; it cannot grow with the recording.
        self._recent = deque()
        self._recent_charge = 0.0

        self.delivered = 0
        self.refused_interval = 0
        self.refused_rate = 0
        self.refused_charge = 0
        self.refused_session = 0

    @property
    def refused(self) -> int:
        return (self.refused_interval + self.refused_rate
                + self.refused_charge + self.refused_session)

    @property
    def min_interval_ms(self) -> float:
        return self.min_interval_frames / self.frame_rate_hz * 1e3

    def _expire(self, at_frame: int) -> None:
        cutoff = at_frame - self.window_frames
        while self._recent and self._recent[0][0] < cutoff:
            _, charge = self._recent.popleft()
            self._recent_charge -= charge

    def check(self, at_frame: int, charge_pc: float = 0.0):
        """May a stimulus go out now? Returns (allowed, reason, which_limit).

        Records nothing: a caller that asks and then does not stimulate must
        not have consumed budget. `record()` is separate for that reason.
        """
        if self.max_stimuli is not None and self.delivered >= self.max_stimuli:
            return False, (
                f"the session limit of {self.max_stimuli} stimuli has been "
                "reached"
            ), "session"

        if (self._last_frame is not None
                and at_frame - self._last_frame < self.min_interval_frames):
            gap_ms = (at_frame - self._last_frame) / self.frame_rate_hz * 1e3
            return False, (
                f"only {gap_ms:.1f} ms since the last stimulus; the floor is "
                f"{self.min_interval_ms:.1f} ms"
            ), "interval"

        self._expire(at_frame)
        if len(self._recent) >= self.max_rate_hz:
            return False, (
                f"{len(self._recent)} stimuli already in the last second; the "
                f"ceiling is {self.max_rate_hz:g} Hz"
            ), "rate"

        if (self.max_charge_per_second_pc is not None
                and abs(self._recent_charge) + abs(charge_pc)
                > self.max_charge_per_second_pc):
            return False, (
                f"{abs(self._recent_charge):.0f} pC delivered in the last "
                f"second and this pulse adds {abs(charge_pc):.0f}; the budget "
                f"is {self.max_charge_per_second_pc:.0f} pC/s"
            ), "charge"

        return True, "within limits", None

    def record(self, at_frame: int, charge_pc: float = 0.0) -> None:
        """Note a stimulus that was actually delivered."""
        self._expire(at_frame)
        self._last_frame = at_frame
        self._recent.append((at_frame, charge_pc))
        self._recent_charge += charge_pc
        self.delivered += 1

    def note_refusal(self, which: str) -> None:
        """Count a refusal against the limit that caused it."""
        if which == "interval":
            self.refused_interval += 1
        elif which == "rate":
            self.refused_rate += 1
        elif which == "charge":
            self.refused_charge += 1
        elif which == "session":
            self.refused_session += 1

    def reset(self) -> None:
        """Forget every stimulus and every refusal. Before a run only."""
        self._last_frame = None
        self._recent.clear()
        self._recent_charge = 0.0
        self.delivered = 0
        self.refused_interval = 0
        self.refused_rate = 0
        self.refused_charge = 0
        self.refused_session = 0

    def warnings(self) -> list:
        problems = []
        if self.refused_interval or self.refused_rate:
            problems.append(
                f"the closed loop wanted to stimulate more often than the "
                f"limits allow: {self.refused_interval} refused by the "
                f"{self.min_interval_ms:.0f} ms floor, {self.refused_rate} by "
                f"the {self.max_rate_hz:g} Hz ceiling. The limits held, but a "
                "policy pressing against them all session is one that needs "
                "looking at - and those stimuli were not delivered."
            )
        if self.refused_charge:
            problems.append(
                f"{self.refused_charge} stimuli were refused by the charge "
                f"budget of {self.max_charge_per_second_pc:.0f} pC/s. That "
                "budget only bites on charge-unbalanced pulses, so net charge "
                "was accumulating fast enough to matter."
            )
        if self.refused_session:
            problems.append(
                f"{self.refused_session} stimuli were refused after the "
                f"session limit of {self.max_stimuli} was reached."
            )
        return problems

    def summary(self) -> dict:
        return {
            "delivered": self.delivered,
            "refused": self.refused,
            "refused_interval": self.refused_interval,
            "refused_rate": self.refused_rate,
            "refused_charge": self.refused_charge,
            "refused_session_limit": self.refused_session,
            "min_interval_ms": self.min_interval_ms,
            "max_rate_hz": self.max_rate_hz,
            "max_charge_per_second_pc": self.max_charge_per_second_pc,
        }


# --------------------------------------------------------------------------
# policies: what an experiment wants, as opposed to what is safe
# --------------------------------------------------------------------------

class EchoPolicy:
    """Stimulate once per detected spike, on any watched channel.

    The simplest closed loop there is, and the one most likely to press
    against the envelope: on a bursting culture it will ask to fire hundreds
    of times a second. That is not a defect of the policy - it is what the
    envelope is for.

    `trigger_channels` restricts which detector channels count. None means
    all of them.
    """

    name = "echo"

    def __init__(self, trigger_channels=None):
        self.trigger_channels = (
            None if trigger_channels is None else set(trigger_channels))

    def decide(self, spikes, frame_now):
        for spike in spikes:
            if (self.trigger_channels is None
                    or spike.channel in self.trigger_channels):
                return spike
        return None

    def reset(self) -> None:
        """No state to forget. Present so every policy can be reset."""

    def describe(self) -> str:
        where = ("any watched channel" if self.trigger_channels is None
                 else f"channels {sorted(self.trigger_channels)}")
        return f"echo: stimulate on each spike from {where}"


class RatePolicy:
    """Stimulate when the firing rate falls below a target.

    A homeostatic loop: quiet preparation, more stimulation; active
    preparation, less. The rate is measured over a sliding window, and the
    window matters - too short and it chases individual bursts, too long and
    it responds minutes after the thing it is responding to.

    Note the asymmetry this creates with the envelope: a *silent* culture
    makes this policy ask continuously, so a detector that has gone deaf -
    a dislodged electrode, a wrong threshold - looks exactly like a culture
    that needs stimulating. The envelope bounds what that costs; nothing here
    can tell the two apart, and a session whose refusal counts are high should
    be read with that in mind.
    """

    name = "rate"

    def __init__(self, frame_rate_hz: float, target_hz: float, *,
                 window_seconds: float = 5.0, trigger_channels=None):
        if target_hz < 0:
            raise ValueError(f"target_hz must not be negative, got {target_hz}")
        if window_seconds <= 0:
            raise ValueError(
                f"window_seconds must be positive, got {window_seconds}")
        self.frame_rate_hz = frame_rate_hz
        self.target_hz = target_hz
        self.window_frames = int(round(window_seconds * frame_rate_hz))
        self.trigger_channels = (
            None if trigger_channels is None else set(trigger_channels))
        self._spike_frames = deque()

    @property
    def rate_hz(self) -> float:
        if not self._spike_frames:
            return 0.0
        return len(self._spike_frames) / (self.window_frames / self.frame_rate_hz)

    def decide(self, spikes, frame_now):
        for spike in spikes:
            if (self.trigger_channels is None
                    or spike.channel in self.trigger_channels):
                self._spike_frames.append(spike.frame)
        cutoff = frame_now - self.window_frames
        while self._spike_frames and self._spike_frames[0] < cutoff:
            self._spike_frames.popleft()

        if self.rate_hz >= self.target_hz:
            return None
        # Nothing to point at: the trigger is an absence, not an event. The
        # frame is "now" so the envelope's interval floor still applies.
        return _Absence(frame=frame_now)

    def reset(self) -> None:
        self._spike_frames.clear()

    def describe(self) -> str:
        return (f"rate: stimulate while the measured rate is below "
                f"{self.target_hz:g} Hz "
                f"(window {self.window_frames / self.frame_rate_hz:g} s)")


@dataclass(frozen=True)
class _Absence:
    """Stands in for a spike when the trigger is something not happening."""

    frame: int
    channel: int = None
    amplitude: float = None


class ClosedLoop:
    """Detector, policy and envelope, driven one packet at a time.

    Fed from the acquisition thread. `process` returns a `Decision` and never
    raises: a loop that has broken must stop being a loop, not stop the
    recording.
    """

    def __init__(self, detector, policy, envelope, *, send=None,
                 charge_pc: float = 0.0, slow_decision_us: float = SLOW_DECISION_US):
        self.detector = detector
        self.policy = policy
        self.envelope = envelope
        self.send = send
        self.charge_pc = charge_pc
        self._slow_decision_us = slow_decision_us

        self.blocks = 0
        self.spikes_seen = 0
        self.stimuli_sent = 0
        self.send_failures = 0
        self.errors = 0
        self.suspended_reason = None
        self.max_decision_us = 0.0
        self.total_decision_us = 0.0
        self.slow_decisions = 0
        self.last_decision = None

    @property
    def suspended(self) -> bool:
        return self.suspended_reason is not None

    @property
    def mean_decision_us(self) -> float:
        if not self.blocks:
            return 0.0
        return self.total_decision_us / self.blocks

    def warm_up(self, block_frames: int = 64, n_blocks: int = 8) -> float:
        """Run the whole path a few times, then forget it. Returns the cost.

        The first call through this loop costs about ten milliseconds - numpy
        allocating, code paths being touched for the first time, caches cold.
        Ten milliseconds on the acquisition thread is roughly five dropped
        packets, at the start of every closed-loop recording, and it would
        have been noticed on the instrument as "we always lose a few at the
        beginning" and attributed to almost anything else.

        So it is paid here instead, before acquisition starts, and then
        undone: the detector, the policy and the envelope are all reset, so
        the synthetic blocks used to warm the code up leave nothing behind.

        Returns the worst decision time observed during the warm-up, which is
        the number that would otherwise have landed on the first real packet.
        """
        import numpy as np

        rng = np.random.default_rng(0)
        send, self.send = self.send, None      # nothing goes out during this
        try:
            for _ in range(n_blocks):
                block = rng.normal(0.0, 1.0, (block_frames, self.detector.n_channels))
                self.process(block)
        finally:
            self.send = send

        cost = self.max_decision_us
        self.detector.reset()
        self.policy.reset()
        self.envelope.reset()
        self.blocks = 0
        self.spikes_seen = 0
        self.stimuli_sent = 0
        self.send_failures = 0
        self.errors = 0
        self.suspended_reason = None
        self.max_decision_us = 0.0
        self.total_decision_us = 0.0
        self.slow_decisions = 0
        self.last_decision = None
        return cost

    def process(self, block) -> Decision:
        """One block in, one decision out. Never raises.

        The whole path - filter, detect, decide, check, send - happens here,
        on the acquisition thread, and is timed end to end. That number is the
        loop's latency budget and the only one worth arguing about.
        """
        if self.suspended:
            return Decision(False, "the loop is suspended")

        started = time.perf_counter()
        try:
            return self._process(block)
        except Exception as exc:  # noqa: BLE001 - the recording outranks the loop
            self.errors += 1
            self.suspended_reason = (
                f"the closed loop raised {exc!r} and has been disconnected. "
                "The recording is unaffected; no further stimuli will be "
                "delivered by it."
            )
            return Decision(False, self.suspended_reason)
        finally:
            elapsed_us = (time.perf_counter() - started) * 1e6
            self.blocks += 1
            self.total_decision_us += elapsed_us
            if elapsed_us > self.max_decision_us:
                self.max_decision_us = elapsed_us
            if elapsed_us > self._slow_decision_us:
                self.slow_decisions += 1

    def _process(self, block) -> Decision:
        spikes = self.detector.detect(block)
        self.spikes_seen += len(spikes)
        frame_now = self.detector._frames_seen

        trigger = self.policy.decide(spikes, frame_now)
        if trigger is None:
            decision = Decision(False, "the policy did not ask to stimulate")
            self.last_decision = decision
            return decision

        allowed, reason, which = self.envelope.check(
            trigger.frame, self.charge_pc)
        if not allowed:
            self.envelope.note_refusal(which)
            decision = Decision(
                False, reason, frame=trigger.frame,
                channel=getattr(trigger, "channel", None), refused_by=which)
            self.last_decision = decision
            return decision

        if self.send is not None:
            try:
                self.send(trigger)
            except Exception as exc:  # noqa: BLE001 - counted, never raised
                self.send_failures += 1
                decision = Decision(
                    False, f"the stimulus was not delivered: {exc}",
                    frame=trigger.frame,
                    channel=getattr(trigger, "channel", None))
                self.last_decision = decision
                return decision

        # Budget is consumed only by a stimulus that actually went out.
        self.envelope.record(trigger.frame, self.charge_pc)
        self.stimuli_sent += 1
        decision = Decision(
            True, "stimulated", frame=trigger.frame,
            channel=getattr(trigger, "channel", None))
        self.last_decision = decision
        return decision

    def warnings(self) -> list:
        problems = []
        if self.suspended:
            problems.append(self.suspended_reason)
        if self.send_failures:
            problems.append(
                f"{self.send_failures} stimuli were decided on but not "
                "delivered - the stimulus log records why each one failed."
            )
        if self.slow_decisions:
            problems.append(
                f"{self.slow_decisions} closed-loop decisions took longer "
                f"than {self._slow_decision_us:g} us on the acquisition "
                f"thread (slowest {self.max_decision_us:.0f} us). That time "
                "comes out of the packet queue's drain."
            )
        problems.extend(self.envelope.warnings())
        return problems

    def summary(self) -> dict:
        return {
            "policy": self.policy.describe(),
            "blocks": self.blocks,
            "spikes_seen": self.spikes_seen,
            "stimuli_sent": self.stimuli_sent,
            "send_failures": self.send_failures,
            "suspended": self.suspended,
            "max_decision_us": self.max_decision_us,
            "mean_decision_us": self.mean_decision_us,
            "slow_decisions": self.slow_decisions,
            "envelope": self.envelope.summary(),
        }


class PacketLoop:
    """Runs a `ClosedLoop` from raw packets, decoding only what it watches.

    The detector never sees the whole array. Decoding 4096 channels to make a
    decision about four is 3 ms of work per packet - measured in
    `biocam/analysis/spikes.py` - against a 2 ms budget on the thread that
    drains the packet queue. So the watched channels are sliced out of the
    payload and nothing else is touched.

    Like the activity display, this never raises: a loop that has broken must
    stop being a loop, not stop the recording.
    """

    def __init__(self, loop, params, channels):
        import numpy as np

        from biocam.data.frames import DTYPE_BY_BYTE_SIZE

        channels = list(channels)
        if not channels:
            raise ValueError("a closed loop needs at least one channel to watch")
        if len(channels) != loop.detector.n_channels:
            raise ValueError(
                f"the detector was built for {loop.detector.n_channels} "
                f"channels but {len(channels)} were given to watch"
            )
        total = params.total_channels
        for channel in channels:
            if not 0 <= channel < total:
                raise ValueError(
                    f"channel {channel} is outside the {total}-channel array"
                )

        self.loop = loop
        self.channels = np.asarray(channels, dtype=np.intp)
        self._dtype = DTYPE_BY_BYTE_SIZE[params.ch_sample_byte_size]
        self._total_channels = total
        self._bytes_per_frame = params.bytes_per_frame
        self._offset = float(params.offset)
        self._scale = float(params.adc_counts_to_value)
        self.decode_errors = 0

    def observe(self, packet):
        """Decode the watched channels and run one decision. Never raises."""
        try:
            import numpy as np

            frames = len(packet.payload) // self._bytes_per_frame
            if frames < 1:
                return None
            block = np.frombuffer(
                packet.payload, dtype=self._dtype,
                count=frames * self._total_channels,
            ).reshape(frames, self._total_channels)[:, self.channels]
            # Into the analogue unit, because the detector's thresholds are in
            # microvolts and a session comparing them against raw counts would
            # be comparing two different quantities that both look like
            # numbers.
            return self.loop.process(self._offset + block * self._scale)
        except Exception:  # noqa: BLE001 - the recording outranks the loop
            self.decode_errors += 1
            return None

    def warnings(self):
        problems = list(self.loop.warnings())
        if self.decode_errors:
            problems.append(
                f"the closed loop failed to decode {self.decode_errors} "
                "packet(s). The recording itself is unaffected."
            )
        return problems

    def summary(self):
        return {**self.loop.summary(),
                "watched_channels": self.channels.tolist(),
                "decode_errors": self.decode_errors}
