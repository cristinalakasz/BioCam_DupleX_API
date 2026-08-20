"""Make a small synthetic recording, so the UI can be driven with no instrument.

    python tools/make_demo_recording.py demo

Writes `demo.raw` and `demo_meta.json`. The signal is not physiological and
is not meant to be: it exists so the window has packets to move, gaps to
report and a clock to advance. Anything read off it as science would be read
off a sine wave and some noise.
"""

import json
import sys
from pathlib import Path

import numpy as np

# Run as `python tools/make_demo_recording.py`, which puts tools/ on the path
# and not the repository root - so the self-check below could not import the
# detector it verifies with.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# The instrument's real sample rate, and a smaller array.
#
# That trade is deliberate and it went the other way first. A 4096-electrode
# demo at a reduced 1 kHz keeps the picture looking like the bench, but 1 kHz
# cannot represent a spike at all: a 0.8 ms waveform is 0.8 samples, and
# np.hanning(0) is an empty array. The generator duly reported "236 spikes
# planted" while planting nothing, and spike detection found nothing in a
# file built to demonstrate spike detection.
#
# The array size is cosmetic; the sample rate is not. So this is 32x32 at the
# real rate, and the window derives its grid from the channel count, so the
# array is still an array to click. The instrument is 64x64.
FRAME_RATE_HZ = 18557.720703125
N_CHANNELS = 1024        # 32 x 32; the DupleX is 64 x 64
SECONDS = 2.0


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in ("-h", "--help"):
        print(__doc__ or "")
        print("usage: python tools/make_demo_recording.py [prefix]")
        print()
        print("  prefix  what to name the pair of files (default: demo),")
        print("          producing <prefix>.raw and <prefix>_meta.json")
        return 0
    if argv and argv[0].startswith("-"):
        # Otherwise an unrecognised flag becomes the filename, and asking for
        # help writes a 76 MB file called --help.raw. Which it did.
        print(f"error: unknown option {argv[0]!r}. This takes a filename "
              "prefix, not flags; try --help.", file=sys.stderr)
        return 2
    stem = Path(argv[0]) if argv else Path("demo")
    n_frames = int(FRAME_RATE_HZ * SECONDS)

    rng = np.random.default_rng(20260817)
    side = int(round(N_CHANNELS ** 0.5))
    t = np.arange(n_frames) / FRAME_RATE_HZ

    # Per-electrode noise amplitude. Modest and fairly uniform, with a few
    # dead electrodes and a mild gradient.
    #
    # It used to vary by a factor of fifteen, with the "lively" patch being
    # the noisiest - and then the spikes were planted on that same patch,
    # where a 260-count waveform sat inside a 118-count noise floor and was
    # undetectable. On a real array a bright electrode is bright because it
    # has spikes on it, not because it has more noise, so the structure in
    # the picture now comes from the spikes.
    # 1-based row and column throughout, matching ChCoord and the window's
    # array display. The generator originally indexed 0-based here and
    # 1-based in its printed output, which put the spikes one row and one
    # column away from the electrodes it told the operator to click.
    rows, cols = np.mgrid[1:side + 1, 1:side + 1]
    centre = np.exp(-(((rows - 10) ** 2 + (cols - 13) ** 2) / 24.0))
    second = np.exp(-(((rows - 23) ** 2 + (cols - 20) ** 2) / 12.0))
    gain = (10.0 + 6.0 * centre + 4.0 * second).ravel()
    gain[rng.choice(N_CHANNELS, size=40, replace=False)] = 0.0   # dead

    # Spiking electrodes. Without these the file is noise of varying
    # amplitude, which the array display shows nicely and the spike detector
    # correctly finds nothing in - so the demo could not demonstrate the
    # feature it exists to teach.
    #
    # Two waveform shapes on each spiking electrode, deliberately: a narrow
    # deep one and a wide shallow one, so spike *sorting* has two units to
    # find rather than one. They are not models of real neurons; they are two
    # shapes far enough apart to see the sorters work.
    spiking = [(10, 13), (11, 13), (10, 14), (11, 12), (23, 20), (24, 21)]
    # Both must survive the 300 Hz high-pass the detector applies, or half
    # the "units" are invisible and sorting has one shape to work with. A
    # 2.2 ms waveform is already at the edge of what that filter passes; 1.4
    # is comfortably inside it and still clearly different from 0.8.
    narrow = -260.0 * np.hanning(int(0.0008 * FRAME_RATE_HZ))
    wide = -190.0 * np.hanning(int(0.0014 * FRAME_RATE_HZ))
    spike_times = {}
    for row, col in spiking:
        # The same mapping biocam.ui.arrayview.channel_index uses, and for
        # the same reason: a demo whose spikes are not where it says they are
        # teaches the operator that the display is wrong.
        channel = (row - 1) * side + (col - 1)
        times = []
        frame = int(rng.integers(200, 800))
        while frame < n_frames - len(wide) - 2:
            times.append((frame, rng.random() < 0.5))
            # Busy enough that two seconds gives each electrode well over
            # the twenty waveforms a sorter needs. A demo that cannot reach
            # the minimum only ever demonstrates the refusal.
            frame += int(rng.integers(0.008 * FRAME_RATE_HZ,
                                      0.05 * FRAME_RATE_HZ))
        spike_times[channel] = times

    data = np.empty((n_frames, N_CHANNELS), dtype=np.uint16)
    # Written in blocks: the whole array as float64 would be several GB.
    block = 500
    for start in range(0, n_frames, block):
        stop = min(start + block, n_frames)
        drift = 2048 + 120 * np.sin(2 * np.pi * 3.0 * t[start:stop])[:, None]
        chunk = drift + rng.normal(0, 1, (stop - start, N_CHANNELS)) * gain
        for channel, times in spike_times.items():
            for frame, is_narrow in times:
                shape = narrow if is_narrow else wide
                if start <= frame < stop - len(shape):
                    at = frame - start
                    chunk[at:at + len(shape), channel] += (
                        shape * rng.normal(1.0, 0.06))
        np.clip(chunk, 0, 4095, out=chunk)
        data[start:stop] = chunk.astype(np.uint16)
    data.tofile(stem.with_suffix(".raw"))

    planted = sum(len(v) for v in spike_times.values())
    print(f"  {planted} spikes planted on {len(spiking)} electrodes "
          f"(two waveform shapes each, so sorting has something to find)")
    print("  spiking electrodes (1-based row,col): "
          + ", ".join(f"{r},{c}" for r, c in spiking))

    _check_the_spikes_are_findable(data, spike_times, planted)

    meta = {
        "frame_rate_hz": FRAME_RATE_HZ,
        "total_channels": N_CHANNELS,
        "ch_sample_byte_size": 2,
        "bit_depth": 12,
        "adc_counts_to_value": 2.0146520146520146,
        "offset": -4125.0,
        "min_digital_value": 0,
        "max_digital_value": 4095,
        "note": ("Synthetic. Generated by tools/make_demo_recording.py for "
                 "driving the UI without an instrument. Not real signal, and "
                 "the frame rate is 1 kHz rather than the instrument's "
                 "18.5 kHz so that a 4096-electrode demo stays a sane size."),
    }
    meta_path = stem.with_name(stem.stem + "_meta.json")
    meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")

    size_mb = stem.with_suffix(".raw").stat().st_size / 1e6
    print(f"{stem.with_suffix('.raw')}  ({size_mb:.1f} MB, {n_frames:,} frames, "
          f"{N_CHANNELS} channels, {SECONDS:g} s)")
    print(f"{meta_path}")
    print()
    print("Drive the window with it:")
    print(f"  python -m biocam.ui --replay {stem.with_suffix('.raw')} "
          f"--meta {meta_path}")
    return 0


def _check_the_spikes_are_findable(data, spike_times, planted):
    """Run the real detector over what was just written, and say what it found.

    A generator that reports planting spikes while planting empty arrays is
    exactly what happened here once, and nothing downstream noticed until a
    whole UI session came back with zero detections. This is cheap and it
    makes the file self-verifying: if the numbers below are far apart, the
    demo is wrong and says so rather than teaching someone that detection
    does not work.
    """
    from biocam.analysis.spikes import SpikeDetector

    channels = sorted(spike_times)
    block = data[:, channels].astype(np.float64)
    detector = SpikeDetector(len(channels), FRAME_RATE_HZ,
                             threshold_sigmas=5.0)
    found = 0
    for start in range(0, block.shape[0], 512):
        found += len(detector.detect(block[start:start + 512]))
    print(f"  the detector finds {found} of them at 5 sigma")
    if found < 0.8 * planted:
        print("  WARNING: far fewer found than planted - this demo cannot "
              "demonstrate spike detection. Check the sample rate against "
              "the waveform widths.", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
