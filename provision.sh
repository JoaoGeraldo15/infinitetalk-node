#!/usr/bin/env bash
# Provisionamento do nó — roda no boot, dentro da imagem.
#
# Baixa os modelos que o Wan2GP vai usar e deixa a máquina pronta. O adapter
# sobe em paralelo pelo supervisor e reporta `provisioning` até terminar.
#
# ⚠️ Fica DENTRO da imagem de propósito. A variável PROVISIONING_SCRIPT da
# base-image do Vast serve para customizar imagens de terceiros por URL; como
# a imagem é nossa, uma dependência externa só adicionaria um ponto de falha.

set -uo pipefail

log() { printf '\n\033[1;36m[provision] %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m[provision] ! %s\033[0m\n' "$*" >&2; }

WAN2GP_ROOT="${WAN2GP_ROOT:-/workspace/Wan2GP}"
CKPTS="${WAN2GP_ROOT}/ckpts"
MIN_FREE_GB="${MIN_FREE_GB:-30}"

log "iniciando · modelo=${MODEL_TYPE:-padrão} · revisão=${HF_REVISION:-main}"

# ── espaço em disco ───────────────────────────────────────────────────
# O disco do Vast é ESTÁTICO: descobrir que faltou espaço no meio de um
# download de 20 GB custa a sessão inteira. Aborta cedo.
livre=$(df -BG --output=avail "$WAN2GP_ROOT" 2>/dev/null | tail -1 | tr -dc '0-9')
if [ -n "$livre" ] && [ "$livre" -lt "$MIN_FREE_GB" ]; then
  warn "só ${livre} GB livres (mínimo ${MIN_FREE_GB}). Aumente o disco no template."
  exit 1
fi
log "disco: ${livre} GB livres"

# ── achar o CLI do Hugging Face ───────────────────────────────────────
# ⚠️ Ele mora no VENV da imagem (/venv/main/bin/hf), e este script roda pelo
# supervisor, sem o venv ativado. Sem isto, todo download falhava com
# "hf: command not found" — e como o `hf auth login` falhava pelo mesmo
# motivo, a mensagem que aparecia era "HF_TOKEN inválido", apontando para o
# lugar errado. Medido em 2026-08-19.
#
# O bootstrap grava em .python o interpretador que tem o torch; o `hf` está
# no mesmo diretório.
PY_DO_VENV=$(cat "${ADAPTER_ROOT:-/opt/node}/.python" 2>/dev/null || true)
if [ -n "$PY_DO_VENV" ] && [ -x "$PY_DO_VENV" ]; then
  export PATH="$(dirname "$PY_DO_VENV"):$PATH"
fi

# `hf` é o comando atual; `huggingface-cli` é o nome antigo, ainda presente em
# imagens mais velhas. Testamos os dois antes de desistir.
HF=""
for candidato in hf huggingface-cli; do
  if command -v "$candidato" >/dev/null 2>&1; then HF="$candidato"; break; fi
done

if [ -z "$HF" ]; then
  warn "CLI do Hugging Face não encontrado (nem 'hf' nem 'huggingface-cli')."
  warn "PATH=$PATH"
  warn "Os pesos NÃO serão pré-baixados; o primeiro job vai baixá-los e levar"
  warn "uns 20 minutos a mais."
  exit 1
fi
log "CLI do Hugging Face: $(command -v "$HF")"

# ── autenticação no Hugging Face ──────────────────────────────────────
# Sem token, o HF limita a taxa: na Fase 0b o download caiu para ~2 MB/s num
# link de 215 Mbps. Com token, usa a banda disponível.
if [ -n "${HF_TOKEN:-}" ]; then
  if "$HF" auth login --token "$HF_TOKEN" >/dev/null 2>&1; then
    log "autenticado no Hugging Face"
  else
    warn "HF_TOKEN recusado pelo Hugging Face — o download vai ficar lento"
  fi
else
  warn "HF_TOKEN ausente: downloads não autenticados são limitados pelo HF"
fi

# ── modelos ───────────────────────────────────────────────────────────
# O Wan2GP baixa sob demanda na primeira geração. Fazer isso aqui evita que o
# PRIMEIRO vídeo da sessão pague o custo — na Fase 0b isso inflou a primeira
# medição de 25x para 60x tempo real.
mkdir -p "$CKPTS"

baixar() {
  local repo="$1" destino="$2"; shift 2
  local rev=()
  [ -n "${HF_REVISION:-}" ] && rev=(--revision "$HF_REVISION")
  log "baixando $repo"
  local t0; t0=$(date +%s)
  if "$HF" download "$repo" --local-dir "$destino" "${rev[@]}" "$@"; then
    log "  ok em $(( $(date +%s) - t0 ))s"
  else
    warn "  FALHOU: $repo"
    return 1
  fi
}

# Os IDs abaixo são configuráveis por variável de ambiente: trocar de modelo
# não exige rebuild da imagem.
falhas=0
baixar "${HF_INFINITETALK:-MeiGen-AI/InfiniteTalk}" "$CKPTS/InfiniteTalk" || falhas=$((falhas+1))
baixar "${HF_LORA:-lightx2v/Wan2.1-I2V-14B-480P-StepDistill-CfgDistill-Lightx2v}" \
       "$CKPTS/loras" \
       --include "${LORA_FILE:-Wan21_I2V_14B_lightx2v_cfg_step_distill_lora_rank64.safetensors}" \
       || falhas=$((falhas+1))

# ⚠️ Até 2026-08-14 havia `|| true` nas duas linhas acima, e este script
# reportava "concluído" com ZERO byte baixado — foi o que aconteceu na primeira
# sessão real. O adapter subia `ready`, e o download de 20 GB só acontecia no
# meio do primeiro job, inflando a medição. Falhar aqui é o ponto: é barato
# descobrir agora e caro descobrir com o cliente esperando.
baixados=$(du -sb "$CKPTS" 2>/dev/null | cut -f1)
baixados=${baixados:-0}
log "pesos em disco: $(numfmt --to=iec "$baixados" 2>/dev/null || echo "$baixados B")"

if [ "$falhas" -gt 0 ] || [ "$baixados" -lt 1000000000 ]; then
  warn "PROVISIONAMENTO INCOMPLETO: ${falhas} download(s) falharam e só há"
  warn "$(numfmt --to=iec "$baixados" 2>/dev/null || echo "$baixados") em ${CKPTS}."
  warn "Causas comuns: HF_TOKEN inválido ou ausente · repositório gated sem"
  warn "aceite · sem espaço em disco. Veja as mensagens acima."
  warn "O adapter vai subir mesmo assim — o Wan2GP baixa sob demanda —, mas o"
  warn "PRIMEIRO job pagará o download inteiro."
  exit 1
fi

log "provisionamento concluído"
