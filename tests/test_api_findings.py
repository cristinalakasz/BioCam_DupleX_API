"""What the API verification found, turned into tests.

These are Layer 2 consequences of Layer 1 defects: the interop call sites
cannot be exercised here, but every one of these defects shows up as wrong
behaviour in code that can be.
"""

import pytest

from biocam.data.recording import AcquisitionParameters, RecordingWriter


class Format:
    """A stand-in for BioCamDataFormat."""

    FrameRate = 18557.720703125
    NWells = 1
    NChsPerWell = 4096
    ChSampleByteSize = 2
    BitDepth = 12
    ADCCountsToValue = 2.0146520146520146
    Offset = -4125.0
    MinDigitalValue = 0
    MaxDigitalValue = 4095


# --------------------------------------------------------------------------
# FINDING 8: NChsPerWell = -1 is a DOCUMENTED return value
# --------------------------------------------------------------------------

def test_a_non_uniform_array_is_refused_rather_than_sized_wrongly():
    # XML:349-352: "If the number of channels is not equal for all wells, -1
    # is returned." Passed through, that gives a negative channel count, hence
    # a negative bytes_per_frame, a negative frames-per-packet and a negative
    # frame count - while the writer keeps accepting bytes. The recording
    # would look like it worked.
    from biocam.cli import _parameters_from

    fmt = Format()
    fmt.NChsPerWell = -1
    with pytest.raises(RuntimeError, match="not equal for all wells"):
        _parameters_from(fmt)


def test_a_zero_channel_array_is_refused():
    from biocam.cli import _parameters_from

    fmt = Format()
    fmt.NChsPerWell = 0
    with pytest.raises(RuntimeError, match="cannot describe a recordable"):
        _parameters_from(fmt)


def test_a_normal_format_still_passes_through():
    from biocam.cli import _parameters_from

    params = _parameters_from(Format())
    assert params.total_channels == 4096
    assert params.bytes_per_frame == 8192


def test_what_the_unchecked_version_would_have_produced():
    # A negative control on the test above: confirm the failure it prevents is
    # real, not hypothetical. This is the arithmetic that used to run.
    wells, per_well = 1, -1
    total = wells * per_well
    assert total == -1
    assert total * 2 == -2, "a negative bytes_per_frame"


# --------------------------------------------------------------------------
# FINDING 2: 0 is the documented "not available" sentinel, not a time
# --------------------------------------------------------------------------

PARAMS = AcquisitionParameters(
    frame_rate_hz=1000.0, total_channels=4, ch_sample_byte_size=2,
    bit_depth=12, adc_counts_to_value=1.0, offset=0.0,
    min_digital_value=0, max_digital_value=4095,
)


def test_an_unavailable_timestamp_never_becomes_first_timestamp(tmp_path):
    # XML:1923: "...or 0 when the timestamp is not available." Recorded as a
    # first_timestamp, a later reader takes a sentinel for an origin.
    from biocam.data.recording import read_sidecar

    raw, meta = tmp_path / "r.raw", tmp_path / "r_meta.json"
    with RecordingWriter(raw, meta, PARAMS) as writer:
        writer.write_packet(timestamp=0, counter=1, payload=b"\x00" * 8)
        writer.write_packet(timestamp=0, counter=2, payload=b"\x00" * 8)
        writer.write_packet(timestamp=4242, counter=3, payload=b"\x00" * 8)
    side = read_sidecar(meta)["integrity"]
    assert side["first_timestamp"] == 4242, (
        "a 'not available' sentinel was recorded as the recording's origin")
    assert side["timestamps_unavailable"] == 2


def test_a_recording_with_no_usable_timestamps_says_so(tmp_path):
    from biocam.data.recording import read_sidecar

    raw, meta = tmp_path / "r.raw", tmp_path / "r_meta.json"
    with RecordingWriter(raw, meta, PARAMS) as writer:
        for counter in range(1, 6):
            writer.write_packet(timestamp=0, counter=counter,
                                payload=b"\x00" * 8)
    side = read_sidecar(meta)["integrity"]
    assert side["first_timestamp"] is None, (
        "no timestamp was available, so there is no origin to report")
    assert side["timestamps_unavailable"] == 5


def test_usable_timestamps_are_unaffected(tmp_path):
    from biocam.data.recording import read_sidecar

    raw, meta = tmp_path / "r.raw", tmp_path / "r_meta.json"
    with RecordingWriter(raw, meta, PARAMS) as writer:
        for i, counter in enumerate(range(1, 4), start=1):
            writer.write_packet(timestamp=i * 100, counter=counter,
                                payload=b"\x00" * 8)
    side = read_sidecar(meta)["integrity"]
    assert side["first_timestamp"] == 100
    assert side["last_timestamp"] == 300
    assert side["timestamps_unavailable"] == 0


# --------------------------------------------------------------------------
# FINDING 9: a payload that disagrees with its header
# --------------------------------------------------------------------------

def test_a_payload_mismatch_reaches_the_sidecar(tmp_path):
    from biocam.data.recording import read_sidecar

    raw, meta = tmp_path / "r.raw", tmp_path / "r_meta.json"
    with RecordingWriter(raw, meta, PARAMS) as writer:
        writer.write_packet(timestamp=1, counter=1, payload=b"\x00" * 8)
        writer.note_payload_mismatches(3)
    side = read_sidecar(meta)["integrity"]
    assert side["payload_length_mismatches"] == 3


def a_clean_recording(tmp_path, name, mismatches=0):
    """Enough consecutive packets to earn a clean verdict."""
    from biocam.data.recording import read_sidecar

    raw = tmp_path / f"{name}.raw"
    meta = tmp_path / f"{name}_meta.json"
    with RecordingWriter(raw, meta, PARAMS) as writer:
        for counter in range(1, 21):
            writer.write_packet(timestamp=counter * 100, counter=counter,
                                payload=b"\x00" * 8)
        if mismatches:
            writer.note_payload_mismatches(mismatches)
        # Explicitly: without it __exit__ writes status "failed", and a failed
        # status downgrades any clean verdict to "unknown" - which is exactly
        # what made the first version of this baseline useless.
        writer.finalise("duration_reached")
    return read_sidecar(meta)["integrity"]


def test_a_payload_mismatch_stops_the_verdict_being_clean(tmp_path):
    # The baseline has to be clean, or "not clean" proves nothing. The first
    # version of this test wrote a single packet, whose verdict is "unknown"
    # either way - so it passed with the fix reverted, and the negative
    # control is the only reason that was noticed.
    baseline = a_clean_recording(tmp_path, "baseline")
    assert baseline["verdict"] == "clean", baseline

    flagged = a_clean_recording(tmp_path, "flagged", mismatches=1)
    assert flagged["verdict"] != "clean", (
        "the frame alignment of this recording is in doubt and the verdict "
        "said it was clean")
    assert flagged["payload_length_mismatches"] == 1

# --------------------------------------------------------------------------
# FINDING 1: the CLI threw away the device's clock factor
# --------------------------------------------------------------------------

class Pkt:
    def __init__(self, timestamp):
        self.timestamp = timestamp
        self.counter = 0
        self.payload = b""


def fed(clock, n=5, cycles_per_frame=10_000):
    """Run some packets through, as a writer would."""
    for i in range(1, n + 1):
        clock.observe_totals(Pkt(i * cycles_per_frame),
                             frames_written_total=i * 100,
                             frames_missing_total=0)
    return clock


def test_a_clock_without_the_factor_cannot_cross_check_itself():
    # The point of Finding 1. Given the factor, the two estimates are
    # independent and can disagree. Without it, the device estimate reduces
    # algebraically to the frame estimate and the check cannot fail - so a
    # `biocam record` run reported agreement that meant nothing.
    from biocam.data.clock import AcquisitionClock

    supplied = fed(AcquisitionClock(1000.0, cycles_per_us=10.0))
    calibrated = fed(AcquisitionClock(1000.0))
    assert supplied.cross_check_is_meaningful is True
    assert calibrated.cross_check_is_meaningful is False, (
        "a self-calibrated clock claimed its own cross-check was evidence")


def test_a_clock_given_the_factor_reports_a_device_source():
    from biocam.data.clock import AcquisitionClock

    clock = fed(AcquisitionClock(1000.0, cycles_per_us=10.0))
    assert clock.read().source == "device"


def test_a_self_calibrated_clock_says_so_in_its_warnings():
    from biocam.data.clock import AcquisitionClock

    clock = fed(AcquisitionClock(1000.0), n=250, cycles_per_frame=10_000)
    assert any("cannot detect" in w or "identity" in w
               for w in clock.warnings()), clock.warnings()


# --------------------------------------------------------------------------
# FINDING 5: the queued-pulse limit is the device's memory, not a per-call arg
# --------------------------------------------------------------------------

def test_two_legal_sends_can_exceed_the_devices_memory():
    # The arithmetic the cumulative check exists for. XML:4954 says an
    # overflow makes the NEXT Send silently ignore its arguments - the call
    # that overflows appears to succeed.
    from biocam.stim.train import MAX_TRAIN_PULSES

    first, second = 600, 600
    assert first <= MAX_TRAIN_PULSES
    assert second <= MAX_TRAIN_PULSES
    assert first + second > MAX_TRAIN_PULSES


# --------------------------------------------------------------------------
# Re-review: a payload-unit guess must not condemn every recording
# --------------------------------------------------------------------------

def a_recording_with(tmp_path, name, packets, mismatches, sample=None):
    from biocam.data.recording import read_sidecar

    raw = tmp_path / f"{name}.raw"
    meta = tmp_path / f"{name}_meta.json"
    with RecordingWriter(raw, meta, PARAMS) as writer:
        for counter in range(1, packets + 1):
            writer.write_packet(timestamp=counter * 100, counter=counter,
                                payload=bytes(8))
        if mismatches:
            writer.note_payload_mismatches(mismatches, sample=sample)
        writer.finalise("duration_reached")
    return writer, read_sidecar(meta)["integrity"]


def test_every_packet_mismatching_is_read_as_a_unit_error_not_data_loss(tmp_path):
    # PayloadLength has NO documented unit. If this software compares the
    # wrong one, every packet disagrees - and letting that set the verdict
    # would report every otherwise-perfect recording as gaps_detected, which
    # trades the integrity claim the sidecar exists to make for a guess.
    writer, integrity = a_recording_with(tmp_path, "all", packets=20,
                                         mismatches=20, sample=(4096, 8192))
    assert integrity["verdict"] == "clean", (
        "a wrong unit assumption condemned a recording that lost nothing")
    assert integrity["payload_length_mismatches"] == 20
    text = " ".join(writer.payload_warnings())
    assert "wrong units" in text, text
    assert "4096" in text and "8192" in text, "the numbers that settle it"


def test_some_packets_mismatching_is_read_as_lost_alignment(tmp_path):
    # Some but not all is the shape of a real problem: the frames after such
    # a packet may be offset, which in the signal looks like data.
    writer, integrity = a_recording_with(tmp_path, "some", packets=20,
                                         mismatches=3)
    assert integrity["verdict"] != "clean"
    text = " ".join(writer.payload_warnings())
    assert "3 of 20" in text, text


def test_no_mismatch_says_nothing(tmp_path):
    writer, integrity = a_recording_with(tmp_path, "none", packets=20,
                                         mismatches=0)
    assert integrity["verdict"] == "clean"
    assert writer.payload_warnings() == []


def test_the_mismatch_numbers_reach_the_sidecar(tmp_path):
    # Written and never read is how a diagnostic becomes decoration.
    _writer, integrity = a_recording_with(tmp_path, "sample", packets=10,
                                          mismatches=2, sample=(4096, 8192))
    assert integrity["payload_mismatch_sample"] == [4096, 8192]
    assert integrity["packets_written"] == 10


# --------------------------------------------------------------------------
# Re-review: the buffer depths, and which one actually matters
# --------------------------------------------------------------------------

def test_the_three_documented_buffer_depths_are_recorded():
    # XML:4959-4976 lists three, and the smallest is the one a closed loop
    # fills fastest: every Send adds one pulse, and send_now is what the loop
    # calls at spike rate.
    from biocam.interop.stimulator import BUFFER_DEPTHS

    assert BUFFER_DEPTHS == {"pulses": 64, "endpoints": 288, "timestamps": 1024}
    assert min(BUFFER_DEPTHS, key=BUFFER_DEPTHS.get) == "pulses"


def test_the_pulse_buffer_fills_first_under_a_closed_loop():
    # The arithmetic behind the warning. At the safety envelope's default
    # 10 Hz ceiling, the 64-deep pulse buffer is the binding one - reached in
    # about six seconds of sustained stimulation, long before the 1024-deep
    # timestamp buffer an earlier version of this code guarded instead.
    from biocam.interop.stimulator import BUFFER_DEPTHS
    from biocam.loop import DEFAULT_MAX_RATE_HZ

    seconds_to_fill = BUFFER_DEPTHS["pulses"] / DEFAULT_MAX_RATE_HZ
    assert seconds_to_fill < 10.0, (
        f"{seconds_to_fill:.1f} s of sustained closed-loop stimulation fills "
        "the smallest documented buffer")
