# Nó de GPU: Wan2GP + adapter FastAPI — CAMINHO ALTERNATIVO.
#
# ⚠️ O caminho PADRÃO hoje é o `bootstrap.sh` via PROVISIONING_SCRIPT: sem build,
# sem push, e corrigir o adapter é editar o repo em vez de reconstruir 7,5 GB.
# Ver README.md. Este Dockerfile existe para quando o adapter estabilizar e você
# quiser congelar tudo numa imagem — o conteúdo é o mesmo.
#
# Deriva da imagem VALIDADA na Fase 0b — tag fixa, nunca `latest`. Fixar congela
# Wan2GP, CUDA e a base-image do Vast (TLS, autenticação, supervisor, portal).
#
# Custo real de construir (medido no Docker Hub, não estimado):
#   base comprimida 7,5 GB · pico de disco no build ~25 GB · push 7,5 GB se o
#   Docker Hub não reaproveitar as camadas da base.
FROM vastai/wan2gp:7e45fe7-2026-08-10-cuda-12.9

# /opt/node é o mesmo caminho que o bootstrap.sh usa, para que o
# supervisor/adapter.sh funcione idêntico nos dois modos.
COPY adapter/     /opt/node/adapter/
COPY provision.sh /opt/node/provision.sh
COPY supervisor/adapter.conf /etc/supervisor/conf.d/adapter.conf
COPY supervisor/adapter.sh   /opt/supervisor-scripts/adapter.sh

# O adapter importa o Wan2GP no mesmo processo, então precisa do Python que tem
# o torch. Aqui resolvemos em build-time e gravamos onde o adapter.sh lê — a
# mesma detecção que o bootstrap.sh faz em runtime.
RUN chmod +x /opt/node/provision.sh /opt/supervisor-scripts/adapter.sh && \
    for py in /venv/main/bin/python /opt/venv/bin/python "$(command -v python3)"; do \
      if [ -x "$py" ] && "$py" -c 'import torch' 2>/dev/null; then \
        echo "$py" > /opt/node/.python; break; \
      fi; \
    done && \
    test -s /opt/node/.python || { echo "nenhum Python com torch na base"; exit 1; } && \
    "$(cat /opt/node/.python)" -m pip install --no-cache-dir \
      -r /opt/node/adapter/requirements.txt

# Externa 8000 -> interna 18000: com portas diferentes, o Caddy da base-image
# entra no meio e fornece TLS + autenticação sem código nosso.
ENV PORTAL_CONFIG="localhost:1111:11111:/:Instance Portal|localhost:8000:18000:/:InfiniteTalk API|localhost:8080:18080:/:Jupyter" \
    ENABLE_HTTPS="true" \
    ADAPTER_PORT="18000" \
    ADAPTER_ROOT="/opt/node" \
    WAN2GP_ROOT="/workspace/Wan2GP"

EXPOSE 8000
