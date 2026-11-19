# Disponible de unidades

Los transportes informan, desde un **link propio** (sin usuario ni clave), las unidades que tienen para despachar:
patente, refrigerado o seco, pallets, **ETA** (fecha y hora estimada de arribo), chofer y comentario. Un **panel** muestra
el disponible del día o de un rango, por transporte contra su **cuota** y por día; cada unidad se puede **usar**
(se descuenta del disponible) registrando **dónde se usó**.

Cruza cada carga con el **maestro de unidades** (bloqueado, documento vencido), el **exporte de GCG** (estado y
vencimiento de documentos de chofer y unidad) y una **lista de bloqueados**: si algo está bloqueado se le avisa al
transporte al cargar y la unidad queda marcada en el disponible.

Todo es **HTML + CSS + JavaScript** y un panel en **Python (FastAPI)** con **una sola base SQLite**.
Preparado para **IA**: webhook saliente con cada evento y API con llave para que un agente (por ejemplo en n8n) consulte y cargue.

## Instalar

```sh
cp .env.example .env        # poné una contraseña en PANEL_ADMIN_PASSWORD
docker compose up -d --build
```

Panel: `http://localhost:8140/panel/` con `admin` y la contraseña del `.env` (pide cambiarla). La base se crea sola.
Para publicarlo en internet poné adelante un proxy con HTTPS.

## Pantallas

- **Panel**: Desde/Hasta (Hoy, Ayer, 7 días, Este mes), totales (disponibles, refri, seco, usadas, bloqueadas,
  puestas/sacadas), por transporte con cuota, Copiar link y WhatsApp, por día, unidades del día con **Usar**, usos y
  movimientos, CSV, alta de transportes.
- **Transportes**: links, cuota, celular, nº de proveedor, baja, link nuevo.
- **Consulta**: patente o DNI → maestro, GCG con documentos, bloqueos; bloquear / desbloquear a mano.
- **Importar**: maestro de unidades (exporte de equipos), GCG (ExportaciónExcel) y lista de bloqueados. Los Excel se
  leen en el navegador y al servidor van solo las columnas que se usan.
- **Integraciones**: estado del webhook, últimos envíos, documentación de la API.
- **Usuarios** (administrador, operador, consulta) y **Ayuda**.

## Integración con n8n / agentes

- **Webhook saliente** (`WEBHOOK_URL`): POST `{"evento", "enviado", "datos"}` por cada `movimiento`, `alerta_bloqueo`,
  `uso` y `uso_anulado`. Firma opcional `X-Disponible-Firma: sha256=<HMAC>` con `WEBHOOK_SECRET`.
- **API** (`AGENTE_API_KEY`, header `Authorization: Bearer ...`):
  `GET /api/v1/estado`, `GET /api/v1/transportes`, `GET /api/v1/unidad/{patente}`, `GET /api/v1/chofer/{dni}`,
  `POST /api/v1/movimientos`, `POST /api/v1/usos`, `GET /api/v1/usos`.

## Estructura

```
web/            cargar.html (página del transporte) · index.html
nginx.conf      sirve web/ y hace de proxy a /panel/ y /api/
panel/          Dockerfile + app/ (FastAPI)
  app/main.py       rutas (panel, página del transporte, API)
  app/logica.py     disponible, usos, alertas, imports
  app/db.py         esquema SQLite (se crea solo)
  app/webhooks.py   eventos salientes
  app/auth.py       usuarios y roles (Argon2id)
  app/respaldo.py   backup diario de la base (/data/respaldos, 30 días)
ignorar/        datos sensibles (exportes, notas): fuera de git salvo el LEEME.md
```

Nada de producción va al repo: `.env`, la base, `ignorar/` y los Excel están en `.gitignore`.
