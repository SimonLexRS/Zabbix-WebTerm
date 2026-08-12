#!/usr/bin/env bash
# Run Catalyst WebTerm Playwright diagnosis via the official Playwright image.
set -euo pipefail

ROOT=/home/user_admin/zabbix-coolify
SCRIPT_DIR="$ROOT/modules/webterm/proxy/scripts"
OUT_DIR="$SCRIPT_DIR/diag-output"
IMAGE="${PLAYWRIGHT_IMAGE:-mcr.microsoft.com/playwright/python:v1.49.1-jammy}"
CONTINUE_FILE=/tmp/webterm-pw-continue
URL="${WEBTERM_DIAG_URL:-http://ia020203:8081/zabbix.php?action=dashboard.view&dashboardid=396&from=now-24h&to=now}"
TIMEOUT_SEC="${WEBTERM_DIAG_TIMEOUT:-900}"

mkdir -p "$OUT_DIR"
rm -f "$CONTINUE_FILE" /tmp/webterm-pw-diagnose.status
echo "waiting" > /tmp/webterm-pw-diagnose.status

cat <<EOF
========================================================================
DIAGNÓSTICO PLAYWRIGHT — pausa para login
1) En TU navegador abre:
   $URL
2) Inicia sesión en Zabbix.
3) Abre Connect > Web al Catalyst y deja la UI en el estado problemático
   (login WLC o pantalla rota).
4) Cuando esté listo, ejecuta en el servidor:
   touch $CONTINUE_FILE
========================================================================
EOF

echo "Esperando $CONTINUE_FILE (timeout ${TIMEOUT_SEC}s)..."
ready=0
for _ in $(seq 1 "$TIMEOUT_SEC"); do
  if [[ -f "$CONTINUE_FILE" ]]; then
    echo "Señal recibida."
    rm -f "$CONTINUE_FILE"
    ready=1
    break
  fi
  sleep 1
done
if [[ "$ready" -ne 1 ]]; then
  echo "TIMEOUT esperando login/señal" >&2
  echo "timeout" > /tmp/webterm-pw-diagnose.status
  exit 1
fi
echo "capturing" > /tmp/webterm-pw-diagnose.status

TOKEN=$(docker compose -f "$ROOT/docker-compose.yml" logs --since=45m webterm-proxy 2>/dev/null \
  | grep -oE '/webterm/web/[A-Za-z0-9_-]{20,}/' | tail -1 | sed -E 's|/webterm/web/([^/]+)/|\1|' || true)
echo "Token detectado: ${TOKEN:-NINGUNO}"
docker compose -f "$ROOT/docker-compose.yml" logs --since=45m webterm-proxy > "$OUT_DIR/proxy-logs-latest.txt" 2>/dev/null || true
printf '%s' "${TOKEN:-}" > "$OUT_DIR/token.txt"

docker run --rm --network host \
  -e WEBTERM_DIAG_URL="$URL" \
  -e WEBTERM_DIAG_TOKEN="${TOKEN:-}" \
  -e WEBTERM_DIAG_PAUSE=none \
  -e WEBTERM_DIAG_PROXY_LOG=/work/modules/webterm/proxy/scripts/diag-output/proxy-logs-latest.txt \
  -v "$ROOT:/work" \
  -w /work/modules/webterm/proxy/scripts \
  "$IMAGE" \
  bash -lc 'pip3 install -q --root-user-action=ignore playwright==1.49.1 && python diagnose_catalyst_playwright.py --headless --pause-mode none'

echo "done" > /tmp/webterm-pw-diagnose.status
echo "Salida en $OUT_DIR"
ls -lt "$OUT_DIR" | head -20
