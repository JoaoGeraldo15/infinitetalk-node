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
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from config import CONFIG

log = logging.getLogger("adapter.pipeline")

# Quanto o recorte pode descartar antes de ser destrutivo. Os limites são
# ASSIMÉTRICOS de propósito: o risco não é o mesmo nas duas direções.
#
#   · cortar ALTURA tira a cabeça do avatar — é o que produziu, em
#     2026-08-19, um vídeo mostrando do peito para baixo;
#   · cortar LARGURA tira as laterais do cenário, e o rosto continua lá.
#
# Um limite único reprovava o caso legítimo de foto 2:3 para vídeo 9:16, que
# corta 16% da largura e não machuca nada.
PERDA_MAXIMA_ALTURA = 0.20
PERDA_MAXIMA_LARGURA = 0.40


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


def cortar_inicio(video: Path, segundos: float, destino: Path) -> Path:
    """Remove os primeiros `segundos` do vídeo.

    Serve ao encadeamento. Quando geramos um pedaço com `video_source`, o
    Wan2GP devolve **a origem seguida do trecho novo** — medido na primeira
    sessão real: fonte de 3,24 s + áudio de 29,1 s = saída de 32,36 s.
    Concatenar essas saídas direto duplicaria cada pedaço.

    Cortando aqui, o resultado carrega só o trecho novo, e ele vira a origem do
    pedaço seguinte — o que também mantém a origem com tamanho constante em vez
    de crescer a cada pedaço até estourar.
    """
    _run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{segundos:.3f}",
          "-i", str(video), "-c:v", "libx264", "-crf", "18", "-preset", "medium",
          "-c:a", "aac", "-b:a", "192k", str(destino)])
    return destino


def dimensoes(video: Path) -> tuple[int, int]:
    """Largura e altura reais do vídeo, pelo ffprobe."""
    saida = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "csv=s=x:p=0", str(video)],
        capture_output=True, text=True, check=False).stdout.strip()
    m = re.match(r"(\d+)x(\d+)", saida)
    if not m:
        raise RuntimeError(f"não consegui ler as dimensões de {video}: {saida!r}")
    return int(m.group(1)), int(m.group(2))


# Onde ancorar o recorte vertical da foto do avatar. 0 = topo, 0,5 = centro.
#
# Rosto fica na parte de cima do enquadramento, então centralizar é o pior
# lugar possível: numa foto 2:3 virando 16:9, o corte centralizado pega do
# peito para baixo. 0,15 tira um pouco de ar acima da cabeça e todo o excesso
# de baixo — que é o que um editor humano faria.
ANCORA_VERTICAL = 0.15


def enquadrar_imagem(imagem: Path, proporcao: str, destino: Path) -> Path | None:
    """Recorta a foto do avatar para a proporção do vídeo pedido.

    ⚠️ Isto existe porque o Wan2GP **ignora a resolução que pedimos** e segue a
    proporção da imagem de referência. Confirmado no fonte dele
    (`shared/utils/utils.py`, `calculate_new_dimensions`): com o padrão
    `fit_canvas = 0`, ele preserva a proporção da imagem e só ajusta a escala
    para bater com o orçamento de pixels.

    Medido em 2026-08-19: foto 1024x1536 e pedido de 832x480 produziram
    512x768 — retrato. Recortar o vídeo depois seria pior: tirar uma faixa
    16:9 do meio de um retrato mostra do peito para baixo.

    Recortando a FOTO antes, o Wan2GP já gera na proporção certa e ninguém
    precisa mutilar o vídeo depois.

    Devolve None quando a foto já está na proporção pedida.
    """
    try:
        pw, ph = (int(v) for v in proporcao.split(":"))
    except ValueError:
        log.warning("proporção ilegível: %r — foto sem recorte", proporcao)
        return None
    if pw <= 0 or ph <= 0:
        return None

    largura, altura = dimensoes(imagem)

    if largura * ph > altura * pw:      # foto larga demais → corta os lados
        nova_l, nova_a = round(altura * pw / ph), altura
    else:                                # alta demais → corta topo e base
        nova_l, nova_a = largura, round(largura * ph / pw)

    nova_l, nova_a = min(nova_l, largura), min(nova_a, altura)
    if abs(nova_l - largura) <= 2 and abs(nova_a - altura) <= 2:
        return None

    # Horizontal centralizado (rostos ficam no meio); vertical ancorado no
    # topo (rostos ficam em cima).
    x = (largura - nova_l) // 2
    y = round((altura - nova_a) * ANCORA_VERTICAL)

    log.info("enquadrando a foto %dx%d → %dx%d em %s (corte y=%d)",
             largura, altura, nova_l, nova_a, proporcao, y)
    _run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(imagem),
          "-vf", f"crop={nova_l}:{nova_a}:{x}:{y}", str(destino)])
    return destino


def recortar(video: Path, proporcao: str, destino: Path) -> Path | None:
    """Recorta para a proporção exata pedida, centralizado.

    ⚠️ As dimensões saem do vídeo REAL, medido com ffprobe — não de uma tabela.
    A versão anterior usava um "LARGURAxALTURA" fixo da configuração,
    assumindo que o Wan2GP gerou exatamente a resolução pedida. Ele nem sempre
    gera: em 2026-08-19 pedimos 16:9 (832x480), veio retrato, e o ffmpeg
    abortou com "Invalid too big or non positive size for width '832'",
    perdendo a geração inteira no último passo.

    Recorta sempre **para dentro** do quadro, nunca amplia, e devolve None
    quando o vídeo já está na proporção certa (nada a fazer).
    """
    try:
        pw, ph = (int(v) for v in proporcao.split(":"))
    except ValueError:
        log.warning("proporção ilegível: %r — sem recorte", proporcao)
        return None
    if pw <= 0 or ph <= 0:
        return None

    largura, altura = dimensoes(video)

    # O maior retângulo com a proporção pedida que cabe no quadro atual.
    if largura * ph > altura * pw:      # quadro largo demais → corta os lados
        nova_l, nova_a = altura * pw // ph, altura
    else:                                # alto demais → corta topo e base
        nova_l, nova_a = largura, largura * ph // pw

    # H.264 exige dimensões pares.
    nova_l -= nova_l % 2
    nova_a -= nova_a % 2

    # Diferença de 1–2 px é arredondamento, não vale um reencode.
    if abs(nova_l - largura) <= 2 and abs(nova_a - altura) <= 2:
        return None

    # ⚠️ LIMITE DE SEGURANÇA. Recortar 2% para acertar a proporção é ajuste;
    # recortar 60% é destruir o enquadramento.
    #
    # Medido em 2026-08-19: pedimos 16:9, o Wan2GP gerou 512x768 (retrato,
    # seguindo a proporção da FOTO DE REFERÊNCIA em vez da resolução pedida),
    # e o recorte centralizado virou 512x288 — uma faixa do meio do corpo, com
    # a cabeça do avatar cortada fora. O vídeo saiu inutilizável depois de
    # 33 minutos de GPU.
    #
    # Entregar com a proporção um pouco errada é ruim; entregar decapitado é
    # pior. Acima do limite, devolvemos o vídeo como veio e registramos por quê
    # — a correção de verdade é usar uma foto de avatar na proporção do vídeo.
    perda_l = 1 - nova_l / largura
    perda_a = 1 - nova_a / altura
    if perda_a > PERDA_MAXIMA_ALTURA or perda_l > PERDA_MAXIMA_LARGURA:
        log.warning(
            "recorte para %s descartaria %.0f%% da altura e %.0f%% da largura "
            "(%dx%d → %dx%d): ENTREGANDO SEM RECORTAR. O vídeo saiu em outra "
            "proporção porque o Wan2GP segue a proporção da IMAGEM DE "
            "REFERÊNCIA, não a resolução pedida — use uma foto de avatar no "
            "mesmo formato do vídeo.",
            proporcao, perda_a * 100, perda_l * 100, largura, altura, nova_l, nova_a)
        return None

    log.info("recortando %dx%d → %dx%d (%s)", largura, altura, nova_l, nova_a, proporcao)
    _run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(video),
          "-vf", f"crop={nova_l}:{nova_a}:(iw-{nova_l})/2:(ih-{nova_a})/2",
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
