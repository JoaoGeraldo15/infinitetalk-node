#!/usr/bin/env bash
# Por que este nó não apareceu na plataforma. Rode NA MÁQUINA (terminal do
# Jupyter). Não altera nada: só lê, testa e aponta o culpado.
#
#   bash /opt/node/diagnostico.sh
#
# Nunca imprime segredo: dos tokens sai só um hash curto, que serve para
# comparar com o outro lado sem expor o valor.

ok()   { printf '  \033[0;32m✓\033[0m %s\n' "$*"; }
nok()  { printf '  \033[0;31m✗\033[0m %s\n' "$*"; FALHAS=$((FALHAS+1)); }
info() { printf '    %s\n' "$*"; }
secao(){ printf '\n\033[1;36m── %s ──\033[0m\n' "$*"; }
h()    { printf %s "$1" | sha256sum | cut -c1-12; }
FALHAS=0

source /etc/environment 2>/dev/null || true

secao "1. variáveis no /etc/environment"
# Este é o arquivo. O que o PROCESSO enxerga é outra coisa — ver seção 2.
[ -n "$BACKEND_URL" ]   && ok "BACKEND_URL = $BACKEND_URL" \
                        || nok "BACKEND_URL AUSENTE — o nó nem tenta se registrar"
[ -n "$BACKEND_TOKEN" ] && ok "BACKEND_TOKEN definido (sha256: $(h "$BACKEND_TOKEN"))" \
                        || nok "BACKEND_TOKEN AUSENTE"
[ -n "$PUBLIC_IPADDR" ] && ok "PUBLIC_IPADDR = $PUBLIC_IPADDR" \
                        || nok "PUBLIC_IPADDR ausente (o Vast deveria injetar)"
[ -n "$VAST_TCP_PORT_8000" ] && ok "VAST_TCP_PORT_8000 = $VAST_TCP_PORT_8000" \
                        || nok "VAST_TCP_PORT_8000 ausente — sem porta direta o nó não tem endereço"
[ -n "$OPEN_BUTTON_TOKEN" ] && ok "OPEN_BUTTON_TOKEN definido (sha256: $(h "$OPEN_BUTTON_TOKEN"))" \
                        || info "OPEN_BUTTON_TOKEN vazio — só importa se o Caddy exigir"

secao "2. o que o PROCESSO do adapter enxerga"
# A pegadinha mais comum: acrescentar a variável no /etc/environment e não
# reiniciar. O processo guarda o ambiente de quando NASCEU; a seção 1 pode
# estar toda verde e esta, vermelha.
PID=$(supervisorctl pid adapter 2>/dev/null)
if [ -z "$PID" ] || [ "$PID" = "0" ]; then
  nok "adapter não está rodando"
  supervisorctl status 2>&1 | sed 's/^/    /'
else
  ok "adapter rodando (pid $PID), no ar há $(ps -o etime= -p "$PID" | tr -d ' ')"
  PROC_URL=$(tr '\0' '\n' < "/proc/$PID/environ" 2>/dev/null | sed -n 's/^BACKEND_URL=//p')
  if [ -n "$PROC_URL" ]; then
    ok "o processo enxerga BACKEND_URL = $PROC_URL"
  else
    nok "o PROCESSO não tem BACKEND_URL, mesmo que o arquivo tenha"
    info "conserto:  supervisorctl restart adapter"
  fi
fi

secao "3. o adapter responde?"
SAUDE=$(curl -s -m 10 localhost:18000/healthz)
if [ -n "$SAUDE" ]; then
  ok "healthz respondeu"
  echo "$SAUDE" | python3 -m json.tool 2>/dev/null | sed 's/^/    /' || info "$SAUDE"
else
  nok "healthz não respondeu — o adapter subiu mas travou, ou ainda está carregando"
fi

secao "4. a máquina alcança a plataforma?"
if [ -n "$BACKEND_URL" ]; then
  CODIGO=$(curl -s -o /dev/null -m 15 -w '%{http_code}' "${BACKEND_URL%/}/api/v1/health")
  case "$CODIGO" in
    200) ok "plataforma respondeu 200" ;;
    000) nok "não alcançou a plataforma (DNS, firewall ou TLS)"
         info "detalhe:"; curl -sS -m 15 -o /dev/null "${BACKEND_URL%/}/api/v1/health" 2>&1 | sed 's/^/      /' ;;
    *)   nok "plataforma respondeu HTTP $CODIGO" ;;
  esac
fi

secao "5. tentativa de registro REAL (é aqui que o motivo aparece)"
# Repete exatamente o que o adapter faz. A plataforma manda o motivo por
# extenso no corpo — segredo errado, dono ambíguo, URL recusada.
# ⚠️ container_id DESCARTÁVEL, nunca o real: o registro é um upsert por
# (dono, container_id), então usar o id verdadeiro sobrescreveria o registro
# bom com o token de mentira daqui — e o nó pararia de funcionar por causa do
# diagnóstico.
FALSO="diagnostico-$(date +%s)"
if [ -z "$PUBLIC_IPADDR" ] || [ -z "$VAST_TCP_PORT_8000" ]; then
  info "pulado: sem PUBLIC_IPADDR/VAST_TCP_PORT_8000 não há URL para registrar"
elif [ -n "$BACKEND_URL" ] && [ -n "$BACKEND_TOKEN" ]; then
  RESP=$(curl -s -m 30 -w '\n%{http_code}' -X POST "${BACKEND_URL%/}/api/v1/gpu-nodes/register" \
    -H "Authorization: Bearer $BACKEND_TOKEN" -H 'Content-Type: application/json' \
    -d "{\"url\":\"https://${PUBLIC_IPADDR}:${VAST_TCP_PORT_8000}\",
         \"token\":\"diagnostico-somente-teste\",
         \"portal_token\":\"${OPEN_BUTTON_TOKEN}\",
         \"container_id\":\"${FALSO}\",
         \"status\":\"provisioning\",\"message\":\"teste de diagnóstico\"}")
  CODIGO=$(echo "$RESP" | tail -1)
  CORPO=$(echo "$RESP" | sed '$d')
  case "$CODIGO" in
    200) ok "a plataforma ACEITOU o registro — o caminho está inteiro" ;;
    401) nok "401: BACKEND_TOKEN != GPU_NODE_REGISTER_TOKEN da plataforma"
         info "compare o hash da seção 1 com o da VPS:"
         info "  grep '^GPU_NODE_REGISTER_TOKEN=' .env | cut -d= -f2- | tr -d '\"\\n' | sha256sum | cut -c1-12" ;;
    400) nok "400: a plataforma recusou os dados" ; info "$CORPO" ;;
    503) nok "503: GPU_NODE_REGISTER_TOKEN não está configurado NA PLATAFORMA" ;;
    *)   nok "HTTP $CODIGO" ; info "$CORPO" ;;
  esac
  info ""
  info "⚠️ Se deu 200, este teste criou um registro descartável chamado"
  info "   '${FALSO}'. Ele NÃO afeta o registro real (id diferente), mas"
  info "   apague-o na plataforma para não poluir a lista."
fi

secao "6. o que o log já disse sobre registro"
LOG=/var/log/portal/adapter.log
if [ -f "$LOG" ]; then
  LINHAS=$(grep -a "registr\|BACKEND\|recusou" "$LOG" | tail -15)
  [ -n "$LINHAS" ] && echo "$LINHAS" | sed 's/^/    /' \
                   || info "(o log existe, mas nunca mencionou registro)"
else
  info "(sem $LOG — o adapter nunca chegou a escrever log)"
fi

printf '\n'
[ "$FALHAS" = 0 ] && printf '\033[0;32mNenhuma falha encontrada.\033[0m\n' \
                  || printf '\033[0;31m%s ponto(s) de falha acima.\033[0m\n' "$FALHAS"
