"""`biocam probe`: reads the instrument's reports and sends nothing.

Fake device and stimulator only - no DLLs, no clr. These check the Python
side: that every section is reported, that a failed read is shown rather than
raised, that nothing is ever started or sent, and that the output file is
written even when the probe fails.
"""

import types

import pytest

from biocam.interop.probe import endpoint_coverage, printable, run_probe


class FakeNetStimulator:
    def __init__(self):
        self.calls = []
        self.MaxPulseCount = 64
        self.TimeResolutionMicroSec = 10
        self.AmplitudeResolutionUM = 1.0
        self.IsInitialized = True
        self.IsStimulating = False
        self.EndPoints = [object()] * 4096
        self.Properties = types.SimpleNamespace(
            TimeResolutionMicroSec=10, AmplitudeResolution=1.0,
            MinAmplitude=-1000.0, MaxAmplitude=1000.0, MaxPulseDuration=1000,
            MaxPulsePhaseDuration=0, MinTotalDuration=0, MaxTotalDuration=0,
            MaxChPulseCount=1000000, MinTime=0, IsCurrentStimulator=True,
            UnitMeasureString="µA", IsValid=True)
        self.no_endpoint = {(3, 4)}

    def GetInternalEndPoint(self, coord):
        self.calls.append("GetInternalEndPoint")
        return None if coord in self.no_endpoint else types.SimpleNamespace(
            IsValid=True, IsInternal=True)

    def Start(self):
        self.calls.append("Start")

    def Send(self, *args):
        self.calls.append("Send")


class FakeDevice:
    def __init__(self, net):
        self.biocam = types.SimpleNamespace(
            Stimulator=net, ClockCyclesToMilliseconds=lambda n: n / 50_000.0)
        self.data_format = types.SimpleNamespace(
            FrameRate=18557.720703125, NWells=1, NChsPerWell=4096,
            ChSampleByteSize=2, DataPacketLength=0, DataPacketNFrames=0,
            DataPacketTimeSpanMs=2, OptimizeDataPacketLatency=True,
            BitDepth=12, ADCCountsToValue=2.0146520146520146, Offset=-4125.0,
            MinDigitalValue=0, MaxDigitalValue=4095)
        self.entered = self.exited = 0

    def __enter__(self):
        self.entered += 1
        return self

    def __exit__(self, *exc):
        self.exited += 1
        return False


class FakeStimulator:
    """Stands in for biocam.interop.stimulator.Stimulator."""

    def __init__(self, device):
        self.device = device
        self.exited = 0
        self.constraints = "StimConstraints(time_resolution_us=10, ...)"

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.exited += 1
        return False


def probe(net=None, grid=(4, 4), **kw):
    net = net or FakeNetStimulator()
    device = FakeDevice(net)
    stimulators = []

    def stimulator_factory(dev):
        stimulators.append(FakeStimulator(dev))
        return stimulators[-1]

    lines = []
    kw.setdefault("stimulator_factory", stimulator_factory)
    status = run_probe(*grid, lines.append, device_factory=lambda: device,
                       chcoord=lambda r, c: (r, c), **kw)
    return status, "\n".join(lines), device, net, stimulators


def test_every_section_is_reported():
    status, text, *_ = probe()
    assert status == 0
    for heading in ("== CONNECT", "== DATA FORMAT", "== CLOCK",
                    "== STIMULATOR", "== ELECTRODE ENDPOINTS", "== RELEASED"):
        assert heading in text
    assert "FrameRate" in text and "18557.720703125" in text
    assert "Properties.UnitMeasureString" in text
    assert "= 50 clock cycles per microsecond" in text.replace(":", " =")


def test_nothing_is_started_or_sent():
    # The probe is safe to run on a chip with anything on it: it initializes
    # the stimulator and reads. Start is where stimuli become possible.
    _, _, _, net, _ = probe()
    assert "Start" not in net.calls
    assert "Send" not in net.calls


def test_electrodes_without_an_endpoint_are_listed():
    status, text, *_ = probe()
    assert "electrodes checked: 16" in text
    assert "without an endpoint (None): 1" in text
    assert "3,4" in text
    assert "endpoint not valid or not internal: 0" in text
    assert "raised an error: 0" in text


def test_a_property_that_cannot_be_read_is_reported_not_raised():
    net = FakeNetStimulator()
    del net.Properties.MinTime
    status, text, *_ = probe(net)
    assert "Properties.MinTime" in text and "FAILED" in text
    assert "Properties.MaxAmplitude" in text      # the rest still read


def test_everything_is_released():
    _, _, device, _, stimulators = probe()
    assert device.exited == 1
    assert stimulators[0].exited == 1


def test_no_stimulator_still_reports_the_rest_and_releases():
    def unavailable(device):
        raise RuntimeError("IsAvailable() returned false")

    status, text, device, _, _ = probe(stimulator_factory=unavailable)
    assert status == 2
    assert "stimulator unavailable" in text
    assert "== DATA FORMAT" in text
    assert device.exited == 1


def test_endpoint_errors_are_counted():
    class Raising(FakeNetStimulator):
        def GetInternalEndPoint(self, coord):
            raise RuntimeError("not started")

    coverage = endpoint_coverage(Raising(), 2, 2, lambda r, c: (r, c))
    assert len(coverage["failed"]) == 4


def test_obfuscated_text_cannot_crash_the_output(monkeypatch):
    monkeypatch.setattr("sys.stdout", types.SimpleNamespace(encoding="cp1252"))
    text = printable(RuntimeError(""))
    text.encode("cp1252")
    assert printable("µA") == "'µA'"


def test_the_cli_writes_its_file_even_when_the_probe_fails(tmp_path, monkeypatch):
    import biocam.interop.probe as probe_module
    from biocam.cli import main

    def failing(n_rows, n_cols, emit, **kw):
        emit("== CONNECT")
        raise TimeoutError("No free BioCAM found.")

    monkeypatch.setattr(probe_module, "run_probe", failing)
    assert main(["probe", "--output-dir", str(tmp_path)]) == 2
    [saved] = tmp_path.glob("probe_*.txt")
    assert "No free BioCAM found" in saved.read_text(encoding="utf-8")


def test_an_endpoint_flagged_invalid_counts_as_unusable():
    # The XML documents no null return, only StimEndPoint.IsValid and
    # IsInternal. Counting only None would report every electrode usable
    # if the driver returns invalid endpoint objects instead.
    class Endpoint:
        def __init__(self, valid=True, internal=True):
            self.IsValid, self.IsInternal = valid, internal

    class Net(FakeNetStimulator):
        def GetInternalEndPoint(self, coord):
            return {(1, 2): Endpoint(valid=False),
                    (2, 1): Endpoint(internal=False)}.get(coord, Endpoint())

    coverage = endpoint_coverage(Net(), 2, 2, lambda r, c: (r, c))
    assert coverage["invalid"] == [(1, 2), (2, 1)]
    assert coverage["missing"] == []


def test_a_member_that_is_a_method_is_not_shown_as_a_value():
    from biocam.interop.probe import read_members

    class Thing:
        def IsValid(self):
            return True

    [(name, text)] = read_members(Thing(), ["IsValid"])
    assert "method" in text and "bound" not in text
