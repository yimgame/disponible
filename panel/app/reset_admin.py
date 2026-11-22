"""Resetear la contraseña de un administrador (por si se pierde el acceso al panel).

Uso (dentro del container o con Python, desde la carpeta panel/):
    python -m app.reset_admin                 # usuario "admin", contraseña temporal al azar
    python -m app.reset_admin juan            # otro usuario
    python -m app.reset_admin admin MiClave1234   # con una contraseña elegida (mínimo 10 caracteres)
    python -m app.reset_admin desbloquear     # solo libera la espera por intentos fallidos (no cambia claves)

Deja al usuario activo, con rol administrador, le pide cambiar la contraseña al entrar y cierra sus sesiones.
Si el usuario no existe, lo crea como administrador. Siempre libera la espera por intentos fallidos.
"""
import secrets
import sys
from contextlib import closing

from . import auth, db


def resetear(usuario="admin", clave=None):
    usuario = (usuario or "admin").strip().lower()
    clave = clave or secrets.token_urlsafe(9)
    if len(clave) < 10:
        raise SystemExit("La contraseña tiene que tener al menos 10 caracteres.")
    auth.DB = db.DB
    db.iniciar()
    auth.iniciar()
    with closing(auth.con()) as c, c:
        existe = c.execute("SELECT 1 FROM usuarios WHERE usuario=?", (usuario,)).fetchone()
        if existe:
            c.execute("""UPDATE usuarios SET clave_hash=?, cambiar_clave=1, activo=1, admin=1, rol='admin'
                         WHERE usuario=?""", (auth.ph.hash(clave), usuario))
        else:
            c.execute("""INSERT INTO usuarios (usuario, nombre, clave_hash, admin, rol, activo, cambiar_clave)
                         VALUES (?, 'Administrador', ?, 1, 'admin', 1, 1)""", (usuario, auth.ph.hash(clave)))
        c.execute("DELETE FROM sesiones WHERE usuario=?", (usuario,))
    auth.desbloquear_todo()
    return usuario, clave, bool(existe)


def desbloquear():
    auth.DB = db.DB
    db.iniciar()
    auth.iniciar()
    return auth.desbloquear_todo()


if __name__ == "__main__":
    args = sys.argv[1:]
    if args and args[0].lower() in ("desbloquear", "--desbloquear"):
        n = desbloquear()
        print(f"\n  Listo: se liberó la espera por intentos fallidos ({n} intento(s) borrados). Ya se puede volver a entrar.\n")
        sys.exit(0)
    usuario, clave, existia = resetear(args[0] if args else "admin", args[1] if len(args) > 1 else None)
    print()
    print(f"  Usuario:    {usuario}  ({'contraseña reseteada' if existia else 'creado como administrador'})")
    print(f"  Contraseña: {clave}")
    print()
    print("  Al entrar al panel te va a pedir cambiarla. También se liberó la espera por intentos fallidos.")
    print("  Base:", db.DB)
    print()
