#!/usr/bin/env bash
# Sobe o provisionamento (pesos) e depois a API. Chamado pelo supervisor.
set -uo pipefail
source /etc/environment 2>/dev/null || true

RAIZ="${ADAPTER_ROOT:-/opt/node}"

# O bootstrap gravou aqui o Python que enxerga o torch da imagem. Usar outro
# quebraria o `from shared.api import init` do adapter — e com ele o modelo
# residente na VRAM entre os vídeos.
PY=$(cat "$RAIZ/.python" 2>/dev/null || command -v python3)

# O download dos pesos roda uma vez; o marcador evita repetir a cada restart do
# supervisor. Apague /workspace/.provisionado para forçar de novo.
MARCA=/workspace/.provisionado
if [ ! -f "$MARCA" ]; then
  "$RAIZ/provision.sh" && touch "$MARCA"
fi

# cd porque o adapter usa imports planos (`from config import CONFIG`).
cd "$RAIZ/adapter" || exit 1

# 127.0.0.1 de propósito: quem expõe para fora é o Caddy da base-image, que
# põe TLS e autenticação na frente. Ver PORTAL_CONFIG no README.
exec "$PY" -u -m uvicorn main:app \
  --host 127.0.0.1 --port "${ADAPTER_PORT:-18000}" --log-level info
