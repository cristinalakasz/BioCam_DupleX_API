"""Layer 1 - driving the BioCAM stimulator.

Nothing here can be executed without the instrument. Every .NET member is
verified against `API/3Brain.BioCamDriver.xml` and, for the `_3Brain.Common`
types it has no XML for, against the assembly itself via
`python -m biocam.interop.reflect`. Where a construct is a **pythonnet** idiom
rather than a .NET member - building an array, selecting an overload - it is
verified by running it on the development machine, which needs the DLLs but no
BioCAM. Those are marked individually below.

One exception, stated so the paragraph above stays literally true. All three
`Send` overload *keys* resolve against the real `IBioCamStim` method table -
`python -m biocam.interop.verify_stim_model` checks that on every run. What
has not been executed is the *call*: an overload selected via
`Overloads[..., UInt64&]`, invoked with the out argument omitted, returning a
two-tuple. That needs a live stimulator. Failure would be a loud `TypeError`
at the call site, not a wrong stimulus.

## The lifecycle is Initialize -> Start -> Stop -> Close, in TWO brackets

Those four calls do not nest inside one another. 3Brain's sample brackets
them against different things:

    Initialize  <-> device control   (MainForm.cs:111 / :122)
    Start/Stop  <-> data streaming   (MainForm.cs:186,192 / :210,213)

So `__enter__`/`__exit__` here do Initialize and Close, and `stimulating()`
does Start and Stop:

    with BioCamDevice() as device, Stimulator(device) as stim:
        ...StartDataStreaming...
        with stim.stimulating():
            stim.send_now(plan, pattern)
        ...StopDataStreaming...

Folding all four into one context manager - which this used to do - made the
sample's ordering impossible to express: the stimulator was necessarily
started before any acquisition existed, on every session.

`connector.py` calls `Initialize()` (line 185) and `Close()` (line 214) and
never `Start()`. What that actually causes is *not* silent failure, contrary to
what this module previously claimed: the XML documents every `Send` overload as
throwing `InvalidOperationException` "when the stimulator has not started"
(BioCamStimBase.Send). connector.py never calls `Send` at all, so the defect
there is an incomplete lifecycle rather than an observed silent failure. Issue
#22 exists to establish what the DupleX really does.

Each of the four steps returns a bool *and* documents exceptions, so both are
handled. In particular:

    Initialize  throws InvalidOperationException if already initialized
    Start       throws InvalidOperationException if already started
    Stop        throws InvalidOperationException if not started
    Close       throws InvalidOperationException if not initialized
    Reset       throws InvalidOperationException if not started
    Send        throws InvalidOperationException if not started,
                ArgumentNullException on a null pulse,
                ArgumentException on invalid endpoints or timestamps

`connector.py` already calls `Initialize()` during `connect()`, so a session
that uses both it and this class would hit the "already initialized" throw.
`__enter__` checks `IsInitialized` and `IsStimulating` first for that reason.

## Ordering against acquisition matters

3Brain's own sample starts the stimulator *after* data streaming and stops it
*before* (`MainForm.cs:186,192` then `:210,213`). The latency the `out UInt64`
overload reports is in clock cycles "relative to the beginning of the
acquisition", and scheduled timestamps are microseconds from the same origin.
Started before an acquisition exists, neither has a reference point.

That makes streaming **required for `send_scheduled`** - a timestamp measured
from an acquisition that does not exist is meaningless - and merely
**advisable for `send_now`**, which still delivers a stimulus; only the
returned latency loses its meaning. Whether the driver itself requires the
ordering is not documented either way, so `__enter__` warns rather than
refuses. Making it refuse was tried and was wrong: it left `biocam stim`
unable to run at all, since nothing on that path starts an acquisition.

The pulse arithmetic lives in `biocam.stim`, which is testable. This module
only translates a validated plan into .NET objects and calls the driver, so
that the untestable surface stays as thin as it can be.
"""

from contextlib import contextmanager

from biocam.stim.electrodes import ElectrodeGrid, StimPattern, validate_pattern
from biocam.stim.pulse import PulsePlan, verify_built_pulse
from biocam.stim.train import MAX_TRAIN_PULSES

# Only StimProtocolType.RealTime is supported here. XML (T:StimProtocolType)
# says of Static: "All stimulation arguments like the pulse shape, end points
# and timestamps, must be first loaded before the stimulation starts" - the
# opposite of the order this class enforces, which is Start() first and Send()
# afterwards. Supporting Static means a different class, not a flag. The enum
# itself is imported where it is used; naming it here as a bare constant would
# only invite passing a string to Initialize().


class StimulatorError(RuntimeError):
    """The stimulator refused an operation, or was used out of order."""


class Stimulator:
    """Claims the stimulator for the duration of a with-block.

    Two brackets, not one - see the module docstring:

        with BioCamDevice() as device, Stimulator(device) as stim:
            ...start data streaming...
            with stim.stimulating():
                stim.send_now(pulse_plan, pattern)
            ...stop data streaming...

    `__enter__`/`__exit__` run `Initialize`/`Close` and bracket the device.
    `stimulating()` runs `Start`/`Stop` and brackets the acquisition. Sending
    outside a `stimulating()` block raises rather than silently doing nothing.
    """

    def __init__(
        self,
        device,
        *,
        grid: ElectrodeGrid = None,
        enforce_column_rule: bool = True,
        warn=None,
        log=None,
        clock=None,
    ):
        self._device = device
        self._grid = grid
        self._enforce_column_rule = enforce_column_rule
        self._warn = warn or (lambda message: None)
        # A StimulusLog and an AcquisitionClock, both optional and both pure
        # Layer 2. Given them, every attempt is recorded with the acquisition
        # time it was made at - which is the correspondence every later
        # analysis depends on, and which cannot be reconstructed afterwards.
        self._log = log
        self._clock = clock
        # Pulses handed to the device's internal memory and not known to
        # have executed. A high-water mark - see _check_schedule.
        self._queued_pulses = 0
        self._stimulator = None
        self._initialized = False
        self._started = False
        self._constraints = None
        self._send_immediate = None
        self._send_scheduled = None
        self._maybe_started = False

    # -- lifecycle -------------------------------------------------------

    def _safe_warn(self, message):
        """warn() that cannot itself raise. For teardown paths only."""
        try:
            self._warn(message)
        except BaseException:  # noqa: BLE001 - nothing left to report it to
            pass

    @staticmethod
    def _read(what: str, read):
        """Read one driver property, turning a .NET failure into ours.

        These assemblies are obfuscated: an escaping exception carries
        private-use codepoints in place of method names, and printing one to a
        cp1252 console raises UnicodeEncodeError - so the real error is
        replaced by an encoding error about the real error. Wrapping keeps the
        member name that failed in the message and keeps the type something
        callers can catch.
        """
        try:
            return read()
        except BaseException as exc:
            raise StimulatorError(
                f"reading {what} raised {exc!r}. That is a driver-level "
                "failure before any stimulation was attempted; the BioCAM may "
                "have been disconnected or claimed by another process."
            ) from exc

    def __enter__(self):
        if getattr(self._device, "biocam", None) is None:
            # Without this the line below raises a bare "'NoneType' object has
            # no attribute 'Stimulator'". device.py guards data_format the
            # same way and for the same reason.
            raise StimulatorError(
                "device.biocam is None: the BioCamDevice was never "
                "successfully entered, or has already exited. Use "
                "`with BioCamDevice() as device:` around this block."
            )

        # Every read below goes through _read(). These are plain property
        # accesses, but they are property accesses *on the driver*, and an
        # unwrapped .NET exception from one arrives as an obfuscated
        # _3Brain exception whose text carries private-use codepoints - which
        # a cp1252 console then fails to print, replacing the real error with
        # a UnicodeEncodeError. Initialize and Start were already wrapped; the
        # asymmetry was the bug.
        # No streaming check here. __enter__ is claim-time Initialize, and
        # the sample Initializes before streaming too - MainForm.cs:111, inside
        # TakeBioCamControl, long before :186. Warning here would have been
        # wrong on its own terms and would have fired on every correctly
        # ordered session, drowning out start()'s copy, which is the one that
        # means the ordering has regressed.
        stimulator = self._read("IBioCam.Stimulator",
                                lambda: self._device.biocam.Stimulator)
        if stimulator is None:
            raise StimulatorError(
                "biocam.Stimulator is None. The BioCAM firmware reports no "
                "stimulator module. BioCamSlotInfo.HasBioCamStimulator() "
                "reports whether the instrument has one at all."
            )

        if not self._read("IBioCamStim.IsAvailable()", stimulator.IsAvailable):
            raise StimulatorError(
                "IBioCamStim.IsAvailable() returned false: the stimulator "
                "module is not available. It may not be installed, or another "
                "application may hold it."
            )

        # XML: Initialize throws InvalidOperationException when the stimulator
        # is already initialized. connector.py:185 calls Initialize() during
        # connect(), so this is reachable in an ordinary session rather than
        # only in a misuse.
        if self._read("IBioCamStim.IsInitialized",
                      lambda: stimulator.IsInitialized):
            raise StimulatorError(
                "the stimulator is already initialized. Initialize() would "
                "throw InvalidOperationException. Something else in this "
                "process already holds it - connector.py calls Initialize() "
                "during connect(); use one or the other, not both."
            )
        if self._read("IBioCamStim.IsStimulating",
                      lambda: stimulator.IsStimulating):
            raise StimulatorError(
                "the stimulator reports it is already stimulating, so Start() "
                "would probably throw InvalidOperationException - the XML "
                "defines IsStimulating as 'whether the stimulator is running' "
                "and says Start throws when 'already started', which are very "
                "likely the same condition but are not documented as such."
            )

        # Only now, with every state check passed, does this instance take
        # ownership. Set earlier, a failure above would leave the object
        # holding a live handle it never claimed, with __exit__ never running
        # because __enter__ did not return - and `is_stimulating` would go on
        # querying a stimulator this instance does not own.
        self._stimulator = stimulator

        from _3Brain.BioCamDriver import StimProtocolType

        # The real enum rather than a bare int: whether pythonnet coerces an
        # int to a .NET enum parameter is a pythonnet question this repository
        # has no reason to depend on. Passed explicitly for the same reason -
        # not relying on the C# default either (issue #17).
        protocol_type = StimProtocolType.RealTime

        # Both failure paths call _shutdown() before raising. Neither Stop nor
        # Close fires (both flags are still False), so it is a no-op apart
        # from releasing self._stimulator - which is the point: __enter__ is
        # about to raise, so __exit__ will never run, and the instance must
        # not be left holding a handle it does not own.
        try:
            initialized = stimulator.Initialize(protocol_type)
        except BaseException as exc:
            self._shutdown()
            raise StimulatorError(
                f"IBioCamStim.Initialize({protocol_type}) raised {exc!r}. The "
                "XML documents InvalidOperationException when already "
                "initialized and ArgumentException when the protocol type is "
                "not supported. Nothing has been started."
            ) from exc
        if not initialized:
            self._shutdown()
            raise StimulatorError(
                f"IBioCamStim.Initialize({protocol_type}) returned false. The "
                "stimulator did not initialize; nothing has been started."
            )
        self._initialized = True
        return self

    # -- the streaming bracket -------------------------------------------

    def start(self) -> None:
        """Start the stimulator. Call this AFTER data streaming has begun.

        `Start`/`Stop` bracket the acquisition, not the device. 3Brain's
        sample is explicit about it: `Initialize` sits inside
        TakeBioCamControl (MainForm.cs:111) and `Close` inside
        ReleaseBioCamControl (:122), while `Start` comes *after*
        StartDataStreaming (:186 then :192) and `Stop` *before*
        StopDataStreaming (:210 then :213).

        This used to be folded into `__enter__` alongside Initialize, which
        made the sample's ordering structurally impossible to express - the
        stimulator was necessarily started before any acquisition existed, on
        every session. Since the latency this reports is measured in clock
        cycles "relative to the beginning of the acquisition", that origin may
        not have existed yet.
        """
        if self._stimulator is None or not self._initialized:
            raise StimulatorError(
                "cannot start: the stimulator is not initialized. Use "
                "`with Stimulator(device) as stim:` first."
            )
        if self._started:
            return

        if not self._read("IBioCam.IsStreaming",
                          lambda: self._device.biocam.IsStreaming):
            # Still a warning rather than a refusal: nothing documents
            # streaming as a precondition of Start, and `biocam stim` uses
            # this deliberately for bench work with no recording. But in the
            # ordinary path it should now never fire, which is the point of
            # the split - if it does fire during a UI session, the ordering
            # has regressed.
            self._warn(
                "starting the stimulator with no acquisition running. "
                "3Brain's sample starts it after StartDataStreaming "
                "(MainForm.cs:186,192). Pulses should still be delivered - "
                "that is itself untested (issue #22) - but the latency "
                "send_now reports is measured from the beginning of the "
                "acquisition and has no reference point."
            )

        # The step connector.py omits.
        #
        # _maybe_started mirrors DriverPacketSource._maybe_streaming: if
        # Start() engages the stimulator and THEN raises - undocumented either
        # way - _started would stay False, so neither stop() nor _shutdown()
        # would ever call Stop(), and _shutdown would Close() something that
        # may still be running. Set before the call, cleared only on an
        # explicit False, which is the one case that says nothing engaged.
        self._maybe_started = True
        try:
            started = self._stimulator.Start()
        except BaseException as exc:
            raise StimulatorError(
                f"IBioCamStim.Start() raised {exc!r}. The XML documents "
                "InvalidOperationException when the stimulator has already "
                "started or the protocol type is not supported."
            ) from exc
        if not started:
            self._maybe_started = False
            raise StimulatorError(
                "IBioCamStim.Start() returned false. The stimulator "
                "initialized but did not start, so every subsequent Send "
                "would throw InvalidOperationException."
            )
        self._started = True
        # A fresh Start clears the stimulator's internal buffers, so the
        # queued count starts again with it.
        self._queued_pulses = 0

    def stop(self) -> None:
        """Stop the stimulator. Call this BEFORE data streaming stops.

        Never raises - including from `warn`, which callers on teardown
        paths rely on. `LiveFactory.stop_source_safely` calls this unguarded
        on the strength of that promise, so the guarantee belongs here rather
        than in whatever callable a caller happened to pass.
        """
        if self._stimulator is None or not (self._started or self._maybe_started):
            return
        try:
            if not self._stimulator.Stop():
                self._safe_warn("IBioCamStim.Stop() returned false.")
        except BaseException as exc:  # noqa: BLE001 - teardown must not raise
            self._safe_warn(f"IBioCamStim.Stop() raised {exc!r}.")
        finally:
            self._started = False
            self._maybe_started = False

    @contextmanager
    def stimulating(self):
        """Bracket the acquisition: `Start` on entry, `Stop` on exit.

            with Stimulator(device) as stim:      # Initialize / Close
                ...StartDataStreaming...
                with stim.stimulating():          # Start / Stop
                    ...
                ...StopDataStreaming...
        """
        self.start()
        try:
            yield self
        finally:
            self.stop()

    def __exit__(self, exc_type, exc, tb):
        problems = self._shutdown()
        # Raising here would replace whatever exception brought us into
        # __exit__, so shutdown problems are reported only when nothing else
        # is already propagating.
        if problems and exc_type is None:
            raise StimulatorError(
                "the stimulator did not shut down cleanly: "
                + "; ".join(problems)
                + ". It may still be holding the instrument; reconnecting the "
                "BioCAM may be needed."
            )
        return False

    def _shutdown(self):
        """Stop and Close whatever was opened. Returns problems, never raises.

        Used both by `__exit__` and by `__enter__`'s failure paths. It must
        not raise in the latter case: doing so would replace the real reason
        the stimulator would not start with a message about shutting down.
        """
        stimulator, self._stimulator = self._stimulator, None
        # Everything derived from that stimulator goes with it. The bound Send
        # overloads are held against this particular object, and the cached
        # constraints came from its Properties; carrying either into a second
        # `with` block would silently use the previous session's bindings and
        # limits.
        self._send_immediate = None
        self._send_scheduled = None
        self._constraints = None
        if stimulator is None:
            return []
        problems = []
        # XML: Stop throws when the stimulator has not started, and Close
        # throws when it is not initialized - so both are guarded by the flags
        # rather than called unconditionally.
        # BaseException, not Exception, and deliberately: a KeyboardInterrupt
        # arriving between Stop() and Close() would otherwise leave the
        # stimulator open. Ctrl+C during teardown is therefore recorded as a
        # problem rather than propagating - the same trade biocam/interop/
        # source.py makes in its own stop path, and for the same reason.
        #
        # Stop() normally happened already, in the streaming bracket. This is
        # the safety net for a caller that never opened one, or that raised
        # inside it - XML: Close throws when the stimulator has not been
        # closed cleanly, and Stop throws when it has not started, so both
        # stay behind their flags.
        try:
            if (self._started or self._maybe_started) and not stimulator.Stop():
                problems.append("IBioCamStim.Stop() returned false")
        except BaseException as stop_exc:  # noqa: BLE001 - collected, not raised
            problems.append(f"IBioCamStim.Stop() raised {stop_exc!r}")
        finally:
            self._started = False
            self._maybe_started = False

        try:
            if self._initialized and not stimulator.Close():
                problems.append("IBioCamStim.Close() returned false")
        except BaseException as close_exc:  # noqa: BLE001 - collected, not raised
            problems.append(f"IBioCamStim.Close() raised {close_exc!r}")
        finally:
            self._initialized = False
        return problems

    # -- properties ------------------------------------------------------

    @property
    def constraints(self):
        """The device's own `StimConstraints`, read once and cached.

        This is the object every plan must be built against. Do **not**
        substitute `StimProperties.Default`: it is a placeholder whose time
        resolution is 1 us where the instrument's is coarser, and pulses built
        against it are wrong by that ratio without anything reporting it.
        """
        if self._constraints is None:
            # Only initialization is required, not Start(). The XML documents
            # no precondition on IBioCamStim.Properties, and reading the
            # limits in order to plan against them is a reasonable thing to do
            # before anything has been sent.
            if self._stimulator is None or not self._initialized:
                raise StimulatorError(
                    "cannot read the stimulator's constraints: it is not "
                    "initialized. Use `with Stimulator(device) as stim:`."
                )
            from biocam.stim.constraints import StimConstraints

            self._constraints = StimConstraints.from_stim_properties(
                self._stimulator.Properties
            )
        return self._constraints

    @property
    def cycles_per_us(self):
        """The instrument's clock cycles per microsecond, or None.

        Delegates to `biocam.interop.device.cycles_per_us_of`: the member is
        `IBioCam.ClockCyclesToMilliseconds`, on the device rather than the
        stimulator, so a recording-only session can read it too.
        """
        from biocam.interop.device import cycles_per_us_of

        return cycles_per_us_of(self._device)

    @property
    def is_stimulating(self) -> bool:
        if self._stimulator is None:
            return False
        return bool(self._stimulator.IsStimulating)

    # -- sending ---------------------------------------------------------

    def send_now(self, pulse_plan: PulsePlan, pattern: StimPattern) -> int:
        """Deliver one pulse immediately, returning the reported latency.

        The latency is in **clock cycles**, not microseconds - the XML says it
        is "expressed in clock cycles relative to the beginning of the
        acquisition" and that it "accounts for the time to program all
        endpoints". Convert with `IBioCam.ClockCyclesToMilliseconds(UInt64)`,
        which is what the sample uses (MainForm.cs:272); do not divide by a
        guessed clock rate.
        """
        with self._logging("immediate", pulse_plan, pattern) as entry:
            pulse, positive, negative = self._prepare(pulse_plan, pattern)
            try:
                ok, latency = self._send_immediate(pulse, positive, negative)
            except BaseException as exc:
                raise StimulatorError(
                    f"IBioCamStim.Send raised {exc!r}. The XML documents "
                    "InvalidOperationException when the stimulator has not "
                    "started, ArgumentNullException for a null pulse, and "
                    "ArgumentException for invalid endpoints."
                ) from exc
            # `not ok`, not `ok is not True` - deliberately different from
            # _send_timestamps. That one needs the identity check because it
            # does not unpack, so a by-ref binding would hand it a truthy
            # tuple. Here the unpack above has already proven the shape, so an
            # identity comparison would only add a way to raise AFTER the
            # pulse reached the culture, if pythonnet ever marshals
            # System.Boolean to something other than the True singleton.
            if not ok:
                entry["rejected_by_driver"] = True
                raise StimulatorError(
                    f"IBioCamStim.Send returned {ok!r}: the stimulation values "
                    "were not accepted. Per the XML, an internal buffer "
                    "overflow makes the NEXT call ignore its values, so check "
                    "the endpoint and pulse counts before retrying."
                )
            entry["latency_cycles"] = int(latency)
            entry["delivered"] = True
        return int(latency)

    def send_scheduled(self, plan, pattern: StimPattern) -> None:
        """Queue a train or sequence for the instrument to execute.

        `plan` is a `TrainPlan` or `SequencePlan` from `biocam.stim`.

        Its timestamps are microseconds **from the beginning of the
        acquisition**, not from now. A plan built with `delay_us=0` and sent
        ten minutes into a recording has every timestamp in the past; shift it
        with `plan.shifted_by(current_acquisition_time_us)` first. What the
        instrument does with past timestamps is untested - issue #24.

        A `SequencePlan` whose pulses differ cannot go through this overload:
        `Send` takes exactly one pulse configuration. Load it as a dynamic
        `IStimProtocol` through `IBioCamStimProtocolManager` instead, which
        this module does not yet wrap.
        """
        # Every refusal below happens INSIDE the logging block. They used to
        # raise before it, so a scheduled train refused for any of these
        # reasons left no trace at all - while send_now, whose validation sits
        # inside _prepare, logged every one of its refusals. A stimulus that
        # was supposed to fire and did not is exactly what the log exists to
        # record: in the signal, a hole in a train is indistinguishable from a
        # stimulus that evoked nothing.
        with self._logging("scheduled", plan, pattern) as entry:
            pulse_plan, timestamps = self._check_schedule(plan)
            self._send_timestamps(pulse_plan, pattern, timestamps, entry)

    def _check_schedule(self, plan):
        """Validate a scheduled plan and return its pulse and timestamps."""
        pulse_plans = getattr(plan, "pulse_plans", None)
        if pulse_plans is not None:
            distinct = {p.constructor_args() for p in pulse_plans}
            if len(distinct) > 1:
                raise StimulatorError(
                    f"this sequence has {len(distinct)} distinct pulse "
                    "configurations, and Send(pulse, positive, negative, "
                    "timestamps) carries only one. A varying-pulse sequence "
                    "is a dynamic IStimProtocol and must be loaded through "
                    "IBioCamStimProtocolManager, which is not wrapped yet."
                )
            pulse_plan = pulse_plans[0]
        else:
            pulse_plan = plan.pulse_plan

        timestamps = [float(t) for t in plan.timestamps_us]
        if not timestamps:
            raise StimulatorError("the plan has no timestamps; nothing to send")
        # The planner enforces this, but send_scheduled accepts any object
        # exposing `timestamps_us` - including one built by hand or derived
        # with dataclasses.replace - so it is enforced here too rather than
        # assumed. The XML says 1024; biocam.stim enforces 1000, the
        # intersection of the three sources. See biocam/stim/train.py.
        if len(timestamps) > MAX_TRAIN_PULSES:
            raise StimulatorError(
                f"{len(timestamps)} timestamps exceeds the {MAX_TRAIN_PULSES} "
                "this repository enforces. The XML for Send says 1024, while "
                "MaxCount and the API introduction PDF both say 1000; 1000 is "
                "the intersection."
            )
        # And cumulatively, because the limit is the stimulator's internal
        # memory, not a per-call argument check. Two legal 600-pulse sends are
        # jointly over the documented depth, and the XML is explicit about what
        # then happens (XML:4954): "Any time that one of these memory buffers
        # overflows will cause the next invocation of the method to not
        # consider the new argument values." The call that overflows appears
        # to succeed and the NEXT one silently does nothing - the worst
        # failure mode available, since nothing reports it.
        #
        # This count is a high-water mark, not a live reading: nothing in the
        # API tells us when the device has finished executing queued pulses,
        # so it is only cleared by Reset() or a fresh Start(). It will
        # therefore refuse conservatively over a long session. Refusing a
        # stimulus that would have fit is recoverable; silently dropping the
        # one after an overflow is not.
        if self._queued_pulses + len(timestamps) > MAX_TRAIN_PULSES:
            raise StimulatorError(
                f"{self._queued_pulses} pulse(s) are already queued on the "
                f"stimulator and this plan adds {len(timestamps)}, which is "
                f"past the {MAX_TRAIN_PULSES} its internal memory holds. The "
                "API documents that an overflow makes the NEXT Send silently "
                "ignore its arguments, so this is refused rather than risked. "
                "Call reset() to clear the queue once the train has run - and "
                "note that this count cannot decrease on its own, because "
                "nothing in the API reports when queued pulses have "
                "executed (issue #24)."
            )
        if any(b <= a for a, b in zip(timestamps, timestamps[1:])):
            raise StimulatorError(
                "timestamps must be strictly increasing; the XML calls for an "
                "'ordered array' and documents ArgumentException for invalid "
                "time-stamps."
            )

        if not self._read("IBioCam.IsStreaming",
                          lambda: self._device.biocam.IsStreaming):
            raise StimulatorError(
                "cannot send a scheduled train: the BioCAM is not streaming. "
                "The XML says these timestamps are microseconds 'relative to "
                "the beginning of the acquisition', so with no acquisition "
                "running there is nothing for them to be relative to. What "
                "the instrument would actually do with them is untested "
                "(issue #24) - this refuses rather than finding out on a "
                "culture. Start data streaming first, then shift the plan by "
                "the current acquisition time."
            )

        # The clock was already being read for the log; this is the check it
        # was collected for. A train whose timestamps have all passed is the
        # issue-#24 case, and refusing beats discovering on a culture what the
        # instrument does with it.
        reading = self._read_clock()
        # `timestamps[0]`, not `[-1]`. Testing the last one only refuses a
        # train that has ENTIRELY passed, and accepts one whose first forty of
        # fifty pulses are already in the past - which is the issue-#24
        # undefined case for those forty, and silently changes the delivered
        # protocol. A hole in a train looks exactly like a stimulus that
        # evoked nothing, which is the argument this file makes elsewhere for
        # logging refusals at all.
        if reading is not None and timestamps[0] <= reading.acquisition_us:
            raise StimulatorError(
                f"this plan starts in the past: its first timestamp is "
                f"{timestamps[0]:.0f} us and the acquisition is already at "
                f"{reading.acquisition_us:.0f} us ({reading.source}). "
                "Timestamps are measured from the beginning of the "
                "acquisition, not from now - shift the plan with "
                "TrainPlan.shifted_by(), or use "
                "biocam.data.clock.schedule_after(plan, clock, lead_us). What "
                "the instrument does with past timestamps is untested "
                "(issue #24)."
            )
        return pulse_plan, timestamps

    def _send_timestamps(self, pulse_plan, pattern, timestamps, entry):
        pulse, positive, negative = self._prepare(pulse_plan, pattern)

        import System

        array = System.Array[System.Double](timestamps)
        try:
            ok = self._send_scheduled(pulse, positive, negative, array)
        except BaseException as exc:
            raise StimulatorError(
                f"IBioCamStim.Send raised {exc!r} for {len(timestamps)} "
                "timestamps. The XML documents ArgumentException for invalid "
                "endpoints or time-stamps, and InvalidOperationException when "
                "the stimulator has not started."
            ) from exc
        # `is not True`, not `not ok`. If this binding ever resolved to a
        # by-ref overload the return would be a tuple, and any non-empty tuple
        # is truthy - a rejected send would then report success. send_now is
        # self-checking because it unpacks; this one has to say so.
        if ok is not True:
            entry["rejected_by_driver"] = True
            raise StimulatorError(
                f"IBioCamStim.Send did not return True for {len(timestamps)} "
                f"timestamps (returned {ok!r}): the values were not accepted. "
                "The XML warns that a buffer overflow makes the NEXT call "
                "ignore its values."
            )
        entry["delivered"] = True
        # Counted only after the driver accepted them, so a refused send does
        # not consume budget that was never taken.
        self._queued_pulses += len(timestamps)

    # -- internals -------------------------------------------------------

    @contextmanager
    def _logging(self, kind, plan, pattern):
        """Record one attempt into the log, whatever happens to it.

        A refused stimulus belongs in the record as much as a delivered one:
        a hole in a stimulus train looks, in the recorded signal, exactly like
        a stimulus that evoked nothing. So the failure path writes an entry
        and re-raises rather than letting the attempt vanish.

        The clock is read *before* sending, so the recorded time is a lower
        bound on delivery rather than an upper one. Where the driver reports a
        latency, that supersedes it - see StimulusRecord.best_time_us.
        """
        entry = {"delivered": False, "rejected_by_driver": False,
                 "latency_cycles": None}
        reading = self._read_clock()
        try:
            yield entry
        except BaseException as exc:
            self._record(
                lambda: self._log.failure(
                    kind, exc, plan=plan, pattern=pattern,
                    clock_reading=reading,
                    rejected_by_driver=entry["rejected_by_driver"],
                )
            )
            raise
        if kind == "immediate":
            self._record(
                lambda: self._log.immediate(
                    plan, pattern, clock_reading=reading,
                    latency_cycles=entry["latency_cycles"],
                )
            )
        else:
            self._record(
                lambda: self._log.scheduled(
                    plan, pattern, clock_reading=reading)
            )

    def _record(self, write):
        """Write one log entry, never at the expense of the stimulus.

        Guarded for two distinct reasons, both of which turn a bookkeeping
        problem into a clinical one if left unguarded:

        On the success path the pulse has **already been delivered** by the
        time this runs. An exception here would make `send_now` raise after
        the current reached the culture, and the CLI would print
        "stimulator error" for a stimulus that fired.

        On the failure path an exception here replaces the original
        `StimulatorError` - contextlib re-raises whatever comes out - so the
        colleague would be told about a logging fault instead of the reason
        the stimulus was refused.

        A log that cannot be written is reported and the run continues, the
        same treatment `RecordingWriter._emit` gives a listener that raises.
        """
        if self._log is None:
            return
        try:
            write()
        except Exception as exc:  # noqa: BLE001 - the stimulus outranks its record
            self._warn(
                f"could not record a stimulus in the log ({exc!r}). The "
                "stimulus itself was unaffected, but this session's log is "
                "now incomplete - do not treat it as a full record of what "
                "was delivered."
            )

    def attach_clock(self, clock) -> None:
        """Give this stimulator the acquisition clock for the current session.

        The clock cannot be supplied to `__init__` by the window, and that is
        not an oversight in the caller: the instrument is claimed once and
        held for the window's lifetime, while a clock belongs to one
        recording. So the two are joined here, when a session starts.

        Until this was called, `_read_clock` returned None for every stimulus
        and **every log entry was written with `clock_us: null`** - the log
        recorded what was stimulated and through which electrodes, but not
        when, which is the one correspondence that cannot be reconstructed
        afterwards. The feature existed and no caller reached it.

        Pure Python: no .NET call, nothing to verify on the instrument.
        """
        self._clock = clock

    def _read_clock(self):
        """The acquisition clock's current reading, or None.

        Never raises: a clock that cannot yet say where it is must not stop a
        stimulus that is otherwise valid, and the absent reading is recorded
        as absent rather than as zero.
        """
        if self._clock is None:
            return None
        try:
            return self._clock.read()
        except Exception:  # noqa: BLE001 - an unusable clock is not fatal here
            return None

    def _resolve_overloads(self):
        """Bind the two `Send` overloads explicitly.

        `Send(pulse, pos, neg)` and `Send(pulse, pos, neg, out UInt64)` both
        look like three-argument calls from Python, so leaving the choice to
        pythonnet's overload resolution is a guess. `Overloads[...]` states
        which one is meant.

        The mechanism is verified on this machine (it needs the DLLs, not the
        instrument): `Overloads` selects correctly between same-arity
        overloads, and `clr.GetClrType(T).MakeByRefType()` produces the
        `UInt64&` key an out parameter needs.
        """
        import clr
        import System
        from _3Brain.BioCamDriver import StimEndPoint
        from _3Brain.Common import RectangularStimPulse

        pulse_type = clr.GetClrType(RectangularStimPulse)
        endpoints_type = clr.GetClrType(System.Array[StimEndPoint])
        latency_ref = clr.GetClrType(System.UInt64).MakeByRefType()
        timestamps_type = clr.GetClrType(System.Array[System.Double])

        send = self._stimulator.Send
        try:
            self._send_immediate = send.Overloads[
                pulse_type, endpoints_type, endpoints_type, latency_ref
            ]
            self._send_scheduled = send.Overloads[
                pulse_type, endpoints_type, endpoints_type, timestamps_type
            ]
        except BaseException as exc:
            raise StimulatorError(
                f"could not resolve the Send overloads explicitly ({exc!r}). "
                "Without this, Send(pulse, positive, negative) is ambiguous "
                "between the three-argument overload and the out-latency one, "
                "which take the same number of arguments from Python."
            ) from exc

    def _require_running(self, what: str) -> None:
        if self._stimulator is None or not self._started:
            raise StimulatorError(
                f"cannot {what}: the stimulator is initialized but not "
                "started. Open the streaming bracket - "
                "`with stim.stimulating():` - after data streaming has begun. "
                "Outside it, Send would throw InvalidOperationException."
            )

    def _prepare(self, pulse_plan: PulsePlan, pattern: StimPattern):
        """Validate, then build the .NET pulse and endpoint arrays."""
        self._require_running("send a stimulus")
        if self._send_immediate is None:
            self._resolve_overloads()

        validate_pattern(
            pattern,
            self._grid,
            enforce_column_rule=self._enforce_column_rule,
        )

        # The plan was validated against some StimConstraints; check they are
        # the instrument's. A plan built against StimProperties.Default is the
        # expected way to get this wrong, and it is silent. `unit` is the only
        # field excluded from the comparison - see
        # StimConstraints.matches_numerically, which explains why is_current
        # is compared despite also being a label.
        if not pulse_plan.constraints.matches_numerically(self.constraints):
            raise StimulatorError(
                "this pulse was planned against different limits than the "
                f"instrument reports.\n  planned against: "
                f"{pulse_plan.constraints}\n  instrument:      {self.constraints}\n"
                "Re-plan with `stimulator.constraints`. Sending it as-is would "
                "deliver a pulse of a different duration or amplitude than "
                "intended, without any error."
            )

        pulse = self._build_pulse(pulse_plan)
        positive = self._build_endpoints(pattern.positive)
        negative = self._build_endpoints(pattern.negative)
        return pulse, positive, negative

    def _build_pulse(self, pulse_plan: PulsePlan):
        """Construct the driver's pulse and confirm it matches the plan.

        `RectangularStimPulse` adjusts out-of-range values silently rather
        than raising, so what it returns is checked rather than assumed. See
        `biocam.stim.pulse` for the measurements.
        """
        from _3Brain.Common import RectangularStimPulse

        amplitude1, width1, inter_width, amplitude2, width2 = (
            pulse_plan.constructor_args()
        )
        built = RectangularStimPulse(
            pulse_plan.spec.name,
            self._stimulator.Properties,
            float(amplitude1),
            int(width1),
            int(inter_width),
            float(amplitude2),
            int(width2),
        )
        verify_built_pulse(pulse_plan, built)
        return built

    def _build_endpoints(self, electrodes):
        """Turn `Electrode`s into a .NET `StimEndPoint[]`.

        `System.Array[T](list)` is a pythonnet idiom, not a .NET member; it is
        verified on this machine for both `StimEndPoint[]` and `Double[]`.
        """
        import System
        from _3Brain.BioCamDriver import StimEndPoint
        from _3Brain.Common import ChCoord

        endpoints = []
        for electrode in electrodes:
            # ChCoord is 1-based; biocam.stim.electrodes enforces that, and
            # ElectrodeGrid bounds-checks it - ChCoord.IsValid does not know
            # the array size and reports (65, 65) as valid.
            endpoint = self._stimulator.GetInternalEndPoint(
                ChCoord(int(electrode.row), int(electrode.col))
            )
            if endpoint is None:
                raise StimulatorError(
                    f"GetInternalEndPoint returned nothing for electrode "
                    f"{electrode}. The coordinate is inside the array this "
                    "code was told about, so either the grid is wrong or that "
                    "electrode cannot be used for stimulation (issue #23)."
                )
            endpoints.append(endpoint)
        return System.Array[StimEndPoint](endpoints)
