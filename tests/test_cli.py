import pytest

from biocam.cli import (
    MAX_PACKET_MS, MAX_QUEUE_BYTES, MIN_QUEUE_PACKETS,
    _bytes_per_packet, _queue_size_for, build_parser, main,
)
from biocam.data.recording import AcquisitionParameters

# The full BioCAM DupleX config (4096 channels, 2 bytes/sample, ~18.5 kHz -
# the same figures used elsewhere in this codebase, e.g. recording.py's
# "~152 MB/s" and events.py's RecordingStarted example) - the highest data
# rate the queue byte ceiling has to hold up against.
FULL_DEVICE_PARAMS = AcquisitionParameters(
    frame_rate_hz=18557.720703125, total_channels=4096, ch_sample_byte_size=2,
    bit_depth=12, adc_counts_to_value=1.0, offset=0.0,
    min_digital_value=0, max_digital_value=4095,
)


def test_queue_size_at_the_1ms_default_matches_the_spec_figure():
    bpp = _bytes_per_packet(FULL_DEVICE_PARAMS, 1)
    assert _queue_size_for(1, bpp) == 2000


def test_queue_size_scales_down_for_a_longer_packet_period():
    bpp = _bytes_per_packet(FULL_DEVICE_PARAMS, 10)
    assert _queue_size_for(10, bpp) == 200


def test_queue_size_has_a_floor_for_a_very_long_packet_period():
    # bytes_per_packet=0 disables the byte ceiling (falls back to
    # by_duration), isolating MIN_QUEUE_PACKETS as the only thing that can
    # still be governing at an (out-of-range) very long packet period.
    assert _queue_size_for(5000, 0) == MIN_QUEUE_PACKETS


def test_queue_size_never_exceeds_the_byte_ceiling_across_1_to_250ms():
    """Gate 1, item D: across the full documented --packet-ms range, at the
    highest data rate this codebase reasons about, the queue must never be
    allowed to buffer more than MAX_QUEUE_BYTES - the exact multi-gigabyte
    outcome the old fixed packet-count floor (MIN_QUEUE_SIZE = 100) caused
    at long packet periods."""
    for packet_ms in range(1, MAX_PACKET_MS + 1):
        bpp = _bytes_per_packet(FULL_DEVICE_PARAMS, packet_ms)
        size = _queue_size_for(packet_ms, bpp)
        assert size * bpp <= MAX_QUEUE_BYTES, (
            f"packet_ms={packet_ms}: {size} packets x {bpp:.0f} bytes "
            f"exceeds MAX_QUEUE_BYTES")
        assert size >= MIN_QUEUE_PACKETS


def test_queue_size_at_250ms_no_longer_reaches_multi_gigabyte_territory():
    """The concrete regression this item closes: 250 ms used to floor at
    MIN_QUEUE_SIZE = 100 packets, which at the full-device packet size is
    roughly 3.8 GB. The new floor must not reproduce that."""
    bpp = _bytes_per_packet(FULL_DEVICE_PARAMS, 250)
    size = _queue_size_for(250, bpp)
    buffered_bytes = size * bpp
    assert buffered_bytes < MAX_QUEUE_BYTES
    assert buffered_bytes < 3.8 * 1024 ** 3


def test_parser_accepts_a_duration():
    args = build_parser().parse_args(["record", "--duration", "30"])
    assert args.duration == 30.0


def test_parser_accepts_run_until_stopped():
    args = build_parser().parse_args(["record"])
    assert args.duration is None


def test_parser_has_an_output_directory_default():
    args = build_parser().parse_args(["record"])
    assert args.output_dir == "recordings"


def test_parser_accepts_convert():
    args = build_parser().parse_args(["convert", "a.raw", "a_meta.json", "a.h5"])
    assert args.raw == "a.raw"
    assert args.out == "a.h5"


def test_parser_accepts_a_positive_packet_ms():
    args = build_parser().parse_args(["record", "--packet-ms", "10"])
    assert args.packet_ms == 10


def test_parser_has_a_packet_ms_default():
    # Gate 1, item E: 2 ms, matching 3Brain's own SampleApp_BioCamCL default
    # (MainForm.Designer.cs), not the most aggressive documented setting.
    args = build_parser().parse_args(["record"])
    assert args.packet_ms == 2


def test_parser_rejects_a_zero_packet_ms():
    """0 would divide by zero in _queue_size_for(); it must be refused at
    parse time, before the device is even opened."""
    with pytest.raises(SystemExit):
        build_parser().parse_args(["record", "--packet-ms", "0"])


def test_parser_rejects_a_negative_packet_ms():
    """A negative value used to fall through to _queue_size_for()'s floor
    silently; it must be refused instead."""
    with pytest.raises(SystemExit):
        build_parser().parse_args(["record", "--packet-ms", "-1"])


def test_parser_rejects_a_non_integer_packet_ms():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["record", "--packet-ms", "abc"])


def test_parser_accepts_the_documented_ceiling_of_250ms():
    args = build_parser().parse_args(["record", "--packet-ms", "250"])
    assert args.packet_ms == 250


def test_parser_rejects_a_packet_ms_above_the_documented_ceiling():
    """Gate 1, item C: the driver's documented range is 1-250 ms (3Brain
    BioCamDriverAPI v2.6 Introduction, page 7). 251 must be refused before
    the device is ever opened, not accepted here and then fail inside
    StartDataStreaming after the device has already been claimed."""
    with pytest.raises(SystemExit):
        build_parser().parse_args(["record", "--packet-ms", "251"])


def test_importing_the_cli_does_not_load_interop():
    """The guard's whole purpose: the CLI must be importable with no DLLs."""
    import sys
    assert "clr" not in sys.modules
    assert "pythonnet" not in sys.modules


def test_convert_command_runs_without_hardware(tmp_path):
    import numpy as np
    from biocam.data.recording import AcquisitionParameters, RecordingWriter

    params = AcquisitionParameters(
        frame_rate_hz=1000.0, total_channels=4, ch_sample_byte_size=2, bit_depth=12,
        adc_counts_to_value=1.0, offset=0.0, min_digital_value=0, max_digital_value=4095,
    )
    raw, meta = tmp_path / "r.raw", tmp_path / "r_meta.json"
    with RecordingWriter(raw, meta, params) as writer:
        writer.write_packet(1, 1, np.arange(8, dtype=np.uint16).tobytes())
        writer.finalise("duration_reached")

    exit_code = main(["convert", str(raw), str(meta), str(tmp_path / "r.h5")])
    assert exit_code == 0
    assert (tmp_path / "r.h5").exists()


def test_unknown_command_returns_an_error_code():
    with pytest.raises(SystemExit):
        main(["nonsense"])


def test_record_command_carries_driver_counters_into_the_sidecar(tmp_path, monkeypatch, capsys):
    """No test previously exercised record_command at all - which is how the
    ordering bug went unnoticed: the CLI used to call note_driver_loss() and
    note_queue_overflow() only after writer.finalise() had already written
    and closed the final sidecar, so those counts never reached disk. A fake
    device and a fake source with non-zero counters, run through the real
    CLI end to end, is what proves record_session's counters= parameter
    (session.py) actually gets the numbers onto the sidecar."""
    import numpy as np

    import biocam.interop.device as device_module
    import biocam.interop.source as source_module
    from biocam.data.recording import read_sidecar
    from biocam.data.replay import Packet

    class FakeDataFormat:
        FrameRate = 1000.0
        NWells = 1
        NChsPerWell = 4
        ChSampleByteSize = 2
        BitDepth = 12
        ADCCountsToValue = 1.0
        Offset = 0.0
        MinDigitalValue = 0
        MaxDigitalValue = 4095

    class FakeDevice:
        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

        @property
        def data_format(self):
            return FakeDataFormat()

    class FakeSource:
        """Stands in for DriverPacketSource: yields a few clean packets (no
        counter gaps) but reports non-zero driver-loss, queue-overflow and
        callback-error counts, the way a real driver session with real
        trouble - but no packet-counter gaps - would."""

        def __init__(self, device, queue_size=None, listener=None):
            self.driver_loss_events = 7
            self.queue_overflows = 3
            self.callback_errors = 1
            self._packets = [
                Packet(timestamp=i, counter=i,
                       payload=np.arange(4, dtype=np.uint16).tobytes())
                for i in range(5)
            ]

        def start(self, packet_timespan_ms=1):
            pass

        def stop(self):
            pass

        def __iter__(self):
            return iter(self._packets)

    monkeypatch.setattr(device_module, "BioCamDevice", FakeDevice)
    monkeypatch.setattr(source_module, "DriverPacketSource", FakeSource)

    exit_code = main(["record", "--output-dir", str(tmp_path), "--name", "fake"])

    meta = read_sidecar(tmp_path / "fake_meta.json")
    integrity = meta["integrity"]
    assert integrity["driver_loss_events"] == 7
    assert integrity["queue_overflows"] == 3
    assert integrity["callback_errors"] == 1
    assert integrity["verdict"] != "clean"
    assert exit_code == 2

    # Gate 1, item F: a run that dropped data says so on the console, not
    # only in the sidecar.
    console = capsys.readouterr().out
    assert "QUEUE OVERFLOW" in console and "3" in console
    assert "DRIVER DATA LOSS" in console and "7" in console
    assert "CALLBACK ERRORS: 1" in console


def test_record_command_drains_buffered_packets_on_keyboard_interrupt(tmp_path, monkeypatch):
    """FIX 1 / FIX 2, exercised through the real CLI: a Ctrl+C partway
    through streaming must not discard whatever was already buffered. The
    fake source raises KeyboardInterrupt after two packets, simulating the
    signal landing mid-loop, with three more packets still sitting in its
    internal buffer - exactly the shape of the bug (up to ~2 s of acquired,
    buffered signal thrown away). The retry record_command makes on
    KeyboardInterrupt must drain those three instead of losing them, and
    source.stop() must have been called before the sidecar was finalised."""
    import collections

    import numpy as np

    import biocam.interop.device as device_module
    import biocam.interop.source as source_module
    from biocam.data.recording import read_sidecar
    from biocam.data.replay import Packet

    class FakeDataFormat:
        FrameRate = 1000.0
        NWells = 1
        NChsPerWell = 4
        ChSampleByteSize = 2
        BitDepth = 12
        ADCCountsToValue = 1.0
        Offset = 0.0
        MinDigitalValue = 0
        MaxDigitalValue = 4095

    class FakeDevice:
        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

        @property
        def data_format(self):
            return FakeDataFormat()

    class FakeSource:
        """Stands in for DriverPacketSource across the interrupt/drain
        retry: the first __iter__ call yields two packets then raises
        KeyboardInterrupt (simulating Ctrl+C landing mid-stream), leaving
        three more packets in the internal buffer that only the drain
        retry's second __iter__ call ever reaches."""

        def __init__(self, device, queue_size=None, listener=None):
            self.driver_loss_events = 0
            self.queue_overflows = 0
            self.callback_errors = 0
            self._buffer = collections.deque(
                Packet(timestamp=i, counter=i,
                       payload=np.arange(4, dtype=np.uint16).tobytes())
                for i in range(5)
            )
            self._interrupted_once = False
            self.stop_calls = 0

        def pending_count(self):
            return len(self._buffer)

        def start(self, packet_timespan_ms=1):
            pass

        def stop(self):
            self.stop_calls += 1

        def __iter__(self):
            if not self._interrupted_once:
                self._interrupted_once = True
                return self._first_pass()
            return self._drain_pass()

        def _first_pass(self):
            yield self._buffer.popleft()
            yield self._buffer.popleft()
            raise KeyboardInterrupt

        def _drain_pass(self):
            while self._buffer:
                yield self._buffer.popleft()

    monkeypatch.setattr(device_module, "BioCamDevice", FakeDevice)
    monkeypatch.setattr(source_module, "DriverPacketSource", FakeSource)

    exit_code = main(["record", "--output-dir", str(tmp_path), "--name", "interrupted"])

    meta = read_sidecar(tmp_path / "interrupted_meta.json")
    # All 5 packets (1 frame each: 4 channels x 2 bytes = 8 bytes) survived.
    assert meta["n_frames_written"] == 5
    integrity = meta["integrity"]
    assert integrity["discarded_at_stop"] == 0
    assert meta["stop_reason"] == "source_exhausted"
    assert exit_code == 0
