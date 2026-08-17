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
import os
import sys
import threading
from contextlib import contextmanager
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

            # ⚠️ O `wgp.py` do Wan2GP faz `os.mkdir("settings")` no nível do
            # módulo, SEM exist_ok — e importar `shared.api` importa o wgp.
            # Resultado: na primeira vez o diretório é criado e tudo funciona;
            # em QUALQUER carga seguinte ele explode com FileExistsError e o
            # adapter nunca fica pronto.
            #
            # É o mesmo bug que derruba o serviço Gradio da própria imagem do
            # Vast (`/var/log/portal/wan2gp.log` mostra o mesmo traceback). Na
            # primeira sessão em GPU eu concluí que não nos afetava porque
            # rodamos noutro diretório de trabalho — funcionou por sorte, e
            # quebrou no primeiro restart da segunda máquina.
            #
            # Tolerar o mkdir durante o import é cirúrgico e reversível;
            # apagar o diretório apagaria estado do Wan2GP.
            with self._mkdir_tolerante():
                from shared.api import init  # noqa: PLC0415

                log.info("iniciando sessão do Wan2GP em %s", CONFIG.wan2gp_root)
                self._session = init(root=CONFIG.wan2gp_root)
            log.info("sessão pronta")

    @staticmethod
    @contextmanager
    def _mkdir_tolerante():
        """Faz `os.mkdir` ignorar FileExistsError, e restaura ao sair."""
        original = os.mkdir

        def mkdir(caminho, *args, **kwargs):
            try:
                original(caminho, *args, **kwargs)
            except FileExistsError:
                log.debug("mkdir tolerado: %s já existe", caminho)

        os.mkdir = mkdir
        try:
            yield
        finally:
            os.mkdir = original

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

        # A imagem de REFERÊNCIA vale para todo pedaço, encadeado ou não: é ela
        # que fixa a identidade da pessoa. O "I" em video_prompt_type ("0KI|")
        # a torna obrigatória — foi por isso que a UI a exigiu na Fase 0b.
        #
        # ⚠️ Até 2026-08-14 esta linha estava só no ramo do primeiro pedaço, e
        # TODO pedaço encadeado morria na validação com
        # "You must provide at least one Reference Image". Custou uma sessão
        # inteira de GPU descobrir.
        s["image_refs"] = [str(imagem)]

        if video_anterior is not None:
            # Encadeamento: "L" faz o Wan2GP continuar do pedaço anterior.
            # `image_start` sai de cena — quem define o ponto de partida agora
            # é o último frame do vídeo anterior.
            s["image_prompt_type"] = "L"
            s["video_source"] = str(video_anterior)
            s.pop("image_start", None)
        else:
            # Primeiro pedaço: parte da foto. "S" exige image_start.
            s["image_prompt_type"] = "S"
            s["image_start"] = str(imagem)
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
            return GeracaoResultado(False, erro=self._extrair_erro(resultado),
                                    segundos=segundos)

        # ⚠️ arquivos[-1], NUNCA arquivos[0]. O Wan2GP grava um arquivo por
        # janela deslizante, cada um mais longo que o anterior — o último é o
        # vídeo completo. Até 2026-08-14 este código pegava o [0] e devolvia
        # só os 81 frames da primeira janela: um pedaço de 29,5 s virava um
        # arquivo de 3,24 s, medido na primeira sessão real.
        arquivo = Path(arquivos[-1])

        # O Wan2GP às vezes devolve caminho RELATIVO ("outputs/....mp4"),
        # relativo ao cwd do processo. O adapter.sh já roda a partir da raiz do
        # Wan2GP justamente por isso, mas resolver aqui também torna o cliente
        # imune a quem o inicia — e o custo é uma linha.
        if not arquivo.is_absolute():
            arquivo = (CONFIG.wan2gp_root / arquivo).resolve()

        if not arquivo.exists():
            return GeracaoResultado(
                False, segundos=segundos,
                erro=f"o Wan2GP relatou sucesso mas o arquivo não existe: {arquivo}")

        return GeracaoResultado(True, arquivo=arquivo, segundos=segundos)

    @staticmethod
    def _extrair_erro(resultado) -> str:
        """Mensagem de erro do Wan2GP.

        ⚠️ O atributo é `errors` (LISTA de GenerationError), não `error`. Eu
        lia `error`, que não existe, e todo job falhava com o inútil "sem
        arquivo gerado" — a mensagem real ("You must provide at least one
        Reference Image") só apareceu inspecionando `dir(resultado)` na mão,
        com a GPU ligada. Um erro de diagnóstico custa mais que o bug.
        """
        erros = getattr(resultado, "errors", None) or []
        partes = []
        for e in erros:
            msg = getattr(e, "message", None) or str(e)
            estagio = getattr(e, "stage", None)
            partes.append(f"[{estagio}] {msg}" if estagio else str(msg))
        if partes:
            return " · ".join(partes)
        # Fallbacks, caso a API mude de forma outra vez.
        return str(getattr(resultado, "error", None) or "sem arquivo gerado")

    @staticmethod
    def _bombear_eventos(job, on_progress) -> None:
        """Repassa os eventos de progresso. Falha aqui nunca derruba o job.

        Duas correções de 2026-08-17, ambas erros meus que deixavam a barra de
        progresso da plataforma parada — o vídeo ficava pronto com ~14% na tela:

        1. `ProgressUpdate.progress` é **percentual (0–100)**, não fração. Eu o
           tratava como fração; qualquer valor acima de 1 virava 100% depois do
           clamp.
        2. `events.iter(timeout=...)` **encerra** quando o timeout expira. Com
           `timeout=0.5`, a thread lia por meio segundo, não via nada (a carga
           do modelo leva minutos) e morria. Sem timeout, o iterador acompanha
           o job até o fim.

        Estrutura confirmada no fonte do Wan2GP (`shared/api.py`):
            SessionEvent(kind: str, data: Any, timestamp: float)
            ProgressUpdate(phase, status, progress: int, current_step,
                           total_steps, raw_phase, unit)
        """
        try:
            for evento in job.events.iter():
                if getattr(evento, "kind", None) != "progress":
                    continue
                dados = getattr(evento, "data", None)
                valor = getattr(dados, "progress", None)
                if valor is None:
                    continue
                fracao = max(0.0, min(1.0, float(valor) / 100.0))
                on_progress(fracao, getattr(dados, "phase", None))
        except Exception:  # noqa: BLE001
            log.debug("stream de eventos encerrado", exc_info=True)


CLIENTE = Wan2GPClient()
