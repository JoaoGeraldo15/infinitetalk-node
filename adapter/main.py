"""API do nó de GPU — o contrato que a plataforma consome.

Rotas (todas exigem `Authorization: Bearer <OPEN_BUTTON_TOKEN>`):

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
import threading
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

# provisioning enquanto o Wan2GP carrega; ready depois.
ESTADO = {"status": "provisioning"}


# ── autenticação ──────────────────────────────────────────────────────

async def autenticar(request: Request) -> None:
    if not CONFIG.auth_token:
        log.warning("OPEN_BUTTON_TOKEN vazio — API SEM autenticação")
        return
    cabecalho = request.headers.get("authorization", "")
    if cabecalho != f"Bearer {CONFIG.auth_token}":
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
    """Carrega o Wan2GP e registra o nó. Roda em thread para não travar o boot."""
    try:
        CLIENTE.carregar()
        FILA.iniciar()
        ESTADO["status"] = "ready"
        log.info("nó PRONTO")
        _registrar_na_plataforma()
    except Exception:  # noqa: BLE001
        ESTADO["status"] = "failed"
        log.exception("falha ao preparar o nó")


def _registrar_na_plataforma() -> None:
    """Avisa a plataforma onde este nó está.

    Opcional: sem BACKEND_URL, o operador cola o endereço no dashboard.
    Útil quando a plataforma não está exposta na internet.
    """
    if not CONFIG.backend_url:
        log.info("BACKEND_URL vazio — registro manual. URL deste nó: %s",
                 CONFIG.public_url or "(desconhecida)")
        return
    payload = {
        "url": CONFIG.public_url,
        "token": CONFIG.auth_token,
        "container_id": CONFIG.container_id,
        "model_type": CONFIG.model_type,
    }
    try:
        r = httpx.post(
            f"{CONFIG.backend_url.rstrip('/')}/api/v1/gpu-nodes/register",
            json=payload, timeout=30,
            headers={"Authorization": f"Bearer {CONFIG.backend_token}"},
        )
        log.info("registro na plataforma: HTTP %s", r.status_code)
    except Exception as exc:  # noqa: BLE001
        log.warning("registro falhou (%s) — use o cadastro manual: %s",
                    exc, CONFIG.public_url)


@asynccontextmanager
async def lifespan(app: FastAPI):
    threading.Thread(target=_preparar, daemon=True).start()
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
