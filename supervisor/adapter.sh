#!/usr/bin/env bash
# Sobe o provisionamento (pesos) e depois a API. Chamado pelo supervisor.
set -uo pipefail
source /etc/environment 2>/dev/null || true

RAIZ="${ADAPTER_ROOT:-/opt/node}"

# O bootstrap gravou aqui o Python que enxerga o torch da imagem. Usar outro
# quebraria o `from shared.api import init` do adapter — e com ele o modelo
# residente na VRAM entre os vídeos.
PY=$(cat "$RAIZ/.python" 2>/dev/null || command -v python3)

# ⚠️ O download dos pesos NÃO acontece mais aqui.
#
# Rodá-lo antes do uvicorn significava ~15 minutos sem API e sem registro: a
# máquina só aparecia na plataforma depois de pronta, e a usuária ficava sem
# nenhum sinal. Agora quem chama o provision.sh é o próprio adapter, numa
# thread, reportando "baixando os modelos" enquanto isso — ver _preparar()
# em main.py. O marcador /workspace/.provisionado continua evitando repetir.

# ⚠️ O diretório de trabalho precisa ser a RAIZ DO WAN2GP, não a do adapter.
#
# O Wan2GP grava os vídeos em `outputs/` relativo ao cwd do processo, e depois
# procura o arquivo pelo mesmo caminho relativo. Rodando de /opt/node/adapter,
# ele salvava em /opt/node/adapter/outputs e o probe interno falhava com
# "ffprobe skipped; file not found: outputs/....mp4" — o job morria DEPOIS de
# 12 minutos de GPU paga (medido em 2026-08-17).
#
# Passar `init(root=...)` não resolve: esse parâmetro diz onde estão os
# modelos, não onde ele escreve as saídas.
cd "${WAN2GP_ROOT:-/workspace/Wan2GP}" || exit 1

# E como o cwd deixou de ser a pasta do adapter, os imports planos dele
# (`from config import CONFIG`) precisam do PYTHONPATH.
export PYTHONPATH="$RAIZ/adapter${PYTHONPATH:+:$PYTHONPATH}"

# 127.0.0.1 de propósito: quem expõe para fora é o Caddy da base-image, que
# põe TLS e autenticação na frente. Ver PORTAL_CONFIG no README.
exec "$PY" -u -m uvicorn main:app \
  --host 127.0.0.1 --port "${ADAPTER_PORT:-18000}" --log-level info
