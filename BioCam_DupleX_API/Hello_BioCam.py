import sys
import os
import time

# 1. Imposta i percorsi
script_dir = os.path.dirname(os.path.abspath(__file__))
api_dir = os.path.join(script_dir, "API")          # <-- cartella corretta

# Aggiungi API\ al PATH (per le DLL native FTDI e simili)
os.environ["PATH"] = api_dir + os.pathsep + os.environ["PATH"]

# 2. Forza pythonnet a usare .NET Framework
from pythonnet import set_runtime
from clr_loader import get_netfx
set_runtime(get_netfx())

import clr

# 3. Carica le assembly 3Brain dalla cartella API\
print("Caricamento DLL 3Brain...")
clr.AddReference(os.path.join(api_dir, "3Brain.Common"))
clr.AddReference(os.path.join(api_dir, "3Brain.BioCamDriver"))

# 4. Importa le classi dai namespace C#
from _3Brain.BioCamDriver import BioCamPool

# 5. Inizializza il sistema
print("Attivazione BioCamPool...")
BioCamPool.Activate()

print("In ascolto di BioCAM connessi (aspetto 3 secondi)...")
time.sleep(3)

# 6. Controlla se ci sono BioCAM connessi
slot_info_list = BioCamPool.BioCamSlotInfo
connected = [info for info in slot_info_list if info.IsBioCamConnected]

if not connected:
    print("\n[!] Nessun BioCAM trovato. Controlla che sia acceso e collegato via USB.")
else:
    print(f"\n[OK] Trovati {len(connected)} BioCAM:")
    for info in connected:
        print(f"  - Modello: {info.CommercialModel}")
        print(f"  - Seriale: {info.SerialNumberAndVersion}")
        print(f"  - Piastra MEA connessa: {info.IsMeaPlateConnected}")

# 7. Pulizia finale
print("\nDisattivazione BioCamPool...")
BioCamPool.Deactivate()
print("Fatto!")