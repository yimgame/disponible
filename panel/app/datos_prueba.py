"""Datos de prueba para el formulario del transporte (autocompletado con la última carga de cada equipo).

Carga un historial de cargas falsas (patentes ZZ…, DNI 99…, comentario «DATO DE PRUEBA», informante «prueba»)
en los días anteriores. Se puede correr más de una vez (no duplica).
    python -m app.datos_prueba <id del transporte>
    python -m app.datos_prueba --borrar          # saca todos los datos de prueba
"""
import sqlite3
import sys
from datetime import date, timedelta

from . import db as _db

if len(sys.argv) > 1 and sys.argv[1] == "--borrar":
    c = sqlite3.connect(_db.DB)
    n = c.execute("DELETE FROM movimientos WHERE informante='prueba' AND comentario='DATO DE PRUEBA'").rowcount
    c.commit()
    print(f"{n} cargas de prueba borradas")
    sys.exit(0)
tid = int(sys.argv[1])
c = sqlite3.connect(_db.DB)
nombre = c.execute("SELECT nombre FROM transportes WHERE id=?", (tid,)).fetchone()[0]
hoy = date.fromisoformat(_db.hoy())
# (dias atrás, patente, patente2, patente3, configuración, tipo, pallets, dni, nombre del chofer)
cargas = [
    (9, "ZZ103AA", "ZZ203AA", "", "tractor_semi", "refri", 30, "99000004", "PRUEBA, CHOFER CUATRO"),
    (8, "ZZ101AA", "ZZ201AA", "", "tractor_semi", "refri", 30, "99000001", "PRUEBA, CHOFER UNO"),
    (8, "ZZ102AA", "ZZ202AA", "", "tractor_semi", "seco", 28, "99000002", "PRUEBA, CHOFER DOS"),
    (6, "ZZ104AA", "ZZ204AA", "", "chasis_acoplado", "seco", 24, "99000005", "PRUEBA, CHOFER CINCO"),
    (5, "ZZ105AA", "ZZ205AA", "ZZ305AA", "tractor_bitren", "seco", 40, "99000006", "PRUEBA, CHOFER SEIS"),
    (4, "ZZ101AA", "ZZ201AA", "", "tractor_semi", "refri", 30, "99000001", "PRUEBA, CHOFER UNO"),
    (3, "ZZ106AA", "", "", "chasis_solo", "refri", 12, "99000007", "PRUEBA, CHOFER SIETE"),
    (3, "ZZ103AA", "ZZ203AA", "", "tractor_semi", "refri", 30, "99000003", "PRUEBA, CHOFER TRES"),
    (2, "ZZ102AA", "ZZ202AA", "", "tractor_semi", "seco", 28, "99000002", "PRUEBA, CHOFER DOS"),
    (1, "ZZ107AA", "ZZ207AA", "", "tractor_semi", "refri", 28, "", ""),
    (1, "ZZ101AA", "ZZ211AA", "", "tractor_semi", "refri", 26, "99000001", "PRUEBA, CHOFER UNO"),
]
n = 0
for dias, p1, p2, p3, conf, tipo, pl, dni, nom in cargas:
    f = (hoy - timedelta(days=dias)).isoformat()
    if c.execute("SELECT 1 FROM movimientos WHERE transporte_id=? AND fecha=? AND patente=? AND informante='prueba'", (tid, f, p1)).fetchone():
        continue                                          # ya cargado (se puede correr de nuevo)
    c.execute("""INSERT INTO movimientos (creado, fecha, hora, transporte_id, transporte, patente, patente2, patente3, configuracion,
                 tipo, pallets, accion, chofer_dni, chofer_nombre, comentario, informante, bloqueado)
                 VALUES (?,?,?,?,?,?,?,?,?,?,?,'agrega',?,?,'DATO DE PRUEBA','prueba',0)""",
              (f + "T07:30:00-03:00", f, "07:30", tid, nombre, p1, p2 or None, p3 or None, conf, tipo, pl, dni, nom))
    n += 1
c.commit()
print(f"{n} cargas de prueba agregadas a «{nombre}»")
