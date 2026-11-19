"""Usuarios del panel: contraseñas con Argon2id, sesiones en SQLite, freno a la fuerza bruta.

Roles:
  admin     todo, incluidos los usuarios del panel
  operador  usa unidades del disponible, maneja transportes e importa (maestro de unidades, GCG, bloqueados)
  consulta  solo mira
Los transportes NO entran al panel: cargan su disponible con su link (token).
"""
import os
import secrets
import sqlite3
import time
from contextlib import closing

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

DB = os.environ.get("PANEL_DB", "/data/panel.db")
SESION_HORAS = 12
MAX_FALLOS, VENTANA = 8, 15 * 60          # 8 intentos fallidos por IP cada 15 minutos

# Argon2id (por defecto en argon2-cffi): 3 pasadas, 64 MiB, 4 hilos — parámetros recomendados (RFC 9106).
ph = PasswordHasher()
_HASH_FALSO = ph.hash(secrets.token_urlsafe(16))
_fallos = {}


def con():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


def iniciar():
    with closing(con()) as c, c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS usuarios (
            usuario TEXT PRIMARY KEY, nombre TEXT, clave_hash TEXT NOT NULL,
            admin INTEGER NOT NULL DEFAULT 0, activo INTEGER NOT NULL DEFAULT 1,
            cambiar_clave INTEGER NOT NULL DEFAULT 1, creado TEXT DEFAULT CURRENT_TIMESTAMP,
            ultimo_ingreso TEXT);
        CREATE TABLE IF NOT EXISTS sesiones (
            token TEXT PRIMARY KEY, usuario TEXT NOT NULL, vence REAL NOT NULL);
        """)
        cols = {r["name"] for r in c.execute("PRAGMA table_info(usuarios)")}
        if "rol" not in cols:
            c.execute("ALTER TABLE usuarios ADD COLUMN rol TEXT")
            c.execute("UPDATE usuarios SET rol = CASE WHEN admin=1 THEN 'admin' ELSE 'consulta' END")
        if "transporte_id" not in cols:
            c.execute("ALTER TABLE usuarios ADD COLUMN transporte_id INTEGER")
        if not c.execute("SELECT 1 FROM usuarios").fetchone():
            usuario = os.environ.get("PANEL_ADMIN_USER", "admin")
            clave = os.environ.get("PANEL_ADMIN_PASSWORD")
            if clave:
                c.execute("INSERT INTO usuarios (usuario, nombre, clave_hash, admin, rol) VALUES (?,?,?,1,'admin')",
                          (usuario, "Administrador", ph.hash(clave)))


def bloqueado(ip):
    ahora = time.time()
    _fallos[ip] = [t for t in _fallos.get(ip, []) if ahora - t < VENTANA]
    return len(_fallos[ip]) >= MAX_FALLOS


def ingresar(usuario, clave, ip):
    """Devuelve el token de sesión o None. Tiempo parejo exista o no el usuario."""
    usuario = (usuario or "").strip().lower()
    with closing(con()) as c, c:
        u = c.execute("SELECT * FROM usuarios WHERE usuario=? AND activo=1", (usuario,)).fetchone()
        try:
            ph.verify(u["clave_hash"] if u else _HASH_FALSO, clave or "")
            ok = u is not None
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            ok = False
        if not ok:
            _fallos.setdefault(ip, []).append(time.time())
            return None
        if ph.check_needs_rehash(u["clave_hash"]):
            c.execute("UPDATE usuarios SET clave_hash=? WHERE usuario=?", (ph.hash(clave), usuario))
        token = secrets.token_urlsafe(32)
        c.execute("DELETE FROM sesiones WHERE vence < ?", (time.time(),))
        c.execute("INSERT INTO sesiones VALUES (?,?,?)", (token, usuario, time.time() + SESION_HORAS * 3600))
        c.execute("UPDATE usuarios SET ultimo_ingreso=datetime('now') WHERE usuario=?", (usuario,))
        _fallos.pop(ip, None)
        return token


def usuario_de(token):
    if not token:
        return None
    with closing(con()) as c:
        return c.execute("""SELECT u.* FROM sesiones s JOIN usuarios u ON u.usuario = s.usuario
                            WHERE s.token=? AND s.vence > ? AND u.activo=1""", (token, time.time())).fetchone()


def salir(token):
    with closing(con()) as c, c:
        c.execute("DELETE FROM sesiones WHERE token=?", (token,))


def cambiar_clave(usuario, actual, nueva):
    with closing(con()) as c, c:
        u = c.execute("SELECT clave_hash FROM usuarios WHERE usuario=?", (usuario,)).fetchone()
        try:
            ph.verify(u["clave_hash"], actual)
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            return "La contraseña actual no es correcta."
        if len(nueva) < 10:
            return "La nueva contraseña tiene que tener al menos 10 caracteres."
        c.execute("UPDATE usuarios SET clave_hash=?, cambiar_clave=0 WHERE usuario=?", (ph.hash(nueva), usuario))
        return None


def listar():
    with closing(con()) as c:
        return c.execute("""SELECT usuario, nombre, admin, rol, activo, ultimo_ingreso FROM usuarios
                            ORDER BY usuario""").fetchall()


ROLES = {"admin": "Administrador", "operador": "Operador", "consulta": "Consulta"}
PERMISOS = {                       # qué puede hacer cada rol
    "usuarios": {"admin"},
    "gestion": {"admin", "operador"},       # usar unidades, transportes, importar
}


def perfil(u):
    """Fila de usuarios -> dict con el rol y los permisos (lo usan main.py y las plantillas)."""
    if not u:
        return None
    d = dict(u)
    d["rol"] = d.get("rol") or ("admin" if d.get("admin") else "consulta")
    d["admin"] = d["rol"] == "admin"
    d["puede"] = {k for k, roles in PERMISOS.items() if d["rol"] in roles}
    d["rol_nombre"] = ROLES[d["rol"]]
    return d


def cambiar_rol(usuario, rol, transporte_id=None):
    if rol not in ROLES:
        raise ValueError("Rol inválido.")
    with closing(con()) as c, c:
        c.execute("UPDATE usuarios SET rol=?, admin=? WHERE usuario=?", (rol, 1 if rol == "admin" else 0, usuario))


def crear(usuario, nombre, rol, transporte_id=None):
    """Crea el usuario con una contraseña temporal (que tiene que cambiar al entrar) y la devuelve."""
    usuario = (usuario or "").strip().lower()
    if not usuario.replace(".", "").replace("_", "").isalnum() or len(usuario) < 3:
        raise ValueError("El usuario tiene que tener 3 o más letras/números (se permiten . y _).")
    if rol not in ROLES:
        raise ValueError("Rol inválido.")
    clave = secrets.token_urlsafe(9)
    with closing(con()) as c, c:
        if c.execute("SELECT 1 FROM usuarios WHERE usuario=?", (usuario,)).fetchone():
            raise ValueError("Ese usuario ya existe.")
        c.execute("INSERT INTO usuarios (usuario, nombre, clave_hash, admin, rol) VALUES (?,?,?,0,'consulta')",
                  (usuario, nombre.strip()[:80], ph.hash(clave)))
    cambiar_rol(usuario, rol, transporte_id)
    return clave


def blanquear(usuario):
    clave = secrets.token_urlsafe(9)
    with closing(con()) as c, c:
        c.execute("UPDATE usuarios SET clave_hash=?, cambiar_clave=1 WHERE usuario=?", (ph.hash(clave), usuario))
        c.execute("DELETE FROM sesiones WHERE usuario=?", (usuario,))
    return clave


def activar(usuario, activo):
    with closing(con()) as c, c:
        c.execute("UPDATE usuarios SET activo=? WHERE usuario=?", (1 if activo else 0, usuario))
        if not activo:
            c.execute("DELETE FROM sesiones WHERE usuario=?", (usuario,))
