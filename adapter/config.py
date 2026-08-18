"""Configuração do nó, toda por variável de ambiente.

Regra de ouro deste projeto: **o que é configuração vai para variável de
ambiente; o que é código vai para a imagem.** Trocar de modelo, de LoRA ou de
número de steps é editar o template do Vast.ai e relançar — sem rebuild.

Os padrões abaixo são os valores VALIDADOS na Fase 0b (2026-08-13, RTX 4090,
Wan2GP v12.452): 25,5x tempo real, ~$0,30 por vídeo de 2 min.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(nome: str, padrao: str) -> str:
    return os.environ.get(nome, padrao)


def _int(nome: str, padrao: int) -> int:
    try:
        return int(os.environ.get(nome, padrao))
    except ValueError:
        return padrao


def _sortear_token() -> str:
    """Token de emergência quando ADAPTER_TOKEN não foi definido no template.

    Impresso no log do adapter no start — ver `main.py`. Nunca reutilizado
    entre sessões, de propósito: um nó destruído não deixa credencial viva.
    """
    import secrets
    return secrets.token_urlsafe(32)


@dataclass(frozen=True)
class Config:
    # ── Wan2GP ────────────────────────────────────────────────────────
    wan2gp_root: Path = Path(_env("WAN2GP_ROOT", "/workspace/Wan2GP"))

    # Selecionados na UI na Fase 0b. Os nomes exatos vêm do settings.json
    # exportado pelo botão "Export Settings to File".
    # CONFIRMADO no settings_base.json exportado da UI (WanGP v12.452):
    # o valor é "infinitetalk", não o rótulo longo que aparece na interface.
    model_type: str = _env("MODEL_TYPE", "infinitetalk")

    # A LoRA é indicada por URL em `activated_loras`, não por nome de preset.
    lora_url: str = _env(
        "LORA_URL",
        "https://huggingface.co/DeepBeepMeep/Wan2.1/resolve/main/loras_accelerators/"
        "Wan21_I2V_14B_lightx2v_cfg_step_distill_lora_rank64.safetensors",
    )
    steps: int = _int("DEFAULT_STEPS", 4)

    # Comportamento do avatar. O lip-sync vem do ÁUDIO; o prompt controla o
    # resto — enquadramento, expressão, intensidade de gesto, cenário.
    # A plataforma sobrescreve por job em settings.prompt.
    default_prompt: str = _env(
        "DEFAULT_PROMPT",
        "A person speaking directly to the camera, natural expression, "
        "subtle head movement, static background",
    )

    # Revisão fixada no Hugging Face: sem isto, um update upstream quebra o
    # boot num sábado. Vazio = "main" (não recomendado em produção).
    hf_revision: str = _env("HF_REVISION", "")

    # ── Limites medidos ───────────────────────────────────────────────
    # Frames por geração. 6000 = 4 minutos a 25 fps.
    #
    # ⚠️ Aqui havia 737, que eu li do SLIDER DO GRADIO e confundi com limite da
    # API. Não é: a janela deslizante do InfiniteTalk é justamente o mecanismo
    # de vídeo longo — 81 frames por vez com 9 de sobreposição, VRAM constante.
    # Testado em 2026-08-17: 1198 frames numa geração só, 17 janelas, lip-sync
    # do início ao fim.
    #
    # O 737 custou caro: forçava fatiar e encadear, e o modo de continuação do
    # Wan2GP gera movimento ocioso SEM lip-sync no trecho novo. O vídeo saía
    # com metade da narração muda, parecendo correto.
    #
    # Por que 6000 e não ilimitado — três custos que crescem com a duração:
    #   · disco: o Wan2GP grava um arquivo por janela, cada um com o vídeo
    #     inteiro até ali. São ~2,7 MB por janela acumulados ao quadrado —
    #     9 janelas ≈ 110 MB, mas 125 janelas ≈ 21 GB;
    #   · deriva: o rosto pode mudar devagar ao longo de muitas janelas;
    #   · custo da falha: uma geração longa que morre no fim perde tudo.
    max_frames: int = _int("MAX_FRAMES", 6000)
    fps: int = _int("FPS", 25)

    # Sobreposição entre pedaços consecutivos. O InfiniteTalk usa janela
    # deslizante de 81 frames; encadear com alguma sobreposição dá ao modelo
    # contexto para a emenda ficar suave.
    chunk_overlap_frames: int = _int("CHUNK_OVERLAP_FRAMES", 0)

    # ── Diretórios ────────────────────────────────────────────────────
    work_dir: Path = Path(_env("WORK_DIR", "/workspace/adapter-work"))
    jobs_dir: Path = Path(_env("JOBS_DIR", "/workspace/adapter-jobs"))

    # Única pasta de onde o job aceita ler arquivo local em vez de URL. Serve
    # para testar com arquivos enviados pelo Jupyter antes de a plataforma
    # existir. É uma cerca, não uma conveniência: sem ela, quem tivesse o token
    # da API leria qualquer arquivo do container.
    local_input_dir: Path = Path(_env("LOCAL_INPUT_DIR", "/workspace"))

    # ── API ───────────────────────────────────────────────────────────
    port: int = _int("ADAPTER_PORT", 18000)

    # Credencial que a plataforma manda no header Authorization.
    #
    # ⚠️ NÃO use OPEN_BUTTON_TOKEN — mas não pelo motivo que eu escrevi aqui
    # antes. Eu dizia que o `=1` do template era "flag booleana, não segredo";
    # é meio verdade: o `=1` PEDE, e o Vast substitui por um token real de 64
    # hex no lançamento (visto em 2026-08-17 no Caddyfile da máquina).
    #
    # O motivo real de manter separado: esse token é a senha do PORTAL — o
    # mesmo que abre o Jupyter e o terminal web da máquina. Usá-lo na API do
    # adapter daria à plataforma uma credencial de acesso total ao host.
    #
    # E eles convivem: o Caddy aceita o dele na query (`?token=`), o adapter
    # aceita o seu no header Authorization. Ver gpu_node_client.py na plataforma.
    #
    # Sem ADAPTER_TOKEN no template, sorteamos um por sessão: um nó com token
    # aleatório é inútil até você ler o log, mas um nó com token adivinhável é
    # pior — qualquer varredura de porta entra.
    auth_token: str = field(default_factory=lambda: _env("ADAPTER_TOKEN", "") or _sortear_token())

    # ── Auto-registro na plataforma ───────────────────────────────────
    # Opcional: sem BACKEND_URL o nó não se registra, e você cola o endereço
    # à mão no dashboard. Útil se a plataforma não estiver exposta na internet.
    backend_url: str = _env("BACKEND_URL", "")
    backend_token: str = _env("BACKEND_TOKEN", "")

    # Injetados pelo Vast.ai no container — é assim que o nó descobre o
    # próprio endereço público para informar à plataforma.
    public_ip: str = _env("PUBLIC_IPADDR", "")
    public_port: str = _env("VAST_TCP_PORT_8000", "")
    container_id: str = _env("CONTAINER_ID", "")

    # ── Proporções suportadas ─────────────────────────────────────────
    # CONFIRMADO em shared/resolutions.py do Wan2GP: a categoria 480p oferece
    # EXATAMENTE cinco resoluções, e o valor gravado no settings é só o "WxH"
    # (o "(9:16)" que aparece na UI é rótulo, não faz parte do valor).
    #
    #     832x624 (4:3) · 624x832 (3:4) · 720x720 (1:1)
    #     832x480 (16:9) · 480x832 (9:16)
    #
    # ⚠️ Dois palpites meus estavam errados: 1:1 é 720x720 (não 640x640) e
    # 4:5 NÃO existe em 480p — eu tinha inventado "576x720".
    #
    # As proporções que o Wan2GP não tem em 480p (2:3, 3:2, 4:5) são geradas na
    # mais próxima e RECORTADAS com ffmpeg depois. O recorte é sempre um
    # subconjunto do quadro gerado (nunca ampliação), custa ~1 s de CPU e não
    # toca na GPU. Todas as dimensões são pares, exigência do H.264.
    #
    # ratio → (resolução gerada no Wan2GP, recorte final ou None)
    resolucoes: dict = field(default_factory=lambda: {
        # ⚠️ Os rótulos "16:9" e "9:16" do Wan2GP NÃO são exatos: 832/480 = 1,733
        # contra 1,778 do 16:9 real, e 480/832 = 0,578 contra 0,5625 do 9:16.
        # Desvio de ~2,5%. Na prática o YouTube põe barras verticais finas e o
        # Instagram corta as laterais por conta própria. Recortamos 12 px (6 de
        # cada lado) para entregar a proporção exata: depois do upscale para
        # 1080 de altura sai 1920x1080 e 608x1080, redondos.
        "16:9": ("832x480", "832x468"),  # corta altura
        "9:16": ("480x832", "468x832"),  # corta largura
        # exata na origem
        "1:1": ("720x720", None),
        "3:4": ("624x832", None),
        "4:3": ("832x624", None),
        # proporções que o Wan2GP não tem em 480p: geradas na nativa mais
        # próxima e recortadas
        "4:5": ("624x832", "624x780"),   # gera 3:4, corta altura
        "2:3": ("624x832", "554x832"),   # gera 3:4, corta largura
        "3:2": ("832x624", "832x554"),   # gera 4:3, corta altura
    })

    @property
    def public_url(self) -> str:
        if not (self.public_ip and self.public_port):
            return ""
        return f"https://{self.public_ip}:{self.public_port}"

    def resolucao(self, aspect_ratio: str) -> str:
        """Resolução a pedir ao Wan2GP; cai no 9:16 se não conhecer."""
        return self.resolucoes.get(aspect_ratio, self.resolucoes["9:16"])[0]

    def recorte(self, aspect_ratio: str) -> str | None:
        """Recorte de ffmpeg depois da geração, ou None se a proporção é nativa."""
        return self.resolucoes.get(aspect_ratio, self.resolucoes["9:16"])[1]

    def max_chunk_seconds(self) -> float:
        return self.max_frames / self.fps


CONFIG = Config()
