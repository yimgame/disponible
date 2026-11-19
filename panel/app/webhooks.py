"""Webhooks salientes (para enganchar n8n / un agente de IA).

Si WEBHOOK_URL está definido (una o varias URLs separadas por coma), cada evento se manda por POST en segundo plano:
    {"evento": "movimiento" | "alerta_bloqueo" | "uso" | "uso_anulado", "enviado": "...", "datos": {...}}
Firma: header X-Disponible-Firma = "sha256=" + HMAC-SHA256(cuerpo, WEBHOOK_SECRET) (si hay secreto).
Nunca frena ni rompe la carga: si n8n no contesta, queda anotado en el log y en ultimos().
"""
import hashlib
import hmac
import json
import os
from collections import deque
from concurrent.futures import ThreadPoolExecutor

import httpx

from . import db

URLS = [u.strip() for u in os.environ.get("WEBHOOK_URL", "").split(",") if u.strip()]
SECRETO = os.environ.get("WEBHOOK_SECRET", "").encode()
EVENTOS = {e.strip() for e in os.environ.get("WEBHOOK_EVENTOS", "").split(",") if e.strip()}   # vacío = todos
_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="webhook")
_ultimos = deque(maxlen=30)


def activo():
    return bool(URLS)


def _mandar(url, cuerpo, evento):
    headers = {"Content-Type": "application/json", "User-Agent": "disponible/1", "X-Disponible-Evento": evento}
    if SECRETO:
        headers["X-Disponible-Firma"] = "sha256=" + hmac.new(SECRETO, cuerpo, hashlib.sha256).hexdigest()
    try:
        r = httpx.post(url, content=cuerpo, headers=headers, timeout=8)
        resultado = f"HTTP {r.status_code}"
    except Exception as e:                       # noqa: BLE001 — nunca romper por el webhook
        resultado = f"error: {type(e).__name__}"
    _ultimos.appendleft({"cuando": db.ahora_iso(), "evento": evento, "url": url.split("?")[0], "resultado": resultado})
    print(f"[webhook] {evento} -> {url.split('?')[0]}: {resultado}", flush=True)


def enviar(evento, datos):
    if not URLS or (EVENTOS and evento not in EVENTOS):
        return
    cuerpo = json.dumps({"evento": evento, "enviado": db.ahora_iso(), "datos": datos}, ensure_ascii=False,
                        default=str).encode()
    for url in URLS:
        _pool.submit(_mandar, url, cuerpo, evento)


def ultimos():
    return list(_ultimos)
