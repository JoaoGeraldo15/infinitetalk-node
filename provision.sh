#!/usr/bin/env bash
# Preparação do nó — roda uma vez, chamado pelo adapter em segundo plano.
#
# ⚠️ ESTE SCRIPT NÃO BAIXA MAIS OS MODELOS. Quem baixa é o próprio Wan2GP, na
# primeira geração, e ele faz isso certo.
#
# O que havia aqui antes, e por que saiu (medido em 2026-08-19):
#
#   · baixava `MeiGen-AI/InfiniteTalk` INTEIRO — 157 GB, sete variantes de
#     18,2 GB cada (single/multi × fp8/int8 × com/sem LoRA). O Wan2GP não usa
#     nenhuma delas: o `defaults/infinitetalk.json` dele aponta para
#     `DeepBeepMeep/Wan2.1`, outro repositório;
#   · baixava a LoRA para `ckpts/loras`, mas o Wan2GP a procura em
#     `loras/wan_i2v/` — o log da geração mostra o caminho.
#
# Resultado: 157 GB de banda e disco desperdiçados, e o Wan2GP baixando tudo de
# novo por baixo. A geração sempre funcionou porque ele nunca dependeu daqui.
#
# O que ele realmente usa, para referência (variante int8, ~19 GB):
#   wan2.1_image2video_480p_14B_quanto_mbf16_int8.safetensors      15,8 GB
#   wan2.1_infinitetalk_single_14B_quanto_mbf16_int8.safetensors    2,4 GB
#   loras_accelerators/Wan21_I2V_14B_lightx2v_...rank64.safetensors 0,7 GB
#
# Pré-baixar isso exigiria replicar o layout de diretórios que o Wan2GP espera.
# Errar o layout custa o dobro: baixa e ele baixa de novo. Enquanto não houver
# medição do layout real, deixar com ele é mais barato e mais confiável.

set -uo pipefail

log() { printf '\n\033[1;36m[provision] %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m[provision] ! %s\033[0m\n' "$*" >&2; }

WAN2GP_ROOT="${WAN2GP_ROOT:-/workspace/Wan2GP}"
MIN_FREE_GB="${MIN_FREE_GB:-40}"

log "preparando · modelo=${MODEL_TYPE:-padrão}"

# ── espaço em disco ───────────────────────────────────────────────────
# O disco do Vast é ESTÁTICO: descobrir que faltou espaço no meio da primeira
# geração custa a sessão inteira. Aborta cedo.
#
# ~19 GB de pesos + VAE, text encoder e wav2vec2 + saídas de vídeo. 40 GB é o
# mínimo com folga; 100 GB no template deixa tranquilo.
livre=$(df -BG --output=avail "$WAN2GP_ROOT" 2>/dev/null | tail -1 | tr -dc '0-9')
if [ -n "$livre" ] && [ "$livre" -lt "$MIN_FREE_GB" ]; then
  warn "só ${livre} GB livres (mínimo ${MIN_FREE_GB}). Aumente o disco no template."
  exit 1
fi
log "disco: ${livre} GB livres"

# ── autenticação no Hugging Face ──────────────────────────────────────
# Continua valendo: sem token o HF limita a taxa, e na Fase 0b o download caiu
# para ~2 MB/s num link de 215 Mbps. Autenticar aqui vale para os downloads que
# o Wan2GP fizer depois, porque o token fica no ~/.cache/huggingface.
#
# ⚠️ O CLI mora no venv da imagem, e este script roda pelo supervisor, sem o
# venv ativado. Sem ajustar o PATH, dava "hf: command not found" — e como o
# `auth login` falhava pelo mesmo motivo, a mensagem dizia "HF_TOKEN inválido",
# apontando para o lugar errado.
PY_DO_VENV=$(cat "${ADAPTER_ROOT:-/opt/node}/.python" 2>/dev/null || true)
if [ -n "$PY_DO_VENV" ] && [ -x "$PY_DO_VENV" ]; then
  export PATH="$(dirname "$PY_DO_VENV"):$PATH"
fi

HF=""
for candidato in hf huggingface-cli; do
  if command -v "$candidato" >/dev/null 2>&1; then HF="$candidato"; break; fi
done

if [ -z "$HF" ]; then
  warn "CLI do Hugging Face não encontrado (nem 'hf' nem 'huggingface-cli')."
  warn "PATH=$PATH"
  warn "Os downloads do Wan2GP vão funcionar, mas sem autenticação e mais lentos."
elif [ -n "${HF_TOKEN:-}" ]; then
  if "$HF" auth login --token "$HF_TOKEN" >/dev/null 2>&1; then
    log "autenticado no Hugging Face"
  else
    warn "HF_TOKEN recusado pelo Hugging Face — os downloads vão ficar lentos"
  fi
else
  warn "HF_TOKEN ausente: downloads não autenticados são limitados pelo HF"
fi

# ── pré-download dos pesos grandes ────────────────────────────────────
# ⚠️ Já tentamos isto antes e deu errado: baixávamos o repositório
# `MeiGen-AI/InfiniteTalk` inteiro (157 GB, sete variantes) para
# `ckpts/InfiniteTalk/`, e o Wan2GP não lia nada daquilo — ele busca em
# `DeepBeepMeep/Wan2.1` e grava DIRETO em `ckpts/`, sem subpasta.
#
# Desta vez os nomes e o destino vieram do log da própria máquina baixando:
#
#   wan2.1_image2video_480p_14B_quanto_mbf16(…): 54%|███ | 9.12G/17.0G
#
# Por que pré-baixar: sem isto o nó reporta "pronto" com o modelo principal
# ainda ausente, e a PRIMEIRA geração de cada máquina gasta ~5 min baixando —
# um tempo que não aparece em barra nenhuma e faz a usuária achar que travou.
#
# Se algum nome estiver errado, o custo é só banda: o Wan2GP baixa por cima e
# nada quebra.
if [ -n "$HF" ]; then
  baixar_peso() {
    local arquivo="$1" destino="$2"
    if [ -f "$destino/$arquivo" ]; then
      log "  já em disco: $arquivo"; return 0
    fi
    log "baixando $arquivo"
    "$HF" download DeepBeepMeep/Wan2.1 --include "$arquivo" --local-dir "$destino" \
      || warn "  falhou (o Wan2GP baixa sob demanda): $arquivo"
  }

  CKPTS="${WAN2GP_ROOT}/ckpts"
  mkdir -p "$CKPTS" "${WAN2GP_ROOT}/loras/wan_i2v"

  baixar_peso "${HF_MODELO_I2V:-wan2.1_image2video_480p_14B_quanto_mbf16_int8.safetensors}" "$CKPTS"
  baixar_peso "${HF_MODELO_TALK:-wan2.1_infinitetalk_single_14B_quanto_mbf16_int8.safetensors}" "$CKPTS"

  # A LoRA vai para outro lugar — `loras/wan_i2v/`, e não `ckpts/`. Descobrimos
  # pelo log da geração: "Lora 'loras/wan_i2v/Wan21_...' was loaded".
  lora="${LORA_FILE:-Wan21_I2V_14B_lightx2v_cfg_step_distill_lora_rank64.safetensors}"
  if [ ! -f "${WAN2GP_ROOT}/loras/wan_i2v/$lora" ]; then
    log "baixando a LoRA"
    "$HF" download DeepBeepMeep/Wan2.1 --include "loras_accelerators/$lora" \
      --local-dir "${WAN2GP_ROOT}/loras/wan_i2v" 2>/dev/null \
      && mv -f "${WAN2GP_ROOT}/loras/wan_i2v/loras_accelerators/$lora" \
               "${WAN2GP_ROOT}/loras/wan_i2v/" 2>/dev/null \
      && rmdir "${WAN2GP_ROOT}/loras/wan_i2v/loras_accelerators" 2>/dev/null \
      || warn "  LoRA não pré-baixada (o Wan2GP busca sozinho)"
  fi

  log "pesos em disco: $(du -sh "$CKPTS" 2>/dev/null | cut -f1)"
fi

log "preparação concluída"
