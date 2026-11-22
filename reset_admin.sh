#!/bin/sh
# Disponible de unidades - resetear la clave de un administrador.
# Uso:  ./reset_admin.sh [usuario] [clave]     (por defecto: admin y una clave temporal al azar)
#       ./reset_admin.sh desbloquear            (solo libera la espera por intentos fallidos)
# Si el panel corre en Docker (container "disponible-panel") lo hace adentro; si no, con Python sobre PANEL_DB.
CONTENEDOR="${CONTENEDOR:-disponible-panel}"
USUARIO="${1:-admin}"
if command -v docker >/dev/null 2>&1 && docker ps --format '{{.Names}}' 2>/dev/null | grep -qx "$CONTENEDOR"; then
  echo "Ejecutando \"$USUARIO\" en el container $CONTENEDOR..."
  exec docker exec "$CONTENEDOR" python -m app.reset_admin "$USUARIO" $2
fi
echo "No encontré el container \"$CONTENEDOR\": lo hago con Python sobre la base local."
export PANEL_DB="${PANEL_DB:-$(dirname "$0")/data/disponible.db}"
cd "$(dirname "$0")/panel" && exec python3 -m app.reset_admin "$USUARIO" $2
