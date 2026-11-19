"""Disponible de unidades: página del transporte (/cargar?t=<token>), panel (/panel/) y API para agentes (/api/v1/).

Todo vive en una base SQLite (/data/disponible.db) que se crea sola. Ver README.md.
"""
import csv
import io
import json
import os
import secrets
import time
from contextlib import closing
from datetime import date, timedelta
from urllib.parse import quote, urlparse

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pathlib import Path

from . import auth, db, gcg_api, logica, respaldo, webhooks

BASE = Path(__file__).resolve().parent
PREFIJO = "/panel"
COOKIE = "disp_sesion"
AGENTE_KEY = os.environ.get("AGENTE_API_KEY", "")

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
app.mount(PREFIJO + "/static", StaticFiles(directory=BASE / "static"), name="static")
plantillas = Jinja2Templates(directory=BASE / "templates")
plantillas.env.globals["P"] = PREFIJO


@app.on_event("startup")
def _inicio():
    auth.DB = db.DB
    db.iniciar()
    auth.iniciar()
    respaldo.iniciar()


def _ip(req):
    return req.headers.get("cf-connecting-ip") or req.headers.get("x-real-ip") or req.client.host


def _https(req):
    return req.headers.get("x-forwarded-proto") == "https" or "https" in req.headers.get("cf-visitor", "")


def _usuario(req):
    return auth.perfil(auth.usuario_de(req.cookies.get(COOKIE)))


def _mismo_origen(req):
    origen = req.headers.get("origin") or req.headers.get("referer")
    if origen and urlparse(origen).netloc != req.headers.get("host"):
        raise HTTPException(403, "Origen no permitido")


def _requiere(req, permiso=None):
    u = _usuario(req)
    if not u:
        raise HTTPException(303, headers={"Location": PREFIJO + "/login"})
    if permiso and permiso not in u["puede"]:
        raise HTTPException(403, "Tu usuario no tiene permiso para esto")
    return u


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _fecha(s, defecto=None):
    try:
        return date.fromisoformat(s).isoformat() if s else defecto
    except ValueError:
        return defecto


def _err(e, codigo=400):
    return JSONResponse({"ok": False, "error": str(e)}, status_code=codigo)


@app.middleware("http")
async def _cabeceras(req, siguiente):
    r = await siguiente(req)
    r.headers.setdefault("X-Content-Type-Options", "nosniff")
    r.headers.setdefault("Referrer-Policy", "same-origin")
    r.headers.setdefault("Cache-Control", "no-store")
    if req.url.path.startswith(PREFIJO):
        r.headers.setdefault("X-Frame-Options", "DENY")
    return r


# ------------------------------------------------------------------ login / cuenta
@app.get(PREFIJO + "/login", response_class=HTMLResponse)
def login_form(req: Request):
    return plantillas.TemplateResponse(req, "login.html", {"error": None})


@app.post(PREFIJO + "/login")
def login(req: Request, usuario: str = Form(""), clave: str = Form("")):
    _mismo_origen(req)
    ip = _ip(req)
    if auth.bloqueado(ip):
        return plantillas.TemplateResponse(req, "login.html", {"error": "Demasiados intentos fallidos. Esperá 15 minutos."},
                                           status_code=429)
    token = auth.ingresar(usuario, clave, ip)
    if not token:
        return plantillas.TemplateResponse(req, "login.html", {"error": "Usuario o contraseña incorrectos."}, status_code=401)
    r = RedirectResponse(PREFIJO + "/", 303)
    r.set_cookie(COOKIE, token, max_age=auth.SESION_HORAS * 3600, httponly=True, samesite="strict", secure=_https(req),
                 path=PREFIJO)
    return r


@app.post(PREFIJO + "/salir")
def salir(req: Request):
    _mismo_origen(req)
    auth.salir(req.cookies.get(COOKIE))
    r = RedirectResponse(PREFIJO + "/login", 303)
    r.delete_cookie(COOKIE, path=PREFIJO)
    return r


@app.get(PREFIJO + "/cuenta", response_class=HTMLResponse)
def cuenta(req: Request):
    return plantillas.TemplateResponse(req, "cuenta.html", {"u": _requiere(req), "msg": None, "error": None})


@app.post(PREFIJO + "/cuenta", response_class=HTMLResponse)
def cuenta_post(req: Request, actual: str = Form(""), nueva: str = Form(""), repetir: str = Form("")):
    _mismo_origen(req)
    u = _requiere(req)
    error = "Las contraseñas nuevas no coinciden." if nueva != repetir else auth.cambiar_clave(u["usuario"], actual, nueva)
    if error:
        return plantillas.TemplateResponse(req, "cuenta.html", {"u": u, "msg": None, "error": error}, status_code=400)
    return RedirectResponse(PREFIJO + "/", 303)


def _usuarios_pagina(req, u, nuevo=None, error=None):
    return plantillas.TemplateResponse(req, "usuarios.html", {"u": u, "usuarios": auth.listar(), "nuevo": nuevo,
                                                              "error": error, "roles": auth.ROLES},
                                       status_code=400 if error else 200)


@app.get(PREFIJO + "/usuarios", response_class=HTMLResponse)
def usuarios(req: Request):
    return _usuarios_pagina(req, _requiere(req, "usuarios"))


@app.post(PREFIJO + "/usuarios", response_class=HTMLResponse)
def usuarios_post(req: Request, accion: str = Form(...), usuario: str = Form(""), nombre: str = Form(""),
                  rol: str = Form("consulta")):
    _mismo_origen(req)
    u = _requiere(req, "usuarios")
    nuevo, error = None, None
    try:
        if accion == "crear":
            nuevo = (usuario.strip().lower(), auth.crear(usuario, nombre, rol))
        elif usuario == u["usuario"]:
            error = "No podés modificar tu propio usuario desde acá (usá Mi cuenta)."
        elif accion == "rol":
            auth.cambiar_rol(usuario, rol)
        elif accion == "blanquear":
            nuevo = (usuario, auth.blanquear(usuario))
        elif accion in ("activar", "desactivar"):
            auth.activar(usuario, accion == "activar")
    except ValueError as e:
        error = str(e)
    return _usuarios_pagina(req, u, nuevo, error)


@app.get(PREFIJO, include_in_schema=False)
def _sin_barra():
    return RedirectResponse(PREFIJO + "/", 307)


# ------------------------------------------------------------------ panel: el disponible del día
@app.get(PREFIJO + "/", response_class=HTMLResponse)
def hoy(req: Request):
    u = _requiere(req)
    if u["cambiar_clave"]:
        return RedirectResponse(PREFIJO + "/cuenta", 303)
    return plantillas.TemplateResponse(req, "hoy.html", {"u": u, "hoy": db.hoy(), "maestro": logica.transportistas_del_maestro(),
                                                         "bases": [b["nombre"] for b in logica.bases(solo_activas=True)]})


@app.get(PREFIJO + "/api/estado")
def api_estado(req: Request, fecha: str = "", transporte: str = ""):
    _requiere(req)
    return logica.estado_dia(_fecha(fecha, db.hoy()), _int(transporte))


@app.get(PREFIJO + "/api/rango")
def api_rango(req: Request, desde: str = "", hasta: str = ""):
    _requiere(req)
    d, h = _rango(desde, hasta)
    try:
        return logica.rango(d, h)
    except logica.Error as e:
        return _err(e)


@app.post(PREFIJO + "/api/transporte")
async def api_transporte(req: Request):
    """Alta o cambio desde el panel (como antes: mismo nombre = mismo link)."""
    _mismo_origen(req)
    _requiere(req, "gestion")
    b = await req.json()
    t = next((x for x in logica.transportes() if x["nombre"].lower() == str(b.get("nombre") or "").strip().lower()), None)
    try:
        nuevos = {k: v for k, v in b.items() if v not in (None, "")}
        if t:
            nuevos["nombre"] = t["nombre"]           # mismo transporte: se conserva el nombre como estaba
        t = logica.guardar_transporte({**(t or {}), **nuevos}, t["id"] if t else None)
    except (logica.Error, ValueError) as e:
        return _err(e)
    return {"ok": True, "transporte": t}


@app.get(PREFIJO + "/api/alertas/{patente}")
def api_alertas(req: Request, patente: str, dni: str = ""):
    _requiere(req)
    return {"alertas": logica.alertas(patente, dni), **logica.info_unidad(patente)}


@app.post(PREFIJO + "/api/usar")
async def api_usar(req: Request):
    _mismo_origen(req)
    u = _requiere(req, "gestion")
    b = await req.json()
    try:
        return {"ok": True, "uso": logica.usar(b.get("patente"), b, u["usuario"], _fecha(b.get("fecha"), db.hoy()))}
    except logica.Error as e:
        return _err(e)


@app.post(PREFIJO + "/api/usos/{uid}/anular")
def api_anular(req: Request, uid: int):
    _mismo_origen(req)
    u = _requiere(req, "gestion")
    try:
        logica.anular_uso(uid, u["usuario"])
    except logica.Error as e:
        return _err(e)
    return {"ok": True}


# ------------------------------------------------------------------ historial (usos y movimientos) + Excel
def _rango(desde, hasta):
    h = _fecha(hasta, db.hoy())
    return _fecha(desde, h), h


@app.get(PREFIJO + "/api/historial/{tabla}")
def api_historial(req: Request, tabla: str, desde: str = "", hasta: str = "", transporte: str = ""):
    _requiere(req)
    if tabla not in ("usos", "movimientos"):
        raise HTTPException(404)
    d, h = _rango(desde, hasta)
    return {"desde": d, "hasta": h, "filas": logica.listar(tabla, d, h, _int(transporte))}


COLS_CSV = {
    "usos": [("fecha", "Fecha"), ("creado", "Registrado"), ("transporte", "Transporte"), ("patente", "Patente"),
             ("tipo", "Tipo"), ("pallets", "Pallets"), ("destino", "Destino"), ("viaje", "Viaje / orden"),
             ("planta", "Planta"), ("detalle", "Detalle"), ("usuario", "Usuario"), ("anulado", "Anulado")],
    "movimientos": [("fecha", "Fecha"), ("hora", "Hora"), ("transporte", "Transporte"), ("patente", "Patente"),
                    ("accion", "Acción"), ("tipo", "Tipo"), ("pallets", "Pallets"), ("eta", "ETA"),
                    ("chofer_dni", "DNI chofer"), ("chofer_nombre", "Chofer"), ("bloqueado", "Bloqueado"),
                    ("bloqueo_motivo", "Motivo bloqueo"), ("comentario", "Comentario"), ("informante", "Informó")],
}


@app.get(PREFIJO + "/historial/{tabla}.csv")
def historial_csv(req: Request, tabla: str, desde: str = "", hasta: str = "", transporte: str = ""):
    _requiere(req)
    if tabla not in COLS_CSV:
        raise HTTPException(404)
    d, h = _rango(desde, hasta)
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow([t for _, t in COLS_CSV[tabla]])
    for f in logica.listar(tabla, d, h, _int(transporte)):
        w.writerow(["" if f.get(k) is None else f.get(k) for k, _ in COLS_CSV[tabla]])
    return Response("﻿" + buf.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{tabla}-{d}-a-{h}.csv"'})


# ------------------------------------------------------------------ transportes
@app.get(PREFIJO + "/transportes", response_class=HTMLResponse)
def transportes_pagina(req: Request, msg: str = "", error: str = ""):
    u = _requiere(req)
    return plantillas.TemplateResponse(req, "transportes.html", {
        "u": u, "transportes": logica.transportes(), "maestro": logica.transportistas_del_maestro(),
        "bases": logica.bases(), "msg": msg, "error": error})


@app.post(PREFIJO + "/transportes")
async def transportes_post(req: Request):
    _mismo_origen(req)
    _requiere(req, "gestion")
    f = {k: str(v) for k, v in (await req.form()).items()}
    try:
        if f.get("accion") == "token":
            logica.nuevo_token(_int(f.get("id")))
            msg = "Link nuevo generado: el anterior ya no funciona."
        else:
            t = logica.guardar_transporte(f, _int(f.get("id")))
            msg = f"Transporte «{t['nombre']}» guardado."
    except (logica.Error, ValueError) as e:
        return RedirectResponse(f"{PREFIJO}/transportes?error={quote(str(e))}", 303)
    return RedirectResponse(f"{PREFIJO}/transportes?msg={quote(msg)}", 303)


@app.post(PREFIJO + "/transportes/generar")
async def transportes_generar(req: Request):
    _mismo_origen(req)
    _requiere(req, "gestion")
    b = await req.json()
    return {"ok": True, **logica.generar_transportes_maestro(max(1, _int(b.get("min_unidades")) or 1),
                                                           b.get("simular", True) is not False)}


@app.post(PREFIJO + "/bases")
async def bases_post(req: Request):
    _mismo_origen(req)
    _requiere(req, "gestion")
    f = {k: str(v) for k, v in (await req.form()).items()}
    try:
        logica.guardar_base(f.get("nombre"), f.get("tipo"), f.get("activo", "1") == "1", _int(f.get("id")))
        msg = "Base guardada."
    except logica.Error as e:
        return RedirectResponse(f"{PREFIJO}/transportes?error={quote(str(e))}#bases", 303)
    return RedirectResponse(f"{PREFIJO}/transportes?msg={quote(msg)}#bases", 303)


# ------------------------------------------------------------------ análisis de rendimiento (gráficos + Ishikawa)
@app.get(PREFIJO + "/analisis", response_class=HTMLResponse)
def analisis_pagina(req: Request):
    u = _requiere(req)
    return plantillas.TemplateResponse(req, "analisis.html", {"u": u, "hoy": db.hoy(),
                                                               "transportes": logica.transportes()})


@app.get(PREFIJO + "/api/analisis")
def api_analisis(req: Request, desde: str = "", hasta: str = "", transporte: str = ""):
    _requiere(req)
    d, h = _rango(desde, hasta)
    try:
        return logica.analisis(d, h, _int(transporte))
    except logica.Error as e:
        return _err(e)


@app.get(PREFIJO + "/analisis.csv")
def analisis_csv(req: Request, desde: str = "", hasta: str = "", transporte: str = ""):
    """Reporte de rendimiento: resumen, ranking de transportes, causas (Ishikawa) y su detalle."""
    _requiere(req)
    d, h = _rango(desde, hasta)
    a = logica.analisis(d, h, _int(transporte))
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    k = a["kpis"]
    w.writerow(["Reporte de rendimiento del disponible", f"{d} a {h}", f"{a['dias']} días con movimientos"])
    w.writerow([])
    w.writerow(["Cargadas", "Asignadas", "Meta (cuota x días)", "Cumplimiento %", "Uso %", "Bloqueadas", "Bloqueadas %",
                "Anticipación ETA mediana (h)", "Con chofer %"])
    w.writerow([k["cargadas"], k["asignadas"], k["meta"], k["cumplimiento"], k["uso"], k["bloqueadas"], k["pct_bloq"],
                k["anticipacion_mediana"], k["pct_chofer"]])
    w.writerow([])
    w.writerow(["Transporte", "Cuota/día", "Meta", "Cargadas", "Cumplimiento %", "Asignadas", "Uso %", "Sin asignar",
                "Bloqueadas", "Bloqueadas %", "Con ETA %", "Con chofer %", "Sacadas"])
    for x in a["ranking"]:
        w.writerow([x["transporte"], x["cuota"], x["meta"], x["cargadas"], x["cumplimiento"], x["asignadas"], x["uso"],
                    x["ociosas"], x["bloqueadas"], x["pct_bloq"], x["pct_eta"], x["pct_chofer"], x["sacadas"]])
    w.writerow([])
    w.writerow(["Ishikawa — efecto:", a["efecto"]])
    w.writerow(["Categoría", "Causa", "Casos"])
    for cat in a["ishikawa"]:
        for c in cat["causas"]:
            w.writerow([cat["categoria"], c["texto"], c["cantidad"]])
    w.writerow([])
    w.writerow(["Detalle de causas"])
    w.writerow(["Categoría", "Causa", "Fecha", "Transporte", "Patente", "Detalle"])
    for cat in a["ishikawa"]:
        for c in cat["causas"]:
            for x in a["casos"][c["clave"]]:
                w.writerow([cat["categoria"], c["texto"], x["fecha"], x["transporte"], x["patente"], x["detalle"]])
    w.writerow([])
    w.writerow(["Base / destino", "Asignadas", "Refri", "Seco", "Pallets"])
    for b in a["bases"]:
        w.writerow([b["base"], b["asignadas"], b["refri"], b["seco"], b["pallets"]])
    return Response("\ufeff" + buf.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="rendimiento-{d}-a-{h}.csv"'})


# ------------------------------------------------------------------ consulta (unidad / chofer) y bloqueos
@app.get(PREFIJO + "/consulta", response_class=HTMLResponse)
def consulta(req: Request, q: str = "", msg: str = ""):
    u = _requiere(req)
    res = None
    if q.strip():
        dni = logica.norm_dni(q)
        pat = logica.norm_patente(q)
        if dni and len(dni) >= 6 and not any(ch.isalpha() for ch in q):
            res = {"tipo": "chofer", **logica.info_chofer(dni), "alertas": logica.alertas(None, dni)}
            res["semaforo"] = logica.semaforo_chofer(res["gcg"])
        else:
            res = {"tipo": "unidad", **logica.info_unidad(pat), "alertas": logica.alertas(pat)}
        if res.get("gcg"):
            res["gcg"]["docs"] = sorted(json.loads(res["gcg"]["documentos"] or "{}").items())
    return plantillas.TemplateResponse(req, "consulta.html", {"u": u, "q": q, "res": res, "msg": msg, "hoy": db.hoy(),
                                                              "bloqueos": logica.bloqueos(), "gcg_vivo": gcg_api.activo(),
                                                              "resumen": logica.resumen_gcg()})


@app.get(PREFIJO + "/api/gcg/{dni}")
def api_gcg_vivo(req: Request, dni: str):
    """Consulta en vivo a la API de GCG (si hay GCG_API_KEY)."""
    _requiere(req)
    return gcg_api.consultar(dni)


@app.post(PREFIJO + "/criticos")
async def criticos_post(req: Request):
    _mismo_origen(req)
    _requiere(req, "gestion")
    f = {k: str(v) for k, v in (await req.form()).items()}
    try:
        logica.guardar_critico(f, _int(f.get("id")))
        msg = "Documento crítico guardado."
    except logica.Error as e:
        return RedirectResponse(f"{PREFIJO}/importar?error={quote(str(e))}#criticos", 303)
    return RedirectResponse(f"{PREFIJO}/importar?msg={quote(msg)}#criticos", 303)


@app.post(PREFIJO + "/consulta/bloqueo")
async def consulta_bloqueo(req: Request):
    _mismo_origen(req)
    u = _requiere(req, "gestion")
    f = {k: str(v) for k, v in (await req.form()).items()}
    try:
        if f.get("accion") == "quitar":
            logica.quitar_bloqueo(_int(f.get("id")))
            msg = "Bloqueo quitado."
        else:
            logica.agregar_bloqueo(f.get("tipo"), f.get("clave"), f.get("motivo"), u["usuario"])
            msg = "Bloqueo agregado."
    except logica.Error as e:
        msg = str(e)
    return RedirectResponse(f"{PREFIJO}/consulta?q={quote(f.get('q', ''))}&msg={quote(msg)}", 303)


# ------------------------------------------------------------------ importar (maestro de unidades, GCG, bloqueados)
@app.get(PREFIJO + "/importar", response_class=HTMLResponse)
def importar_pagina(req: Request, msg: str = "", error: str = ""):
    return plantillas.TemplateResponse(req, "importar.html", {"u": _requiere(req, "gestion"), "msg": msg, "error": error,
                                                              "criticos": logica.criticos(solo_activos=False)})


@app.post(PREFIJO + "/api/importar/{tipo}")
async def importar(req: Request, tipo: str):
    _mismo_origen(req)
    u = _requiere(req, "gestion")
    b = await req.json()
    simular = b.get("simular", True) is not False
    filas = b.get("filas") or []
    if tipo == "unidades":
        r = logica.importar_unidades(filas, simular)
    elif tipo == "gcg":
        r = logica.importar_gcg(filas, simular)
    elif tipo == "bloqueados":
        r = logica.importar_bloqueados(filas, u["usuario"], simular, b.get("reemplazar", True) is not False)
    else:
        raise HTTPException(404)
    return {"ok": True, "simulado": simular, **r}


# ------------------------------------------------------------------ integraciones (n8n / agentes) y ayuda
@app.get(PREFIJO + "/integraciones", response_class=HTMLResponse)
def integraciones(req: Request):
    u = _requiere(req, "usuarios")
    return plantillas.TemplateResponse(req, "integraciones.html", {
        "u": u, "webhook": webhooks.activo(), "urls": [x.split("?")[0] for x in webhooks.URLS],
        "firma": bool(webhooks.SECRETO), "eventos": sorted(webhooks.EVENTOS), "ultimos": webhooks.ultimos(),
        "api": bool(AGENTE_KEY), "base": f"{'https' if _https(req) else 'http'}://{req.headers.get('host')}"})


@app.post(PREFIJO + "/integraciones/prueba")
def integraciones_prueba(req: Request):
    _mismo_origen(req)
    u = _requiere(req, "usuarios")
    webhooks.enviar("prueba", {"mensaje": "Prueba desde el panel del disponible", "usuario": u["usuario"]})
    time.sleep(1.5)
    return RedirectResponse(PREFIJO + "/integraciones", 303)


@app.get(PREFIJO + "/ayuda", response_class=HTMLResponse)
def ayuda(req: Request):
    return plantillas.TemplateResponse(req, "ayuda.html", {"u": _requiere(req)})


@app.get(PREFIJO + "/salud")
def salud():
    return {"ok": True}


# ------------------------------------------------------------------ público: el transporte con su link
def _transporte_token(token):
    t = logica.transporte(token=token)
    if not t:
        raise HTTPException(404, "Link inválido o dado de baja")
    return t


def _mio(t):
    e = logica.estado_dia(db.hoy(), t["id"])
    with closing(db.con()) as c:
        unis = [dict(r) for r in c.execute("""SELECT patente, tipo, pallets, bloqueado FROM unidades
                                              WHERE nro_proveedor=? ORDER BY patente""", (t["nro_proveedor"] or "-",))]
    return {"ok": True, "transporte": t["nombre"], "fecha": e["fecha"], "cuota": t["cuota_dia"],
            "estado": e["transportes"][0] if e["transportes"] else None, "filas": e["filas"], "unidades": unis}


@app.get(PREFIJO + "/publico/t/{token}")
def publico_mio(token: str):
    return _mio(_transporte_token(token))


@app.get(PREFIJO + "/publico/t/{token}/alertas")
def publico_alertas(token: str, patente: str = "", dni: str = ""):
    _transporte_token(token)
    u = logica.info_unidad(patente)["unidad"]
    return {"alertas": logica.alertas(patente, dni) if logica.patente_valida(logica.norm_patente(patente)) else [],
            "unidad": {k: u[k] for k in ("tipo", "pallets")} if u else None}


@app.post(PREFIJO + "/publico/t/{token}/movimiento")
async def publico_movimiento(token: str, req: Request):
    t = _transporte_token(token)
    try:
        b = await req.json()
        fila, al = logica.registrar_movimiento(t, b, "link")
    except logica.Error as e:
        return _err(e)
    return {**_mio(t), "registrado": fila, "alertas": al}


# ------------------------------------------------------------------ API para agentes (n8n): Authorization: Bearer <AGENTE_API_KEY>
def _agente(req):
    k = req.headers.get("authorization", "").removeprefix("Bearer ").strip() or req.headers.get("x-api-key", "")
    if not AGENTE_KEY or not secrets.compare_digest(k, AGENTE_KEY):
        raise HTTPException(401, "Falta o no es válida la llave de la API (Authorization: Bearer ...)")


@app.get("/api/v1/estado")
def ag_estado(req: Request, fecha: str = "", transporte_id: str = ""):
    _agente(req)
    return logica.estado_dia(_fecha(fecha, db.hoy()), _int(transporte_id))


@app.get("/api/v1/transportes")
def ag_transportes(req: Request):
    _agente(req)
    return [{k: t[k] for k in ("id", "nombre", "nro_proveedor", "cuota_dia", "telefono", "activo")} for t in logica.transportes()]


@app.get("/api/v1/unidad/{patente}")
def ag_unidad(req: Request, patente: str):
    _agente(req)
    return {**logica.info_unidad(patente), "alertas": logica.alertas(patente)}


@app.get("/api/v1/chofer/{dni}")
def ag_chofer(req: Request, dni: str):
    _agente(req)
    return {**logica.info_chofer(dni), "alertas": logica.alertas(None, dni)}


@app.post("/api/v1/movimientos")
async def ag_movimiento(req: Request):
    _agente(req)
    b = await req.json()
    t = logica.transporte(tid=_int(b.get("transporte_id"))) if b.get("transporte_id") else logica.transporte(token=b.get("token"))
    if not t:
        return _err("Transporte inválido (mandá transporte_id o token).", 404)
    try:
        fila, al = logica.registrar_movimiento(t, b, "agente")
    except logica.Error as e:
        return _err(e)
    return {"ok": True, "movimiento": fila, "alertas": al}


@app.post("/api/v1/usos")
async def ag_uso(req: Request):
    _agente(req)
    b = await req.json()
    try:
        return {"ok": True, "uso": logica.usar(b.get("patente"), b, "agente", _fecha(b.get("fecha"), db.hoy()))}
    except logica.Error as e:
        return _err(e)


@app.get("/api/v1/usos")
def ag_usos(req: Request, desde: str = "", hasta: str = "", transporte_id: str = ""):
    _agente(req)
    d, h = _rango(desde, hasta)
    return logica.listar("usos", d, h, _int(transporte_id))
