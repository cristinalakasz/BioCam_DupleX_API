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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="biocam")
    sub = parser.add_subparsers(dest="command", required=True)

    record = sub.add_parser("record", help="record from the instrument")
    record.add_argument("--duration", type=float, default=None,
                        help="seconds to record; omit to run until stopped")
    record.add_argument("--name", type=str, default=None,
                        help="base name for the output files")
    record.add_argument("--output-dir", type=str, default="recordings")
    record.add_argument("--packet-ms", type=int, default=1,
                        help="acquisition period in milliseconds")

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

        source = DriverPacketSource(device, listener=report)
        source.start(packet_timespan_ms=args.packet_ms)
        try:
            with RecordingWriter(raw_path, meta_path, params,
                                 listener=report) as writer:
                try:
                    result = record_session(source, writer,
                                            duration_sec=args.duration,
                                            stop_event=stop)
                except KeyboardInterrupt:
                    stop.set()
                    result = record_session(source, writer, stop_event=stop)
                writer.note_driver_loss(source.driver_loss_events)
                writer.note_queue_overflow(source.queue_overflows)
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
