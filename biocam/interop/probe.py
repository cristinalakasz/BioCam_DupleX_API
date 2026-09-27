"""Layer 1 - read what the instrument reports about itself. Sends nothing.

`python -m biocam.cli probe` claims the BioCAM, initializes the stimulator
(Initialize only - never Start, never Send), reads every property the
hardware-verification issues ask for, and releases everything. It answers
issues #11 (DataFormat), #21 (StimProperties) and the first half of #23
(which electrodes have a stimulation endpoint) in one run, with no pulse
delivered and nothing recorded.

Every read is individually guarded and reported as a value or as FAILED
with the reason: a property that cannot be read is itself an answer, and one
failure must not hide the rest.

**Untested on the instrument.** Member names are from
API/3Brain.BioCamDriver.xml and docs/api/*.md (read off the assemblies by
reflection), except StimProperties.MinTime and .IsValid, which are read to
learn whether they exist. A name the object does not have reports FAILED
rather than guessing.
"""

import sys

DATA_FORMAT_MEMBERS = (
    # XML: BioCamDataFormat.
    "FrameRate", "NWells", "NChsPerWell", "ChSampleByteSize",
    "DataPacketLength", "DataPacketNFrames", "DataPacketTimeSpanMs",
    "OptimizeDataPacketLatency",
    # No XML (inherited from _3Brain.Common) - issue #11. All seven read off
    # the assembly by reflection: docs/api/device-reference.md.
    "BitDepth", "ADCCountsToValue", "Offset", "MinDigitalValue",
    "MaxDigitalValue", "MeanDigitalValue", "SaturationDigitalValue",
)

STIM_PROPERTY_MEMBERS = (
    # StimProperties (_3Brain.Common), the list issue #21 asks for. All but
    # the last two are in the reflection dump in
    # docs/api/stimulation-reference.md; MinTime and IsValid are in neither
    # that nor the XML, and are read to find out whether they exist.
    "TimeResolutionMicroSec", "AmplitudeResolution", "MinAmplitude",
    "MaxAmplitude", "MaxPulseDuration", "MaxPulsePhaseDuration",
    "MinTotalDuration", "MaxTotalDuration", "MaxChPulseCount", "MinTime",
    "IsCurrentStimulator", "UnitMeasureString", "IsValid",
)

STIMULATOR_MEMBERS = (
    # XML: IBioCamStim.
    "MaxPulseCount", "TimeResolutionMicroSec", "AmplitudeResolutionUM",
    "IsInitialized", "IsStimulating",
)

# Electrodes without an endpoint listed in full up to this many; the count
# is always complete.
MAX_LISTED = 50


def printable(value) -> str:
    """repr(), or ascii() where the console cannot encode it.

    These assemblies are obfuscated: their exception texts carry private-use
    codepoints, and printing one to a cp1252 console raises
    UnicodeEncodeError - which would end the probe over a message about the
    real problem. 'µA' still prints as itself where the console can show it.
    """
    text = repr(value)
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    try:
        text.encode(encoding)
    except (UnicodeEncodeError, LookupError):
        return ascii(value)
    return text


def read_members(obj, names) -> list:
    """(name, text) for each member: its value, or FAILED and the reason."""
    rows = []
    for name in names:
        try:
            value = getattr(obj, name)
            if callable(value):
                # A method, not a property: calling it is not a read.
                rows.append((name, "is a method, not a property - not called"))
                continue
            rows.append((name, printable(value)))
        except Exception as exc:  # noqa: BLE001 - a failed read is an answer
            rows.append((name, f"FAILED - {printable(exc)}"))
    return rows


def endpoint_coverage(stimulator, n_rows, n_cols, chcoord) -> dict:
    """Ask for the internal endpoint of every electrode. Sends nothing.

    `chcoord` builds a `ChCoord(row, col)`; passed in so the caller owns the
    .NET import. Coordinates are 1-based, as ChCoord is.
    """
    missing, invalid, failed = [], [], []
    for row in range(1, n_rows + 1):
        for col in range(1, n_cols + 1):
            try:
                endpoint = stimulator.GetInternalEndPoint(chcoord(row, col))
                if endpoint is None:
                    missing.append((row, col))
                # The XML documents no null return, only these two flags
                # (XML:5147, 5179). Counting None alone would report every
                # electrode usable if the driver returns an invalid object.
                elif not endpoint.IsValid or not endpoint.IsInternal:
                    invalid.append((row, col))
            except Exception as exc:  # noqa: BLE001 - counted and reported
                failed.append(((row, col), printable(exc)))
    return {"checked": n_rows * n_cols, "missing": missing,
            "invalid": invalid, "failed": failed}


def format_coverage(coverage) -> list:
    lines = [f"electrodes checked: {coverage['checked']}"]
    for key, label in (("missing", "without an endpoint (None)"),
                       ("invalid", "endpoint not valid or not internal")):
        cells = coverage[key]
        lines.append(f"{label}: {len(cells)}")
        if cells:
            shown = ", ".join(f"{r},{c}" for r, c in cells[:MAX_LISTED])
            more = len(cells) - MAX_LISTED
            lines.append(f"  {shown}"
                         + (f" ... and {more} more" if more > 0 else ""))
    lines.append(f"raised an error: {len(coverage['failed'])}")
    if coverage["failed"]:
        (r, c), reason = coverage["failed"][0]
        lines.append(f"  first: {r},{c}: {reason}")
    return lines


def run_probe(n_rows, n_cols, emit, *, device_factory=None,
              stimulator_factory=None, chcoord=None) -> int:
    """Claim, read, release. Returns 0 if every section completed, else 2.

    `emit(line)` receives each output line. The factories and `chcoord`
    default to the real driver; tests pass fakes.
    """
    if device_factory is None:
        from biocam.interop.device import BioCamDevice as device_factory
    if stimulator_factory is None:
        from biocam.interop.stimulator import Stimulator as stimulator_factory

    from biocam.interop.device import cycles_per_us_of

    status = 0
    emit("== CONNECT")
    with device_factory() as device:
        emit("claimed the BioCAM: IsConnected and MeaPlate.IsConnected are true")

        emit("")
        emit("== DATA FORMAT (issue #11)")
        try:
            data_format = device.data_format
        except Exception as exc:  # noqa: BLE001
            emit(f"DataFormat: FAILED - {printable(exc)}")
            status = 2
        else:
            for name, text in read_members(data_format, DATA_FORMAT_MEMBERS):
                emit(f"{name:28} = {text}")

        emit("")
        emit("== CLOCK")
        factor = cycles_per_us_of(device)
        emit("ClockCyclesToMilliseconds: "
             + (f"{factor:g} clock cycles per microsecond" if factor
                else "FAILED or unusable - acquisition times will be "
                     "self-calibrated and unchecked"))

        emit("")
        emit("== STIMULATOR (issue #21) - Initialize only; nothing is sent")
        try:
            stimulator_cm = stimulator_factory(device)
            stim = stimulator_cm.__enter__()
        except Exception as exc:  # noqa: BLE001
            emit(f"stimulator unavailable: {printable(exc)}")
            emit("(recording still works; stimulation will not)")
            return 2
        try:
            status = max(status, _read_stimulator(
                device, stim, n_rows, n_cols, chcoord, emit))
        except BaseException:
            stimulator_cm.__exit__(*sys.exc_info())
            raise
        # Raises if Stop/Close reported a problem; the device still releases.
        stimulator_cm.__exit__(None, None, None)
    emit("")
    emit("== RELEASED the stimulator and the BioCAM")
    return status


def _read_stimulator(device, stim, n_rows, n_cols, chcoord, emit) -> int:
    """Read the stimulator's properties and endpoint coverage. Sends nothing."""
    status = 0
    net = device.biocam.Stimulator
    for name, text in read_members(net, STIMULATOR_MEMBERS):
        emit(f"Stimulator.{name:22} = {text}")
    try:
        emit(f"Stimulator.EndPoints length    = {len(net.EndPoints)}")
    except Exception as exc:  # noqa: BLE001
        emit(f"Stimulator.EndPoints length    = FAILED - {printable(exc)}")
    try:
        properties = net.Properties
    except Exception as exc:  # noqa: BLE001
        emit(f"Properties: FAILED - {printable(exc)}")
        status = 2
    else:
        for name, text in read_members(properties, STIM_PROPERTY_MEMBERS):
            emit(f"Properties.{name:22} = {text}")
    try:
        emit(f"limits this software will use: {stim.constraints}")
    except Exception as exc:  # noqa: BLE001
        emit(f"limits this software will use: FAILED - {printable(exc)}")
        status = 2

    emit("")
    emit(f"== ELECTRODE ENDPOINTS (issue #23), {n_rows}x{n_cols} grid")
    if chcoord is None:
        from _3Brain.Common import ChCoord as chcoord
    for line in format_coverage(
            endpoint_coverage(net, n_rows, n_cols, chcoord)):
        emit(line)
    return status
