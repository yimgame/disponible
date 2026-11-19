"""Configuración guardada en la base, con los valores sensibles (ej. la API key de GCG) cifrados.

Cifrado: la clave maestra es un archivo aparte (/data/.clave_secretos, 32 bytes al azar, solo lectura para el panel),
así una copia suelta de la base no expone la API key. Flujo pseudoaleatorio SHA-256 en modo contador + HMAC-SHA256
para detectar cambios (sin librerías extra). Formato guardado: base64(nonce16 | cifrado | tag32).
"""
import base64
import hashlib
import hmac
import os
import secrets
from contextlib import closing
from pathlib import Path

from . import db

ESQUEMA = "CREATE TABLE IF NOT EXISTS config (clave TEXT PRIMARY KEY, valor TEXT, actualizado TEXT, por TEXT);"


def _archivo_clave():
    return Path(db.DB).parent / ".clave_secretos"


def _clave():
    p = _archivo_clave()
    if not p.exists():
        p.write_bytes(secrets.token_bytes(32))
        os.chmod(p, 0o600)
    return p.read_bytes()


def _flujo(clave, nonce, n):
    out, i = b"", 0
    while len(out) < n:
        out += hashlib.sha256(clave + nonce + i.to_bytes(8, "big")).digest()
        i += 1
    return out[:n]


def cifrar(texto):
    k = _clave()
    nonce = secrets.token_bytes(16)
    datos = texto.encode()
    c = bytes(a ^ b for a, b in zip(datos, _flujo(k, nonce, len(datos))))
    tag = hmac.new(k, nonce + c, hashlib.sha256).digest()
    return base64.b64encode(nonce + c + tag).decode()


def descifrar(guardado):
    k = _clave()
    raw = base64.b64decode(guardado)
    nonce, c, tag = raw[:16], raw[16:-32], raw[-32:]
    if not hmac.compare_digest(tag, hmac.new(k, nonce + c, hashlib.sha256).digest()):
        raise ValueError("El valor guardado no se puede descifrar (¿cambió la clave del servidor?).")
    return bytes(a ^ b for a, b in zip(c, _flujo(k, nonce, len(c)))).decode()


def iniciar():
    with closing(db.con()) as c, c:
        c.execute(ESQUEMA)


def get(clave, defecto=None):
    with closing(db.con()) as c:
        r = c.execute("SELECT valor FROM config WHERE clave=?", (clave,)).fetchone()
    return r["valor"] if r and r["valor"] is not None else defecto


def poner(clave, valor, por=""):
    with closing(db.con()) as c, c:
        c.execute("INSERT INTO config VALUES (?,?,?,?) ON CONFLICT(clave) DO UPDATE SET valor=excluded.valor, "
                  "actualizado=excluded.actualizado, por=excluded.por", (clave, valor, db.ahora_iso(), por))


def info(clave):
    with closing(db.con()) as c:
        r = c.execute("SELECT * FROM config WHERE clave=?", (clave,)).fetchone()
    return dict(r) if r else None


def get_secreto(clave):
    v = get(clave)
    if not v:
        return None
    try:
        return descifrar(v)
    except Exception:                     # noqa: BLE001
        return None


def set_secreto(clave, valor, por=""):
    poner(clave, cifrar(valor) if valor else None, por)
