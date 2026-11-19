"""Consulta en vivo a la API de GCG (WS Trabajador), con el mismo criterio que la app de bloqueos.

GET https://api.gcgevolution.com/interfaces/api/{API_KEY}/trabajador/{DNI}
Devuelve "documentos": [{nombre: "010" (nº del catálogo de GCG), fechaVto: "DD-MM-AAAA", estado: bool}],
"estadoHabilitacion" ("Habilitado" = el verde del trabajador), "situacion" (3 = dado de baja) e "iso" (país).
Solo funciona si GCG_API_KEY está en el entorno; si no, la consulta en vivo no aparece en el panel.
"""
import os

import httpx

from . import logica

API_KEY = os.environ.get("GCG_API_KEY", "").strip()
URL = "https://api.gcgevolution.com/interfaces/api/{api_key}/trabajador/{dni}"
SITUACION_BAJA = 3


def activo():
    return bool(API_KEY)


def consultar(dni):
    """{"ok", "semaforo": {color, texto}, "criticos": [...], "documentos": [...], "nombre", ...} o {"ok": False, "error"}."""
    d = logica.norm_dni(dni)
    if not API_KEY:
        return {"ok": False, "error": "No hay API key de GCG configurada (GCG_API_KEY en el .env del servidor)."}
    if not d:
        return {"ok": False, "error": "DNI inválido."}
    try:
        r = httpx.get(URL.format(api_key=API_KEY, dni=d), headers={"Accept": "application/json"}, timeout=15)
        r.raise_for_status()
        data = r.json()
    except httpx.HTTPStatusError as e:
        return {"ok": False, "error": f"GCG respondió {e.response.status_code}" + (" (no existe ese DNI)" if e.response.status_code == 404 else "")}
    except Exception as e:                     # noqa: BLE001
        return {"ok": False, "error": f"No se pudo consultar GCG ({type(e).__name__})"}
    return {"ok": True, "dni": d, **evaluar(data)}


def evaluar(data):
    habilitado = str(data.get("estadoHabilitacion") or "").strip().lower() == "habilitado"
    try:
        baja = int(data.get("situacion")) == SITUACION_BAJA
    except (TypeError, ValueError):
        baja = False
    iso = (data.get("iso") or "").strip().upper() or None
    por_numero = {str(x.get("nombre") or "").strip().lstrip("0") or "0": x for x in (data.get("documentos") or [])}
    fecha = lambda v: str(v).replace("-", "/") if v else None
    criticos, faltan = [], []
    for c in logica.criticos():
        if not logica._aplica(c, iso):
            continue
        if not c["gcg_numero"]:
            criticos.append({"documento": c["nombre"], "estado": "sin Nº GCG", "fecha": None})
            faltan.append(c["nombre"] + " (sin Nº GCG mapeado)")
            continue
        x = por_numero.get(str(int(c["gcg_numero"])))
        if x is None:
            est = "no lo pide GCG" if habilitado else "falta"
        else:
            est = "vigente" if x.get("estado") else "vencido"
        criticos.append({"documento": c["nombre"], "estado": est, "fecha": fecha(x.get("fechaVto")) if x else None})
        if est in ("falta", "vencido"):
            faltan.append(f"{c['nombre']}" + (f" ({fecha(x.get('fechaVto'))})" if x and x.get("fechaVto") else ""))
    if baja:
        sem = {"color": "rojo", "texto": "dado de baja en GCG"}
    elif faltan:
        sem = {"color": "rojo", "texto": "documentación crítica vencida o faltante: " + ", ".join(faltan)}
    elif not habilitado:
        sem = {"color": "amarillo", "texto": "no habilitado en GCG (sin críticos vencidos)"}
    else:
        sem = {"color": "verde", "texto": "habilitado, críticos al día"}
    docs = sorted(({"numero": x.get("nombre"), "estado": bool(x.get("estado")), "fecha": fecha(x.get("fechaVto"))}
                   for x in (data.get("documentos") or [])), key=lambda x: str(x["numero"]))
    nombre = " ".join(str(data.get(k) or "") for k in ("apellido", "nombre")).strip() or data.get("nombreCompleto")
    return {"semaforo": sem, "criticos": criticos, "documentos": docs, "nombre": nombre, "pais": iso,
            "habilitado": habilitado, "baja": baja}
