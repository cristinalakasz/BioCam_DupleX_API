import threading
import time

import pytest

from biocam.cli import (
    MAX_PACKET_MS, MAX_QUEUE_BYTES, MIN_QUEUE_PACKETS,
    _bytes_per_packet, _probe_data_format, _queue_size_for, _ConsolePrinter,
    build_parser, main,
)
from biocam.data.events import RecordingStarted
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


def test_probe_data_format_reports_every_member_when_all_resolve():
    # finding 9: a normal, successful run must still print every value -
    # issue #11 asks the colleague to compare these against the known-good
    # June recording, so they need to be visible even when nothing failed.
    class FakeDataFormat:
        BitDepth = 12
        ADCCountsToValue = 1.0
        Offset = 0.0
        MinDigitalValue = 0
        MaxDigitalValue = 4095

    lines = _probe_data_format(FakeDataFormat())

    assert len(lines) == 5
    assert any("BitDepth: 12" in line for line in lines)
    assert any("ADCCountsToValue: 1.0" in line for line in lines)
    assert any("Offset: 0.0" in line for line in lines)
    assert any("MinDigitalValue: 0" in line for line in lines)
    assert any("MaxDigitalValue: 4095" in line for line in lines)
    assert not any("FAILED" in line for line in lines)


def test_probe_data_format_isolates_one_failing_member_from_the_rest():
    # finding 9: an AttributeError on one member (BitDepth is deliberately
    # missing here) must not stop the probe from reading - and reporting -
    # the other four. A plain, unguarded attribute-block read (the old
    # _parameters_from() behaviour) would have aborted on the first miss and
    # said nothing about the rest.
    class PartialDataFormat:
        # BitDepth intentionally absent.
        ADCCountsToValue = 1.0
        Offset = 0.0
        MinDigitalValue = 0
        MaxDigitalValue = 4095

    lines = _probe_data_format(PartialDataFormat())

    assert len(lines) == 5
    failed = [line for line in lines if "FAILED" in line]
    assert len(failed) == 1
    assert "BitDepth" in failed[0]
    assert any("ADCCountsToValue: 1.0" in line for line in lines)
    assert any("Offset: 0.0" in line for line in lines)
    assert any("MinDigitalValue: 0" in line for line in lines)
    assert any("MaxDigitalValue: 4095" in line for line in lines)


def test_record_command_prints_the_data_format_probe_before_parameters_from(
        tmp_path, monkeypatch, capsys):
    # finding 9, exercised through the real CLI: the probe must run and
    # print unconditionally, before _parameters_from() reads the same
    # members - so a colleague sees the values (or the one that failed)
    # even on a session that never gets past device-claim.
    import numpy as np

    import biocam.interop.device as device_module
    import biocam.interop.source as source_module
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
        def __init__(self, device, queue_size=None, listener=None):
            self.driver_loss_events = 0
            self.queue_overflows = 0
            self.callback_errors = 0
            self._packets = [
                Packet(timestamp=0, counter=0,
                       payload=np.arange(4, dtype=np.uint16).tobytes())
            ]

        def start(self, packet_timespan_ms=1):
            pass

        def stop(self):
            pass

        def __iter__(self):
            return iter(self._packets)

    monkeypatch.setattr(device_module, "BioCamDevice", FakeDevice)
    monkeypatch.setattr(source_module, "DriverPacketSource", FakeSource)

    main(["record", "--output-dir", str(tmp_path), "--name", "probe"])

    console = capsys.readouterr().err
    assert "DataFormat probe" in console
    assert "BitDepth: 12" in console
    assert "ADCCountsToValue: 1.0" in console
    assert "Offset: 0.0" in console
    assert "MinDigitalValue: 0" in console
    assert "MaxDigitalValue: 4095" in console
    # The probe's own banner must appear before the values it produced.
    assert console.index("DataFormat probe") < console.index("BitDepth: 12")


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
    # only in the sidecar. MEDIUM 5: the end-of-run summary runs after the
    # printer has closed and writes to stderr, not stdout - see
    # record_command's summary_lines block in cli.py.
    console = capsys.readouterr().err
    assert "QUEUE OVERFLOW" in console and "3" in console
    assert "DRIVER DATA LOSS" in console and "7" in console
    assert "CALLBACK ERRORS: 1" in console
    assert "also recorded in the sidecar" in console


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


# --- Critical: printing must never happen on the consumer thread ---

def test_console_printer_prints_off_the_calling_thread(monkeypatch):
    """report() enqueues; only the printer's own daemon thread calls
    print(). If report() printed directly, print() would run on this
    test's thread instead."""
    caller_thread = threading.current_thread()
    seen_threads = []
    real_print = print

    def spying_print(*args, **kwargs):
        seen_threads.append(threading.current_thread())
        real_print(*args, **kwargs)

    monkeypatch.setattr("biocam.cli.print", spying_print, raising=False)

    printer = _ConsolePrinter()
    assert printer._thread.daemon
    assert printer._thread is not caller_thread

    printer.report(RecordingStarted(path="x.raw", total_channels=4,
                                    frame_rate_hz=1000.0))
    printer.close()

    assert seen_threads
    assert seen_threads[0] is not caller_thread
    assert seen_threads[0] is printer._thread


def test_console_printer_report_never_blocks_when_the_ring_is_full():
    # maxsize=1 with the daemon thread's own _run replaced by a no-op means
    # nothing ever drains the queue, so the second report() call must find
    # the ring full and drop-and-count rather than block.
    printer = _ConsolePrinter(maxsize=1)
    printer._stop.set()
    printer._thread.join(timeout=2.0)  # stop the real daemon from draining

    event = RecordingStarted(path="x.raw", total_channels=4, frame_rate_hz=1000.0)
    printer.report(event)  # fills the ring (maxsize=1)

    start = time.monotonic()
    printer.report(event)  # must drop, not block
    elapsed = time.monotonic() - start

    assert elapsed < 1.0
    assert printer.dropped == 1


def test_console_printer_close_drains_pending_events_before_returning(capsys):
    printer = _ConsolePrinter(maxsize=10)
    for i in range(5):
        printer.report(RecordingStarted(path=f"{i}.raw", total_channels=4,
                                        frame_rate_hz=1000.0))
    printer.close()

    out = capsys.readouterr().out
    assert out.count("\n") == 5
    assert printer.dropped == 0


def test_a_failing_source_stop_in_the_outer_finally_does_not_mask_the_real_exception(
        tmp_path, monkeypatch):
    """HIGH 3: source.stop() has no docstring and is deliberately not
    idempotent after a failure - a retry (the outer `finally: source.stop()`
    safety net) can raise. Left unguarded, that raise would replace whatever
    exception is already propagating - here, an OSError simulating a full
    disk during acquisition - with a confusing, unrelated one about the
    stream failing to stop. The real failure must still be what the caller
    sees; the stop failure must be reported, not silently lost either."""
    import biocam.interop.device as device_module
    import biocam.interop.source as source_module

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
        """Raises a plain OSError partway through streaming (simulating a
        full disk), and fails every call to stop() too - the scenario
        Critical review flagged: an unguarded second source.stop() call in
        cli.py's outer `finally` would otherwise mask the OSError."""

        def __init__(self, device, queue_size=None, listener=None):
            self.driver_loss_events = 0
            self.queue_overflows = 0
            self.callback_errors = 0

        def pending_count(self):
            return 0

        def start(self, packet_timespan_ms=1):
            pass

        def stop(self):
            raise RuntimeError("StopDataStreaming failed.")

        def __iter__(self):
            raise OSError("disk full")
            yield  # pragma: no cover - makes this a generator function

    monkeypatch.setattr(device_module, "BioCamDevice", FakeDevice)
    monkeypatch.setattr(source_module, "DriverPacketSource", FakeSource)

    with pytest.warns(RuntimeWarning, match="source.stop\\(\\) failed"):
        with pytest.raises(OSError, match="disk full"):
            main(["record", "--output-dir", str(tmp_path), "--name", "diskfull"])


# --- HIGH 1: the writer must claim its output file before the source starts ---

def test_record_command_enters_the_writer_before_starting_the_source(tmp_path, monkeypatch):
    """HIGH 1: previously source.start() ran before `with RecordingWriter(...)`,
    so the driver could already be streaming into a bounded queue with
    nothing consuming it while the writer's __enter__ performed several
    filesystem syscalls (mkdir, open, a sidecar write). The writer must now
    be entered first, and the source started only once that has succeeded."""
    import biocam.cli as cli_module
    import biocam.interop.device as device_module
    import biocam.interop.source as source_module
    from biocam.data.recording import RecordingWriter

    calls = []

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
        def __init__(self, device, queue_size=None, listener=None):
            self.driver_loss_events = 0
            self.queue_overflows = 0
            self.callback_errors = 0

        def start(self, packet_timespan_ms=1):
            calls.append("source_started")

        def stop(self):
            calls.append("source_stopped")

        def __iter__(self):
            return iter([])

    class OrderTrackingWriter(RecordingWriter):
        def __enter__(self):
            calls.append("writer_entered")
            return super().__enter__()

    monkeypatch.setattr(device_module, "BioCamDevice", FakeDevice)
    monkeypatch.setattr(source_module, "DriverPacketSource", FakeSource)
    monkeypatch.setattr(cli_module, "RecordingWriter", OrderTrackingWriter)

    main(["record", "--output-dir", str(tmp_path), "--name", "order"])

    assert "writer_entered" in calls and "source_started" in calls
    assert calls.index("writer_entered") < calls.index("source_started")


# --- LOW: printer.close() must run even if BioCamDevice() itself raises ---

def test_printer_is_closed_even_if_biocamdevice_construction_raises(tmp_path, monkeypatch):
    """LOW: a combined `with BioCamDevice() as device, printer:` statement
    would never call printer.close() if BioCamDevice() itself (construction,
    not __enter__) raised, since printer's own context-manager protocol
    would never begin - leaking its daemon thread. record_command now closes
    printer in an unconditional `finally` instead."""
    import biocam.cli as cli_module
    import biocam.interop.device as device_module

    close_calls = []
    real_close = cli_module._ConsolePrinter.close

    def spying_close(self, timeout=2.0):
        close_calls.append(True)
        return real_close(self, timeout)

    monkeypatch.setattr(cli_module._ConsolePrinter, "close", spying_close)

    class ExplodingDevice:
        def __init__(self, *args, **kwargs):
            raise RuntimeError("device construction blew up")

    monkeypatch.setattr(device_module, "BioCamDevice", ExplodingDevice)

    with pytest.raises(RuntimeError, match="device construction blew up"):
        main(["record", "--output-dir", str(tmp_path), "--name", "explode"])

    assert close_calls == [True]


# --- LOW: refuse to run below the Python version POLL_INTERVAL_SEC needs ---

def test_record_command_refuses_to_run_below_the_required_python_version(
        tmp_path, monkeypatch):
    """LOW: biocam.interop.source.POLL_INTERVAL_SEC only delivers its
    intended ~1 ms polling latency on Windows from Python 3.11 onward - on
    3.10 and earlier, time.sleep() rounds up to the ~15.6 ms system timer.
    record_command must refuse outright rather than silently recording with
    ~15x the intended poll latency."""
    import biocam.cli as cli_module

    monkeypatch.setattr(cli_module.sys, "version_info", (3, 10, 5))

    with pytest.raises(RuntimeError, match="requires Python 3.11"):
        main(["record", "--output-dir", str(tmp_path)])


# --- MEDIUM 4: print() itself failing must be counted, distinct from `dropped` ---

def test_console_printer_counts_print_failures_separately_from_dropped(monkeypatch):
    """MEDIUM 4: _run() used to catch a print() failure (e.g. a broken
    stdout) and pass with nothing incremented, so `dropped` read 0 while
    output was still being lost. print_failures must count that case
    separately from `dropped` (a full ring, never attempted)."""
    def raising_print(*args, **kwargs):
        raise OSError("broken stdout")

    monkeypatch.setattr("biocam.cli.print", raising_print, raising=False)

    printer = _ConsolePrinter()
    printer.report(RecordingStarted(path="x.raw", total_channels=4, frame_rate_hz=1000.0))
    printer.close()

    assert printer.print_failures == 1
    assert printer.dropped == 0


# --- HIGH 2/HIGH 3: the GC measurement delta reaches the end-of-run summary ---

def test_record_command_reports_the_gc_measurement_delta_on_stderr(
        tmp_path, monkeypatch, capsys):
    """HIGH 2/HIGH 3: cli.py must report the source's start()/stop() gc
    measurement delta at the end of a run when the source exposes it -
    turning "we believe pythonnet does not leak cycles" into a number a
    colleague can report from a real run, instead of an assumption. Printed
    on stderr (MEDIUM 5), alongside the rest of the end-of-run summary."""
    import biocam.interop.device as device_module
    import biocam.interop.source as source_module

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
        def __init__(self, device, queue_size=None, listener=None):
            self.driver_loss_events = 0
            self.queue_overflows = 0
            self.callback_errors = 0
            self.gc_counts_at_start = (10, 2, 1)
            self.gc_counts_at_stop = (15, 3, 1)
            self.gc_objects_at_start = 1000
            self.gc_objects_at_stop = 1050

        def start(self, packet_timespan_ms=1):
            pass

        def stop(self):
            pass

        def __iter__(self):
            return iter([])

    monkeypatch.setattr(device_module, "BioCamDevice", FakeDevice)
    monkeypatch.setattr(source_module, "DriverPacketSource", FakeSource)

    main(["record", "--output-dir", str(tmp_path), "--name", "gcdelta"])

    err = capsys.readouterr().err
    assert "GC (informational" in err
    assert "+50" in err  # object total delta: 1050 - 1000
    # The gc delta is not written anywhere else - it must not be claimed as
    # part of the sidecar record, only labelled as console/session-only.
    assert "console/session-only" in err
