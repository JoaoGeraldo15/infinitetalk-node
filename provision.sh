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

# ── autenticação no Hugging Face ──────────────────────────────────────
# Sem token, o HF limita a taxa: na Fase 0b o download caiu para ~2 MB/s num
# link de 215 Mbps. Com token, usa a banda disponível.
if [ -n "${HF_TOKEN:-}" ]; then
  hf auth login --token "$HF_TOKEN" >/dev/null 2>&1 && log "autenticado no Hugging Face" \
    || warn "HF_TOKEN inválido — o download vai ficar lento"
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
  if hf download "$repo" --local-dir "$destino" "${rev[@]}" "$@"; then
    log "  ok em $(( $(date +%s) - t0 ))s"
  else
    warn "  FALHOU: $repo"
    return 1
  fi
}

# Os IDs abaixo são configuráveis por variável de ambiente: trocar de modelo
# não exige rebuild da imagem.
baixar "${HF_INFINITETALK:-MeiGen-AI/InfiniteTalk}" "$CKPTS/InfiniteTalk" || true
baixar "${HF_LORA:-lightx2v/Wan2.1-I2V-14B-480P-StepDistill-CfgDistill-Lightx2v}" \
       "$CKPTS/loras" \
       --include "${LORA_FILE:-Wan21_I2V_14B_lightx2v_cfg_step_distill_lora_rank64.safetensors}" || true

log "pesos em disco: $(du -sh "$CKPTS" 2>/dev/null | cut -f1)"
log "provisionamento concluído"
