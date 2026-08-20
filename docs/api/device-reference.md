# Device and acquisition surface, read from the assemblies

Reflection output, not transcription from the XML. Everything here needs the
3Brain DLLs and **no instrument** — the same standing as
[stimulation-reference.md](stimulation-reference.md). Regenerate rather than
trusting this file: `python -m biocam.interop.reflect <TypeName>`.

Recorded because a claim in a code comment with no artefact behind it is not
much better than a guess.

```
Loaded 3Brain assemblies:
  3Brain.Common  v1.0.8808.14973
  3Brain.BioCamDriver  v2.6.8808.14987
  3Brain.Diagnostic  v1.0.1.0
  3Brain.Deployment.Drivers  v1.0.0.0
```

## BioCamPool — settles issue #17

The C# default the sample relies on, and the value `device.py` already passed:

```
    Int32 NOpenBoards  (get)
    Void Activate(Boolean supportBioCamInvalidSerial = False) [static]
    Boolean CloseBoard(Int32 slotIndex) [static]
    Void Deactivate() [static]
    Int32[] GetSlotIndexesBioCam() [static]
    Int32[] GetSlotIndexesConnectedBioCam() [static]
    Int32[] GetSlotIndexesFreeBioCam() [static]
    Boolean OpenBoard(Int32 slotIndex) [static]
    Void ReleaseBioCamControl(Int32 slotIndex) [static]
    IBioCam TakeBioCamControl(Int32 slotIndex) [static]
    IBioCam TakeFirstFreeBioCamControl() [static]
```

## IBioCam — streaming and the clock

`StartDataStreaming`'s parameter **names** are what `source.py` passes as
keywords, and its first parameter really is nullable. That settles the names
against the assembly; whether pythonnet binds keyword arguments to a .NET
method is a pythonnet question and remains untested on the lab machine.

`ClockCyclesToMilliseconds` is what `cycles_per_us_of` calls to get the
factor that makes the acquisition clock's cross-check able to fail at all.

```
    BioCamDataFormat DataFormat  (get)
    Boolean IsStreaming  (get)
    Double ClockCyclesToMilliseconds(UInt64 clockCycles)
    Boolean StartDataStreaming(Nullable<Int32> dataPacketTimeSpanMs = None, Boolean optimizeDataPacketLatency = False)
    Boolean StopDataStreaming()
```

## DataPacketHeader

`PacketCounter` is a `UInt16` **property**, not merely a constructor
parameter — which is what `biocam/data/integrity.py` counts wraps against, so
`COUNTER_MODULUS = 65536` is a read fact rather than a deduction.

`PayloadLength` is an `Int32`. **The XML gives it no unit** (XML:1916-1919);
this software assumes bytes and counts disagreements rather than asserting
them — see `biocam/interop/source.py` and
`RecordingWriter._suspect_payload_alignment`.

```
    UInt16 PacketCounter  (get/set)
    Int32 PayloadLength  (get/set)
    Byte Reserved  (get/set)
    BioCamUsbComSignalType SignalType  (get/set)
    UInt64 Timestamp  (get/set)
```

## IBioCamStim — lifecycle and limits

`Boolean Reset()` is what `Stimulator.reset()` calls.

`MaxPulseCount`, `TimeResolutionMicroSec` and `AmplitudeResolutionUM` are read
from the device at run time. **They are not currently cross-checked** against
the constants in `biocam/stim/`, which were derived from three disagreeing
written sources — worth comparing on the first lab session.

```
    Double AmplitudeResolutionUM  (get)
    Int32 MaxPulseCount  (get)
    Int32 TimeResolutionMicroSec  (get)
    Boolean Close()
    Boolean Initialize(StimProtocolType protocolType = 1)
    Boolean Reset()
    Boolean Start()
    Boolean Stop()
```

## BioCamDataFormat

`NChsPerWell` returns **-1** when the wells do not all have the same number of
channels (XML:349-352). That is refused in `cli.py::_parameters_from` rather
than passed through as a negative channel count.

`DataPacketLength` and `DataPacketNFrames` are the driver's own statement of
packet geometry and are **not currently read** — either would let the recorder
check its frames-per-packet arithmetic against the driver's on the first
packet.

```
    .ctor(IBioCam owner, Int32 dataPacketTimeSpanMs, Boolean optimizeDataPacketLatency, Double minAnalogValue, Double maxAnalogValue, Int32 minDigitalValue, Int32 maxDigitalValue)
    Double ADCCountsToValue  (get)
    Int32 BitDepth  (get/set)
    Int32 ChSampleByteSize  (get)
    Int32 DataPacketLength  (get)
    Int32 DataPacketNFrames  (get)
    Double FrameRate  (get)
    Int32 MaxDigitalValue  (get/set)
    Int32 MeanDigitalValue  (get)
    Int32 MinDigitalValue  (get/set)
    Int32 NChsPerWell  (get)
    Int32 NWells  (get)
    Double Offset  (get)
    Int32 SaturationDigitalValue  (get)
```

## DataLossEventArgs

`NReceivedFrames` is the driver's own authoritative frame count and is **not
currently read**. It is the one independent check on elapsed time that still
works when `Timestamp` is 0 — which is exactly the case where the acquisition
clock is weakest.

```
    Int32 Counter
    Int64 NReceivedFrames
```
