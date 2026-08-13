"""Fatiamento, encadeamento e concatenação — a parte que a plataforma não vê.

⚠️ MEDIDO na Fase 0b: o Wan2GP gera no máximo **737 frames (~29,5 s)** por
chamada. Um vídeo de 2 min precisa de ~4 gerações encadeadas.

Este módulo esconde isso. A plataforma manda um áudio de qualquer duração e
recebe um vídeo só.

O corte entre pedaços tenta cair num **silêncio** em vez de no meio de uma
palavra — emendar no meio de um fonema deixa marca audível e visível no
lip-sync.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

from config import CONFIG

log = logging.getLogger("adapter.pipeline")


def _run(cmd: list[str]) -> None:
    r = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if r.returncode != 0:
        raise RuntimeError(f"{cmd[0]} falhou: {r.stderr.strip()[:400]}")


def duracao(caminho: Path) -> float:
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(caminho)],
        capture_output=True, text=True, check=False)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0


def detectar_silencios(audio: Path, ruido_db: int = -35,
                       minimo: float = 0.25) -> list[float]:
    """Instantes (em segundos) onde há silêncio, via `silencedetect` do ffmpeg.

    Usado para escolher pontos de corte que não partam palavras ao meio.
    """
    r = subprocess.run(
        ["ffmpeg", "-i", str(audio), "-af",
         f"silencedetect=noise={ruido_db}dB:d={minimo}", "-f", "null", "-"],
        capture_output=True, text=True, check=False)
    pontos: list[float] = []
    for linha in r.stderr.splitlines():
        if "silence_start:" in linha:
            try:
                pontos.append(float(linha.split("silence_start:")[1].strip()))
            except (IndexError, ValueError):
                continue
    return pontos


@dataclass
class Pedaco:
    indice: int
    inicio: float
    fim: float
    audio: Path

    @property
    def segundos(self) -> float:
        return self.fim - self.inicio

    @property
    def frames(self) -> int:
        return max(5, round(self.segundos * CONFIG.fps))


def fatiar_audio(audio: Path, destino: Path) -> list[Pedaco]:
    """Divide o áudio em pedaços de no máximo `max_chunk_seconds`.

    Prefere cortar em silêncio: procura o silêncio mais próximo do limite
    ideal, dentro de uma janela de tolerância. Sem silêncio conveniente,
    corta no limite mesmo.
    """
    destino.mkdir(parents=True, exist_ok=True)
    total = duracao(audio)
    limite = CONFIG.max_chunk_seconds()

    if total <= limite:
        único = destino / "chunk_000.wav"
        _run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(audio),
              "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(único)])
        return [Pedaco(0, 0.0, total, único)]

    silencios = detectar_silencios(audio)
    log.info("áudio de %.1fs, %d silêncios detectados", total, len(silencios))

    # Tolerância para "puxar" o corte até um silêncio: 15% do limite.
    tolerancia = limite * 0.15
    cortes: list[float] = [0.0]
    while cortes[-1] + limite < total:
        ideal = cortes[-1] + limite
        candidatos = [s for s in silencios
                      if ideal - tolerancia <= s <= ideal
                      and s > cortes[-1] + 1.0]
        cortes.append(max(candidatos) if candidatos else ideal)
    cortes.append(total)

    pedacos: list[Pedaco] = []
    for i in range(len(cortes) - 1):
        ini, fim = cortes[i], cortes[i + 1]
        arq = destino / f"chunk_{i:03d}.wav"
        _run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(audio),
              "-ss", f"{ini:.3f}", "-to", f"{fim:.3f}",
              "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(arq)])
        pedacos.append(Pedaco(i, ini, fim, arq))

    log.info("fatiado em %d pedaços (%.1fs cada, em média)",
             len(pedacos), total / len(pedacos))
    return pedacos


def concatenar(videos: list[Path], destino: Path) -> Path:
    """Junta os pedaços num vídeo só.

    Reencoda porque os pedaços podem ter parâmetros ligeiramente diferentes,
    e `-c copy` produziria um arquivo com timestamps quebrados.
    """
    if len(videos) == 1:
        _run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(videos[0]),
              "-c", "copy", str(destino)])
        return destino

    lista = destino.parent / f".{destino.stem}_concat.txt"
    lista.write_text("\n".join(f"file '{v.resolve()}'" for v in videos))
    _run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
          "-i", str(lista), "-c:v", "libx264", "-crf", "18", "-preset", "medium",
          "-c:a", "aac", "-b:a", "192k", str(destino)])
    lista.unlink(missing_ok=True)
    return destino


def recortar(video: Path, alvo: str, destino: Path) -> Path:
    """Recorta o quadro para uma proporção que o Wan2GP não gera nativamente.

    O 480p do Wan2GP só tem 4:3, 3:4, 1:1, 16:9 e 9:16 (ver
    `shared/resolutions.py` dele). Para 2:3, 3:2 e 4:5 geramos na proporção
    nativa mais próxima e cortamos aqui — sempre **para dentro** do quadro
    gerado, nunca ampliando, e centralizado para não decapitar o avatar.

    `alvo` vem de `CONFIG.recorte()` no formato "LARGURAxALTURA".
    """
    largura, altura = (int(v) for v in alvo.split("x"))
    _run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(video),
          "-vf", f"crop={largura}:{altura}:(iw-{largura})/2:(ih-{altura})/2",
          "-c:v", "libx264", "-crf", "18", "-preset", "medium",
          "-c:a", "copy", str(destino)])
    return destino


def anexar_audio(video: Path, audio: Path, destino: Path) -> Path:
    """Substitui a trilha do vídeo pelo áudio original completo.

    Os pedaços carregam suas fatias; usar o áudio íntegro no final evita
    qualquer descontinuidade acumulada nas emendas.
    """
    _run(["ffmpeg", "-y", "-loglevel", "error",
          "-i", str(video), "-i", str(audio),
          "-map", "0:v:0", "-map", "1:a:0",
          "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
          "-shortest", str(destino)])
    return destino
