# Stimulation API reference

**Date:** 2026-08-17
**Status:** Recovered by reflection over the shipped assemblies. Verified
without an instrument. The *numbers* the DupleX reports still need the lab.

---

## Why this document exists

`CLAUDE.md` records `RectangularStimPulse` and `StimProperties` as
unverifiable: they live in `_3Brain.Common`, and this repository ships no
`3Brain.Common.xml`. That is true of the *documentation*. It is not true of the
*assembly*.

Loading a DLL and reading its metadata needs pythonnet and the DLLs, and **no
instrument** — no BioCAM is claimed, no pool is activated, no USB call is made.
So the types can be read here, exactly, rather than guessed.

Regenerate rather than trusting this transcription:

```
python -m biocam.interop.reflect                  # the stimulation types
python -m biocam.interop.reflect --all-stim       # all 41 matching "stim"
python -m biocam.interop.reflect RectangularStimPulse StimProperties
```

Assemblies read: `3Brain.Common v1.0.8808.14973`,
`3Brain.BioCamDriver v2.6.8808.14987`.

---

## 1. The dangerous part first

`RectangularStimPulse` **accepts almost anything and quietly adjusts it.**
Nothing raises. Every measurement below was taken against the real assembly.

| Ask for | Get back | Nothing raises |
|---|---|---|
| amplitude `2000` µA (range ±1000) | `1000` µA | ✅ silent |
| amplitude `-2000` µA | `-1000` µA | ✅ silent |
| amplitude `7.0` µA on a 5 µA grid | `5.0` µA | ✅ silent |
| widths `10000/1/1` ticks (max 10000) | `10000/0/0` | ✅ silent |
| widths `5000/1/5000` | `5000/1/4999` | ✅ silent |
| widths `8000/0/8000` | `8000/0/2000` | ✅ silent |

### The one that matters

That last row is the reason `biocam/stim/` exists.

```
requested:  +100 µA for 8000 ticks, then −100 µA for 8000 ticks
            → net charge 0. Perfectly balanced.

delivered:  +100 µA for 8000 ticks, then −100 µA for 2000 ticks
            → net charge +600 000 µA·ticks.

IsBiphasic: still True.
```

A charge-balanced request became one that injects net DC, and **there is no
in-band signal that it happened**. `IsBiphasic` only goes false when the second
phase is truncated all the way to zero; a partial truncation leaves it true.

Net DC through a microelectrode drives electrolysis, corrodes the electrode and
damages the tissue near it. This is not a run that fails — it is a run that
looks fine and quietly does the wrong thing, which is the failure mode this
whole repository is organised against.

**The rule:** the total `width1 + interWidth + width2` is capped at
`MaxPulseDuration`, and the overflow is taken off the **later** fields. Phase 1
is never shortened; the inter-phase gap and phase 2 absorb it.

### What this repository does about it

- `biocam.stim.plan()` refuses any pulse the driver would adjust, and reports
  *every* problem at once rather than the first — a colleague on the instrument
  gets one attempt per turnaround.
- `biocam.stim.verify_built_pulse()` compares the object .NET actually returned
  against the plan, catching limits this repository does not know about.
- `python -m biocam.interop.verify_stim_model` checks both directions: every
  pulse `plan()` accepts must come back unchanged, and every pulse it refuses
  must be one the driver would have altered. All 10 cases agree.

---

## 2. `StimProperties.Default` is a placeholder — do not build against it

```
TimeResolutionMicroSec  = 1
AmplitudeResolution     = 1.0
MinAmplitude            = -1000.0
MaxAmplitude            = 1000.0
MaxPulseDuration        = 10000
MaxPulsePhaseDuration   = 0          ← unset
MinTotalDuration        = 0          ← unset
MaxTotalDuration        = 0          ← unset
MaxChPulseCount         = 1000000
IsCurrentStimulator     = True
UnitMeasureString       = 'µA'       (U+00B5, U+0041)
```

Three fields are zero, which is not a limit but an absence. And the time
resolution is 1 µs where the DupleX stimulator's is understood to be coarser.

Build a pulse against this default and every duration is wrong by the ratio of
the two resolutions — **silently**, because every value involved is legal.

Read the real constraints from the instrument: `biocam.Stimulator.Properties`,
wrapped by `biocam.stim.StimConstraints.from_stim_properties()`.
`biocam.interop.stimulator.Stimulator` refuses to send a pulse planned against
constraints that differ from the ones the device reports.

---

## 3. Units and geometry

**Amplitudes are µA.** `IsCurrentStimulator = True` and
`UnitMeasureString = 'µA'` — this is a current source, not a voltage source.

**Durations are ticks, not microseconds.** `Width1`, `InterWidth` and `Width2`
are `Int32` counts of `TimeResolutionMicroSec`. Passing microseconds where
ticks are expected is a silent error of exactly that factor.

**`TotalDurationMicroSec` returns `0.0`** unless the pulse was built with a
`DataSamplingTimeConverter`. Do not use it. `TotalDurationSamples` works;
multiply by `TimeResolutionMicroSec` yourself.

**Pulse geometry.** A biphasic pulse with a gap is eight points:

```
RectangularStimPulse("p", props, +100 µA, w1=100, iw=50, −100 µA, w2=100)

  (0,0) (0,+100) (100,+100) (100,0) (150,0) (150,−100) (250,−100) (250,0)
```

With `interWidth = 0` it is six points.

**Electrode coordinates are 1-based.** `ChCoord(1, 1)` is the first electrode;
`ChCoord(0, 0)` reports `IsNone = True` and raises from `ToIdentifier()`.

**`ChCoord.IsValid` does not bound-check the array.** `ChCoord(65, 65)` on a
64×64 plate reports `IsValid = True`. The type knows nothing about how large
the MEA is, so the bound must be checked before it — `biocam.stim.ElectrodeGrid`
does. `BioCamDataFormat` exposes `NChsPerWell` and `NWells` but **no** row or
column count; the geometry sits behind `IMeaPlatePilot`, which needs the
instrument.

---

## 4. Type surface

### `RectangularStimPulse` (`_3Brain.Common`)

```
.ctor(String friendlyName, StimProperties constraints,
      Double amplitude1, Int32 width1, Int32 interWidth,
      Double amplitude2, Int32 width2)
.ctor(… , DataSamplingTimeConverter timeConverter)

Amplitude1/2 (Double, get/set)   Width1/2, InterWidth (Int32, get/set)
IsBiphasic (get)                 TotalDurationSamples (Int32, get)
TotalDurationMicroSec (get)      Properties (StimProperties, get/set)
TimeIn (get/set)  TimeOut (get)  Type (StimPulseType, get)
static Default, static Empty
```

`Default` is `+100 µA / 50 ticks / 25 gap / −100 µA / 50 ticks`, named
"Default Pulse".

Setters clamp too, and **differently from the constructor**: setting
`Width1 = 10_000_000` on a pulse with `w2 = 100` yields `9900`, preserving
`w2`. Constructor and setter apply the budget in different orders.

### `StimProperties` (`_3Brain.Common`)

```
.ctor(Int32 timeresolution, Double amplitudeResolution,
      Int32 minAmplitude, Int32 maxAmplitude)
.ctor(Int32 timeResolution, Int32 maxPulseDuration,
      Int64 minTotalTime, Int64 maxTotalTime, Double amplitudeResolution,
      Int32 minAmplitude, Int32 maxAmplitude, Int32 maxChPulseCount)
```

Note the four-argument form takes `minAmplitude`/`maxAmplitude` as `Int32`
while the properties are `Double`.

### `IBioCamStim` (`_3Brain.BioCamDriver`), reached via `IBioCam.Stimulator`

```
Boolean Initialize(StimProtocolType protocolType = 1)
Boolean Start()      Boolean Stop()      Boolean Close()
Boolean Reset()      Boolean IsAvailable()

Boolean Send(RectangularStimPulse, StimEndPoint[] pos, StimEndPoint[] neg)
Boolean Send(… , out UInt64 latency)
Boolean Send(… , Double[] timestamps)

StimEndPoint GetInternalEndPoint(ChCoord)
StimEndPoint GetExternalEndPoint(BioCamStimExternalEndPoint)
StimEndPoint GetReferenceElectrodeEndPoint()
StimEndPoint GetEndPointById(Int64) / GetEndPointByName(String)

Properties (StimProperties)   IsInitialized   IsStimulating
MaxPulseCount   TimeResolutionMicroSec   AmplitudeResolutionUM
EndPoints (StimEndPoint[])    Protocol (IBioCamStimProtocolManager)
```

The accessor is **`Stimulator`**, not `Stim`.

### Every lifecycle call throws as well as returning a bool

From the XML for `BioCamStimBase`. Handling only the bool is not enough:

| Call | Throws |
|---|---|
| `Initialize` | `InvalidOperationException` if already initialized; `ArgumentException` if the protocol type is unsupported |
| `Start` | `InvalidOperationException` if already started, or the protocol type is unsupported |
| `Stop` | `InvalidOperationException` if **not** started |
| `Close` | `InvalidOperationException` if **not** initialized |
| `Reset` | `InvalidOperationException` if **not** started |
| `Send` (all three) | `InvalidOperationException` if not started; `ArgumentNullException` on a null pulse; `ArgumentException` on invalid endpoints or timestamps |

`connector.py` calls `Initialize()` during `connect()`, so a session that uses
both it and `biocam.interop.stimulator` hits the "already initialized" throw.
`Stimulator.__enter__` checks `IsInitialized` and `IsStimulating` first.

### Ordering against acquisition

3Brain's sample starts the stimulator **after** `StartDataStreaming` and stops
it **before** `StopDataStreaming` (`MainForm.cs:186,192` then `:210,213`). Not
a stylistic preference: reported latency is in clock cycles "relative to the
beginning of the acquisition", and scheduled timestamps are microseconds from
the same origin. Started before an acquisition exists, both are undefined.

### `StimProtocolType.Static` is a different design, not a flag

The XML says of `Static`: *"All stimulation arguments … must be first loaded
before the stimulation starts"* — the opposite of `RealTime`, where `Start()`
precedes `Send()`. `biocam.interop.stimulator` supports `RealTime` only.

### The reported latency is clock cycles, not microseconds

Convert with `IBioCam.ClockCyclesToMilliseconds(UInt64)`, which is what the
sample uses (`MainForm.cs:272`). Do not divide by a guessed clock rate.

### Enums

```
StimProtocolType    Static = 0, RealTime = 1        (Initialize defaults to 1)
StimPulseType       Rectangular = 0, Arbitrary = 1
StimTrainType       SinglePulse = 0, PairedPulse = 1, Burst = 2
StimStatus          Unknown=0 Idle=1 Loading=2 Ready=3
                    Playing=4 Finished=5 FinishedError=6
BioCamStimExternalEndPoint
                    Ext1Plus=0 Ext1Minus=1 … Ext4Plus=6 Ext4Minus=7
```

---

## 5. Limits, and where each one comes from

| Limit | Value | Source |
|---|---|---|
| pulse values per `Send` | 64 | driver XML |
| endpoint values per `Send` | 288 | driver XML |
| timestamps per `Send` | **1024** | driver XML |
| queued future stimuli | **1000** | API intro PDF |
| `MaxCount` (train) | **1000** | `StimulusBaseTrainConfigurationOptionsConstants` |
| `MinDistance` / `MaxDistance` | 1000 µs / 3 600 000 µs | same constants object |
| endpoints per spatial configuration | 1000 | API intro PDF |
| positive/negative may not share a column | — | API intro PDF |
| chip reconfiguration cost | 26 µs + 8.4 µs × (rows − 1) | API intro PDF |
| `MAX_N_PULSES_PER_BURST` | 1000 | `StimBurst` static field |
| `MAX_N_REPETITIONS` | 200 | `StimBurst` static field |

**The three sources disagree on the timestamp limit**: the XML says 1024, the
constants object and the PDF both say 1000. `biocam.stim` enforces 1000 — the
intersection. The 24 extra slots are not worth discovering which source is
stale.

Buffer overflow is **silent and delayed**. The XML is explicit: *"Any time that
one of these memory buffers overflows will cause the next invocation of the
method to not consider the new argument values."* The call that overflows
appears to succeed; the *following* call is the one that silently does nothing.

---

## 6. Timestamps are measured from the start of acquisition

From the XML for `Send(pulse, pos, neg, Double[] timestamps)`:

> The ordered array of pulse time-stamps **in microsecond relative to the
> beginning of the acquisition**.

Not relative to the moment of sending. A train planned as "start in half a
second" and sent ten minutes into a recording has every timestamp in the past.
What the instrument does then is **untested** — plausibly it fires the whole
train at once, plausibly it discards it. Neither is what was meant.

`TrainPlan.shifted_by(current_acquisition_time_us)` performs the conversion.
The same applies to the latency reported by the `out UInt64` overload: clock
cycles relative to the beginning of the acquisition.

---

## 7. These assemblies are obfuscated

Exception text carries private-use codepoints (U+E000 and up) in place of
method names:

```
ArgumentOutOfRangeException: Specified argument was out of the range of valid values.
   at _3Brain.Common.RectangularStimPulse.(Double , Int32 , …)
```

Printing one to a cp1252 Windows console raises `UnicodeEncodeError` — so the
error handler crashes and **hides the real error**. Every entry point that may
print driver exception text forces UTF-8 output with a lossy fallback, so that
reporting a failure can never itself be the failure.

---

## 8. What still needs the instrument

Everything above was established without one. These cannot be:

1. **The DupleX's real `StimProperties`** — time resolution, amplitude
   resolution, min/max amplitude, `MaxPulseDuration`. The rules are verified;
   the numbers are not. Everything in `biocam/stim/` is parameterised on them.
2. **The array geometry** — that the plate is 64×64, and which member reports
   it.
3. **That `Start()` is what makes pulses fire.** The lifecycle
   `Initialize → Start → Stop → Close` comes from the XML;
   `connector.py` omits `Start()` and this is believed to be why stimulation
   never fired. Believed, not demonstrated — issue #22.
4. **Whether `GetInternalEndPoint` returns an endpoint for every electrode**,
   or only for a subset — issue #23.
5. **The column-separation rule** — from the PDF, never exercised — issue #23.
6. **What the instrument does with timestamps in the past** — issue #24. This
   is also where the *current acquisition time* has to come from, and nothing
   in this repository can yet read it.

Issues #21–#24 carry runnable procedures. **#21 is the one to close first**:
every limit in `biocam/stim/` is parameterised on the DupleX's real
`StimProperties`.

### Settled here, not in the lab

These were on the list and came off it. Each needed pythonnet and the DLLs, and
none needed a BioCAM:

- **pythonnet's out-parameter convention.** A method with an `out` parameter
  returns `(returnValue, outParam)` as a tuple, and binds whether or not a
  placeholder is passed — checked against
  `StimProperties.JsonSerialize(out String)`.
- **Overload ambiguity in `Send`.** `Send(pulse, pos, neg)` and
  `Send(pulse, pos, neg, out UInt64)` are both three-argument calls from
  Python. `Overloads[…]` selects explicitly, and
  `clr.GetClrType(T).MakeByRefType()` produces the `UInt64&` key an out
  parameter needs. All three overload keys resolve against the **real**
  `IBioCamStim` method table, checked on every `verify_stim_model` run — so
  `stimulator.py` names the overload it means rather than letting resolution
  guess. The remaining gap is the *call*, not the key: invoking the
  out-parameter form and getting a two-tuple back needs a live stimulator.
- **`System.Array[T](list)`** works for `StimEndPoint[]` and `Double[]`.
- **`StimProperties.MaxPulseDuration` is settable**, confirmed with a value
  other than the constructor default — a 500-tick cap really does truncate a
  400/0/400 pulse to 400/0/100. Two cases in `verify_stim_model` use a
  non-default cap for exactly this reason; without them the whole check would
  pass even if the assignment were a silent no-op.
- **The five `BioCamDataFormat` properties from issue #11 exist** —
  `BitDepth`, `ADCCountsToValue`, `Offset`, `MinDigitalValue`,
  `MaxDigitalValue`. Whether they return sane *values* still needs the
  instrument.
