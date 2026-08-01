"""
recorder.py
===========
Usa BioCamConnector per registrare dati dal BioCAM DupleX.

Ogni registrazione apre una connessione pulita e la chiude completamente
al termine, liberando il controllo del device.

Uso da riga di comando:
    python recorder.py --duration 10 --name test1
    python recorder.py --duration 60 --name baseline --output-dir C:/dati

Uso programmatico:
    from connector import BioCamConnector
    from recorder import record, load_recording

    record(duration_sec=10, name="esperimento1")

    data, meta = load_recording("recordings/esperimento1.raw",
                                "recordings/esperimento1_meta.json")
    print(data.shape)  # (n_frames, n_canali_totali)
"""

import os
import json
import time
import argparse

import numpy as np

from connector import BioCamConnector, decode_payload


# ---------------------------------------------------------------------------
# Recorder
# ---------------------------------------------------------------------------

class BioCamRecorder:
    """
    Gestisce una singola sessione di registrazione.
    Il BioCamConnector viene aperto e chiuso interamente dentro record().
    """

    def __init__(self, output_dir: str = "recordings"):
        self.output_dir = output_dir
        self.raw_path   = None
        self.meta_path  = None

    def record(self, duration_sec: float, name: str = None,
               data_packet_timespan_ms: int = None):
        """
        Apre connessione, registra duration_sec secondi, chiude tutto.

        Parametri
        ----------
        duration_sec            : durata registrazione in secondi
        name                    : nome base file output (default: timestamp)
        data_packet_timespan_ms : intervallo pacchetti ms (None = default driver)

        Restituisce
        -----------
        (raw_path, meta_path) — percorsi dei file salvati
        """
        os.makedirs(self.output_dir, exist_ok=True)
        base_name = name or time.strftime("%Y%m%d_%H%M%S")
        self.raw_path  = os.path.join(self.output_dir, f"{base_name}.raw")
        self.meta_path = os.path.join(self.output_dir, f"{base_name}_meta.json")

        n_frames_written = 0
        packet_log       = []
        error            = None
        done             = threading.Event()

        # Apre connessione — il with garantisce disconnect anche in caso di errore
        with BioCamConnector() as conn:
            df = conn.biocam.DataFormat
            print(f"[recorder] Sampling rate  : {df.FrameRate} Hz")
            print(f"[recorder] Canali totali  : {df.NWells * df.NChsPerWell}")
            print(f"[recorder] Bit depth      : {df.BitDepth} bit")

            start_time = time.time()

            with open(self.raw_path, "wb") as raw_file:

                def _on_data(event_args):
                    nonlocal n_frames_written, error
                    if done.is_set():
                        return
                    try:
                        arr = decode_payload(df, event_args)
                        raw_file.write(arr.tobytes())
                        packet_log.append({
                            "timestamp"    : float(time.time() - start_time),
                            "frame_offset" : n_frames_written,
                            "n_frames"     : int(arr.shape[0]),
                        })
                        n_frames_written += arr.shape[0]
                    except Exception as e:
                        error = e
                        print(f"[recorder] ERRORE decodifica: {e}")

                    if (time.time() - start_time) >= duration_sec:
                        done.set()

                conn.start_streaming(_on_data, data_packet_timespan_ms)
                print(f"[recorder] Registrazione avviata -> {self.raw_path}")
                print(f"[recorder] Durata: {duration_sec}s  (Ctrl+C per interrompere)")

                try:
                    done.wait(timeout=duration_sec + 5)
                except KeyboardInterrupt:
                    print("[recorder] Interruzione manuale.")
                finally:
                    conn.stop_streaming()

            # conn.disconnect() chiamato automaticamente dall'__exit__ del with

        # Scrivi metadati
        meta = {
            "frame_rate_hz"       : df.FrameRate,
            "n_wells"             : df.NWells,
            "n_channels_per_well" : df.NChsPerWell,
            "total_channels"      : df.NWells * df.NChsPerWell,
            "ch_sample_byte_size" : df.ChSampleByteSize,
            "bit_depth"           : df.BitDepth,
            "adc_counts_to_value" : df.ADCCountsToValue,
            "offset"              : df.Offset,
            "min_digital_value"   : df.MinDigitalValue,
            "max_digital_value"   : df.MaxDigitalValue,
            "n_frames_total"      : n_frames_written,
            "duration_sec"        : duration_sec,
            "packet_log"          : packet_log,
        }
        with open(self.meta_path, "w") as f:
            json.dump(meta, f, indent=2)

        print(f"[recorder] Completato: {n_frames_written} frame salvati.")
        print(f"[recorder] Raw    -> {self.raw_path}")
        print(f"[recorder] Meta   -> {self.meta_path}")

        if error:
            print(f"[recorder] ATTENZIONE: si sono verificati errori durante la registrazione: {error}")

        return self.raw_path, self.meta_path


# ---------------------------------------------------------------------------
# Funzione di comodo
# ---------------------------------------------------------------------------

def record(duration_sec: float, name: str = None, output_dir: str = "recordings",
           data_packet_timespan_ms: int = None):
    """Funzione rapida: crea un recorder e registra."""
    rec = BioCamRecorder(output_dir=output_dir)
    return rec.record(duration_sec=duration_sec, name=name,
                      data_packet_timespan_ms=data_packet_timespan_ms)


# ---------------------------------------------------------------------------
# Lettura registrazione salvata
# ---------------------------------------------------------------------------

_DTYPE_BY_BYTE_SIZE = {1: "uint8", 2: "uint16", 4: "uint32"}


def load_recording(raw_path: str, meta_path: str, as_analog: bool = True):
    """
    Carica una registrazione salvata.

    Restituisce (data, meta):
        data -> np.ndarray shape (n_frames, n_canali_totali)
                float64 in µV se as_analog=True, altrimenti ADC counts
        meta -> dict metadati
    """
    with open(meta_path) as f:
        meta = json.load(f)

    total_channels = meta["total_channels"]
    dtype_name = _DTYPE_BY_BYTE_SIZE.get(meta["ch_sample_byte_size"])
    if dtype_name is None:
        raise RuntimeError(f"ch_sample_byte_size={meta['ch_sample_byte_size']} non gestito.")

    raw      = np.fromfile(raw_path, dtype=dtype_name)
    n_frames = len(raw) // total_channels
    data     = raw[: n_frames * total_channels].reshape(n_frames, total_channels)

    if as_analog:
        data = meta["offset"] + data.astype("float64") * meta["adc_counts_to_value"]

    return data, meta


# ---------------------------------------------------------------------------
# Esecuzione da riga di comando
# ---------------------------------------------------------------------------

import threading  # noqa: E402 (import qui per non inquinare il namespace del modulo)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Registra dati dal BioCAM DupleX.")
    parser.add_argument("--duration",   type=float, default=10.0,        help="Durata in secondi (default: 10)")
    parser.add_argument("--name",       type=str,   default=None,         help="Nome base file output")
    parser.add_argument("--output-dir", type=str,   default="recordings", help="Cartella output")
    parser.add_argument("--packet-ms",  type=int,   default=None,         help="Intervallo pacchetti in ms")
    args = parser.parse_args()

    print("=== Registrazione BioCAM ===")
    try:
        raw_path, meta_path = record(
            duration_sec            = args.duration,
            name                    = args.name,
            output_dir              = args.output_dir,
            data_packet_timespan_ms = args.packet_ms,
        )

        print("\n=== Verifica dati ===")
        data, meta = load_recording(raw_path, meta_path)
        print(f"Shape (frame x canali): {data.shape}")
        print(f"Min: {data.min():.3f} µV | Max: {data.max():.3f} µV | Media: {data.mean():.3f} µV")

    except Exception as e:
        print(f"ERRORE: {e}")
        import sys
        sys.exit(1)