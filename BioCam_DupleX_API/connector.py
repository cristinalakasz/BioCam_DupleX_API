"""
connector.py
============
Libreria di connessione al BioCAM DupleX via pythonnet.
Importata da recorder.py — non va lanciata direttamente.

Requisiti:
    pip install pythonnet numpy
    Windows + .NET Framework 4.7
    DLL 3Brain nella cartella API/ accanto a questo file.
"""

import os
import sys
import time
import threading

import numpy as np

import pythonnet
pythonnet.load("netfx")

import clr  # noqa: E402


DEFAULT_DLL_DIR = os.path.join(os.path.dirname(__file__), "API")


# ---------------------------------------------------------------------------
# DLL
# ---------------------------------------------------------------------------

def _load_dlls(dll_dir: str) -> None:
    if dll_dir not in sys.path:
        sys.path.insert(0, dll_dir)
    for name in ["3Brain.Common", "3Brain.BioCamDriver"]:
        dll_path = os.path.join(dll_dir, f"{name}.dll")
        if not os.path.isfile(dll_path):
            raise FileNotFoundError(f"DLL non trovata: {dll_path}")
        clr.AddReference(dll_path)
    print(f"[connector] DLL caricate da: {dll_dir}")


def _import_3brain():
    from _3Brain.BioCamDriver import BioCamPool, BioCamDupleX  # noqa
    from _3Brain.Common import RectangularStimPulse, ChCoord    # noqa
    return BioCamPool, BioCamDupleX, RectangularStimPulse, ChCoord


# ---------------------------------------------------------------------------
# Decodifica payload
# ---------------------------------------------------------------------------

def decode_payload(data_format, event_args) -> np.ndarray:
    """
    Converte event_args di DataReceived in numpy array ADC counts.
    Prova tutti i nomi di attributo noti sul tipo .NET.
    Shape output: (n_frames, n_canali_totali)
    """
    raw_bytes = None
    for attr in ("RawData", "Payload", "Data", "Buffer", "Samples"):
        if hasattr(event_args, attr):
            raw_bytes = bytes(getattr(event_args, attr))
            break
    if raw_bytes is None:
        raw_bytes = bytes(event_args)

    n_ch  = data_format.NWells * data_format.NChsPerWell
    bsize = data_format.ChSampleByteSize
    dtype_map = {1: np.uint8, 2: np.uint16, 4: np.uint32}
    if bsize not in dtype_map:
        raise ValueError(f"ChSampleByteSize={bsize} non supportato.")
    dtype    = dtype_map[bsize]
    raw      = np.frombuffer(raw_bytes, dtype=dtype)
    n_frames = len(raw) // n_ch
    return raw[: n_frames * n_ch].reshape(n_frames, n_ch)


def digital_to_analog(data: np.ndarray, offset: float, scale: float) -> np.ndarray:
    return offset + data.astype(np.float64) * scale


# ---------------------------------------------------------------------------
# BioCamConnector
# ---------------------------------------------------------------------------

class BioCamConnector:
    """
    Gestisce apertura e chiusura completa della connessione al BioCAM DupleX.

    Uso corretto (context manager):
        with BioCamConnector() as conn:
            # conn.biocam disponibile qui
            conn.start_streaming(callback)
            time.sleep(10)
            conn.stop_streaming()
        # tutto rilasciato qui
    """

    def __init__(self, dll_dir: str = DEFAULT_DLL_DIR, timeout_sec: int = 30):
        self.dll_dir     = dll_dir
        self.timeout_sec = timeout_sec

        self.biocam      = None
        self._slot_index = -1
        self._connected  = False

        self._device_ready      = threading.Event()
        self._streaming_handler = None

        self._BioCamPool          = None
        self._BioCamDupleX        = None
        self.RectangularStimPulse = None
        self.ChCoord              = None

    # ------------------------------------------------------------------
    # Connessione
    # ------------------------------------------------------------------

    def connect(self) -> "IBioCam":
        if self._connected:
            return self.biocam

        print("[connector] Caricamento DLL...")
        _load_dlls(self.dll_dir)

        (self._BioCamPool,
         self._BioCamDupleX,
         self.RectangularStimPulse,
         self.ChCoord) = _import_3brain()

        print("[connector] Attivazione BioCamPool...")
        self._BioCamPool.Activate()
        self._BioCamPool.BioCamsStatusChanged += self._on_status_changed

        # Polling attivo: controlla ogni 0.5s per max 15s
        print("[connector] Rilevamento dispositivo...")
        for attempt in range(30):
            slots = list(self._BioCamPool.BioCamSlotInfo)
            for idx, info in enumerate(slots):
                print(f"[connector]   tentativo {attempt}: slot {idx} "
                      f"biocam={info.IsBioCamConnected} plate={info.IsMeaPlateConnected}")
                if info.IsBioCamConnected and info.IsMeaPlateConnected:
                    print(f"[connector] Dispositivo pronto: {info.CommercialModel} "
                          f"(SN: {info.SerialNumberAndVersion})")
                    self._device_ready.set()
                    break
            if self._device_ready.is_set():
                break
            time.sleep(0.5)

        # Se il polling non ha trovato nulla, aspetta ancora l'evento (callback asincrono)
        print(f"[connector] Attesa BioCAM + piastra (timeout {self.timeout_sec}s)...")
        if not self._device_ready.wait(timeout=self.timeout_sec):
            self._cleanup_pool()
            raise TimeoutError(
                "Timeout: nessun BioCAM con piastra trovato. "
                "Controlla USB e piastra."
            )

        # Attesa slot libero
        time.sleep(1)
        self._slot_index = self._get_free_slot()

        print(f"[connector] Prendendo controllo slot {self._slot_index}...")
        self.biocam = self._BioCamPool.TakeBioCamControl(self._slot_index)

        if self.biocam is None:
            self._cleanup_pool()
            raise RuntimeError(
                "TakeBioCamControl ha restituito None. "
                "Chiudi BrainWave o altri software 3Brain e riprova."
            )

        print(f"[connector] Tipo oggetto BioCAM: {type(self.biocam)}")

        if not self.biocam.IsConnected:
            self._cleanup_pool()
            raise RuntimeError("BioCAM non connesso dopo TakeBioCamControl.")
        if not self.biocam.MeaPlate.IsConnected:
            self._cleanup_pool()
            raise RuntimeError("Piastra MEA non connessa.")

        try:
            self.biocam.Stimulator.Initialize()
            print("[connector] Stimolatore inizializzato.")
        except Exception as e:
            print(f"[connector] Stimolatore non disponibile: {e}")

        self._connected = True
        print(f"[connector] Connesso: {self._device_info()}")
        return self.biocam

    # ------------------------------------------------------------------
    # Disconnessione — rilascia tutto nell'ordine corretto
    # ------------------------------------------------------------------

    def disconnect(self) -> None:
        if not self._connected:
            return

        print("[connector] Disconnessione in corso...")

        # 1. Ferma streaming
        try:
            if self.biocam is not None and self.biocam.IsStreaming:
                self.stop_streaming()
        except Exception as e:
            print(f"[connector] Avviso stop_streaming: {e}")

        # 2. Chiudi stimolatore
        try:
            if self.biocam is not None:
                self.biocam.Stimulator.Close()
        except Exception as e:
            print(f"[connector] Avviso Stimulator.Close: {e}")

        # 3. Rilascia controllo BioCAM
        try:
            if self._slot_index >= 0:
                self._BioCamPool.ReleaseBioCamControl(self._slot_index)
                print(f"[connector] Slot {self._slot_index} rilasciato.")
        except Exception as e:
            print(f"[connector] Avviso ReleaseBioCamControl: {e}")

        # 4. Deactivate pool
        self._cleanup_pool()

        self.biocam      = None
        self._slot_index = -1
        self._connected  = False
        self._device_ready.clear()
        print("[connector] Disconnesso.")

    def _cleanup_pool(self):
        try:
            self._BioCamPool.BioCamsStatusChanged -= self._on_status_changed
        except Exception:
            pass
        try:
            self._BioCamPool.Deactivate()
            print("[connector] BioCamPool disattivato.")
        except Exception as e:
            print(f"[connector] Avviso Deactivate: {e}")

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.disconnect()
        return False

    # ------------------------------------------------------------------
    # Streaming
    # ------------------------------------------------------------------

    def start_streaming(self, callback, data_packet_timespan_ms: int = None) -> None:
        if not self._connected or self.biocam is None:
            raise RuntimeError("Connector non connesso.")

        def _handler(sender, event_args):
            try:
                callback(event_args)
            except Exception as e:
                print(f"[connector] Errore callback: {e}")

        self._streaming_handler = _handler
        self.biocam.DataReceived += self._streaming_handler

        if data_packet_timespan_ms is not None:
            self.biocam.StartDataStreaming(data_packet_timespan_ms)
        else:
            self.biocam.StartDataStreaming()

        print("[connector] Streaming avviato.")

    def stop_streaming(self) -> None:
        try:
            self.biocam.StopDataStreaming()
        except Exception as e:
            print(f"[connector] Avviso StopDataStreaming: {e}")

        if self._streaming_handler is not None:
            try:
                self.biocam.DataReceived -= self._streaming_handler
            except Exception:
                pass
            self._streaming_handler = None

        print("[connector] Streaming fermato.")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _on_status_changed(self, sender, event_args):
        for info in list(self._BioCamPool.BioCamSlotInfo):
            if info.IsBioCamConnected:
                status = f"{info.CommercialModel} (SN: {info.SerialNumberAndVersion})"
                if info.IsMeaPlateConnected:
                    status += " — Piastra connessa ✓"
                    self._device_ready.set()
                else:
                    status += " — Piastra NON connessa ✗"
                print(f"[connector] {status}")

    def _get_free_slot(self) -> int:
        for _ in range(20):
            free = list(self._BioCamPool.GetSlotIndexesFreeBioCam())
            if free:
                return free[0]
            time.sleep(0.5)
        raise RuntimeError("Nessun slot BioCAM libero. Ricollegare USB.")

    def _device_info(self) -> str:
        try:
            return (
                f"BioCAM DupleX | "
                f"Piastra: {self.biocam.MeaPlate.ConnectedMeaPlateModel} | "
                f"Frequenza: {self.biocam.DataFormat.FrameRate} Hz"
            )
        except Exception:
            return "BioCAM DupleX (info non disponibili)"