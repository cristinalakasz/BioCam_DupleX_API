"""Command line for recording and conversion.

    python -m biocam.cli record --duration 60
    python -m biocam.cli record                     (until Ctrl+C)
    python -m biocam.cli convert in.raw in_meta.json out.h5

This module must NOT import biocam.interop at module scope. Doing so would pull
clr into any process that imports the CLI, and the suite would stop running on a
machine without the 3Brain DLLs. The import happens inside record_command().
"""

import argparse
import shutil
import threading
import time
from pathlib import Path

from biocam.data.events import DiskLow, describe
from biocam.data.recording import AcquisitionParameters, RecordingWriter
from biocam.preflight import bytes_per_second, check_disk_space
from biocam.session import record_session

# Queue default: approximately two seconds of buffering (design spec §6). That
# reasoning is stated in packets-per-second, not in a fixed packet count - it
# only comes out to 2000 packets at the 1 ms default. Sizing the queue with a
# fixed packet count instead of this formula would silently redefine "two
# seconds" at any other --packet-ms: 10 ms packets would need only ~200 of
# them for two seconds, while a fixed 2000-packet queue at 10 ms packets would
# ask for roughly 3 GB. MIN_QUEUE_SIZE keeps a long packet period (few
# packets, but a burst can still arrive) from being sized down to something
# that overflows immediately.
QUEUE_BUFFER_SECONDS = 2.0
MIN_QUEUE_SIZE = 100


def _queue_size_for(packet_ms: int) -> int:
    """Queue capacity for roughly QUEUE_BUFFER_SECONDS of packets at the
    chosen acquisition period, with a floor."""
    return max(MIN_QUEUE_SIZE, int(QUEUE_BUFFER_SECONDS * 1000 / packet_ms))


def _packet_ms(value: str) -> int:
    """argparse type for --packet-ms: a positive integer.

    argparse's own `type=int` accepts 0 and negative values - 0 would divide
    by zero in _queue_size_for(), and a negative value would silently floor
    to MIN_QUEUE_SIZE instead of being rejected. Catching that here means the
    CLI refuses before the device is even opened, with a message that names
    the valid range, instead of dying inside queue sizing or accepting
    nonsense.
    """
    try:
        parsed = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"--packet-ms must be a positive integer (>= 1), got {value!r}")
    if parsed < 1:
        raise argparse.ArgumentTypeError(
            f"--packet-ms must be a positive integer (>= 1), got {parsed}")
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
    record.add_argument("--packet-ms", type=_packet_ms, default=1,
                        help="acquisition period in milliseconds (positive integer, >= 1)")

    convert = sub.add_parser("convert", help="convert a recording to HDF5")
    convert.add_argument("raw")
    convert.add_argument("meta")
    convert.add_argument("out")

    return parser


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

    base = args.name or time.strftime("%Y%m%d_%H%M%S")
    out_dir = Path(args.output_dir)
    raw_path = out_dir / f"{base}.raw"
    meta_path = out_dir / f"{base}_meta.json"

    stop = threading.Event()
    report = lambda event: print(describe(event))

    with BioCamDevice() as device:
        params = _parameters_from(device.data_format)

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

        source = DriverPacketSource(device, queue_size=_queue_size_for(args.packet_ms),
                                    listener=report)
        source.start(packet_timespan_ms=args.packet_ms)
        try:
            with RecordingWriter(raw_path, meta_path, params,
                                 listener=report) as writer:
                try:
                    result = record_session(source, writer,
                                            duration_sec=args.duration,
                                            stop_event=stop, counters=source)
                except KeyboardInterrupt:
                    stop.set()
                    # counters is intentionally omitted on this retry.
                    # record_session's `finally` already transferred it from
                    # `source` onto `writer` before this KeyboardInterrupt
                    # reached us - that guarantee is what closes this path
                    # against the same bug for every exit, not just a clean
                    # one. Passing counters=source again here would add the
                    # same cumulative totals from `source` a second time on
                    # top of a writer that already has them.
                    result = record_session(source, writer, stop_event=stop)
        finally:
            source.stop()

    if source.callback_errors:
        print(f"CALLBACK ERRORS: {source.callback_errors} exceptions raised "
              "inside a driver callback")

    return 0 if result.verdict == "clean" else 2


def convert_command(args) -> int:
    from biocam.convert import main as convert_main
    return convert_main([args.raw, args.meta, args.out])


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "record":
        return record_command(args)
    return convert_command(args)


if __name__ == "__main__":
    raise SystemExit(main())
