"""Command line for recording and conversion.

    python -m biocam.cli record --duration 60
    python -m biocam.cli record                     (until Ctrl+C)
    python -m biocam.cli convert in.raw in_meta.json out.h5

This module must NOT import biocam.interop at module scope. Doing so would pull
clr into any process that imports the CLI, and the suite would stop running on a
machine without the 3Brain DLLs. The import happens inside record_command().

record_command's stop handling closes two data-loss defects, both about
packets that were genuinely acquired (buffered in the source's queue) and
then thrown away instead of being written or counted:

FIX 1 - Ctrl+C used to retry record_session with the stop flag already set,
which broke out after writing a single packet and left everything else still
buffered (up to ~2 s worth - see QUEUE_BUFFER_SECONDS below) silently
discarded, with the sidecar still reporting "clean". The retry now passes
drain=True, which makes record_session consume the source to exhaustion -
writing every packet still buffered - bounded by session.DRAIN_DEADLINE_SEC
so a source that never stops yielding cannot hang the process; whatever is
still unread when that deadline elapses is counted into discarded_at_stop
rather than dropped.

FIX 2 - source.stop() used to sit in a `finally` outside the `with
RecordingWriter` block, so the sidecar was finalised while streaming was
still active: packets arriving in that window were buffered and never
drained, uncounted, and could still leave the sidecar reading "clean". Both
calls to record_session now pass stop_source=source.stop, which record_session
invokes - before it calls writer.finalise() - as soon as its loop ends, for
any reason. That stops new packets from arriving as early as this code can
manage; anything still sitting in the queue at that point is counted into
discarded_at_stop instead of being silently abandoned. The `finally: source.
stop()` below is a safety net for exit paths where stop_source never got a
chance to run, not the primary fix - by the time either record_session call
above returns or raises, stop() has normally already happened. HIGH 3: stop()
has no docstring and is deliberately *not* idempotent after a failure - it
leaves its internal _streaming flag True so a retry calls StopDataStreaming()
again rather than silently skipping it - so this second call is ordinarily a
no-op only in the sense that there is nothing left to stop, not because
calling it twice is guaranteed harmless; it can still raise, and that raise
is guarded (see the `finally` below) so it can never mask whatever exception,
if any, is already propagating out of this function.

HIGH 1 (Gate 1 final pass) - source.start() used to run before `with
RecordingWriter(...)`, so the driver was already streaming into a bounded
queue with nothing consuming it while the writer's own __enter__ performed
several filesystem syscalls (mkdir, open, a sidecar write via mkstemp/write/
os.replace). On a synced or antivirus-scanned volume, os.replace() alone can
take longer than the queue's own ~2 s budget (QUEUE_BUFFER_SECONDS below),
producing overflows at t=0 on an otherwise healthy run - loss caused entirely
by setup ordering, before a single second of real acquisition has happened.
record_command now enters `with RecordingWriter(...)` first and calls
source.start() only once that has succeeded, inside it. This is also better
on failure: a writer that cannot open its output file should never be able
to claim the stream in the first place - previously a RecordingWriter
failure after a successful source.start() left the driver streaming into a
queue that would now never be drained at all until the outer `finally`'s
source.stop() ran.
"""

import argparse
import queue
import shutil
import sys
import threading
import time
import warnings
from pathlib import Path

from biocam.data.events import DiskLow, DriverDataLoss, QueueOverflow, describe
from biocam.data.recording import AcquisitionParameters, RecordingWriter
from biocam.preflight import bytes_per_second, check_disk_space
from biocam.session import record_session

# Queue default: approximately two seconds of buffering (design spec §6). That
# reasoning is stated in packets-per-second, not in a fixed packet count - it
# only comes out to 2000 packets at the 1 ms default. Sizing the queue with a
# fixed packet count instead of this formula would silently redefine "two
# seconds" at any other --packet-ms: 10 ms packets would need only ~200 of
# them for two seconds.
QUEUE_BUFFER_SECONDS = 2.0

# Gate 1, item D: MAX_QUEUE_BYTES replaces a fixed packet-count floor
# (formerly MIN_QUEUE_SIZE = 100) that dominated _queue_size_for() above
# roughly a 20 ms packet period. Because the queue is sized in *packets* but
# a packet's byte size grows with packet_ms, a fixed packet floor makes the
# buffered *duration* - and worst-case memory - grow with packet_ms instead
# of staying bounded: 100 packets at 250 ms (the documented ceiling - see
# MAX_PACKET_MS below) is roughly 25 s of the full BioCAM DupleX config
# (4096 channels, 2 bytes/sample, ~18.5 kHz), about 3.8 GB - the exact
# multi-gigabyte outcome QUEUE_BUFFER_SECONDS's own reasoning above argues
# against, and it is reached only once the writer is already falling behind,
# which is the worst possible moment to be committing 3.8 GB. 512 MiB is
# generous headroom for a writer that is briefly slow, while being nowhere
# near "claims all available memory" territory.
MAX_QUEUE_BYTES = 512 * 1024 ** 2  # 512 MiB

# Packet-count floor, independent of MAX_QUEUE_BYTES: even at the longest
# documented packet period (250 ms), the queue should still hold a handful
# of packets rather than being sized down to almost nothing. 8 packets at
# 250 ms is already what QUEUE_BUFFER_SECONDS's duration formula gives on its
# own (2.0 s / 0.25 s = 8), so this floor is a safety net for callers outside
# the documented range, not a value that changes behaviour within it.
MIN_QUEUE_PACKETS = 8

# Documented Acquisition Time Period range - 3Brain BioCamDriverAPI v2.6
# Introduction, page 7: "The API can be set to operate with a custom
# Acquisition Time Period (ATP ...) between 1ms and 250ms." Anything above
# this is accepted by argparse's own int parsing but rejected by the driver
# only after StartDataStreaming has already claimed the device - refusing it
# here means the CLI never opens the device for a value the driver was never
# going to accept.
MAX_PACKET_MS = 250

# LOW: biocam/interop/source.py's POLL_INTERVAL_SEC = 0.001 only actually
# delivers ~1 ms sleeps on Python 3.11+, where time.sleep()'s underlying
# implementation on Windows was changed to use a higher-resolution timer. On
# 3.10 and earlier, time.sleep() rounds up to the ~15.6 ms system timer
# resolution - silently restoring, on an older interpreter, almost exactly
# the latency POLL_INTERVAL_SEC exists to remove, with no error or warning
# to say so. biocam/preflight.py's MIN_PYTHON is (3, 12), already above this
# floor, but preflight is opt-in (`python -m biocam.preflight`) and nothing
# previously stopped `python -m biocam.cli record` itself from running on an
# older interpreter and quietly recording with 15x the intended poll
# latency. record_command now refuses to start in that case.
MIN_PYTHON_FOR_POLL_PRECISION = (3, 11)


def _bytes_per_packet(params: AcquisitionParameters, packet_ms: int) -> float:
    """Expected payload size of one packet at the chosen acquisition period.

    Queue sizing happens before source.start(), so no real packet has been
    measured yet - this is the size the driver is expected to deliver:
    frame_rate_hz * packet_ms / 1000 frames per packet, each bytes_per_frame
    bytes.
    """
    return params.bytes_per_frame * params.frame_rate_hz * packet_ms / 1000.0


def _queue_size_for(packet_ms: int, bytes_per_packet: float) -> int:
    """Queue capacity in packets, bounded by both duration and bytes.

    Two independent ceilings, plus a floor:
      - `by_duration`: roughly QUEUE_BUFFER_SECONDS of packets at the chosen
        acquisition period - what governs at short packet periods, where
        each packet is small.
      - `by_bytes`: as many packets as fit under MAX_QUEUE_BYTES at the
        chosen packet size - what has to govern instead at long packet
        periods, where a fixed packet-count floor would otherwise let the
        buffered duration (and worst-case memory) grow without bound (Gate 1,
        item D).
      - MIN_QUEUE_PACKETS as a floor under `by_duration` only, so a very
        long packet period still buffers a few packets rather than being
        sized to (near-)zero.

    MEDIUM 7: the floor is applied to `by_duration` before the byte ceiling
    is imposed, not after - `max(MIN_QUEUE_PACKETS, min(by_duration,
    by_bytes))` (the previous form) applied the floor outside the byte
    bound, so a byte ceiling smaller than MIN_QUEUE_PACKETS worth of
    packets would have been overridden by the floor instead of capping the
    result. Computing `min(max(MIN_QUEUE_PACKETS, by_duration), by_bytes)`
    instead makes by_bytes a hard ceiling that always wins. This is latent
    within the documented 1-250 ms --packet-ms range at this device's data
    format - MAX_QUEUE_BYTES (512 MiB) never shrinks by_bytes below
    MIN_QUEUE_PACKETS anywhere in that range (see
    test_queue_size_never_exceeds_the_byte_ceiling_across_1_to_250ms in
    tests/test_cli.py) - but the ceiling must be provably hard, not merely
    true in practice for the range currently exercised.

    bytes_per_packet <= 0 (queue sizing runs before any real packet has
    been measured, so this is only reachable with a synthetic 0) disables
    the byte ceiling entirely rather than imposing a degenerate 0-byte one:
    there is no real per-packet size to bound against, so only the floored
    `by_duration` applies.
    """
    by_duration = int(QUEUE_BUFFER_SECONDS * 1000 / packet_ms)
    floored = max(MIN_QUEUE_PACKETS, by_duration)
    if bytes_per_packet > 0:
        by_bytes = int(MAX_QUEUE_BYTES / bytes_per_packet)
        return min(floored, by_bytes)
    return floored


def _packet_ms(value: str) -> int:
    """argparse type for --packet-ms: an integer in the documented range.

    argparse's own `type=int` accepts 0, negative values, and anything above
    the driver's documented ceiling. 0 would divide by zero in
    _queue_size_for(), a negative value would silently floor to
    MIN_QUEUE_PACKETS instead of being rejected, and a value above
    MAX_PACKET_MS would claim the device via BioCamDevice() and only then
    fail inside StartDataStreaming - defeating the point of validating here.
    Catching all of it in this type function means the CLI refuses before
    the device is even opened, with a message that names the valid range.
    """
    try:
        parsed = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"--packet-ms must be an integer between 1 and {MAX_PACKET_MS}, "
            f"got {value!r}")
    if parsed < 1 or parsed > MAX_PACKET_MS:
        raise argparse.ArgumentTypeError(
            f"--packet-ms must be between 1 and {MAX_PACKET_MS} ms "
            "(3Brain BioCamDriverAPI v2.6 Introduction, page 7), "
            f"got {parsed}")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="biocam")
    sub = parser.add_subparsers(dest="command", required=True)

    record = sub.add_parser("record", help="record from the instrument")
    record.add_argument("--duration", type=float, default=None,
                        help="seconds to record; omit to run until stopped")
    record.add_argument("--name", type=str, default=None,
                        help="base name for the output files")
    record.add_argument("--output-dir", type=str, default="recordings")
    record.add_argument(
        "--packet-ms", type=_packet_ms, default=2,
        help="acquisition period in milliseconds (integer, 1-250; default 2, "
             "matching 3Brain's own SampleApp_BioCamCL default "
             "(MainForm.Designer.cs) rather than the most aggressive "
             "documented setting - doubles the callback budget at no cost, "
             "reversible once issue #12 measures real callback latency)")

    convert = sub.add_parser("convert", help="convert a recording to HDF5")
    convert.add_argument("raw")
    convert.add_argument("meta")
    convert.add_argument("out")

    stim = sub.add_parser(
        "stim",
        help="send a stimulus, or plan one without an instrument (--dry-run)",
        description=(
            "Amplitudes are in the stimulator's unit (uA on the DupleX) and "
            "durations in microseconds. A pulse is refused rather than "
            "adjusted if the driver would alter it - see "
            "docs/api/stimulation-reference.md."
        ),
    )
    stim.add_argument("--amplitude", type=float, required=True,
                      help="first-phase amplitude, uA")
    stim.add_argument("--phase-us", type=float, required=True,
                      help="first-phase duration, microseconds")
    stim.add_argument("--gap-us", type=float, default=0.0,
                      help="inter-phase gap, microseconds (default 0)")
    stim.add_argument(
        "--amplitude2", type=float, default=None,
        help="second-phase amplitude, uA (default: the negative of "
             "--amplitude, giving a charge-balanced pulse)")
    stim.add_argument(
        "--phase2-us", type=float, default=None,
        help="second-phase duration, microseconds (default: same as "
             "--phase-us)")
    stim.add_argument(
        "--positive", type=_electrode_list, required=True,
        help="positive endpoints as 1-based row,col pairs separated by "
             "semicolons, e.g. '10,10' or '10,10;11,10'")
    stim.add_argument("--negative", type=_electrode_list, required=True,
                      help="negative endpoints, same format as --positive")
    stim.add_argument("--count", type=int, default=1,
                      help="number of pulses (default 1)")
    group = stim.add_mutually_exclusive_group()
    group.add_argument("--rate-hz", type=float, default=None,
                       help="train rate; requires --count above 1")
    group.add_argument("--period-us", type=float, default=None,
                       help="train period in microseconds; alternative to "
                            "--rate-hz")
    stim.add_argument(
        "--delay-us", type=float, default=0.0,
        help="when the train starts, in microseconds FROM THE BEGINNING OF "
             "THE ACQUISITION - not from now (see the reference document)")
    stim.add_argument(
        "--allow-unbalanced", action="store_true",
        help="permit a pulse that injects net charge. Sustained net charge "
             "drives electrolysis at the electrode; this is off by default "
             "for that reason")
    stim.add_argument(
        "--allow-short-period", action="store_true",
        help="permit a period below the driver's 1000 us minimum distance")
    stim.add_argument(
        "--no-column-rule", action="store_true",
        help="skip the check that positive and negative endpoints use "
             "different electrode columns")
    stim.add_argument(
        "--grid", type=_grid, default="64x64",
        help="MEA dimensions as ROWSxCOLS, for bounds-checking electrodes "
             "(default 64x64). ChCoord does not bounds-check, so this is the "
             "only place an out-of-array electrode is caught")
    stim.add_argument(
        "--dry-run", action="store_true",
        help="plan and print the stimulus without touching the instrument. "
             "Needs no BioCAM and no DLLs; use it to check a protocol before "
             "a lab session")
    stim.add_argument(
        "--time-resolution-us", type=int, default=None,
        help="stimulator clock period, for --dry-run only. On the instrument "
             "the real value is read from the device and this is ignored")
    stim.add_argument(
        "--amplitude-resolution", type=float, default=1.0,
        help="amplitude step, for --dry-run only (default 1.0)")
    stim.add_argument(
        "--max-amplitude", type=float, default=1000.0,
        help="amplitude limit, for --dry-run only (default 1000)")
    stim.add_argument(
        "--max-total-ticks", type=int, default=1000,
        help="MaxPulseDuration in ticks, for --dry-run only (default 1000)")

    return parser


def _electrode_list(value: str):
    """Parse '10,10;11,12' into a tuple of Electrodes.

    Coordinates are 1-based, matching ChCoord. Kept in the CLI rather than in
    biocam.stim because it is a text format, not a stimulation concept.
    """
    from biocam.stim import Electrode

    electrodes = []
    for chunk in value.split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        parts = chunk.split(",")
        if len(parts) != 2:
            raise argparse.ArgumentTypeError(
                f"{chunk!r} is not a row,col pair; expected e.g. '10,10' or "
                "'10,10;11,12'")
        try:
            row, col = (int(part) for part in parts)
        except ValueError:
            raise argparse.ArgumentTypeError(
                f"{chunk!r} has a non-integer coordinate") from None
        electrodes.append(Electrode(row, col))
    if not electrodes:
        raise argparse.ArgumentTypeError("no electrodes given")
    return tuple(electrodes)


def _grid(value: str):
    from biocam.stim import ElectrodeGrid

    parts = value.lower().split("x")
    if len(parts) != 2:
        raise argparse.ArgumentTypeError(
            f"{value!r} is not ROWSxCOLS, e.g. '64x64'")
    try:
        rows, cols = (int(part) for part in parts)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"{value!r} has a non-integer dimension") from None
    try:
        return ElectrodeGrid(rows, cols)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None


class _ConsolePrinter:
    """Bounded ring plus a daemon thread that owns every actual print().

    Critical: `report = lambda event: print(describe(event))` used to run
    print() itself on the consumer thread - the same thread that is the
    only thing draining record_session's packet queue (see
    QUEUE_BUFFER_SECONDS above). print() blocks under entirely ordinary
    operating conditions, not just exotic ones: on Windows, clicking inside
    a console window enables QuickEdit mode and suspends every further
    write to stdout until Enter is pressed; a full pipe, or a slow log
    collector reading the other end of stdout, has the same effect. An
    operator glancing at the console mid-recording - the most ordinary
    thing an operator does - could stall the drain and start dropping
    packets in the callback, silently, for no reason connected to the
    instrument at all.

    report() (called from the consumer thread, via RecordingWriter's and
    DriverPacketSource's listener parameters) only ever enqueues; it must
    never block or print. The daemon thread started in __init__ is the only
    thing that calls print(), and it does so off the consumer thread
    entirely. The queue is bounded (`maxsize`) and, mirroring the
    drop-and-count discipline biocam/interop/source.py already uses for its
    own packet queue (see that module's docstring on why not
    deque(maxlen=...)), a full ring drops the *new* event and counts it
    rather than blocking report() or silently evicting something already
    queued. Losing console output is acceptable; losing it without saying
    so is not - see `dropped` and, for the other way output can be lost,
    `print_failures`.
    """

    def __init__(self, maxsize: int = 1000):
        self._queue = queue.Queue(maxsize=maxsize)
        self._dropped = 0
        # MEDIUM 4: a full ring (`dropped`, above) is one way console output
        # is lost; a print() call that itself raises - a broken stdout, or
        # describe() meeting an event type it does not recognise - is
        # another. _run() used to catch that and pass with no counter
        # incremented at all, so `dropped` read 0 while output was still
        # being lost - silently contradicting this class's own docstring,
        # which says losing output without saying so is unacceptable.
        # print_failures counts that second case separately from `dropped`,
        # since they mean different things: `dropped` is "never attempted
        # because the ring was full", print_failures is "attempted and
        # print() itself failed".
        self._print_failures = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="biocam-printer", daemon=True)
        self._thread.start()

    def report(self, event) -> None:
        """The listener callback. Runs on the consumer thread - must never
        block, print, or otherwise do anything but enqueue."""
        try:
            self._queue.put_nowait(event)
        except queue.Full:
            self._dropped += 1

    def _run(self) -> None:
        """The daemon thread body. Never the consumer thread, never the
        driver's thread - it exists to keep print() off both."""
        while True:
            try:
                event = self._queue.get(timeout=0.2)
            except queue.Empty:
                if self._stop.is_set():
                    return
                continue
            try:
                print(describe(event))
            except Exception:
                # A broken stdout (or an event type describe() does not
                # recognise) must not kill the printer thread - later
                # events should still get a chance. Mirrors
                # RecordingWriter._emit()'s own listener-exception handling:
                # a console failure is not a reason to lose anything else.
                # MEDIUM 4: unlike that handling, this failure must still be
                # counted - see print_failures above.
                self._print_failures += 1

    def close(self, timeout: float = 2.0) -> None:
        """Stop the daemon thread once the ring is drained (or `timeout`
        elapses), so every event enqueued before this call is printed
        before the run reports its dropped count and exits."""
        self._stop.set()
        self._thread.join(timeout=timeout)

    @property
    def dropped(self) -> int:
        return self._dropped

    @property
    def print_failures(self) -> int:
        """Count of enqueued events print() itself failed on (MEDIUM 4) -
        distinct from `dropped` (events never attempted because the ring
        was full)."""
        return self._print_failures

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False


# finding 9 (Gate 1 final pass): the five _3Brain.Common DataFormat members
# _parameters_from() reads below. None appears in
# API/3Brain.BioCamDriver.xml - verified, zero occurrences, while FrameRate
# and NWells (also read there) each have exactly one - so all five are
# presumed inherited from _3Brain.Common, which ships no XML in this repo
# (see CLAUDE.md). Order matches _parameters_from().
_DATA_FORMAT_PROBE_MEMBERS = (
    "BitDepth", "ADCCountsToValue", "Offset", "MinDigitalValue",
    "MaxDigitalValue",
)


def _probe_data_format(data_format) -> list:
    """Read each undocumented DataFormat member individually and report it.

    finding 9: _parameters_from() below reads all five of these members in
    one block, immediately after the device is claimed and before a single
    packet has arrived. Because none of the five is documented in this repo
    (see _DATA_FORMAT_PROBE_MEMBERS above), an AttributeError on any one of
    them used to abort the whole session there - and a plain AttributeError
    names only the first member it happened to hit, leaving nothing to say
    about the other four, with the colleague 600 km away and nothing to
    report but a traceback.

    This probe reads every member individually so one failure cannot hide
    whether the others resolve, and returns one line per member
    unconditionally - not only the ones that fail - because issue #11 asks
    the colleague to compare these exact values against the known-good June
    recording, so the values themselves need to be visible on an ordinary,
    successful run too, not just a broken one. A dead session should return
    a diagnosis, not a stack trace.
    """
    lines = []
    for name in _DATA_FORMAT_PROBE_MEMBERS:
        try:
            value = getattr(data_format, name)
        except Exception as exc:
            lines.append(f"  {name}: FAILED - {exc!r}")
        else:
            lines.append(f"  {name}: {value!r}")
    return lines


def _parameters_from(data_format) -> AcquisitionParameters:
    return AcquisitionParameters(
        frame_rate_hz=data_format.FrameRate,
        total_channels=data_format.NWells * data_format.NChsPerWell,
        ch_sample_byte_size=data_format.ChSampleByteSize,
        bit_depth=data_format.BitDepth,
        adc_counts_to_value=data_format.ADCCountsToValue,
        offset=data_format.Offset,
        min_digital_value=data_format.MinDigitalValue,
        max_digital_value=data_format.MaxDigitalValue,
    )


def record_command(args) -> int:
    from biocam.interop.device import BioCamDevice
    from biocam.interop.source import DriverPacketSource

    # LOW: refuse to run below the Python version POLL_INTERVAL_SEC's ~1 ms
    # latency actually requires - see MIN_PYTHON_FOR_POLL_PRECISION above.
    if sys.version_info < MIN_PYTHON_FOR_POLL_PRECISION:
        required = ".".join(str(p) for p in MIN_PYTHON_FOR_POLL_PRECISION)
        actual = ".".join(str(p) for p in sys.version_info[:3])
        raise RuntimeError(
            f"biocam record requires Python {required}+ (found {actual}): "
            "biocam.interop.source.POLL_INTERVAL_SEC (1 ms) only delivers "
            "that resolution on Windows from Python 3.11 onward - on 3.10 "
            "and earlier, time.sleep() rounds up to the ~15.6 ms system "
            "timer, silently restoring the latency POLL_INTERVAL_SEC exists "
            "to remove."
        )

    base = args.name or time.strftime("%Y%m%d_%H%M%S")
    out_dir = Path(args.output_dir)
    raw_path = out_dir / f"{base}.raw"
    meta_path = out_dir / f"{base}_meta.json"

    stop = threading.Event()
    # Critical: printing must never happen on the consumer thread - see
    # _ConsolePrinter's docstring. report() only enqueues; printer's own
    # daemon thread is the only thing that calls print().
    #
    # LOW: printer.close() runs in the `finally` below, not via a
    # `with BioCamDevice() as device, printer:` combined statement. The
    # combined form does not guarantee cleanup here: if `BioCamDevice()`
    # itself (the constructor call, before __enter__ is even reached) were
    # to raise, printer's own __enter__/__exit__ would never run at all,
    # leaking its daemon thread with nothing left to stop it. A plain
    # try/finally around the whole body closes printer unconditionally
    # regardless of where or how the body fails.
    printer = _ConsolePrinter()
    report = printer.report

    try:
        with BioCamDevice() as device:
            # finding 9: probe every undocumented DataFormat member
            # individually, before _parameters_from() reads the same five
            # in one uninterruptible block - see _probe_data_format's
            # docstring. Printed directly, not through printer's bounded
            # ring: streaming has not started (source.start() has not been
            # called yet), so nothing here can compete with the callback -
            # the same reasoning that makes the end-of-run summary further
            # down safe to print directly once acquisition has stopped.
            data_format = device.data_format
            print(
                "DataFormat probe (finding 9 - members with no XML in "
                "this repo; compare against the known-good June recording "
                "per issue #11):",
                file=sys.stderr,
            )
            for line in _probe_data_format(data_format):
                print(line, file=sys.stderr)
            params = _parameters_from(data_format)

            if args.duration is not None:
                out_dir.mkdir(parents=True, exist_ok=True)
                rate = bytes_per_second(params.total_channels,
                                        params.ch_sample_byte_size,
                                        params.frame_rate_hz)
                space = check_disk_space(out_dir, args.duration, rate)
                if not space.ok:
                    free = shutil.disk_usage(out_dir).free
                    report(DiskLow(free_bytes=free,
                                   required_bytes=int(args.duration * rate)))
                    return 1

            queue_size = _queue_size_for(
                args.packet_ms, _bytes_per_packet(params, args.packet_ms))
            source = DriverPacketSource(device, queue_size=queue_size, listener=report)

            # HIGH 1: the writer is entered before the source is started -
            # see the module docstring. RecordingWriter.__enter__ performs
            # several filesystem syscalls (mkdir, open, a sidecar write via
            # mkstemp/write/os.replace); running source.start() ahead of
            # this used to let the driver stream into a bounded queue with
            # nothing consuming it while those syscalls ran. It also means
            # a writer that cannot open its output file can no longer claim
            # the stream in the first place.
            with RecordingWriter(raw_path, meta_path, params,
                                 listener=report) as writer:
                # Gate 1, item A: start() lives inside the try whose finally
                # calls source.stop(). A failure here - or from
                # record_session below - would otherwise leave source.stop()
                # uncalled: nothing else in the process would restore the
                # switch interval or unfreeze gc (see the equivalent fix
                # inside source.start() itself, in
                # biocam/interop/source.py). HIGH 3: stop() has no docstring
                # and is deliberately not idempotent after a failure - see
                # the module docstring's FIX 2 paragraph - so calling it
                # here even when start() itself is what failed is safe (it
                # is guarded below, so a failure cannot mask start()'s own
                # exception), not a guaranteed no-op.
                try:
                    source.start(packet_timespan_ms=args.packet_ms)
                    try:
                        result = record_session(source, writer,
                                                duration_sec=args.duration,
                                                stop_event=stop, counters=source,
                                                stop_source=source.stop)
                    except KeyboardInterrupt:
                        stop.set()
                        # The first call's `finally` already ran stop_source
                        # (source.stop()) before this KeyboardInterrupt
                        # reached us - see FIX 2 in the module docstring -
                        # so streaming is already stopped and the buffer can
                        # no longer grow. This retry drains what is now a
                        # finite backlog instead of discarding it on the
                        # spot - see FIX 1. counters is passed again
                        # (drain_deadline_exceeded is the only way this call
                        # can find anything still pending, and that is
                        # exactly what needs counting); stop_source is
                        # passed too so this call is correct even run on its
                        # own.
                        result = record_session(source, writer, drain=True,
                                                counters=source,
                                                stop_source=source.stop)
                finally:
                    # A safety net, not the primary fix: by the time either
                    # call to record_session above returns or raises,
                    # stop_source has already run, so this second call is
                    # usually a no-op; it only does real work if something
                    # prevented the stop_source call from ever happening
                    # (e.g. source.start() itself raised).
                    #
                    # HIGH 3: stop() has no docstring, and it deliberately
                    # leaves its internal _streaming flag True when
                    # StopDataStreaming fails, precisely so a retry calls it
                    # again instead of silently skipping it - so it is not
                    # idempotent in general, and this call can raise. Left
                    # unguarded, that raise would replace whatever exception
                    # is already propagating through this `finally` - e.g.
                    # the OSError from a full disk that the `with
                    # RecordingWriter` block above is unwinding from - with
                    # a confusing, unrelated one about the stream failing to
                    # stop. Same treatment as the sidecar write's own
                    # masking fix in RecordingWriter.__exit__: report it as
                    # a warning, never let it mask the original failure or
                    # (on a clean exit) become the only thing raised.
                    try:
                        source.stop()
                    except Exception as exc:
                        warnings.warn(
                            f"source.stop() failed during cleanup: {exc}",
                            RuntimeWarning,
                        )
    finally:
        printer.close()

    # MEDIUM 4/MEDIUM 5: everything below is an end-of-run summary written
    # to stderr, after printer.close() - i.e. after the recording itself is
    # already finished, sidecar and all. It bypasses the bounded ring/daemon
    # thread that exists specifically to keep print() off the consumer
    # thread during acquisition (see _ConsolePrinter's docstring); that
    # protection is no longer needed once acquisition has stopped, but
    # print() can still block here for the same reasons it always can
    # (QuickEdit, a full pipe, a slow log collector).
    #
    # Two groups, not one, and they are not interchangeable: queue_overflows/
    # driver_loss_events/callback_errors are genuinely part of the sidecar's
    # integrity block (RecordingWriter._write_sidecar), so a blocked or
    # unread console here is a lost convenience, never a lost record, for
    # those specifically. printer.dropped/print_failures and the gc delta
    # below are not written anywhere else - they describe this process's own
    # console/session, not the recording - so claiming they are "also in the
    # sidecar" would be exactly the kind of confidently wrong statement this
    # codebase exists to avoid making.
    sidecar_lines = []
    if source.queue_overflows:
        sidecar_lines.append(describe(QueueOverflow(total=source.queue_overflows)))
    if source.driver_loss_events:
        sidecar_lines.append(describe(DriverDataLoss(total=source.driver_loss_events)))
    if source.callback_errors:
        sidecar_lines.append(
            f"CALLBACK ERRORS: {source.callback_errors} exceptions raised "
            "inside a driver callback")

    session_only_lines = []
    if printer.dropped:
        session_only_lines.append(
            f"CONSOLE OUTPUT DROPPED: {printer.dropped} event(s) - printing "
            "lagged behind acquisition and the console ring was full; "
            "nothing was lost from the recording itself, only from what "
            "reached the screen")
    if printer.print_failures:
        # MEDIUM 4: the other way console output is lost - print() itself
        # raised (e.g. a broken stdout), not just a full ring.
        session_only_lines.append(
            f"CONSOLE PRINT FAILURES: {printer.print_failures} event(s) "
            "could not be printed (e.g. a broken stdout); nothing was lost "
            "from the recording itself, only from what reached the screen")

    # HIGH 2/HIGH 3: a real number in place of "we believe pythonnet does
    # not leak cycles" - see biocam/interop/source.py's module docstring.
    # getattr with a default, same reasoning as elsewhere on this path (e.g.
    # record_session's `counters` handling): a source without these
    # attributes (a test double, or any future non-driver source) must pass
    # through untouched rather than raise. Printed whenever a start()/stop()
    # cycle actually completed (both snapshots present and not None);
    # absent is not reported as a delta of zero, since that would
    # misrepresent "never measured" as "measured, no change".
    gc_counts_at_start = getattr(source, "gc_counts_at_start", None)
    gc_counts_at_stop = getattr(source, "gc_counts_at_stop", None)
    gc_objects_at_start = getattr(source, "gc_objects_at_start", None)
    gc_objects_at_stop = getattr(source, "gc_objects_at_stop", None)
    if (gc_counts_at_start is not None and gc_counts_at_stop is not None
            and gc_objects_at_start is not None and gc_objects_at_stop is not None):
        counts_delta = tuple(
            after - before for after, before in
            zip(gc_counts_at_stop, gc_counts_at_start))
        objects_delta = gc_objects_at_stop - gc_objects_at_start
        session_only_lines.append(
            "GC (informational, not a pass/fail check): tracked object "
            f"total changed by {objects_delta:+d} "
            f"({gc_objects_at_start} -> {gc_objects_at_stop}); "
            f"per-generation allocation counts changed by {counts_delta} "
            f"(from {gc_counts_at_start} to {gc_counts_at_stop})")

    if sidecar_lines or session_only_lines:
        print("End-of-run summary:", file=sys.stderr)
        if sidecar_lines:
            print("  (also recorded in the sidecar)", file=sys.stderr)
            for line in sidecar_lines:
                print(f"  {line}", file=sys.stderr)
        if session_only_lines:
            print("  (console/session-only - not part of the sidecar)",
                  file=sys.stderr)
            for line in session_only_lines:
                print(f"  {line}", file=sys.stderr)

    return 0 if result.verdict == "clean" else 2


def convert_command(args) -> int:
    from biocam.convert import main as convert_main
    return convert_main([args.raw, args.meta, args.out])


def _spec_from(args):
    """Build a PulseSpec from the parsed arguments.

    The second phase defaults to the mirror image of the first, because that
    is the charge-balanced pulse and it is what an experimenter almost always
    means. Asking for an unbalanced one should be deliberate.
    """
    from biocam.stim import PulseSpec

    amplitude2 = (
        -args.amplitude if args.amplitude2 is None else args.amplitude2
    )
    phase2_us = args.phase_us if args.phase2_us is None else args.phase2_us
    return PulseSpec(
        amplitude1=args.amplitude,
        phase1_us=args.phase_us,
        inter_us=args.gap_us,
        amplitude2=amplitude2,
        phase2_us=phase2_us,
        name="cli-pulse",
    )


def _plan_stimulus(args, constraints):
    """Plan a pulse or a train, whichever the arguments describe."""
    from biocam.stim import TrainSpec, plan, plan_train

    spec = _spec_from(args)
    balanced = not args.allow_unbalanced

    if args.count <= 1 and args.rate_hz is None and args.period_us is None:
        return plan(spec, constraints, require_charge_balance=balanced), None

    if args.rate_hz is not None:
        train = TrainSpec.at_rate(
            spec, count=args.count, rate_hz=args.rate_hz,
            delay_us=args.delay_us, name="cli-train")
    elif args.period_us is not None:
        train = TrainSpec(
            spec, count=args.count, period_us=args.period_us,
            delay_us=args.delay_us, name="cli-train")
    else:
        raise SystemExit(
            "--count above 1 needs --rate-hz or --period-us to say how fast "
            "the train repeats")

    train_plan = plan_train(
        train, constraints,
        require_charge_balance=balanced,
        allow_short_period=args.allow_short_period,
    )
    return train_plan.pulse_plan, train_plan


def stim_command(args) -> int:
    from biocam.stim import (
        PatternValidationError,
        PulseValidationError,
        StimConstraints,
        StimPattern,
        TrainValidationError,
        validate_pattern,
    )

    pattern = StimPattern(
        positive=args.positive, negative=args.negative, name="cli-pattern"
    )

    if args.dry_run:
        if args.time_resolution_us is None:
            print(
                "--dry-run needs --time-resolution-us: the stimulator's clock "
                "period decides how every duration is interpreted, and this "
                "machine cannot read it from the instrument. Do not guess it "
                "for a protocol you intend to run - read it from the device "
                "(it is reported by `biocam stim` without --dry-run) or from "
                "docs/api/stimulation-reference.md once the lab has confirmed "
                "it.",
                file=sys.stderr,
            )
            return 2
        constraints = StimConstraints(
            time_resolution_us=args.time_resolution_us,
            amplitude_resolution=args.amplitude_resolution,
            min_amplitude=-args.max_amplitude,
            max_amplitude=args.max_amplitude,
            max_total_ticks=args.max_total_ticks,
        )
    else:
        constraints = None  # read from the device below

    if constraints is None:
        # Imported here, never at module scope: importing biocam.interop
        # requires the 3Brain DLLs, and `biocam convert` must keep working on
        # a machine that has none.
        from biocam.interop.device import BioCamDevice
        from biocam.interop.stimulator import Stimulator

        with BioCamDevice() as device, Stimulator(
            device,
            grid=args.grid,
            enforce_column_rule=not args.no_column_rule,
        ) as stimulator:
            constraints = stimulator.constraints
            print(f"stimulator constraints: {constraints}")
            try:
                pulse_plan, train_plan = _plan_stimulus(args, constraints)
                validate_pattern(
                    pattern, args.grid,
                    enforce_column_rule=not args.no_column_rule)
            except (
                PulseValidationError,
                TrainValidationError,
                PatternValidationError,
            ) as exc:
                print(f"\nrefused: {exc}", file=sys.stderr)
                return 2

            print(f"\n{(train_plan or pulse_plan).describe()}")
            print(f"positive: {', '.join(str(e) for e in pattern.positive)}")
            print(f"negative: {', '.join(str(e) for e in pattern.negative)}")

            if train_plan is None:
                latency = stimulator.send_now(pulse_plan, pattern)
                print(f"\nsent. latency {latency} clock cycles "
                      "(relative to the beginning of the acquisition)")
            else:
                stimulator.send_scheduled(train_plan, pattern)
                print(f"\nqueued {train_plan.count} pulses. Timestamps are "
                      "relative to the beginning of the acquisition, so this "
                      "fires only if the acquisition has not passed them.")
        return 0

    # --dry-run: no device, no DLLs.
    try:
        pulse_plan, train_plan = _plan_stimulus(args, constraints)
        validate_pattern(
            pattern, args.grid, enforce_column_rule=not args.no_column_rule)
    except (
        PulseValidationError,
        TrainValidationError,
        PatternValidationError,
    ) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2

    print(f"planned against: {constraints}")
    print(f"\n{(train_plan or pulse_plan).describe()}")
    print(f"positive: {', '.join(str(e) for e in pattern.positive)}")
    print(f"negative: {', '.join(str(e) for e in pattern.negative)}")
    if train_plan is not None:
        shown = ", ".join(f"{t:g}" for t in train_plan.timestamps_us[:8])
        more = " ..." if train_plan.count > 8 else ""
        print(f"timestamps (us from start of acquisition): {shown}{more}")
        print(f"train net charge: {train_plan.net_charge_pc:+g} pC")
    print(
        "\nNOT SENT (--dry-run). The constraints above were supplied on the "
        "command line, not read from an instrument; if they differ from the "
        "device's, this plan is wrong."
    )
    return 0


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "record":
        return record_command(args)
    if args.command == "stim":
        return stim_command(args)
    return convert_command(args)


if __name__ == "__main__":
    raise SystemExit(main())
