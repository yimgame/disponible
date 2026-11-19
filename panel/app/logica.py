"""Lógica del disponible: transportes, movimientos (pone/saca), usos, estado del día, alertas de bloqueo e imports.

Estado de cada patente en un día = su último evento del día (movimientos y usos no anulados, por hora):
  agrega -> disponible · uso -> usada (se descuenta) · quita -> sacada
Cargado = disponible + usada; quedan = disponible.
"""
import json
import re
import secrets
import unicodedata
from contextlib import closing
from datetime import date, datetime, timedelta

from . import db, webhooks

AVISO_DOC_DIAS = 15


class Error(ValueError):
    pass


def _clave(s):
    s = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode().lower()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", s).split())


def norm_patente(p):
    return re.sub(r"[\s.\-_]", "", str(p or "")).upper()[:10]


def patente_valida(p):
    return bool(re.fullmatch(r"(?=.*[A-Z])(?=.*\d)[A-Z0-9]{5,8}", p))


def norm_dni(d):
    d = re.sub(r"\D", "", str(d or "").split(".")[0]).lstrip("0")
    return d if re.fullmatch(r"\d{6,12}", d) else ""


def _txt(v, n=200):
    return " ".join(str(v or "").split())[:n]


def _fecha_iso(s):
    """'2026-11-19', '19/11/2026', '19-11-2026', serial de Excel o '2026-11-19 10:00 ...' -> 'AAAA-MM-DD' ('' si no)."""
    s = str(s or "").strip()
    m = re.match(r"(\d{4})-(\d{1,2})-(\d{1,2})", s)
    try:
        if m:
            return date(int(m[1]), int(m[2]), int(m[3])).isoformat()
        m = re.match(r"(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{4})", s)
        if m:
            return date(int(m[3]), int(m[2]), int(m[1])).isoformat()
        if re.fullmatch(r"\d{5}(\.\d+)?", s):
            return (date(1899, 12, 30) + timedelta(days=int(float(s)))).isoformat()
    except ValueError:
        pass
    return ""


# ------------------------------------------------------------------ transportes
def transportes(solo_activos=False):
    with closing(db.con()) as c:
        filas = [dict(r) for r in c.execute("SELECT * FROM transportes ORDER BY nombre COLLATE NOCASE")]
    return [t for t in filas if t["activo"] or not solo_activos]


def transporte(tid=None, token=None):
    with closing(db.con()) as c:
        r = (c.execute("SELECT * FROM transportes WHERE id=?", (tid,)) if tid else
             c.execute("SELECT * FROM transportes WHERE token=? AND activo=1", (str(token or ""),))).fetchone()
    return dict(r) if r else None


def guardar_transporte(datos, tid=None):
    nombre = _txt(datos.get("nombre"), 80)
    if len(nombre) < 2:
        raise Error("Poné el nombre del transporte.")
    campos = {"nombre": nombre, "nro_proveedor": re.sub(r"\D", "", str(datos.get("nro_proveedor") or "").split(".")[0]) or None,
              "cuota_dia": max(0, min(500, int(float(datos.get("cuota_dia") or 0)))),
              "telefono": re.sub(r"[^\d+]", "", str(datos.get("telefono") or ""))[:20],
              "activo": 0 if str(datos.get("activo", "1")) in ("0", "false", "False") else 1,
              "notas": _txt(datos.get("notas"), 500)}
    with closing(db.con()) as c, c:
        otro = c.execute("SELECT id FROM transportes WHERE nombre=? COLLATE NOCASE AND id IS NOT ?", (nombre, tid)).fetchone()
        if otro:
            raise Error("Ya hay un transporte con ese nombre.")
        if tid:
            c.execute(f"UPDATE transportes SET {', '.join(k + '=?' for k in campos)} WHERE id=?", [*campos.values(), tid])
        else:
            tid = c.execute(f"INSERT INTO transportes ({', '.join(campos)}, token, creado) VALUES ({', '.join('?' * len(campos))}, ?, ?)",
                            [*campos.values(), secrets.token_urlsafe(18), db.ahora_iso()]).lastrowid
    return transporte(tid)


def nuevo_token(tid):
    with closing(db.con()) as c, c:
        c.execute("UPDATE transportes SET token=? WHERE id=?", (secrets.token_urlsafe(18), tid))
    return transporte(tid)


def transportistas_del_maestro():
    """Nombres y nº de proveedor del maestro de unidades (para el alta de transportes)."""
    with closing(db.con()) as c:
        return [dict(r) for r in c.execute("""SELECT nro_proveedor, MAX(transportista) AS nombre, COUNT(*) AS unidades
                                              FROM unidades WHERE transportista IS NOT NULL AND transportista<>''
                                              GROUP BY nro_proveedor ORDER BY nombre""")]


# ------------------------------------------------------------------ alertas (bloqueos, documentos, GCG)
def _docs_vencidos(docs_json, hoy):
    vencidos, por_vencer = [], []
    for nombre, valor in (json.loads(docs_json or "{}") or {}).items():
        f = _fecha_iso(valor)
        if not f:
            continue
        if f < hoy:
            vencidos.append(f"{nombre} ({f[8:10]}/{f[5:7]}/{f[:4]})")
        elif f <= (date.fromisoformat(hoy) + timedelta(days=AVISO_DOC_DIAS)).isoformat():
            por_vencer.append(f"{nombre} ({f[8:10]}/{f[5:7]}/{f[:4]})")
    return vencidos, por_vencer


def info_unidad(patente):
    p = norm_patente(patente)
    with closing(db.con()) as c:
        u = c.execute("SELECT * FROM unidades WHERE patente=?", (p,)).fetchone()
        b = [dict(r) for r in c.execute("SELECT * FROM bloqueos WHERE tipo='unidad' AND clave=? AND activo=1", (p,))]
        g = c.execute("SELECT * FROM gcg WHERE tipo='vehiculo' AND clave=?", (p,)).fetchone()
    return {"patente": p, "unidad": dict(u) if u else None, "bloqueos": b, "gcg": dict(g) if g else None}


def info_chofer(dni):
    d = norm_dni(dni)
    with closing(db.con()) as c:
        b = [dict(r) for r in c.execute("SELECT * FROM bloqueos WHERE tipo='chofer' AND clave=? AND activo=1", (d,))]
        g = c.execute("SELECT * FROM gcg WHERE tipo='trabajador' AND clave=?", (d,)).fetchone()
    return {"dni": d, "bloqueos": b, "gcg": dict(g) if g else None}


def alertas(patentes, dni=None, en_vivo=True, fresco=False, forzar_vivo=False):
    """[{nivel: bloqueo|aviso, texto}] para mostrarle al transporte y guardar en el disponible.
    patentes: una patente o la lista del equipo (tractor, semi, acoplado…). Semáforo GCG: rojo bloquea, amarillo avisa."""
    if isinstance(patentes, str) or patentes is None:
        patentes = [patentes] if patentes else []
    varias = len(patentes) > 1
    hoy = db.hoy()
    out = []
    for patente in patentes:
        cual = f" ({norm_patente(patente)})" if varias else ""
        iu = info_unidad(patente)
        u = iu["unidad"]
        if u and u["bloqueado"]:
            extra = f" — documento vencido: {u['doc_vencido']}" if u.get("doc_vencido") else ""
            out.append({"nivel": "bloqueo", "texto": f"Unidad bloqueada en el maestro de unidades{cual}{extra}"})
        if u and u.get("fecha_expiracion") and u["fecha_expiracion"] < hoy:
            out.append({"nivel": "aviso", "texto": f"Unidad con fecha de expiración vencida ({u['fecha_expiracion']}){cual}"})
        for b in iu["bloqueos"]:
            out.append({"nivel": "bloqueo", "texto": f"Unidad bloqueada: {b['motivo'] or 'sin motivo'} ({b['origen']}){cual}"})
        out += _alertas_semaforo("unidad", patente, iu["gcg"], en_vivo, fresco, forzar_vivo, cual)
    if dni:
        ic = info_chofer(dni)
        for b in ic["bloqueos"]:
            out.append({"nivel": "bloqueo", "texto": f"Chofer bloqueado: {b['motivo'] or 'sin motivo'} ({b['origen']})"})
        out += _alertas_semaforo("chofer", dni, ic["gcg"], en_vivo, fresco, forzar_vivo)
    return out


def _alertas_semaforo(tipo, clave, g, en_vivo, fresco=False, forzar_vivo=False, cual=""):
    quien = "Chofer" if tipo == "chofer" else "Unidad"
    sem = semaforo(tipo, clave, g, en_vivo, fresco, forzar_vivo)
    out = []
    if sem["color"] == "rojo":
        out.append({"nivel": "bloqueo", "texto": f"{quien} en ROJO{cual}: {sem['texto']}"})
    elif sem["color"] == "amarillo":
        out.append({"nivel": "aviso", "texto": f"{quien} en AMARILLO{cual}: {sem['texto']}"})
    elif sem["color"] == "sin_dato" and tipo == "chofer":
        out.append({"nivel": "aviso", "texto": "El chofer no figura en el último exporte de GCG"})
    if sem.get("proximos"):
        out.append({"nivel": "aviso", "texto": ("Críticos por vencer: " if tipo == "chofer" else f"Críticos de la unidad por vencer{cual}: ")
                    + ", ".join(sem["proximos"])})
    if sem.get("otros_vencidos"):
        out.append({"nivel": "aviso", "texto": f"GCG {tipo}, otros documentos vencidos{cual}: " + ", ".join(sem["otros_vencidos"])})
    return out


def semaforo(tipo, clave, g=None, en_vivo=True, fresco=False, forzar_vivo=False):
    """Semáforo de un chofer (DNI) o una unidad (dominio): en vivo contra la API de GCG si está configurada
    (con caché), si no —o si GCG no responde— desde el último exporte importado."""
    from . import gcg_api                      # import local: gcg_api usa este módulo
    if en_vivo and (gcg_api.activo() if forzar_vivo else gcg_api.en_carga()):
        r = gcg_api.consultar(tipo, clave, usar_cache=not fresco)
        if r.get("ok"):
            return {**r["semaforo"], "proximos": r["proximos"], "criticos": r["criticos"], "otros_vencidos": [],
                    "pendientes": r["pendientes"], "consultado": r.get("consultado"),
                    "fuente": "GCG en vivo" + (" (caché)" if r.get("cache") else "")}
    if g is None:
        g = (info_chofer(clave) if tipo == "chofer" else info_unidad(clave))["gcg"]
    return semaforo_exporte(g, tipo)


# ------------------------------------------------------------------ semáforo documental del chofer (criterio de la app de bloqueos)
DIAS_ALERTA_CRITICOS = 7
PAIS_ISO = {"argentina": "AR", "paraguay": "PY", "brasil": "BR", "brazil": "BR", "chile": "CL", "bolivia": "BO",
            "uruguay": "UY", "peru": "PE", "perú": "PE"}


def _norm_doc(t):
    return re.sub(r"[^a-z0-9]", "", unicodedata.normalize("NFKD", str(t or "")).encode("ascii", "ignore").decode().lower())


def criticos(solo_activos=True, tipo=None):
    """tipo: "chofer" o "unidad" (a quién se le controla); None = todos."""
    with closing(db.con()) as c:
        filas = [dict(r) for r in c.execute("SELECT * FROM documentos_criticos ORDER BY aplica_a, orden, nombre")]
    return [d for d in filas if (d["activo"] or not solo_activos) and (not tipo or (d.get("aplica_a") or "chofer") == tipo)]


def _es_critico(nombre_doc, tipo="chofer"):
    n = _norm_doc(nombre_doc)
    return any(_norm_doc(d["nombre"]) == n for d in criticos(tipo=tipo))


def _aplica(doc, iso):
    paises = [p for p in re.split(r"[\s,;]+", (doc.get("paises") or "").upper()) if p]
    return not paises or not iso or iso.upper() in paises


def semaforo_chofer(g, hoy=None):
    return semaforo_exporte(g, "chofer", hoy)


def semaforo_exporte(g, tipo="chofer", hoy=None):
    """verde = habilitado en GCG sin críticos vencidos · amarillo = no habilitado, sin críticos vencidos ·
    rojo = algún crítico vencido (o dado de baja). Como en bloqueos: si GCG lo da HABILITADO, un crítico con fecha
    vencida es uno que ya no le pide (no lo bloquea); cada crítico se controla solo para sus países."""
    hoy = hoy or db.hoy()
    if not g:
        return {"color": "sin_dato", "texto": "no figura en GCG", "vencidos": [], "proximos": [], "criticos": [],
                "otros_vencidos": [], "pendientes": [], "fuente": "sin dato"}
    docs = {_norm_doc(k): (k, v) for k, v in json.loads(g.get("documentos") or "{}").items()}
    habilitado = (g.get("estado") or "") == "HABILITADO"
    baja = "baja" in (g.get("situacion") or "").lower()
    lim = (date.fromisoformat(hoy) + timedelta(days=DIAS_ALERTA_CRITICOS)).isoformat()
    vencidos, proximos, detalle = [], [], []
    for d in criticos(tipo=tipo):
        if not _aplica(d, g.get("pais")):
            continue
        nombre, valor = docs.get(_norm_doc(d["nombre"]), (d["nombre"], ""))
        f = _fecha_iso(valor)
        if not f:
            est = "sin dato"
        elif f < hoy:
            est = "no requerido" if habilitado else "vencido"
        elif f <= lim:
            est = "por vencer"
        else:
            est = "vigente"
        txt = f"{d['nombre']} ({f[8:10]}/{f[5:7]}/{f[:4]})" if f else d["nombre"]
        if est == "vencido":
            vencidos.append(txt)
        elif est == "por vencer":
            proximos.append(txt)
        detalle.append({"documento": d["nombre"], "fecha": f, "estado": est})
    if baja:
        color, texto = "rojo", "dado de baja en GCG"
    elif vencidos:
        color, texto = "rojo", "documentación crítica vencida: " + ", ".join(vencidos)
    elif not habilitado:
        color, texto = "amarillo", f"{(g.get('estado') or 'sin estado').lower()} en GCG (sin críticos vencidos)"
    else:
        color, texto = "verde", "habilitado, críticos al día" if tipo == "chofer" else "habilitada, críticos al día"
    otros, _ = _docs_vencidos(json.dumps({k: v for k, v in json.loads(g.get("documentos") or "{}").items()
                                          if not _es_critico(k, tipo)}), hoy)
    pendientes = [{"documento": d["documento"], "estado": d["estado"], "fecha": d["fecha"], "critico": True}
                  for d in detalle if d["estado"] in ("vencido", "por vencer")]
    pendientes += [{"documento": o.rsplit(" (", 1)[0], "estado": "vencido",
                    "fecha": _fecha_iso(o.rsplit(" (", 1)[1].rstrip(")").replace("/", "-")) if " (" in o else None, "critico": False}
                   for o in otros]
    return {"color": color, "texto": texto, "vencidos": vencidos, "proximos": proximos, "criticos": detalle,
            "otros_vencidos": otros, "pendientes": pendientes, "fuente": f"exporte de GCG del {(g.get('actualizado') or '')[:10]}"}


def guardar_critico(datos, did=None):
    nombre = _txt(datos.get("nombre"), 120)
    if len(nombre) < 3:
        raise Error("Poné el nombre del documento tal como figura en GCG.")
    try:
        numero = int(datos.get("gcg_numero")) if str(datos.get("gcg_numero") or "").strip() else None
    except ValueError:
        raise Error("El Nº GCG tiene que ser un número.")
    paises = ",".join(dict.fromkeys(p for p in re.split(r"[\s,;]+", str(datos.get("paises") or "").upper()) if p))
    activo = 0 if str(datos.get("activo", "1")) == "0" else 1
    aplica = "unidad" if datos.get("aplica_a") == "unidad" else "chofer"
    with closing(db.con()) as c, c:
        if did:
            c.execute("UPDATE documentos_criticos SET nombre=?, gcg_numero=?, paises=?, activo=?, aplica_a=? WHERE id=?",
                      (nombre, numero, paises, activo, aplica, did))
        else:
            if c.execute("SELECT 1 FROM documentos_criticos WHERE nombre=? COLLATE NOCASE", (nombre,)).fetchone():
                raise Error("Ese documento ya está en la lista.")
            c.execute("INSERT INTO documentos_criticos (nombre, gcg_numero, paises, activo, aplica_a, orden) VALUES (?,?,?,?,?,99)",
                      (nombre, numero, paises, activo, aplica))


def resumen_gcg(tipo="chofer"):
    """Semáforo de todos los choferes (o unidades) del último exporte de GCG."""
    with closing(db.con()) as c:
        filas = [dict(r) for r in c.execute("SELECT * FROM gcg WHERE tipo=?", ("trabajador" if tipo == "chofer" else "vehiculo",))]
    out = {"verde": 0, "amarillo": 0, "rojo": 0, "lista": [], "actualizado": max((f["actualizado"] for f in filas), default=None)}
    for g in filas:
        s = semaforo_exporte(g, tipo)
        out[s["color"]] = out.get(s["color"], 0) + 1
        if s["color"] in ("rojo", "amarillo"):
            out["lista"].append({"dni": g["clave"], "nombre": g["nombre"], "contratista": g["contratista"], "color": s["color"],
                                 "texto": s["texto"]})
    out["lista"].sort(key=lambda x: (x["color"] != "rojo", x["contratista"] or "", x["nombre"] or ""))
    return out


# ------------------------------------------------------------------ movimientos (lo que informa el transporte)
def _eta(v):
    """'2026-11-19T15:30' (input datetime-local) -> ISO local; '' si no viene o no se entiende."""
    s = str(v or "").strip()
    if not s:
        return ""
    try:
        d = datetime.fromisoformat(s.replace("Z", ""))
    except ValueError:
        raise Error("La fecha y hora de arribo no es válida.")
    return d.replace(tzinfo=None).isoformat(timespec="minutes")


CONFIGURACIONES = {   # cómo viene armado el equipo: nombre y rol de cada patente (Unidad 1, 2, 3)
    "tractor_semi": ("Tractor + Semi", ["Tractor", "Semi"]),
    "chasis_acoplado": ("Chasis + Acoplado", ["Chasis", "Acoplado"]),
    "tractor_bitren": ("Tractor + Bitren + Semi", ["Tractor", "Bitren", "Semi"]),
    "chasis_solo": ("Chasis / balancín solo", ["Chasis / balancín"]),
}
PALLETS_CHASIS_SOLO = (8, 16)


def clase_equipo(patente):
    """Clase según el tipo de equipo del maestro (CHASIS_…, BALANCIN_…, BITREN_…); None si no está o no se distingue."""
    u = info_unidad(patente)["unidad"]
    t = ((u or {}).get("tipo_equipo") or "").upper()
    if t.startswith(("CHASIS", "BALANCIN")):
        return "chasis"
    if t.startswith("BITREN"):
        return "bitren"
    if t.startswith(("CAMION_", "PORTA_CONTENEDOR")):
        return "semi"                    # unidad de carga enganchada a un tractor (semi / acoplado)
    return None


def validar_equipo(config, patentes, roles):
    """No se puede armar: tractor solo, semi solo, chasis + semi, tractor + acoplado (las configuraciones ya lo impiden)
    ni poner en una posición una patente que el maestro dice que es otra cosa."""
    for p, rol in zip(patentes, roles):
        cl = clase_equipo(p)
        es_chasis = rol.startswith("Chasis")
        if es_chasis and cl and cl != "chasis":
            raise Error(f"{p} figura como {cl} en el maestro de unidades: no puede ir como {rol.lower()}.")
        if not es_chasis and cl == "chasis":
            raise Error(f"{p} es un chasis / balancín en el maestro: elegí «Chasis + Acoplado» o «Chasis / balancín solo».")
        if rol == "Tractor" and cl == "semi":
            raise Error(f"{p} figura como semi / unidad de carga en el maestro: no puede ir como tractor (va en la unidad 2).")
        if cl == "bitren" and config != "tractor_bitren":
            raise Error(f"{p} figura como bitren en el maestro: elegí «Tractor + Bitren + Semi».")


def equipo(m):
    """[(patente, rol)] de un movimiento / fila."""
    roles = CONFIGURACIONES.get(m.get("configuracion") or "", (None, []))[1]
    pats = [p for p in (m.get("patente"), m.get("patente2"), m.get("patente3")) if p]
    return [(p, roles[i] if i < len(roles) else f"Unidad {i + 1}") for i, p in enumerate(pats)]


def registrar_movimiento(t, b, informante="link"):
    patente = norm_patente(b.get("patente"))
    if not patente_valida(patente):
        raise Error("Revisá la patente de la unidad 1 (ej. AB123CD o ABC123).")
    accion = "quita" if b.get("accion") == "quita" else "agrega"
    extras = [norm_patente(b.get(k)) for k in ("patente2", "patente3")]
    config = b.get("configuracion")
    if config not in CONFIGURACIONES:            # sin configuración: se deduce de cuántas patentes vinieron
        n = 1 + sum(1 for x in extras if x)
        config = {1: "chasis_solo", 2: "tractor_semi", 3: "tractor_bitren"}[n]
    roles = CONFIGURACIONES[config][1]
    patentes = [patente] + [x for x in extras][:len(roles) - 1]
    if accion == "agrega":
        for i, (p, rol) in enumerate(zip(patentes, roles)):
            if not patente_valida(p):
                raise Error(f"Falta o está mal la patente de la unidad {i + 1} ({rol.lower()}).")
        if len(set(patentes)) < len(patentes):
            raise Error("Hay patentes repetidas en el equipo.")
        validar_equipo(config, patentes, roles)
    else:
        patentes, config = [patente], None
    # tipo y pallets: del maestro de la unidad que carga (la última del equipo: semi, acoplado o el chasis)
    u = info_unidad(patentes[-1])["unidad"] or info_unidad(patente)["unidad"]
    tipo = b.get("tipo") if b.get("tipo") in ("refri", "seco") else (u["tipo"] if u else "")
    try:
        pallets = int(float(b.get("pallets") or (u["pallets"] if u and u["pallets"] else 0)))
    except ValueError:
        pallets = 0
    dni = norm_dni(b.get("chofer_dni"))
    if b.get("chofer_dni") and not dni:
        raise Error("Revisá el DNI del chofer.")
    if accion == "agrega":
        if not tipo:
            raise Error("Elegí Refrigerado o Seco.")
        if not 1 <= pallets <= 40:
            raise Error("Pallets entre 1 y 40.")
        if config == "chasis_solo" and not PALLETS_CHASIS_SOLO[0] <= pallets <= PALLETS_CHASIS_SOLO[1]:
            raise Error(f"Un chasis / balancín solo lleva de {PALLETS_CHASIS_SOLO[0]} a {PALLETS_CHASIS_SOLO[1]} pallets.")
    eta = _eta(b.get("eta")) if accion == "agrega" else ""
    al = alertas(patentes, dni) if accion == "agrega" else []
    bloq = [a["texto"] for a in al if a["nivel"] == "bloqueo"]
    ahora = db.ahora()
    gch = info_chofer(dni)["gcg"] if dni else None
    chofer_nombre = (gch or {}).get("nombre")
    det = detalle_gcg(list(zip(patentes, roles)), dni, gch) if accion == "agrega" else {}
    chofer_semaforo = (det.get("chofer") or {}).get("color")
    unidad_semaforo = (det.get("unidad") or {}).get("color")
    fila = {"creado": ahora.isoformat(timespec="microseconds"), "fecha": ahora.date().isoformat(),
            "hora": ahora.strftime("%H:%M"), "transporte_id": t["id"], "transporte": t["nombre"], "patente": patente,
            "patente2": patentes[1] if len(patentes) > 1 else None, "patente3": patentes[2] if len(patentes) > 2 else None,
            "configuracion": config,
            "tipo": tipo, "pallets": pallets or None, "accion": accion, "eta": eta, "chofer_dni": dni,
            "chofer_nombre": chofer_nombre, "chofer_semaforo": chofer_semaforo, "unidad_semaforo": unidad_semaforo,
            "gcg_detalle": json.dumps(det, ensure_ascii=False) if det else None, "gcg_consultado": db.ahora_iso() if det else None, "comentario": _txt(b.get("comentario"), 200), "informante": informante,
            "bloqueado": 1 if bloq else 0, "bloqueo_motivo": " · ".join(bloq)[:500],
            "avisos": " · ".join(a["texto"] for a in al if a["nivel"] != "bloqueo")[:800]}
    with closing(db.con()) as c, c:
        fila["id"] = c.execute(f"INSERT INTO movimientos ({', '.join(fila)}) VALUES ({', '.join('?' * len(fila))})",
                               list(fila.values())).lastrowid
        if det:
            c.execute("INSERT INTO gcg_historial (movimiento_id, consultado, por, motivo, detalle) VALUES (?,?,?,?,?)",
                      (fila["id"], fila["gcg_consultado"], informante, "carga", fila["gcg_detalle"]))
    webhooks.enviar("movimiento", {**fila, "alertas": al})
    if bloq:
        webhooks.enviar("alerta_bloqueo", {"transporte": t["nombre"], "patente": patente, "patentes": patentes, "chofer_dni": dni,
                                           "motivos": bloq, "movimiento_id": fila["id"]})
    return fila, al


ORDEN_COLOR = {"rojo": 3, "amarillo": 2, "sin_dato": 1, "verde": 0}


def detalle_gcg(equipo_, dni=None, gch=None, fresco=False, forzar_vivo=False):
    """Semáforo + pendientes de cada patente del equipo y del chofer (lo que ve el proveedor y va a la columna GCG).
    equipo_: [(patente, rol)] o una patente sola. "unidad" resume el equipo (el peor color y todos los pendientes)."""
    if isinstance(equipo_, str):
        equipo_ = [(equipo_, None)]
    out, unidades = {}, []
    for patente, rol in equipo_:
        s = semaforo("unidad", patente, None, True, fresco, forzar_vivo)
        unidades.append({"patente": norm_patente(patente), "rol": rol, "color": s["color"], "texto": s["texto"],
                         "fuente": s.get("fuente"), "pendientes": s.get("pendientes", [])})
    if unidades:
        peor = max(unidades, key=lambda x: ORDEN_COLOR.get(x["color"], 0))
        varias = len(unidades) > 1
        out["unidades"] = unidades
        out["unidad"] = {"color": peor["color"], "texto": peor["texto"], "fuente": peor["fuente"],
                         "pendientes": [{**p, "documento": (f"{x['patente']} · " if varias else "") + p["documento"]}
                                        for x in unidades for p in x["pendientes"]]}
    if dni:
        s = semaforo("chofer", dni, gch, True, fresco, forzar_vivo)
        out["chofer"] = {"color": s["color"], "texto": s["texto"], "fuente": s.get("fuente"), "pendientes": s.get("pendientes", [])}
    return out


def historial_gcg(mov_id):
    """Cómo estaban los documentos en cada consulta de esa carga (la de la carga y cada reconsulta)."""
    with closing(db.con()) as c:
        return [{**dict(r), "detalle": json.loads(r["detalle"] or "{}")}
                for r in c.execute("SELECT * FROM gcg_historial WHERE movimiento_id=? ORDER BY consultado", (mov_id,))]


def reconsultar_gcg(mov_id, transporte_id=None, por=""):
    """Vuelve a consultar GCG (sin caché) para una unidad ya cargada: actualiza semáforos, pendientes y el bloqueo."""
    with closing(db.con()) as c:
        m = c.execute("SELECT * FROM movimientos WHERE id=?", (mov_id,)).fetchone()
    if not m or (transporte_id and m["transporte_id"] != transporte_id):
        raise Error("No existe esa carga.")
    m = dict(m)
    if m["accion"] != "agrega":
        raise Error("Solo se reconsultan unidades cargadas.")
    eq = equipo(m)
    det = detalle_gcg(eq, m["chofer_dni"], None, fresco=True, forzar_vivo=True)
    al = alertas([p for p, _ in eq], m["chofer_dni"], en_vivo=True, forzar_vivo=True)   # usa la respuesta recién guardada
    bloq = [a["texto"] for a in al if a["nivel"] == "bloqueo"]
    antes = m["bloqueado"]
    with closing(db.con()) as c, c:
        c.execute("""UPDATE movimientos SET unidad_semaforo=?, chofer_semaforo=?, gcg_detalle=?, gcg_consultado=?, bloqueado=?,
                     bloqueo_motivo=?, avisos=? WHERE id=?""",
                  ((det.get("unidad") or {}).get("color"), (det.get("chofer") or {}).get("color"), json.dumps(det, ensure_ascii=False),
                   db.ahora_iso(), 1 if bloq else 0, " · ".join(bloq)[:500],
                   " · ".join(a["texto"] for a in al if a["nivel"] != "bloqueo")[:800], mov_id))
        c.execute("INSERT INTO gcg_historial (movimiento_id, consultado, por, motivo, detalle) VALUES (?,?,?,?,?)",
                  (mov_id, db.ahora_iso(), por, "reconsulta", json.dumps(det, ensure_ascii=False)))
    res = {"movimiento_id": mov_id, "patente": m["patente"], "patentes": [p for p, _ in eq], "transporte": m["transporte"], "gcg": det,
           "bloqueado": 1 if bloq else 0, "se_desbloqueo": bool(antes and not bloq), "alertas": al, "por": por}
    webhooks.enviar("gcg_reconsulta", res)
    return res


# ------------------------------------------------------------------ estado del día
def estado_dia(fecha=None, transporte_id=None):
    fecha = fecha or db.hoy()
    with closing(db.con()) as c:
        movs = [dict(r) for r in c.execute("SELECT * FROM movimientos WHERE fecha=? ORDER BY creado", (fecha,))]
        usos = [dict(r) for r in c.execute("SELECT * FROM usos WHERE fecha=? AND anulado=0 ORDER BY creado", (fecha,))]
        tts = {t["id"]: dict(t) for t in c.execute("SELECT * FROM transportes")}
    eventos = sorted([("m", m["creado"], m) for m in movs] + [("u", u["creado"], u) for u in usos], key=lambda e: e[1])
    ult = {}
    for clase, _, e in eventos:
        p = e["patente"]
        if clase == "m":
            if e["accion"] == "agrega":
                ult[p] = {"estado": "disponible", "mov": e, "uso": None}
            elif p in ult and ult[p]["estado"] == "disponible":
                ult[p] = {"estado": "sacada", "mov": e, "uso": None}
        elif p in ult:
            ult[p] = {"estado": "usada", "mov": ult[p]["mov"], "uso": e}
    filas = []
    for p, s in ult.items():
        m = s["mov"]
        if transporte_id and m["transporte_id"] != transporte_id:
            continue
        if s["estado"] == "sacada":
            continue
        filas.append({"patente": p, "estado": s["estado"], "transporte_id": m["transporte_id"], "transporte": m["transporte"],
                      "tipo": m["tipo"], "pallets": m["pallets"] or 0, "eta": m["eta"], "hora": m["hora"],
                      "chofer_dni": m["chofer_dni"], "chofer_nombre": m["chofer_nombre"],
                      "chofer_semaforo": m.get("chofer_semaforo"), "unidad_semaforo": m.get("unidad_semaforo"),
                      "gcg": json.loads(m["gcg_detalle"]) if m.get("gcg_detalle") else None,
                      "patente2": m.get("patente2"), "patente3": m.get("patente3"), "configuracion": m.get("configuracion"),
                      "equipo": [{"patente": p, "rol": r} for p, r in equipo(m)],
                      "gcg_consultado": m.get("gcg_consultado"),
                      "comentario": m["comentario"],
                      "bloqueado": m["bloqueado"], "bloqueo_motivo": m["bloqueo_motivo"], "movimiento_id": m["id"],
                      "uso": s["uso"]})
    filas.sort(key=lambda f: (f["estado"] != "disponible", f["transporte"] or "", f["eta"] or "9", f["patente"]))

    def vacio():
        return {"cargadas": 0, "usadas": 0, "quedan": 0, "pallets_quedan": 0, "bloqueadas": 0,
                "refri": 0, "seco": 0, "refri_quedan": 0, "seco_quedan": 0}
    tot, por_t = vacio(), {}
    for f in filas:
        for g in (tot, por_t.setdefault(f["transporte_id"], vacio())):
            g["cargadas"] += 1
            g[f["tipo"] or "seco"] += 1
            if f["estado"] == "usada":
                g["usadas"] += 1
            else:
                g["quedan"] += 1
                g["pallets_quedan"] += f["pallets"]
                g[(f["tipo"] or "seco") + "_quedan"] += 1
                g["bloqueadas"] += f["bloqueado"]
    resumen = []
    for tid, t in tts.items():
        if transporte_id and tid != transporte_id:
            continue
        g = por_t.get(tid, vacio())
        if not t["activo"] and not g["cargadas"]:
            continue
        cuota = t["cuota_dia"] or 0
        resumen.append({"transporte_id": tid, "transporte": t["nombre"], "cuota": cuota, "token": t["token"],
                        "telefono": t["telefono"], **g,
                        "cumplimiento": round(g["cargadas"] * 100 / cuota) if cuota else None})
    resumen.sort(key=lambda r: (r["cumplimiento"] if r["cumplimiento"] is not None else 999, r["transporte"]))
    return {"fecha": fecha, "filas": filas, "totales": tot, "transportes": resumen,
            "puso": sum(1 for m in movs if m["accion"] == "agrega" and (not transporte_id or m["transporte_id"] == transporte_id)),
            "saco": sum(1 for m in movs if m["accion"] == "quita" and (not transporte_id or m["transporte_id"] == transporte_id))}


# ------------------------------------------------------------------ usos (se descuenta del disponible)
def usar(patente, datos, usuario, fecha=None):
    fecha = fecha or db.hoy()
    p = norm_patente(patente)
    f = next((x for x in estado_dia(fecha)["filas"] if x["patente"] == p), None)
    if not f:
        raise Error(f"{p} no está en el disponible del {fecha}.")
    if f["estado"] != "disponible":
        raise Error(f"{p} ya fue asignada.")
    destino = _txt(datos.get("destino"), 120)
    if not destino:
        raise Error("Elegí la base o el destino al que se asigna.")
    guardar_base(destino, solo_si_no_existe=True)
    uso = {"creado": db.ahora().isoformat(timespec="microseconds"), "fecha": fecha, "movimiento_id": f["movimiento_id"],
           "transporte_id": f["transporte_id"], "transporte": f["transporte"], "patente": p, "tipo": f["tipo"],
           "pallets": f["pallets"], "destino": destino, "viaje": _txt(datos.get("viaje"), 60),
           "planta": _txt(datos.get("planta"), 80), "detalle": _txt(datos.get("detalle"), 300), "usuario": usuario}
    with closing(db.con()) as c, c:
        uso["id"] = c.execute(f"INSERT INTO usos ({', '.join(uso)}) VALUES ({', '.join('?' * len(uso))})",
                              list(uso.values())).lastrowid
    webhooks.enviar("uso", uso)
    return uso


def anular_uso(uid, usuario):
    with closing(db.con()) as c, c:
        r = c.execute("SELECT * FROM usos WHERE id=? AND anulado=0", (uid,)).fetchone()
        if not r:
            raise Error("No existe ese uso (o ya estaba anulado).")
        c.execute("UPDATE usos SET anulado=1, anulado_por=?, anulado_en=? WHERE id=?", (usuario, db.ahora_iso(), uid))
    webhooks.enviar("uso_anulado", {**dict(r), "anulado_por": usuario})


def listar(tabla, desde, hasta, transporte_id=None):
    assert tabla in ("usos", "movimientos")
    sql = f"SELECT * FROM {tabla} WHERE fecha BETWEEN ? AND ?"
    args = [desde, hasta]
    if transporte_id:
        sql += " AND transporte_id=?"
        args.append(transporte_id)
    with closing(db.con()) as c:
        return [dict(r) for r in c.execute(sql + " ORDER BY creado DESC LIMIT 20000", args)]


# ------------------------------------------------------------------ bloqueos manuales
def agregar_bloqueo(tipo, clave, motivo, usuario, origen="manual"):
    if tipo not in ("unidad", "chofer"):
        raise Error("Tipo inválido.")
    clave = norm_patente(clave) if tipo == "unidad" else norm_dni(clave)
    if not clave:
        raise Error("Revisá la patente o el DNI.")
    with closing(db.con()) as c, c:
        c.execute("""INSERT INTO bloqueos (tipo, clave, motivo, origen, activo, creado, creado_por) VALUES (?,?,?,?,1,?,?)
                     ON CONFLICT(tipo, clave, origen) DO UPDATE SET motivo=excluded.motivo, activo=1,
                     creado=excluded.creado, creado_por=excluded.creado_por""",
                  (tipo, clave, _txt(motivo, 300) or None, origen, db.ahora_iso(), usuario))


def quitar_bloqueo(bid):
    with closing(db.con()) as c, c:
        c.execute("UPDATE bloqueos SET activo=0 WHERE id=?", (bid,))


def bloqueos(activos=True):
    with closing(db.con()) as c:
        return [dict(r) for r in c.execute(f"SELECT * FROM bloqueos {'WHERE activo=1' if activos else ''} ORDER BY creado DESC")]


# ------------------------------------------------------------------ imports (el navegador lee los Excel y manda columnas)
def _si(v):
    return 1 if str(v or "").strip().upper() in ("Y", "S", "SI", "SÍ", "YES", "TRUE", "1", "X") else 0


def _tipo_equipo(t):
    t = str(t or "").upper()
    return "refri" if re.search(r"REF|FRIG|TERM", t) else "seco"


def importar_unidades(filas, simular=True):
    """Exporte de equipos: patente, tipo de equipo, pallets, kg, ejes, bloqueado, doc. vencido, expiración, proveedor."""
    r = {"leidas": 0, "nuevas": 0, "actualizadas": 0, "bloqueadas": 0, "invalidas": 0, "refri": 0, "seco": 0}
    with closing(db.con()) as c, c:
        existentes = {x[0] for x in c.execute("SELECT patente FROM unidades")}
        vistos = set()
        for f in filas:
            p = norm_patente(str(f.get("patente") or "").split("_")[0])
            if not patente_valida(p) or p in vistos:
                r["invalidas"] += not patente_valida(p)
                continue
            vistos.add(p)
            num = lambda v: (float(str(v).replace(",", ".")) if re.fullmatch(r"-?\d+([.,]\d+)?", str(v or "").strip()) else None)
            pal = num(f.get("pallets"))
            fila = {"patente": p, "nro_proveedor": re.sub(r"\D", "", str(f.get("nro_proveedor") or "").split(".")[0]) or None,
                    "transportista": _txt(f.get("transportista"), 120), "tipo": _tipo_equipo(f.get("tipo_equipo")),
                    "tipo_equipo": _txt(f.get("tipo_equipo"), 60),
                    "pallets": int(pal) if pal and 0 < pal <= 60 else None, "kg_cap": num(f.get("kg")),
                    "ejes": int(num(f.get("ejes")) or 0) if (num(f.get("ejes")) or 0) <= 10 else None,
                    "satelital": _si(f.get("satelital")), "dedicado": _si(f.get("dedicado")),
                    "bloqueado": _si(f.get("bloqueado")), "doc_vencido": _txt(f.get("doc_vencido"), 200) or None,
                    "fecha_expiracion": _fecha_iso(f.get("expiracion")) or None, "actualizado": db.ahora_iso()}
            r["leidas"] += 1
            r["actualizadas" if p in existentes else "nuevas"] += 1
            r["bloqueadas"] += fila["bloqueado"]
            r[fila["tipo"]] += 1
            if not simular:
                c.execute(f"INSERT OR REPLACE INTO unidades ({', '.join(fila)}) VALUES ({', '.join('?' * len(fila))})",
                          list(fila.values()))
    return r


GCG_FIJAS = {"tipo", "id", "nombredominio", "cuitcuil", "dni", "genero", "pais", "cliente", "locaciones",
             "tipodecontratista", "estado", "vencimiento", "responsable", "situacion", "contratista", "subcontratista",
             "matrizdedocumentos", "nivelderiesgo", "ordenesdecompra", "condiciontrabajador"}


def importar_gcg(filas, simular=True):
    """Exporte de GCG: trabajadores (por DNI) y vehículos (por dominio), con estado y vencimiento de cada documento."""
    r = {"trabajadores": 0, "vehiculos": 0, "no_habilitados": 0, "invalidas": 0, "documentos": 0}
    ahora = db.ahora_iso()
    with closing(db.con()) as c, c:
        vistos = set()
        for f in filas:
            es_trab = _clave(f.get("tipo")).startswith("trabajador") or (f.get("dni") and not f.get("tipo"))
            tipo = "trabajador" if es_trab else "vehiculo"
            clave = norm_dni(f.get("dni")) if es_trab else norm_patente(f.get("nombre"))
            if not clave or (tipo == "vehiculo" and not patente_valida(clave)) or (tipo, clave) in vistos:
                r["invalidas"] += not clave
                continue
            vistos.add((tipo, clave))
            docs = {k: v for k, v in (f.get("documentos") or {}).items() if str(v or "").strip() not in ("", "-")}
            estado = _txt(f.get("estado"), 40).upper()
            fila = {"tipo": tipo, "clave": clave, "nombre": " ".join(str(f.get("nombre") or "").replace(",", " ").split()).title()[:120],
                    "cuil": re.sub(r"\D", "", str(f.get("cuil") or ""))[:11] or None, "estado": estado or None,
                    "contratista": _txt(f.get("contratista"), 120) or None,
                    "subcontratista": _txt(f.get("subcontratista"), 120) or None,
                    "vencimiento": _fecha_iso(f.get("vencimiento")) or None,
                    "documentos": json.dumps(docs, ensure_ascii=False), "actualizado": ahora,
                    "pais": PAIS_ISO.get(_clave(f.get("pais")), (str(f.get("pais") or "").strip().upper()[:2] or None)),
                    "situacion": _txt(f.get("situacion"), 40) or None}
            r["trabajadores" if es_trab else "vehiculos"] += 1
            r["no_habilitados"] += estado not in ("", "HABILITADO")
            r["documentos"] += len(docs)
            if not simular:
                c.execute(f"INSERT OR REPLACE INTO gcg ({', '.join(fila)}) VALUES ({', '.join('?' * len(fila))})",
                          list(fila.values()))
    return r


def importar_bloqueados(filas, usuario, simular=True, reemplazar=True):
    """Lista de bloqueados (patente y/o DNI + motivo). reemplazar: la lista nueva reemplaza a la anterior volcada."""
    r = {"unidades": 0, "choferes": 0, "invalidas": 0, "desactivados": 0}
    nuevos = []
    for f in filas:
        p, d = norm_patente(f.get("patente")), norm_dni(f.get("dni"))
        motivo = _txt(f.get("motivo"), 300)
        ok = False
        if p and patente_valida(p):
            nuevos.append(("unidad", p, motivo)); r["unidades"] += 1; ok = True
        if d:
            nuevos.append(("chofer", d, motivo)); r["choferes"] += 1; ok = True
        r["invalidas"] += not ok
    if simular:
        with closing(db.con()) as c:
            r["desactivados"] = c.execute("SELECT COUNT(*) FROM bloqueos WHERE origen='lista' AND activo=1").fetchone()[0] if reemplazar else 0
        return r
    with closing(db.con()) as c, c:
        if reemplazar:
            r["desactivados"] = c.execute("UPDATE bloqueos SET activo=0 WHERE origen='lista' AND activo=1").rowcount
    for tipo, clave, motivo in nuevos:
        agregar_bloqueo(tipo, clave, motivo, usuario, origen="lista")
    return r


# ------------------------------------------------------------------ panel: rango de fechas (Desde / Hasta)
def rango(desde, hasta):
    """Cierre de cada día del rango (con movimientos) + los movimientos y usos del rango."""
    d0, d1 = date.fromisoformat(desde), date.fromisoformat(hasta)
    if d1 < d0:
        d0, d1 = d1, d0
    if (d1 - d0).days > 92:
        raise Error("El rango máximo es de 3 meses.")
    with closing(db.con()) as c:
        dias = [r[0] for r in c.execute("SELECT DISTINCT fecha FROM movimientos WHERE fecha BETWEEN ? AND ? ORDER BY fecha DESC",
                                        (d0.isoformat(), d1.isoformat()))]
        tts = [{k: t[k] for k in ("id", "nombre", "nro_proveedor", "token", "cuota_dia", "telefono", "activo")}
               for t in c.execute("SELECT * FROM transportes ORDER BY nombre COLLATE NOCASE")]
    cierres = []
    for d in dias:
        e = estado_dia(d)
        cierres.append({"fecha": d, "filas": e["filas"]})
    return {"desde": d0.isoformat(), "hasta": d1.isoformat(), "dias": cierres, "transportes": tts,
            "movimientos": listar("movimientos", d0.isoformat(), d1.isoformat()),
            "usos": listar("usos", d0.isoformat(), d1.isoformat())}



# ------------------------------------------------------------------ bases / destinos
def bases(solo_activas=False):
    with closing(db.con()) as c:
        filas = [dict(r) for r in c.execute("""SELECT b.*, (SELECT COUNT(*) FROM usos u WHERE u.destino = b.nombre COLLATE NOCASE
                                                       AND u.anulado = 0) AS usos FROM bases b ORDER BY b.nombre COLLATE NOCASE""")]
    return [b for b in filas if b["activo"] or not solo_activas]


def guardar_base(nombre, tipo=None, activo=1, bid=None, solo_si_no_existe=False):
    nombre = _txt(nombre, 120)
    if len(nombre) < 2:
        raise Error("Poné el nombre de la base.")
    with closing(db.con()) as c, c:
        ya = c.execute("SELECT id FROM bases WHERE nombre=? COLLATE NOCASE", (nombre,)).fetchone()
        if solo_si_no_existe:
            if not ya:
                c.execute("INSERT INTO bases (nombre, tipo, activo, creado) VALUES (?,?,1,?)", (nombre, tipo, db.ahora_iso()))
            return
        if bid:
            if ya and ya["id"] != bid:
                raise Error("Ya hay una base con ese nombre.")
            c.execute("UPDATE bases SET nombre=?, tipo=?, activo=? WHERE id=?", (nombre, _txt(tipo, 40) or None, 1 if activo else 0, bid))
        elif ya:
            raise Error("Esa base ya existe.")
        else:
            c.execute("INSERT INTO bases (nombre, tipo, activo, creado) VALUES (?,?,1,?)", (nombre, _txt(tipo, 40) or None, db.ahora_iso()))


# ------------------------------------------------------------------ links para todos los proveedores del maestro
def generar_transportes_maestro(min_unidades=1, simular=True):
    """Crea un transporte (con su link) por cada proveedor del maestro de unidades que todavía no tenga uno."""
    existentes = transportes()
    por_prov = {t["nro_proveedor"] for t in existentes if t["nro_proveedor"]}
    por_nombre = {_clave(t["nombre"]) for t in existentes}
    nuevos = [m for m in transportistas_del_maestro() if (m["unidades"] or 0) >= min_unidades and m["nro_proveedor"]
              and m["nro_proveedor"] not in por_prov and _clave(m["nombre"]) not in por_nombre]
    if not simular:
        for m in nuevos:
            try:
                guardar_transporte({"nombre": m["nombre"], "nro_proveedor": m["nro_proveedor"], "cuota_dia": 0})
            except Error:                                  # mismo nombre que otro proveedor: lo distingo
                guardar_transporte({"nombre": f"{m['nombre']} ({m['nro_proveedor']})"[:80], "nro_proveedor": m["nro_proveedor"]})
    return {"nuevos": len(nuevos), "ya_tenian": len(por_prov), "ejemplos": [m["nombre"] for m in nuevos[:12]]}


# ------------------------------------------------------------------ análisis de rendimiento + Ishikawa
CAUSAS = {   # categoría -> [(clave, texto)]
    "Transporte": [("bajo_cuota", "Días por debajo de la cuota"), ("sin_cargar", "Días sin cargar nada"),
                   ("sacadas", "Sacó unidades ya cargadas"), ("sin_eta", "Cargadas sin ETA")],
    "Unidad": [("unidad_rojo", "Unidad en rojo (críticos vencidos)"), ("unidad_gcg", "Unidad en amarillo (no habilitada)"),
               ("bloq_maestro", "Bloqueada en el maestro de unidades"), ("expiracion", "Fecha de expiración vencida"),
               ("fuera_maestro", "Patente que no está en el maestro")],
    "Chofer": [("chofer_rojo", "Chofer en rojo (críticos vencidos)"), ("chofer_gcg", "Chofer en amarillo (no habilitado)"),
               ("sin_chofer", "Cargadas sin chofer informado"), ("chofer_fuera_gcg", "Chofer que no figura en GCG")],
    "Documentación": [("criticos_por_vencer", "Críticos por vencer (7 días)"), ("docs_chofer", "Otros documentos vencidos del chofer"),
                      ("docs_unidad", "Otros documentos vencidos de la unidad")],
    "Bloqueos": [("unidad_lista", "Unidad en lista de bloqueados o manual"), ("chofer_bloq_l", "Chofer en lista de bloqueados o manual")],
    "Asignación": [("ociosas", "Disponibles que quedaron sin asignar"), ("asignadas_bloq", "Asignadas estando bloqueadas"),
                   ("anuladas", "Asignaciones anuladas")],
}


def analisis(desde, hasta, transporte_id=None):
    r = rango(desde, hasta)
    tts = {t["id"]: t for t in r["transportes"]}
    movs = [m for m in r["movimientos"] if not transporte_id or m["transporte_id"] == transporte_id]
    usos = [u for u in r["usos"] if not transporte_id or u["transporte_id"] == transporte_id]
    with closing(db.con()) as c:
        en_maestro = {x[0] for x in c.execute("SELECT patente FROM unidades")}
    dias = sorted(d["fecha"] for d in r["dias"])
    activos = [t for t in tts.values() if t["activo"] and (not transporte_id or t["id"] == transporte_id)]
    meta_dia = sum(t["cuota_dia"] or 0 for t in activos)

    serie, por_t = [], {}
    casos = {k: [] for cat in CAUSAS.values() for k, _ in cat}
    for d in r["dias"]:
        filas = [f for f in d["filas"] if not transporte_id or f["transporte_id"] == transporte_id]
        car = len(filas)
        asi = sum(f["estado"] == "usada" for f in filas)
        serie.append({"fecha": d["fecha"], "cargadas": car, "asignadas": asi, "meta": meta_dia,
                      "refri": sum(f["tipo"] == "refri" for f in filas), "seco": sum(f["tipo"] != "refri" for f in filas)})
        cnt = {}
        for f in filas:
            g = por_t.setdefault(f["transporte_id"], {"cargadas": 0, "asignadas": 0, "bloqueadas": 0, "ociosas": 0})
            g["cargadas"] += 1
            cnt[f["transporte_id"]] = cnt.get(f["transporte_id"], 0) + 1
            if f["estado"] == "usada":
                g["asignadas"] += 1
                if f["bloqueado"]:
                    casos["asignadas_bloq"].append({"fecha": d["fecha"], "transporte": f["transporte"], "patente": f["patente"], "detalle": f["bloqueo_motivo"]})
            else:
                g["ociosas"] += 1
                casos["ociosas"].append({"fecha": d["fecha"], "transporte": f["transporte"], "patente": f["patente"], "detalle": "quedó disponible al cierre"})
            g["bloqueadas"] += f["bloqueado"]
        for t in activos:
            if t["cuota_dia"] and cnt.get(t["id"], 0) < t["cuota_dia"]:
                det = f"cargó {cnt.get(t['id'], 0)} de {t['cuota_dia']}"
                casos["bajo_cuota"].append({"fecha": d["fecha"], "transporte": t["nombre"], "patente": "", "detalle": det})
                if not cnt.get(t["id"]):
                    casos["sin_cargar"].append({"fecha": d["fecha"], "transporte": t["nombre"], "patente": "", "detalle": det})
    serie.sort(key=lambda x: x["fecha"])

    horas, anticip = [0] * 24, []
    for m in movs:
        base = {"fecha": m["fecha"], "transporte": m["transporte"], "patente": m["patente"]}
        if m["accion"] == "quita":
            casos["sacadas"].append({**base, "detalle": m["comentario"] or ""})
            continue
        horas[int((m["hora"] or "0:0").split(":")[0]) % 24] += 1
        if m["eta"]:
            try:
                h = (datetime.fromisoformat(m["eta"]) - datetime.fromisoformat(m["creado"][:19])).total_seconds() / 3600
                anticip.append(round(h, 1))
            except ValueError:
                pass
        else:
            casos["sin_eta"].append({**base, "detalle": ""})
        if m["patente"] not in en_maestro:
            casos["fuera_maestro"].append({**base, "detalle": ""})
        if not m["chofer_dni"]:
            casos["sin_chofer"].append({**base, "detalle": ""})
        # cada motivo / aviso por separado (vienen unidos con " · ")
        partes = [x.strip() for x in ((m["bloqueo_motivo"] or "") + " · " + (m.get("avisos") or "")).split(" · ") if x.strip()]
        reglas = (("bloq_maestro", lambda x: x.startswith("Unidad bloqueada en el maestro")),
                  ("chofer_rojo", lambda x: x.startswith("Chofer en ROJO")),
                  ("chofer_gcg", lambda x: x.startswith("Chofer en AMARILLO") or (x.startswith("Chofer ") and x.endswith("en GCG"))),
                  ("criticos_por_vencer", lambda x: x.startswith("Críticos por vencer") or x.startswith("Críticos de la unidad por vencer")),
                  ("chofer_bloq_l", lambda x: x.startswith("Chofer bloqueado:")),
                  ("unidad_rojo", lambda x: x.startswith("Unidad en ROJO")),
                  ("unidad_gcg", lambda x: x.startswith("Unidad en AMARILLO") or (x.startswith("Unidad ") and x.endswith("en GCG"))),
                  ("unidad_lista", lambda x: x.startswith("Unidad bloqueada:")),
                  ("expiracion", lambda x: "expiración vencida" in x),
                  ("docs_unidad", lambda x: x.startswith("GCG unidad, otros documentos vencidos") or x.startswith("GCG unidad, documentos vencidos")),
                  ("docs_chofer", lambda x: x.startswith("GCG chofer, otros documentos vencidos") or x.startswith("GCG chofer, documentos vencidos")),
                  ("chofer_fuera_gcg", lambda x: "no figura en el último exporte de GCG" in x))
        for clave, cumple in reglas:
            hit = next((x for x in partes if cumple(x)), None)
            if hit:
                casos[clave].append({**base, "detalle": hit[:200]})
    with closing(db.con()) as c:
        anuladas = [dict(x) for x in c.execute("""SELECT fecha, transporte, patente, destino, anulado_por FROM usos
                                                  WHERE anulado=1 AND fecha BETWEEN ? AND ?""", (r["desde"], r["hasta"]))]
    for a in anuladas:
        if not transporte_id or a["transporte"] == (tts.get(transporte_id) or {}).get("nombre"):
            casos["anuladas"].append({"fecha": a["fecha"], "transporte": a["transporte"], "patente": a["patente"],
                                      "detalle": f"{a['destino']} · anuló {a['anulado_por']}"})

    n = max(1, len(dias))
    ranking = []
    for t in activos if transporte_id else [t for t in tts.values() if t["activo"] or t["id"] in por_t]:
        g = por_t.get(t["id"], {"cargadas": 0, "asignadas": 0, "bloqueadas": 0, "ociosas": 0})
        mt = [m for m in movs if m["transporte_id"] == t["id"] and m["accion"] == "agrega"]
        meta = (t["cuota_dia"] or 0) * n
        ranking.append({"transporte": t["nombre"], "transporte_id": t["id"], "cuota": t["cuota_dia"], "meta": meta, **g,
                        "cumplimiento": round(g["cargadas"] * 100 / meta) if meta else None,
                        "uso": round(g["asignadas"] * 100 / g["cargadas"]) if g["cargadas"] else None,
                        "pct_bloq": round(g["bloqueadas"] * 100 / g["cargadas"]) if g["cargadas"] else None,
                        "pct_eta": round(sum(1 for m in mt if m["eta"]) * 100 / len(mt)) if mt else None,
                        "pct_chofer": round(sum(1 for m in mt if m["chofer_dni"]) * 100 / len(mt)) if mt else None,
                        "sacadas": sum(1 for m in movs if m["transporte_id"] == t["id"] and m["accion"] == "quita")})
    ranking = [x for x in ranking if x["meta"] or x["cargadas"]]
    ranking.sort(key=lambda x: (x["cumplimiento"] if x["cumplimiento"] is not None else 999, x["transporte"]))

    bases_ = {}
    for u in usos:
        if not u["anulado"]:
            b = bases_.setdefault(u["destino"], {"base": u["destino"], "asignadas": 0, "refri": 0, "seco": 0, "pallets": 0})
            b["asignadas"] += 1
            b["refri" if u["tipo"] == "refri" else "seco"] += 1
            b["pallets"] += u["pallets"] or 0
    tot_c = sum(s["cargadas"] for s in serie)
    tot_a = sum(s["asignadas"] for s in serie)
    tot_meta = meta_dia * n if serie else 0
    anticip.sort()
    ishikawa = [{"categoria": cat, "causas": [{"clave": k, "texto": txt, "cantidad": len(casos[k])} for k, txt in items]}
                for cat, items in CAUSAS.items()]
    return {"desde": r["desde"], "hasta": r["hasta"], "dias": len(dias),
            "kpis": {"cargadas": tot_c, "asignadas": tot_a, "meta": tot_meta,
                     "cumplimiento": round(tot_c * 100 / tot_meta) if tot_meta else None,
                     "uso": round(tot_a * 100 / tot_c) if tot_c else None,
                     "bloqueadas": sum(g["bloqueadas"] for g in por_t.values()),
                     "pct_bloq": round(sum(g["bloqueadas"] for g in por_t.values()) * 100 / tot_c) if tot_c else None,
                     "anticipacion_mediana": anticip[len(anticip) // 2] if anticip else None,
                     "pct_chofer": round(sum(1 for m in movs if m["accion"] == "agrega" and m["chofer_dni"]) * 100 /
                                         max(1, sum(1 for m in movs if m["accion"] == "agrega"))) if movs else None},
            "serie": serie, "ranking": ranking, "bases": sorted(bases_.values(), key=lambda b: -b["asignadas"]),
            "horas": horas, "anticipacion": anticip, "ishikawa": ishikawa, "casos": casos,
            "efecto": "Disponible incompleto o no apto"}
