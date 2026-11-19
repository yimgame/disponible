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

-- Bases / destinos a los que se asignan las unidades (lista que se administra en el panel).
CREATE TABLE IF NOT EXISTS bases (
    id INTEGER PRIMARY KEY AUTOINCREMENT, nombre TEXT NOT NULL UNIQUE COLLATE NOCASE, tipo TEXT,
    activo INTEGER NOT NULL DEFAULT 1, creado TEXT);
"""

# Columnas agregadas después de la primera versión (la base se migra sola).
COLUMNAS_NUEVAS = {"movimientos": ["avisos TEXT", "chofer_semaforo TEXT", "unidad_semaforo TEXT", "gcg_detalle TEXT", "gcg_consultado TEXT",
                                   "patente2 TEXT", "patente3 TEXT", "configuracion TEXT"],
                   "gcg": ["pais TEXT", "situacion TEXT"],
                   "documentos_criticos": ["aplica_a TEXT NOT NULL DEFAULT 'chofer'"]}

# Documentos críticos (mismo criterio que la app de bloqueos): si uno está vencido el chofer queda en ROJO.
ESQUEMA_CRITICOS = """
-- Historial GCG de cada carga: cómo estaban los documentos en la carga y en cada reconsulta.
CREATE TABLE IF NOT EXISTS gcg_historial (
    id INTEGER PRIMARY KEY AUTOINCREMENT, movimiento_id INTEGER NOT NULL, consultado TEXT NOT NULL, por TEXT,
    motivo TEXT, detalle TEXT);
CREATE INDEX IF NOT EXISTS gcg_historial_mov ON gcg_historial(movimiento_id);
-- Últimas consultas en vivo a la API de GCG (caché para no consultar en cada carga).
CREATE TABLE IF NOT EXISTS gcg_vivo (
    tipo TEXT NOT NULL, clave TEXT NOT NULL, respuesta TEXT, consultado REAL NOT NULL, PRIMARY KEY (tipo, clave));
CREATE TABLE IF NOT EXISTS documentos_criticos (
    id INTEGER PRIMARY KEY AUTOINCREMENT, nombre TEXT NOT NULL UNIQUE COLLATE NOCASE, gcg_numero INTEGER,
    paises TEXT NOT NULL DEFAULT 'AR', activo INTEGER NOT NULL DEFAULT 1, orden INTEGER NOT NULL DEFAULT 0);
"""
CRITICOS_DEFAULT = [("Pago bancarizado haberes", 10), ("Pago Federación de Camioneros", 41), ("Pago Sindicato", 7),
                    ("Pago, DJ F931 y Secuencia 0", 2), ("Planilla kilometraje / horarios y descansos", 9),
                    ("Recibo de haberes", 3)]


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
        c.executescript(ESQUEMA_CRITICOS)
        if not c.execute("SELECT 1 FROM documentos_criticos").fetchone():
            for i, (nombre, numero) in enumerate(CRITICOS_DEFAULT):
                c.execute("INSERT INTO documentos_criticos (nombre, gcg_numero, paises, orden) VALUES (?,?,'AR',?)",
                          (nombre, numero, i))
        for tabla, cols in COLUMNAS_NUEVAS.items():
            existentes = {r["name"] for r in c.execute(f"PRAGMA table_info({tabla})")}
            for col in cols:
                if col.split()[0] not in existentes:
                    c.execute(f"ALTER TABLE {tabla} ADD COLUMN {col}")


def ahora():
    return datetime.now(TZ)


def ahora_iso():
    return ahora().isoformat(timespec="seconds")


def hoy():
    return ahora().date().isoformat()
