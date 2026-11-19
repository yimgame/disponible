"""Consulta en vivo a la API de GCG (WS Trabajador y WS Vehículo), con el mismo criterio que la app de bloqueos.

  GET https://api.gcgevolution.com/interfaces/api/{API_KEY}/trabajador/{DNI}
  GET https://api.gcgevolution.com/interfaces/api/{API_KEY}/vehiculo/{DOMINIO}
Devuelven "documentos": [{nombre: "010" (nº del catálogo de GCG), fechaVto: "DD-MM-AAAA", estado: bool}],
"estadoHabilitacion" ("Habilitado" = el verde), "situacion" (3 = dado de baja) e "iso" (país).

La API key se carga en el panel (GCG → API key) y se guarda cifrada en la base; GCG_API_KEY del entorno queda como respaldo.
Las respuestas se guardan en la tabla gcg_vivo y se reusan durante «cache_horas» para no consultar en cada carga.
"""
import json
import os
import time
from contextlib import closing
from datetime import date, datetime, timedelta

import httpx

from . import db, logica, secretos

URL = "https://api.gcgevolution.com/interfaces/api/{api_key}/{recurso}/{clave}"
RECURSO = {"chofer": "trabajador", "unidad": "vehiculo"}
SITUACION_BAJA = 3


def api_key():
    return secretos.get_secreto("gcg_api_key") or os.environ.get("GCG_API_KEY", "").strip() or None


def activo():
    return bool(api_key())


def en_carga():
    """¿Se consulta GCG en vivo cuando el transporte carga una unidad?"""
    return activo() and secretos.get("gcg_vivo_en_carga", "1") == "1"


def cache_horas():
    try:
        return max(0.0, float(secretos.get("gcg_cache_horas", "6")))
    except ValueError:
        return 6.0


def _normalizar(tipo, clave):
    return logica.norm_dni(clave) if tipo == "chofer" else logica.norm_patente(clave)


def consultar(tipo, clave, usar_cache=True, timeout=12):
    """tipo = "chofer" (DNI) o "unidad" (dominio). Devuelve {"ok", "semaforo", "criticos", "documentos", ...} o {"ok": False, "error"}."""
    k = api_key()
    c = _normalizar(tipo, clave)
    if not k:
        return {"ok": False, "error": "No hay API key de GCG cargada (menú GCG)."}
    if not c:
        return {"ok": False, "error": "DNI o dominio inválido."}
    if usar_cache and cache_horas():
        with closing(db.con()) as con:
            r = con.execute("SELECT respuesta, consultado FROM gcg_vivo WHERE tipo=? AND clave=?", (tipo, c)).fetchone()
        if r and time.time() - r["consultado"] < cache_horas() * 3600:
            return {"ok": True, "tipo": tipo, "clave": c, "cache": True,
                    "consultado": datetime.fromtimestamp(r["consultado"], db.TZ).isoformat(timespec="minutes"),
                    **evaluar(json.loads(r["respuesta"]), tipo)}
    try:
        resp = httpx.get(URL.format(api_key=k, recurso=RECURSO[tipo], clave=c), headers={"Accept": "application/json"},
                         timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
    except httpx.HTTPStatusError as e:
        cod = e.response.status_code
        return {"ok": False, "error": f"GCG respondió {cod}" + (" (no existe en GCG)" if cod == 404 else
                                                               " (API key inválida)" if cod in (401, 403) else "")}
    except Exception as e:                     # noqa: BLE001
        return {"ok": False, "error": f"No se pudo consultar GCG ({type(e).__name__})"}
    with closing(db.con()) as con, con:
        con.execute("INSERT OR REPLACE INTO gcg_vivo VALUES (?,?,?,?)", (tipo, c, json.dumps(data, ensure_ascii=False), time.time()))
    return {"ok": True, "tipo": tipo, "clave": c, "cache": False, "consultado": db.ahora_iso()[:16], **evaluar(data, tipo)}


def _fecha_iso(v):
    try:
        return datetime.strptime(str(v), "%d-%m-%Y").date().isoformat() if v else None
    except ValueError:
        return None


def evaluar(data, tipo="chofer"):
    """Semáforo con el criterio de bloqueos: un crítico está OK si figura vigente, o si no figura y GCG da el
    verde general (si GCG no lo pide, no bloquea). Vencido o faltante sin el verde = rojo. Dado de baja = rojo."""
    habilitado = str(data.get("estadoHabilitacion") or "").strip().lower() == "habilitado"
    try:
        baja = int(data.get("situacion")) == SITUACION_BAJA
    except (TypeError, ValueError):
        baja = False
    iso = (data.get("iso") or "").strip().upper() or None
    hoy = db.hoy()
    lim = (date.fromisoformat(hoy) + timedelta(days=logica.DIAS_ALERTA_CRITICOS)).isoformat()
    por_numero = {str(x.get("nombre") or "").strip().lstrip("0") or "0": x for x in (data.get("documentos") or [])}
    dmy = lambda f: f"{f[8:10]}/{f[5:7]}/{f[:4]}" if f else None
    criticos, faltan, proximos = [], [], []
    for c in logica.criticos(tipo=tipo):
        if not logica._aplica(c, iso):
            continue
        if not c["gcg_numero"]:
            criticos.append({"documento": c["nombre"], "estado": "sin Nº GCG", "fecha": None})
            continue
        x = por_numero.get(str(int(c["gcg_numero"])))
        f = _fecha_iso(x.get("fechaVto")) if x else None
        if x is None:
            est = "no lo pide GCG" if habilitado else "falta"
        elif not x.get("estado"):
            est = "vencido"
        else:
            est = "por vencer" if f and f <= lim else "vigente"
        criticos.append({"documento": c["nombre"], "estado": est, "fecha": f})
        if est in ("falta", "vencido"):
            faltan.append(c["nombre"] + (f" ({dmy(f)})" if f else ""))
        elif est == "por vencer":
            proximos.append(f"{c['nombre']} ({dmy(f)})")
    quien = "Chofer" if tipo == "chofer" else "Unidad"
    if baja:
        sem = {"color": "rojo", "texto": "dado de baja en GCG" if tipo == "chofer" else "dada de baja en GCG"}
    elif faltan:
        sem = {"color": "rojo", "texto": "documentación crítica vencida o faltante: " + ", ".join(faltan)}
    elif not habilitado:
        sem = {"color": "amarillo", "texto": f"no {'habilitado' if tipo == 'chofer' else 'habilitada'} en GCG (sin críticos vencidos)"}
    else:
        sem = {"color": "verde", "texto": f"{'habilitado' if tipo == 'chofer' else 'habilitada'}, críticos al día"}
    docs = sorted(({"numero": x.get("nombre"), "estado": bool(x.get("estado")), "fecha": _fecha_iso(x.get("fechaVto"))}
                   for x in (data.get("documentos") or [])), key=lambda x: str(x["numero"]))
    nombre = (" ".join(str(data.get(k) or "") for k in ("apellido", "nombre")).strip() or data.get("nombreCompleto")
              or data.get("dominio") or data.get("descripcion"))
    return {"semaforo": {**sem, "fuente": "GCG en vivo"}, "criticos": criticos, "proximos": proximos, "documentos": docs,
            "nombre": nombre, "pais": iso, "habilitado": habilitado, "baja": baja, "quien": quien}
