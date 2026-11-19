"""Base SQLite del disponible: se crea sola al arrancar (y se migra sola si le faltan tablas o columnas)."""
import os
import sqlite3
from contextlib import closing
from datetime import datetime
from zoneinfo import ZoneInfo

DB = os.environ.get("PANEL_DB", "/data/disponible.db")
TZ = ZoneInfo("America/Argentina/Buenos_Aires")

ESQUEMA = """
-- Transportes: cada uno carga su disponible con un link propio (token), sin usuario ni clave.
CREATE TABLE IF NOT EXISTS transportes (
    id INTEGER PRIMARY KEY AUTOINCREMENT, nombre TEXT NOT NULL UNIQUE COLLATE NOCASE, nro_proveedor TEXT,
    token TEXT NOT NULL UNIQUE, cuota_dia INTEGER NOT NULL DEFAULT 0, telefono TEXT, activo INTEGER NOT NULL DEFAULT 1,
    notas TEXT, creado TEXT);
CREATE INDEX IF NOT EXISTS transportes_prov ON transportes(nro_proveedor);

-- Maestro de unidades (exporte de equipos): tipo, capacidad y estado de bloqueo / documentos.
CREATE TABLE IF NOT EXISTS unidades (
    patente TEXT PRIMARY KEY, nro_proveedor TEXT, transportista TEXT, tipo TEXT, tipo_equipo TEXT, pallets INTEGER,
    kg_cap REAL, ejes INTEGER, satelital INTEGER, dedicado INTEGER, bloqueado INTEGER NOT NULL DEFAULT 0,
    doc_vencido TEXT, fecha_expiracion TEXT, actualizado TEXT);
CREATE INDEX IF NOT EXISTS unidades_prov ON unidades(nro_proveedor);

-- Bloqueos manuales o de una lista volcada: por unidad (patente) o por chofer (DNI).
CREATE TABLE IF NOT EXISTS bloqueos (
    id INTEGER PRIMARY KEY AUTOINCREMENT, tipo TEXT NOT NULL CHECK (tipo IN ('unidad','chofer')), clave TEXT NOT NULL,
    motivo TEXT, origen TEXT, activo INTEGER NOT NULL DEFAULT 1, creado TEXT, creado_por TEXT,
    UNIQUE (tipo, clave, origen));

-- GCG (exporte de documentación): un registro por trabajador (DNI) o vehículo (dominio).
CREATE TABLE IF NOT EXISTS gcg (
    tipo TEXT NOT NULL, clave TEXT NOT NULL, nombre TEXT, cuil TEXT, estado TEXT, contratista TEXT,
    subcontratista TEXT, vencimiento TEXT, documentos TEXT, actualizado TEXT, PRIMARY KEY (tipo, clave));

-- Lo que informa cada transporte: pone (agrega) o saca (quita) una unidad del disponible.
CREATE TABLE IF NOT EXISTS movimientos (
    id INTEGER PRIMARY KEY AUTOINCREMENT, creado TEXT NOT NULL, fecha TEXT NOT NULL, hora TEXT, transporte_id INTEGER,
    transporte TEXT, patente TEXT NOT NULL, tipo TEXT, pallets INTEGER, accion TEXT NOT NULL, eta TEXT, chofer_dni TEXT,
    chofer_nombre TEXT, comentario TEXT, informante TEXT, bloqueado INTEGER NOT NULL DEFAULT 0, bloqueo_motivo TEXT);
CREATE INDEX IF NOT EXISTS movimientos_fecha ON movimientos(fecha);

-- Uso de una unidad del disponible (se descuenta) y dónde se usó.
CREATE TABLE IF NOT EXISTS usos (
    id INTEGER PRIMARY KEY AUTOINCREMENT, creado TEXT NOT NULL, fecha TEXT NOT NULL, movimiento_id INTEGER,
    transporte_id INTEGER, transporte TEXT, patente TEXT NOT NULL, tipo TEXT, pallets INTEGER, destino TEXT, viaje TEXT,
    planta TEXT, detalle TEXT, usuario TEXT, anulado INTEGER NOT NULL DEFAULT 0, anulado_por TEXT, anulado_en TEXT);
CREATE INDEX IF NOT EXISTS usos_fecha ON usos(fecha);
"""


def con():
    c = sqlite3.connect(DB, timeout=15)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    return c


def iniciar():
    os.makedirs(os.path.dirname(DB) or ".", exist_ok=True)
    with closing(con()) as c, c:
        c.execute("PRAGMA journal_mode=WAL")
        c.executescript(ESQUEMA)


def ahora():
    return datetime.now(TZ)


def ahora_iso():
    return ahora().isoformat(timespec="seconds")


def hoy():
    return ahora().date().isoformat()
