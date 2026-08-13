"""Fronteira com o Wan2GP — o único arquivo que conhece a API dele.

Todo o resto do adapter fala com esta classe. Se o Wan2GP mudar de versão ou
for trocado por outro runtime, **só este arquivo muda**.

A API foi documentada em https://github.com/deepbeepmeep/Wan2GP/blob/main/docs/API.md:

    from shared.api import init
    session = init(root=Path("/workspace/Wan2GP"))
    job = session.submit_task(settings)      # settings = dict
    result = job.result()                    # .generated_files, .success
    for e in job.events.iter(timeout=0.2):   # .kind, .data.progress
        ...

⚠️ NÃO EXECUTADO ainda: este adapter foi escrito sem uma GPU disponível. O
formato exato do dict de settings vem do botão **"Export Settings to File"** da
UI — por isso `settings_base.json` é carregado de disco e apenas *sobrescrito*
aqui, em vez de construído do zero. Se o Wan2GP mudar os nomes dos campos, o
conserto é no JSON, não no código.
"""

from __future__ import annotations

import json
import logging
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

from config import CONFIG

log = logging.getLogger("adapter.wan2gp")

# Caminho do settings exportado pela UI. Serve de base para todo job: o
# adapter só troca áudio, imagem, frames, resolução e prompt.
SETTINGS_BASE = Path(__file__).parent / "settings_base.json"


@dataclass
class GeracaoResultado:
    ok: bool
    arquivo: Path | None = None
    erro: str | None = None
    segundos: float = 0.0


class Wan2GPClient:
    """Envolve a sessão in-process do Wan2GP.

    In-process de propósito: os modelos ficam residentes na VRAM entre os
    jobs. Medido na Fase 0b — a primeira geração custou 60x tempo real (com
    carga) e as seguintes 25,5x. Subprocesso por job pagaria 60x sempre.
    """

    def __init__(self) -> None:
        self._session = None
        self._lock = threading.Lock()
        self._base: dict = {}

    # ── ciclo de vida ─────────────────────────────────────────────────

    def carregar(self) -> None:
        """Inicializa a sessão. Chamado uma vez, no start do adapter."""
        if self._session is not None:
            return
        with self._lock:
            if self._session is not None:
                return
            if SETTINGS_BASE.exists():
                self._base = json.loads(SETTINGS_BASE.read_text())
                log.info("settings base carregado de %s (%d chaves)",
                         SETTINGS_BASE.name, len(self._base))
            else:
                log.warning("%s AUSENTE — usando settings mínimos. Exporte o "
                            "JSON pela UI do Wan2GP e coloque aqui.",
                            SETTINGS_BASE.name)
                self._base = {}

            sys.path.insert(0, str(CONFIG.wan2gp_root))
            from shared.api import init  # noqa: PLC0415

            log.info("iniciando sessão do Wan2GP em %s", CONFIG.wan2gp_root)
            self._session = init(root=CONFIG.wan2gp_root)
            log.info("sessão pronta")

    @property
    def pronto(self) -> bool:
        return self._session is not None

    # ── geração ───────────────────────────────────────────────────────

    def montar_settings(
        self,
        *,
        imagem: Path,
        audio: Path,
        frames: int,
        resolucao: str,
        prompt: str,
        steps: int,
        video_anterior: Path | None = None,
    ) -> dict:
        """Settings de UM pedaço.

        Parte do JSON exportado da UI e sobrescreve só o que varia. Assim os
        dezenas de campos que não nos interessam continuam com o valor que
        você validou na Fase 0b.
        """
        s = dict(self._base)
        # CONFIRMADOS no settings_base.json exportado da UI:
        s.update({
            "model_type": CONFIG.model_type,      # "infinitetalk"
            "prompt": prompt,
            "resolution": resolucao,              # "480x832"
            "video_length": frames,               # em frames, teto 737
            "num_inference_steps": steps,
            "activated_loras": [CONFIG.lora_url],
            "loras_multipliers": "1|",
        })
        # CONFIRMADOS no fonte do Wan2GP (wgp.py, ATTACHMENT_KEYS):
        #   ["image_start", "image_end", "image_refs", "image_guide",
        #    "image_mask", "video_guide", "video_guide2", "video_mask",
        #    "video_source", "audio_guide", "audio_guide2", "audio_source",
        #    "replace_voice_sample", "replace_voice_sample2", "custom_guide"]
        #
        # E os códigos de `image_prompt_type` (também do wgp.py):
        #   "S" → exige image_start   ·   "L" → continua de video_source
        #   "E" → image_end           ·   "V" → video_guide / image_guide
        s["audio_guide"] = str(audio)          # "Voice to follow" na UI

        if video_anterior is not None:
            # Encadeamento: "L" faz o Wan2GP continuar do pedaço anterior.
            # `image_start` sai de cena — quem define o ponto de partida agora
            # é o último frame do vídeo anterior.
            s["image_prompt_type"] = "L"
            s["video_source"] = str(video_anterior)
            s.pop("image_start", None)
        else:
            # Primeiro pedaço: parte da foto. "S" exige image_start; o "I" em
            # video_prompt_type ("0KI|") é o que exige image_refs — foi por
            # isso que a UI reclamou de imagem de referência na Fase 0b.
            s["image_prompt_type"] = "S"
            s["image_start"] = str(imagem)
            s["image_refs"] = [str(imagem)]
            s.pop("video_source", None)
        return s

    def gerar(self, settings: dict, on_progress=None) -> GeracaoResultado:
        """Executa uma geração e devolve o arquivo produzido.

        `on_progress(fracao)` é chamado conforme os eventos chegam, para o
        `GET /jobs/{id}` reportar progresso real em vez de estimativa.
        """
        import time

        if self._session is None:
            return GeracaoResultado(False, erro="sessão do Wan2GP não iniciada")

        t0 = time.perf_counter()
        try:
            job = self._session.submit_task(settings)
        except Exception as exc:  # noqa: BLE001
            return GeracaoResultado(False, erro=f"submit_task: {exc}")

        if on_progress is not None:
            threading.Thread(
                target=self._bombear_eventos, args=(job, on_progress), daemon=True
            ).start()

        try:
            resultado = job.result()
        except Exception as exc:  # noqa: BLE001
            return GeracaoResultado(False, erro=f"result: {exc}",
                                    segundos=time.perf_counter() - t0)

        segundos = time.perf_counter() - t0
        arquivos = getattr(resultado, "generated_files", None) or []
        if not getattr(resultado, "success", False) or not arquivos:
            erro = getattr(resultado, "error", None) or "sem arquivo gerado"
            return GeracaoResultado(False, erro=str(erro), segundos=segundos)

        return GeracaoResultado(True, arquivo=Path(arquivos[0]), segundos=segundos)

    @staticmethod
    def _bombear_eventos(job, on_progress) -> None:
        """Repassa os eventos de progresso. Falha aqui nunca derruba o job."""
        try:
            for evento in job.events.iter(timeout=0.5):
                if getattr(evento, "kind", None) == "progress":
                    dados = getattr(evento, "data", None)
                    fracao = getattr(dados, "progress", None)
                    if fracao is not None:
                        on_progress(float(fracao))
        except Exception:  # noqa: BLE001
            log.debug("stream de eventos encerrado", exc_info=True)


CLIENTE = Wan2GPClient()
