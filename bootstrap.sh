#!/usr/bin/env bash
# PROVISIONING_SCRIPT do nó — o Vast baixa e executa isto no boot.
#
#   Template → Image: vastai/wan2gp:<tag>   (a original, sem build nosso)
#              Env:   PROVISIONING_SCRIPT=https://raw.githubusercontent.com/
#                       <USUARIO>/<REPO>/main/bootstrap.sh
#
# Por que não uma imagem Docker própria: construir exigiria ~25 GB de pico no
# disco e um push de 7,5 GB pela banda de subida. E na Fase 1 vamos ITERAR — o
# adapter nunca rodou numa GPU. Aqui, corrigir é editar o repo e reiniciar o
# supervisor; com imagem, seria build + push + relançar a máquina.
#
# O Dockerfile continua no repo: quando o adapter estabilizar, dá para congelar
# tudo numa imagem sem reescrever nada.
#
# Idempotente de propósito: pode rodar de novo sem estragar nada.

set -uo pipefail

log()  { printf '\n\033[1;36m[bootstrap] %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m[bootstrap] ! %s\033[0m\n' "$*" >&2; }
die()  { printf '\033[1;31m[bootstrap] ✗ %s\033[0m\n' "$*" >&2; exit 1; }

# ── de onde vem o código ──────────────────────────────────────────────
# ADAPTER_REF aceita "main" ou um SHA de commit. Fixar o SHA no template dá a
# mesma garantia que a tag fixa da imagem daria: um push no repo não muda o que
# a máquina de sábado executa.
REPO="${ADAPTER_REPO:?defina ADAPTER_REPO=usuario/repositorio no template}"
REF="${ADAPTER_REF:-main}"
RAIZ="${ADAPTER_ROOT:-/opt/node}"
WAN2GP_ROOT="${WAN2GP_ROOT:-/workspace/Wan2GP}"

log "repo=${REPO} ref=${REF} → ${RAIZ}"

# ── sanidade: estamos na imagem certa? ────────────────────────────────
# Falhar aqui, alto e claro, é muito melhor que falhar daqui a 20 min no meio
# do download dos pesos.
[ -d "$WAN2GP_ROOT" ] || die "$WAN2GP_ROOT não existe — o template está apontando para a imagem certa (vastai/wan2gp)?"

# ── baixar o adapter ──────────────────────────────────────────────────
# Tarball em vez de N downloads de arquivos soltos: uma requisição, e ou vem
# tudo ou não vem nada. Meio-termo aqui viraria um adapter pela metade.
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

# ADAPTER_TARBALL permite apontar para outra origem (GitLab, um .tar.gz num
# bucket, ou file:// num teste local). Vazio = GitHub, o caminho normal.
url="${ADAPTER_TARBALL:-https://github.com/${REPO}/archive/${REF}.tar.gz}"
log "baixando ${url}"
if ! curl -fsSL --retry 3 --retry-delay 2 "$url" -o "$TMP/node.tar.gz"; then
  die "download falhou. Repo público? ADAPTER_REF=${REF} existe?"
fi

mkdir -p "$RAIZ"
tar xzf "$TMP/node.tar.gz" --strip-components=1 -C "$RAIZ" \
  || die "tarball corrompido"

[ -f "$RAIZ/adapter/main.py" ] \
  || die "adapter/main.py não veio. A raiz do repo deve ser o conteúdo da pasta node/."

chmod +x "$RAIZ/provision.sh" "$RAIZ/supervisor/adapter.sh" 2>/dev/null || true
log "código instalado"

# ── descobrir o Python do Wan2GP ──────────────────────────────────────
# CRÍTICO: o adapter faz `from shared.api import init`, ou seja, importa o
# Wan2GP DENTRO do próprio processo — é o que mantém os modelos residentes na
# VRAM entre os vídeos (25,5x tempo real em vez de 60x). Se ele rodar num
# interpretador diferente do que tem o torch da imagem, nada disso funciona.
#
# O teste decisivo é "esse Python enxerga o torch?", não o caminho ser bonito.
achar_python() {
  local candidatos=()
  [ -n "${WAN2GP_PYTHON:-}" ] && candidatos+=("$WAN2GP_PYTHON")
  # Caminhos que a base-image do Vast costuma usar. Se nenhum existir, os
  # dois últimos (PATH) resolvem.
  candidatos+=(
    /venv/main/bin/python
    /opt/venv/bin/python
    /workspace/venv/bin/python
    "$(command -v python3 2>/dev/null)"
    "$(command -v python 2>/dev/null)"
  )
  for py in "${candidatos[@]}"; do
    [ -n "$py" ] && [ -x "$py" ] || continue
    if "$py" -c 'import torch' >/dev/null 2>&1; then
      echo "$py"; return 0
    fi
  done
  return 1
}

PY=$(achar_python) || die "nenhum Python com torch encontrado. Rode 'ls /venv' na
máquina e passe o caminho em WAN2GP_PYTHON no template."

log "Python do Wan2GP: ${PY}  ($("$PY" -c 'import torch;print("torch",torch.__version__)' 2>/dev/null))"
echo "$PY" > "$RAIZ/.python"

# ── dependências do adapter ───────────────────────────────────────────
# São 4 pacotes puros de Python (fastapi, uvicorn, httpx, pydantic) — nenhum
# toca no torch, então não há risco de conflito com o ambiente do Wan2GP.
log "instalando dependências do adapter"
"$PY" -m pip install --no-cache-dir -q -r "$RAIZ/adapter/requirements.txt" \
  || die "pip install falhou"

# ── registrar no supervisor ───────────────────────────────────────────
# O supervisor da base-image reinicia o adapter se ele cair e joga o log no
# painel do Vast. Na Fase 0b perdemos tempo com log em arquivo que ficava
# vazio; por isso o stdout_logfile=/dev/stdout no .conf.
if command -v supervisorctl >/dev/null 2>&1 && [ -d /etc/supervisor/conf.d ]; then
  # Já estava rodando? Então isto é uma REEXECUÇÃO para pegar código novo, e
  # precisamos reiniciar à mão — `supervisorctl update` só mexe no que teve o
  # .conf alterado, e o nosso não muda.
  #
  # No primeiro boot é o contrário: `update` sobe o adapter, e um restart aqui
  # mataria o provision.sh no meio do download de 20 GB de pesos.
  ja_rodava=no
  supervisorctl status adapter 2>/dev/null | grep -q RUNNING && ja_rodava=sim

  mkdir -p /opt/supervisor-scripts
  install -m 755 "$RAIZ/supervisor/adapter.sh" /opt/supervisor-scripts/adapter.sh
  install -m 644 "$RAIZ/supervisor/adapter.conf" /etc/supervisor/conf.d/adapter.conf
  log "supervisor: registrando o adapter"
  supervisorctl reread >/dev/null 2>&1
  supervisorctl update >/dev/null 2>&1

  if [ "$ja_rodava" = sim ]; then
    log "adapter já rodava — reiniciando com o código novo"
    supervisorctl restart adapter >/dev/null 2>&1
  fi

  supervisorctl status adapter || warn "supervisorctl status falhou — veja 'supervisorctl status'"
  ACOMPANHE="supervisorctl tail -f adapter"
else
  # Sem supervisor: sobe destacado para sobreviver ao fim deste script.
  # setsid e não `&` puro: na Fase 0b um processo em background morreu junto
  # com o shell que o criou.
  warn "supervisor ausente — subindo o adapter solto (sem restart automático)"
  LOG=/var/log/adapter.log
  touch "$LOG" 2>/dev/null || LOG="$RAIZ/adapter.log"
  setsid "$RAIZ/supervisor/adapter.sh" >"$LOG" 2>&1 < /dev/null &
  ACOMPANHE="tail -f $LOG"
fi

log "bootstrap concluído — o adapter agora baixa os pesos e sobe a API"
log "acompanhe:  ${ACOMPANHE}"
