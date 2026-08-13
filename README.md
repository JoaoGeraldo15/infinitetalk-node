# Nó de GPU — imagem e adapter

A imagem que roda no Vast.ai: Wan2GP + InfiniteTalk + uma API FastAPI que a
plataforma `11 - AI Creator Plataform` consome.

Contrato completo em [`../INTEGRATION.md`](../INTEGRATION.md) §2.
Decisões e medições em [`../PLAN.md`](../PLAN.md).

## O que roda aqui

```
  plataforma ──HTTPS+Bearer──► adapter FastAPI ──import──► Wan2GP ──► InfiniteTalk
                               (contrato estável)          (mesmo processo)
```

O adapter importa o Wan2GP **no mesmo processo**, então os modelos ficam
residentes na VRAM entre os vídeos. Medido na Fase 0b: primeira geração a 60x
tempo real (com carga), seguintes a **25,5x**. Um subprocesso por job pagaria
60x sempre.

| Arquivo | Papel |
|---|---|
| `adapter/main.py` | rotas do contrato |
| `adapter/jobs.py` | fila **serial** (uma GPU) e execução |
| `adapter/pipeline.py` | fatia o áudio, encadeia, concatena |
| `adapter/wan2gp_client.py` | **única** fronteira com o Wan2GP |
| `adapter/config.py` | tudo configurável por variável de ambiente |
| `bootstrap.sh` | **ponto de entrada** — o Vast baixa e executa no boot |
| `provision.sh` | baixa os pesos dos modelos |
| `supervisor/` | mantém o adapter de pé e joga o log no painel do Vast |
| `Dockerfile` | caminho alternativo, para congelar tudo numa imagem depois |

## Como o código chega na máquina

**Sem imagem Docker própria.** O template aponta para a imagem `vastai/wan2gp`
original e passa a URL do `bootstrap.sh`, que instala o adapter no boot.

```
Template ──► vastai/wan2gp:<tag>  +  PROVISIONING_SCRIPT=.../bootstrap.sh
                                          │
                                          ├─ baixa o tarball do repo
                                          ├─ acha o Python que tem o torch
                                          ├─ pip install (4 pacotes)
                                          └─ registra o adapter no supervisor
                                                  │
                                                  ├─ provision.sh (pesos, ~20 GB)
                                                  └─ uvicorn na 18000
```

Por que não uma imagem própria:

| | Imagem Docker | `bootstrap.sh` |
|---|---|---|
| Disco local para construir | ~25 GB de pico | nenhum |
| Upload | 7,5 GB, 1–4 h na banda residencial | nenhum |
| Corrigir um bug do adapter | build + push + relançar a máquina | editar o repo, `supervisorctl restart adapter` |
| Congelar a versão | tag da imagem | `ADAPTER_REF=<sha do commit>` |

O último item importa: fixar `ADAPTER_REF` num SHA dá a **mesma** garantia de
reprodutibilidade que a tag da imagem daria. Um push no repo não muda o que a
máquina de sábado executa.

O Dockerfile continua aqui. Quando o adapter estabilizar e o ciclo de iteração
não importar mais, dá para construir a imagem sem reescrever nada.

### O repositório

Crie um repo **público** no GitHub com **o conteúdo desta pasta `node/` na
raiz** — não a pasta `node` dentro dele:

```
seu-repo/
├── bootstrap.sh
├── provision.sh
├── adapter/
└── supervisor/
```

Público porque o `bootstrap.sh` baixa sem autenticação. **Não há segredo aqui**
— tokens vivem nas variáveis do template, nunca no código.

### O `settings_base.json`

✅ **Já está em `adapter/settings_base.json`**, exportado da UI na Fase 0b.

São os 67 campos que você validou. O adapter parte deles e sobrescreve só o que
varia por job — prompt, resolução, frames, e os caminhos de mídia. Assim os
dezenas de parâmetros que não nos interessam (`flow_shift`, `sample_solver`,
`cfg_star_switch`…) mantêm o valor que gerou o vídeo aprovado.

Se um dia trocar de modelo, reexporte pela UI e substitua o arquivo.

## Template do Vast.ai

Criado uma vez, reusado todo sábado.

| Campo | Valor |
|---|---|
| Image | `vastai/wan2gp:7e45fe7-2026-08-10-cuda-12.9` ⚠️ tag fixa, nunca `latest` |
| **Launch Mode** | **Entrypoint** ⚠️ nunca Jupyter — ele substitui o entrypoint e o adapter não sobe |
| Disk | **200 GB** (estático, não muda depois) |
| **Private template** | ✅ é aqui que os tokens moram |

**Environment variables** (é este campo, não o "Extra docker run args"):

```
ADAPTER_REPO       = SEU_USUARIO/SEU_REPO
ADAPTER_REF        = main
ADAPTER_TOKEN      = <sorteie: openssl rand -base64 32>
PROVISIONING_SCRIPT= https://raw.githubusercontent.com/SEU_USUARIO/SEU_REPO/main/bootstrap.sh
ENABLE_HTTPS       = true
HF_TOKEN           = hf_...
```

**Extra docker run args** — só o que não é variável:

```
-p 8000:8000
```

### `PORTAL_CONFIG`: editar, nunca acrescentar

⚠️ **O template já vem com um `PORTAL_CONFIG`**, definido pela imagem, listando
o Wan2GP, o Jupyter, o Terminal e o Syncthing. Declarar um segundo `-e
PORTAL_CONFIG=` **não soma — substitui**: com `-e` repetido, o último vence, e
você perde do portal justamente as ferramentas de depuração.

Edite a variável existente e acrescente **no fim**:

```
|localhost:8000:18000:/:InfiniteTalk API
```

Ela precisa vir do template e não do `bootstrap.sh`: o Caddy lê essa variável
quando sobe, antes do provisionamento rodar. É ele quem põe TLS e autenticação
na frente do adapter — sem isso a API fica em HTTP puro.

### `ADAPTER_TOKEN`: o segredo da API

⚠️ **Não é o `OPEN_BUTTON_TOKEN`.** Este README dizia que era; está errado. O
template do Vast define `OPEN_BUTTON_TOKEN=1` — é uma **flag booleana**, não um
segredo. Usá-la deixaria a API do nó protegida pelo token `1`, num IP público.

Sem `ADAPTER_TOKEN`, o adapter sorteia um token por sessão e o imprime no log.
Funciona, mas obriga a ler o log a cada máquina — defina no template.

### Opcionais

```
BACKEND_URL   = https://sua-plataforma.com    # auto-registro (Fase 3)
BACKEND_TOKEN = ...                           # escopo: só /gpu-nodes/register
HF_REVISION   = <commit>                      # congela os pesos
WAN2GP_PYTHON = /venv/main/bin/python         # só se a detecção falhar
```

⚠️ `ADAPTER_REPO` é obrigatório; sem ele o `bootstrap.sh` aborta na primeira
linha, de propósito, em vez de subir uma máquina inútil.

**Filtros de host** — todos aprendidos apanhando na Fase 0b:

| Critério | Por quê |
|---|---|
| RTX 4090 | é onde os 25,5x foram medidos; outra placa dá número que não transfere |
| RAM ≥ 32 GB | o offload de parâmetros do modelo de 14B mora nela |
| PCIe 4.0 x16 | com offload, os pesos atravessam a cada step |
| `disk_bw` ≥ 2000 MB/s | perdemos uma máquina cujo container nunca subiu num disco de 561 MB/s |
| confiabilidade > 98% | |
| banda ≤ $4/TB | cobrada à parte; alguns hosts cobram $26/TB |

## Trocar de modelo, LoRA ou steps

Edite as variáveis do template:

```
-e MODEL_TYPE=infinitetalk
-e LORA_URL=https://huggingface.co/.../arquivo.safetensors
-e DEFAULT_STEPS=10
-e HF_LORA=...  -e LORA_FILE=...
```

⚠️ É `LORA_URL`, não `LORA_PRESET` — a LoRA entra em `activated_loras` como
**URL**, não como nome de preset. (Este README dizia `LORA_PRESET`, que não
existe em `config.py`.)

Regra do projeto: **configuração vai para variável de ambiente; código vai para
o repo.** Mudou o código do adapter? Faça o push e, na máquina:

```bash
source /etc/environment && bash /opt/node/bootstrap.sh
```

Rodar o `bootstrap.sh` de novo é o jeito certo: ele rebaixa o tarball e, se
percebe que o adapter já estava de pé, reinicia com o código novo.
`supervisorctl restart adapter` sozinho **não** serve — ele reexecuta o código
antigo, porque o download mora no bootstrap.

Não precisa relançar a máquina: os pesos já estão em disco e o marcador
`/workspace/.provisionado` evita rebaixá-los. Esse ciclo de ~20 s é a razão
principal de não estarmos usando imagem Docker na Fase 1.

⚠️ **Um modelo por sessão, não por job.** Trocar modelo significa descarregar
20 GB da VRAM e carregar outros 20 — numa sessão de 12 vídeos alternando, você
gastaria mais tempo trocando que gerando.

## O sábado

| | Ação |
|---|---|
| 1 | Alugar a partir do template |
| 2 | *(a máquina baixa os modelos e se registra sozinha, ~15 min)* |
| 3 | No dashboard: "iniciar sessão" |
| 4 | *(a fila drena, ~10 h para 12 vídeos de 2 min)* |
| 5 | Destruir |

Sem `BACKEND_URL`, o passo 2 vira manual: o log imprime a URL do nó e você a
cola no dashboard. Um minuto.

## Estado deste código

⚠️ **O adapter ainda não rodou numa GPU.** Foi escrito sem máquina disponível.

### Verificado

| O quê | Como |
|---|---|
| Fatiamento com corte em silêncio | testado num áudio real de 71,7 s → 3 pedaços, **ambos os cortes caindo em silêncio**, nenhum acima do teto de 737 frames |
| Rotas e autenticação | 401 sem token · 401 com token errado · 503 antes de `ready` · 404 job ausente · 422 payload inválido |
| Configuração por ambiente | `MODEL_TYPE=x DEFAULT_STEPS=10` sobrescreve sem tocar no código |
| `model_type = "infinitetalk"` | do `settings_base.json` real — meu palpite (`infinitetalk_14B_single_480p`) estava **errado** |
| LoRA via `activated_loras` (URL) | idem — não é nome de preset |
| Nomes dos campos de mídia | lidos do `ATTACHMENT_KEYS` no `wgp.py` do Wan2GP |
| Códigos de `image_prompt_type` | `"S"` = start image · `"L"` = continua de `video_source` |
| Resoluções de 480p | lidas de `shared/resolutions.py`: **só** 832x624, 624x832, 720x720, 832x480, 480x832 — meus palpites `640x640` (1:1) e `576x720` (4:5) estavam **errados** |

As 3 proporções que faltavam (`4:5`, `2:3`, `3:2`) são geradas na nativa mais
próxima e recortadas com ffmpeg em `pipeline.recortar()`.

### Ainda não verificado

Sobraram **dois**, e só o primeiro precisa de GPU:

- O **encadeamento** entre pedaços: se `image_prompt_type="L"` + `video_source`
  produz emenda invisível. O `settings_base.json` mostra
  `sliding_window_overlap: 9`, então o mecanismo existe — falta ver o resultado.
  **Exige gerar de verdade.** Um teste de 2 pedaços de ~10 s custa ~$0,05.
- Se o Wan2GP aceita **mais de 737 frames** pela API. O teto pode ser só do
  slider do Gradio; se for, o encadeamento some e o adapter simplifica muito.
  Sai de graça no mesmo teste acima: manda 900 frames e vê se recusa.

Ambos vivem em `wan2gp_client.py` — o único arquivo a ajustar depois do
primeiro job real.
