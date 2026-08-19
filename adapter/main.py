"""API do nó de GPU — o contrato que a plataforma consome.

Rotas (todas exigem `Authorization: Bearer <ADAPTER_TOKEN>`, exceto /healthz):

    GET    /healthz                        pronto?
    POST   /jobs                           enfileira um vídeo
    GET    /jobs/{id}                      status e progresso
    GET    /jobs/{id}/artifact/{nome}      baixa o resultado
    DELETE /jobs/{id}                      libera o disco

⚠️ O nó NÃO recebe credenciais do Supabase. Ele serve os artefatos pelo próprio
HTTP autenticado e a plataforma os armazena. A máquina é alugada de um host
anônimo de marketplace; a service key do Supabase daria acesso ao storage de
todos os usuários. Ver INTEGRATION.md §2.
"""

from __future__ import annotations

import logging
import os
import secrets
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from config import CONFIG
from jobs import CONCLUIDO, FILA
from wan2gp_client import CLIENTE

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s · %(message)s",
)
log = logging.getLogger("adapter")

# O que a plataforma mostra para a usuária. `mensagem` é texto para humano;
# `status` é o que o código decide em cima.
#   provisioning → baixando modelos ou carregando o Wan2GP
#   ready        → aceita jobs
#   failed       → algo quebrou; o log tem o motivo
ESTADO = {"status": "provisioning", "mensagem": "iniciando"}


# ── autenticação ──────────────────────────────────────────────────────

async def autenticar(request: Request) -> None:
    # CONFIG.auth_token nunca é vazio: sem ADAPTER_TOKEN no template, o
    # config.py sorteia um. Não existe modo "sem autenticação" aqui — a
    # máquina tem IP público e é alugada de um host anônimo.
    cabecalho = request.headers.get("authorization", "")
    # compare_digest e não `!=`: comparação de string vaza, pelo tempo, quantos
    # caracteres iniciais o atacante acertou.
    if not secrets.compare_digest(cabecalho, f"Bearer {CONFIG.auth_token}"):
        raise HTTPException(status_code=401, detail="token inválido")


# ── schemas ───────────────────────────────────────────────────────────

class Opcoes(BaseModel):
    aspect_ratio: str = "9:16"
    quality: str = "480p"
    steps: int | None = None
    # Comportamento do avatar: enquadramento, expressão, gestos, cenário.
    # O lip-sync vem do áudio; isto governa o resto.
    prompt: str | None = None


class PedidoJob(BaseModel):
    task: str = Field(default="render", pattern="^(render|upscale)$")
    audio_url: str
    image_url: str
    settings: Opcoes = Opcoes()


# ── ciclo de vida ─────────────────────────────────────────────────────

def _preparar() -> None:
    """Baixa os pesos e carrega o Wan2GP, reportando o progresso.

    ⚠️ O download dos pesos roda AQUI, não no adapter.sh antes do uvicorn.
    Antes era o contrário, e a consequência era ruim: durante os ~15 min de
    download não existia API nem registro, então a máquina só aparecia na
    plataforma quando já estava pronta — a usuária ficava 25 minutos sem
    nenhum sinal, sem saber se deu errado.

    Agora a API responde em segundos e reporta `provisioning`, e a plataforma
    consegue mostrar "baixando modelos" enquanto isso acontece.
    """
    try:
        marca = CONFIG.marca_provisionado
        if not marca.exists():
            ESTADO["mensagem"] = "baixando os modelos (~33 GB)"
            log.info("provisionando: baixando os modelos")
            import subprocess
            # Sem capture_output: a saída do provision.sh vai para o log do
            # adapter, que é o que aparece no painel do Vast.
            r = subprocess.run([str(CONFIG.provision_script)], check=False)
            if r.returncode == 0:
                marca.touch()
            else:
                # Não é fatal: o Wan2GP baixa sob demanda. Mas o primeiro job
                # vai pagar o download, e a usuária precisa saber disso.
                ESTADO["mensagem"] = ("modelos não pré-baixados — o primeiro "
                                      "vídeo vai demorar mais")
                log.warning("provisionamento incompleto (código %s)", r.returncode)

        ESTADO["mensagem"] = "carregando o modelo na memória"
        CLIENTE.carregar()
        FILA.iniciar()
        ESTADO["status"] = "ready"
        ESTADO["mensagem"] = "pronto para gerar"
        log.info("nó PRONTO")
    except Exception as exc:  # noqa: BLE001
        ESTADO["status"] = "failed"
        ESTADO["mensagem"] = f"falhou: {exc}"[:200]
        log.exception("falha ao preparar o nó")


def _registrar_uma_vez() -> bool:
    """Anuncia este nó à plataforma. Devolve se deu certo."""
    payload = {
        "url": CONFIG.public_url,
        "token": CONFIG.auth_token,
        # ⚠️ Sem o token do proxy a plataforma recebe uma URL inutilizável: o
        # Caddy da imagem do Vast devolve 401 e o pedido nem chega ao adapter.
        # Ele é gerado por instância, então só o próprio nó sabe qual é.
        "portal_token": CONFIG.portal_token,
        "container_id": CONFIG.container_id,
        "model_type": CONFIG.model_type,
        # É isto que a plataforma mostra: "baixando modelos", "pronto"...
        "status": ESTADO["status"],
        "message": ESTADO["mensagem"],
    }
    try:
        r = httpx.post(
            f"{CONFIG.backend_url.rstrip('/')}/api/v1/gpu-nodes/register",
            json=payload, timeout=30,
            headers={"Authorization": f"Bearer {CONFIG.backend_token}"},
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("registro falhou: %s", exc)
        return False

    if r.status_code == 200:
        return True
    # Erros de configuração não melhoram com repetição — logue o motivo, que a
    # plataforma manda por extenso (URL recusada, segredo errado...).
    log.warning("plataforma recusou o registro: HTTP %s — %s",
                r.status_code, r.text[:300])
    return False


def _registrar_na_plataforma() -> None:
    """Registra o nó e segue reanunciando como sinal de vida.

    Sem BACKEND_URL não faz nada: o operador cola o endereço no painel. Útil em
    desenvolvimento, ou se a plataforma não estiver exposta na internet.

    Por que insistir: a máquina leva ~25 min baixando modelos antes de chegar
    aqui, e nesse intervalo a plataforma pode ter reiniciado. Uma tentativa
    única deixaria o nó órfão, e a usuária só descobriria ao tentar gerar.

    Por que continuar reanunciando: o registro é idempotente (upsert por
    container_id), então reanunciar é o sinal de vida — é assim que a
    plataforma distingue uma máquina no ar de um registro de sessão passada.
    """
    if not CONFIG.backend_url:
        log.info("BACKEND_URL vazio — registro manual. URL deste nó: %s",
                 CONFIG.public_url or "(desconhecida)")
        return
    if not CONFIG.public_url:
        log.error("PUBLIC_IPADDR/VAST_TCP_PORT_8000 ausentes: o nó não sabe o "
                  "próprio endereço e não tem como se registrar.")
        return
    # Falhas de configuração não melhoram com repetição. Sem esta checagem, um
    # BACKEND_TOKEN vazio produzia seis tentativas e o erro obscuro
    # "Illegal header value b'Bearer '" — que é o httpx recusando montar o
    # cabeçalho, não a plataforma recusando o segredo.
    if not CONFIG.backend_token:
        log.error(
            "BACKEND_TOKEN vazio, mas BACKEND_URL está definido. O nó não tem "
            "como se autenticar para se registrar.\n"
            "  Corrija no template do Vast: BACKEND_TOKEN deve ter o mesmo "
            "valor do GPU_NODE_REGISTER_TOKEN da plataforma.\n"
            "  Enquanto isso, cadastre à mão: %s", CONFIG.public_url)
        return

    espera = 5
    for tentativa in range(1, 7):
        if _registrar_uma_vez():
            log.info("registrado na plataforma: %s", CONFIG.public_url)
            break
        log.info("nova tentativa de registro em %ds (%d/6)", espera, tentativa)
        time.sleep(espera)
        espera = min(espera * 2, 120)
    else:
        log.error("não foi possível registrar após 6 tentativas. O nó funciona, "
                  "mas precisa ser cadastrado à mão: %s", CONFIG.public_url)

    while True:
        # Enquanto provisiona, reanuncia rápido: é o que faz a tela da usuária
        # sair de "baixando modelos" para "pronto" sem ela precisar recarregar.
        # Depois de pronto, o intervalo longo basta — só serve para dizer que a
        # máquina continua viva.
        pronto = ESTADO["status"] == "ready"
        time.sleep(CONFIG.heartbeat_segundos if pronto else 20)
        _registrar_uma_vez()


@asynccontextmanager
async def lifespan(app: FastAPI):
    if os.environ.get("ADAPTER_TOKEN"):
        log.info("autenticação: ADAPTER_TOKEN do template")
    else:
        # Sem isto o nó ficaria inacessível: ninguém sabe o token sorteado.
        log.warning("ADAPTER_TOKEN ausente no template — token SORTEADO para "
                    "esta sessão:\n\n    %s\n\nDefina ADAPTER_TOKEN no template "
                    "para não depender do log.", CONFIG.auth_token)
    threading.Thread(target=_preparar, daemon=True).start()
    # Registro em paralelo, não depois: a plataforma precisa saber da máquina
    # DURANTE o provisionamento, não só quando ele acabar.
    threading.Thread(target=_registrar_na_plataforma, daemon=True).start()
    yield


app = FastAPI(title="InfiniteTalk Node", version="1.0", lifespan=lifespan)


# ── rotas ─────────────────────────────────────────────────────────────

@app.get("/healthz")
async def healthz() -> dict:
    """Sem autenticação de propósito: é sonda de disponibilidade."""
    livre = 0
    try:
        import subprocess
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, check=False).stdout.strip().splitlines()
        livre = int(out[0]) if out else 0
    except Exception:  # noqa: BLE001
        pass
    return {
        "status": ESTADO["status"],
        "message": ESTADO["mensagem"],
        "models_loaded": CLIENTE.pronto,
        "vram_free_mb": livre,
        "queue_depth": FILA.profundidade,
        "model_type": CONFIG.model_type,
        "max_chunk_seconds": round(CONFIG.max_chunk_seconds(), 1),
    }


@app.post("/jobs", dependencies=[Depends(autenticar)])
async def criar_job(pedido: PedidoJob, request: Request) -> dict:
    if ESTADO["status"] != "ready":
        raise HTTPException(status_code=503,
                            detail=f"nó em {ESTADO['status']}")
    job = FILA.enfileirar(pedido.task, pedido.model_dump())
    return {"job_id": job.id, "status": job.status,
            "queue_depth": FILA.profundidade}


@app.get("/jobs/{job_id}", dependencies=[Depends(autenticar)])
async def ver_job(job_id: str, request: Request) -> dict:
    job = FILA.obter(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job não encontrado")
    return job.para_api(str(request.base_url).rstrip("/"))


@app.get("/jobs/{job_id}/artifact/{nome}", dependencies=[Depends(autenticar)])
async def baixar_artefato(job_id: str, nome: str) -> FileResponse:
    job = FILA.obter(job_id)
    if job is None or job.status != CONCLUIDO:
        raise HTTPException(status_code=404, detail="job não concluído")
    artefato = job.artefatos.get(nome)
    if artefato is None or not Path(artefato.caminho).exists():
        raise HTTPException(status_code=404, detail="artefato não encontrado")
    return FileResponse(artefato.caminho, media_type="video/mp4",
                        filename=f"{job_id}-{nome}.mp4")


@app.delete("/jobs/{job_id}", dependencies=[Depends(autenticar)])
async def apagar_job(job_id: str) -> dict:
    return {"deleted": FILA.remover(job_id)}
