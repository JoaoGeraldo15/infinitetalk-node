"""Fila serial de jobs e execução do pipeline completo.

**Serial de propósito**: há uma GPU. Dois vídeos ao mesmo tempo brigariam por
VRAM e ficariam mais lentos que em sequência. A plataforma pode enfileirar à
vontade; o nó processa um por vez.

Estado em memória + JSON por job em disco. Não há banco: o nó é descartável e
vive um fim de semana. A verdade permanente é da plataforma.
"""

from __future__ import annotations

import json
import logging
import queue
import shutil
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

import httpx

from config import CONFIG
from pipeline import (anexar_audio, concatenar, cortar_inicio, duracao,
                      fatiar_audio, recortar)
from wan2gp_client import CLIENTE

log = logging.getLogger("adapter.jobs")

# Mesmos valores do AvatarJob da plataforma — sem tabela de tradução no provider.
PENDENTE, PROCESSANDO, CONCLUIDO, FALHOU = "pending", "processing", "completed", "failed"


@dataclass
class Artefato:
    nome: str
    caminho: str
    largura: int = 0
    altura: int = 0
    duracao_segundos: float = 0.0
    bytes: int = 0


@dataclass
class Job:
    id: str
    task: str
    status: str = PENDENTE
    progress: float = 0.0
    message: str = ""
    error: str | None = None
    gpu_seconds: float = 0.0
    criado_em: float = field(default_factory=time.time)
    settings: dict = field(default_factory=dict)
    artefatos: dict[str, Artefato] = field(default_factory=dict)

    def para_api(self, base_url: str) -> dict:
        return {
            "job_id": self.id,
            "status": self.status,
            "progress": round(self.progress, 3),
            "message": self.message,
            "error": self.error,
            "gpu_seconds": round(self.gpu_seconds, 1),
            "artifacts": {
                nome: {
                    "url": f"{base_url}/jobs/{self.id}/artifact/{nome}",
                    "width": a.largura, "height": a.altura,
                    "duration_seconds": a.duracao_segundos, "bytes": a.bytes,
                }
                for nome, a in self.artefatos.items()
            },
        }


class Fila:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._fila: queue.Queue[str] = queue.Queue()
        self._lock = threading.Lock()
        self._worker: threading.Thread | None = None
        CONFIG.jobs_dir.mkdir(parents=True, exist_ok=True)
        CONFIG.work_dir.mkdir(parents=True, exist_ok=True)

    # ── API pública ───────────────────────────────────────────────────

    def iniciar(self) -> None:
        if self._worker and self._worker.is_alive():
            return
        self._worker = threading.Thread(target=self._loop, daemon=True)
        self._worker.start()
        log.info("worker da fila iniciado")

    def enfileirar(self, task: str, settings: dict) -> Job:
        job = Job(id=uuid.uuid4().hex[:12], task=task, settings=settings)
        with self._lock:
            self._jobs[job.id] = job
        self._salvar(job)
        self._fila.put(job.id)
        log.info("job %s enfileirado (%d na fila)", job.id, self._fila.qsize())
        return job

    def obter(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def remover(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.pop(job_id, None)
        if job is None:
            return False
        dir_job = CONFIG.work_dir / job_id
        if dir_job.exists():
            import shutil
            shutil.rmtree(dir_job, ignore_errors=True)
        (CONFIG.jobs_dir / f"{job_id}.json").unlink(missing_ok=True)
        return True

    @property
    def profundidade(self) -> int:
        return self._fila.qsize()

    # ── worker ────────────────────────────────────────────────────────

    def _loop(self) -> None:
        while True:
            job_id = self._fila.get()
            job = self.obter(job_id)
            if job is None:
                continue
            try:
                self._executar(job)
            except Exception as exc:  # noqa: BLE001
                log.exception("job %s falhou", job.id)
                job.status, job.error = FALHOU, f"{type(exc).__name__}: {exc}"
            finally:
                self._salvar(job)

    def _executar(self, job: Job) -> None:
        job.status = PROCESSANDO
        job.message = "preparando"
        self._salvar(job)

        dir_job = CONFIG.work_dir / job.id
        dir_job.mkdir(parents=True, exist_ok=True)

        s = job.settings
        imagem = self._baixar(s["image_url"], dir_job / "avatar.png")
        audio = self._baixar(s["audio_url"], dir_job / "audio-original")
        audio_wav = dir_job / "audio.wav"
        self._normalizar_audio(audio, audio_wav)

        opcoes = s.get("settings") or {}
        proporcao = opcoes.get("aspect_ratio", "9:16")
        resolucao = CONFIG.resolucao(proporcao)
        # None quando a proporção é nativa do Wan2GP; senão, o quadro pedido.
        recorte = CONFIG.recorte(proporcao)
        prompt = opcoes.get("prompt") or CONFIG.default_prompt
        steps = int(opcoes.get("steps") or CONFIG.steps)

        pedacos = fatiar_audio(audio_wav, dir_job / "chunks")

        # ⚠️ O ENCADEAMENTO ESTÁ DESATIVADO, de propósito.
        #
        # Com `image_prompt_type="L"` + `video_source`, o Wan2GP continua o
        # vídeo e respeita a duração do áudio — mas NÃO aplica lip-sync ao
        # trecho novo: o avatar fica piscando e respirando enquanto a narração
        # corre. Medido em 2026-08-17, num vídeo de 47,9 s: perfeito até os
        # 29,5 s, mudo daí em diante.
        #
        # É o pior tipo de defeito, porque o vídeo *parece* bom. Recusar é
        # melhor que entregar metade da narração muda. Com MAX_FRAMES em 6000
        # (4 min) isto praticamente não acontece; se acontecer, o caminho é
        # aumentar o teto, não voltar a encadear.
        if len(pedacos) > 1:
            job.status = FALHOU
            job.error = (
                f"Áudio de {duracao(audio_wav):.0f}s excede o limite de "
                f"{CONFIG.max_chunk_seconds():.0f}s por geração. Dividir em "
                "pedaços encadeados produziria vídeo sem lip-sync na segunda "
                "metade, então preferimos recusar. Aumente MAX_FRAMES no nó, "
                "ou use um áudio mais curto."
            )
            log.error("job %s recusado: %d pedaços", job.id, len(pedacos))
            return

        job.message = f"0/{len(pedacos)} pedaços"
        self._salvar(job)

        videos: list[Path] = []
        anterior: Path | None = None
        t0 = time.perf_counter()

        for pedaco in pedacos:
            def progresso(fracao: float, i=pedaco.indice, n=len(pedacos)) -> None:
                job.progress = (i + max(0.0, min(1.0, fracao))) / n
                job.message = f"{i + 1}/{n} pedaços"

            settings_wan = CLIENTE.montar_settings(
                imagem=imagem, audio=pedaco.audio, frames=pedaco.frames,
                resolucao=resolucao, prompt=prompt, steps=steps,
                video_anterior=anterior,
            )
            resultado = CLIENTE.gerar(settings_wan, on_progress=progresso)
            job.gpu_seconds += resultado.segundos

            if not resultado.ok or resultado.arquivo is None:
                job.status = FALHOU
                job.error = f"pedaço {pedaco.indice}: {resultado.erro}"
                return

            destino = dir_job / f"parte_{pedaco.indice:03d}.mp4"
            # ⚠️ COPIAR, nunca mover. Aqui havia `resultado.arquivo.replace()`,
            # que renomeia — e tirava o arquivo de `Wan2GP/outputs/`.
            #
            # O Wan2GP guarda referência aos arquivos que gerou e passa por eles
            # quando a tarefa seguinte começa. Sem o arquivo no lugar, o pedaço
            # SEGUINTE morria com "[generation] ffprobe skipped; file not found:
            # outputs/....mp4" — apontando para um arquivo que nós mesmos
            # levamos embora. Medido em 2026-08-17: o pedaço 0 concluía, e o
            # pedaço 1 estourava 13 s depois de começar, na preparação.
            #
            # O custo de copiar é alguns MB por pedaço, num disco de 256 GB que
            # morre junto com a sessão.
            shutil.copy2(resultado.arquivo, destino)

            if anterior is not None:
                # A saída encadeada traz a origem colada na frente. Cortamos os
                # segundos da origem para sobrar só o trecho novo — senão a
                # concatenação repetiria cada pedaço. Ver pipeline.cortar_inicio.
                origem_segundos = duracao(anterior)
                cortado = dir_job / f"parte_{pedaco.indice:03d}_novo.mp4"
                cortar_inicio(destino, origem_segundos, cortado)
                destino = cortado

            videos.append(destino)
            anterior = destino
            job.progress = (pedaco.indice + 1) / len(pedacos)
            self._salvar(job)

        job.message = "concatenando"
        self._salvar(job)

        bruto = concatenar(videos, dir_job / "concat.mp4")
        if recorte:
            job.message = f"recortando para {proporcao}"
            self._salvar(job)
            bruto = recortar(bruto, recorte, dir_job / "crop.mp4")
        master = anexar_audio(bruto, audio_wav, dir_job / "master.mp4")

        largura, altura = self._dimensoes(master)
        job.artefatos["master"] = Artefato(
            nome="master", caminho=str(master),
            largura=largura, altura=altura,
            duracao_segundos=duracao(master), bytes=master.stat().st_size,
        )
        job.status = CONCLUIDO
        job.progress = 1.0
        job.message = f"{len(pedacos)} pedaços em {time.perf_counter() - t0:.0f}s"
        log.info("job %s concluído: %s", job.id, job.message)

    # ── utilidades ────────────────────────────────────────────────────

    @staticmethod
    def _baixar(url: str, destino: Path) -> Path:
        """Traz uma entrada para o diretório do job.

        Em produção a plataforma manda URL https. Mas na primeira sessão de
        validação ela ainda não está no ar, e é preciso poder testar com um
        arquivo enviado pelo Jupyter — daí o caminho local também ser aceito.
        """
        if url.startswith(("http://", "https://")):
            with httpx.stream("GET", url, timeout=300, follow_redirects=True) as r:
                r.raise_for_status()
                with destino.open("wb") as f:
                    for bloco in r.iter_bytes():
                        f.write(bloco)
            return destino

        # Caminho local: restrito a LOCAL_INPUT_DIR (/workspace por padrão).
        # Sem essa cerca, quem tivesse o token da API leria qualquer arquivo do
        # container passando "/etc/shadow" como audio_url.
        origem = Path(url.removeprefix("file://")).resolve()
        permitido = CONFIG.local_input_dir.resolve()
        if not origem.is_relative_to(permitido):
            raise ValueError(
                f"caminho local fora de {permitido}: {origem}. "
                "Use uma URL https ou envie o arquivo para lá.")
        if not origem.is_file():
            raise FileNotFoundError(f"não existe: {origem}")

        shutil.copy2(origem, destino)
        log.info("entrada local copiada: %s", origem)
        return destino

    @staticmethod
    def _normalizar_audio(origem: Path, destino: Path) -> None:
        import subprocess
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", str(origem),
             "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(destino)],
            check=True)

    @staticmethod
    def _dimensoes(video: Path) -> tuple[int, int]:
        import re
        import subprocess
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "csv=s=x:p=0",
             str(video)], capture_output=True, text=True, check=False).stdout
        m = re.match(r"(\d+)x(\d+)", out.strip())
        return (int(m.group(1)), int(m.group(2))) if m else (0, 0)

    @staticmethod
    def _salvar(job: Job) -> None:
        (CONFIG.jobs_dir / f"{job.id}.json").write_text(
            json.dumps(asdict(job), default=str, ensure_ascii=False, indent=2))


FILA = Fila()
