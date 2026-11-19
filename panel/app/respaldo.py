"""Backup diario de la base (transportes, unidades, movimientos, usos, bloqueos, GCG y usuarios).

Copia con la API de backup de SQLite (consistente aunque se esté usando) a /data/respaldos/ (mismo volumen Docker que
la base): NO al NAS, porque el share de Samba es de acceso invitado y la base tiene datos personales y hashes de
contraseñas. Cubre errores y borrados; no cubre perder el disco. Se guardan los últimos 30 días.
"""
import os
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path

from . import db

CARPETA = Path(db.DB).parent / "respaldos"
DIAS = int(os.environ.get("RESPALDO_DIAS", "30"))


def respaldar():
    CARPETA.mkdir(parents=True, exist_ok=True)
    destino = CARPETA / f"panel-{datetime.now():%Y%m%d}.db"
    if destino.exists():
        return None
    tmp = destino.with_suffix(".tmp")
    src, dst = sqlite3.connect(db.DB), sqlite3.connect(tmp)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    tmp.rename(destino)
    for viejo in sorted(CARPETA.glob("panel-*.db"))[:-DIAS]:
        viejo.unlink()
    return destino


def _bucle():
    while True:
        try:
            r = respaldar()
            if r:
                print(f"[respaldo] {r.name} ({r.stat().st_size // 1024} KB)", flush=True)
        except Exception as e:                     # nunca tirar el panel por el backup
            print(f"[respaldo] error: {e}", flush=True)
        time.sleep(3600)


def iniciar():
    threading.Thread(target=_bucle, name="respaldo-db", daemon=True).start()
