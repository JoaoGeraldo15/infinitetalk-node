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
| `provision.sh` | confere disco e autentica no Hugging Face |
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
                                                  └─ uvicorn na 18000
                                                       ├─ provision.sh
                                                       │    ├─ disco + login no HF
                                                       │    └─ pré-baixa ~19 GB de pesos
                                                       └─ Wan2GP carrega o modelo
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
| Disk | **100 GB** (estático, não muda depois) |
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

⚠️ **Não é o `OPEN_BUTTON_TOKEN`** — mas não pelo motivo que este README deu
antes. Eu escrevi que o `=1` era "flag booleana, não segredo"; é meio verdade:
o `=1` **pede**, e o Vast substitui por um token real de 64 hex no lançamento
(visto em 2026-08-17 no Caddyfile da máquina).

O motivo real de manter separado: esse token é a senha do **portal** — o mesmo
que abre o Jupyter e o terminal web. Usá-lo na API do adapter daria à
plataforma uma credencial de acesso total ao host.

Os dois convivem: o Caddy aceita o dele na query (`?token=`), o adapter aceita
o seu no header `Authorization`.

Sem `ADAPTER_TOKEN`, o adapter sorteia um token por sessão e o imprime no log.
Funciona, mas obriga a ler o log a cada máquina — defina no template.

### Auto-registro

Com estas duas, a máquina se anuncia sozinha à plataforma no boot e **você não
cola URL nem token em lugar nenhum**:

```
BACKEND_URL   = https://app.papodecontribuinte.com.br
BACKEND_TOKEN = <mesmo valor do GPU_NODE_REGISTER_TOKEN da plataforma>
```

O nó envia URL pública, o token do adapter e o **token do proxy**
(`OPEN_BUTTON_TOKEN`, gerado pelo Vast por instância — sem ele a plataforma
receberia um endereço que não consegue usar).

O registro acontece **assim que a API sobe**, poucos segundos após o boot, e
não depois de tudo pronto — é o que permite à plataforma mostrar "preparando" e
"baixando os modelos: 8,2 GB" durante os ~20 min de espera.

Se a plataforma não responder, tenta 6 vezes com espera crescente. Depois
reanuncia a cada 20 s enquanto provisiona e a cada 5 min quando pronto — o
registro é idempotente, então reanunciar **é** o sinal de vida.

Sem `BACKEND_URL` nada disso acontece: o log imprime o endereço do nó e você o
cadastra à mão.

### Outros opcionais

```
HF_REVISION      = <commit>                # congela os pesos
WAN2GP_PYTHON    = /venv/main/bin/python   # só se a detecção falhar
HEARTBEAT_SECONDS= 300                     # intervalo do reanúncio
MAX_FRAMES       = 6000                    # 4 min por geração
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

### Primeira sessão real — 2026-08-14, RTX 4090 (Oregon)

Funcionou de ponta a ponta: `bootstrap.sh` instalou o adapter, a detecção do
Python acertou no primeiro candidato (`/venv/main/bin/python`, torch
2.7.1+cu128), a API respondeu `ready`, um áudio de 101,88 s foi fatiado em
**4 pedaços** e o pedaço 0 gerou vídeo.

O **pedaço 1 falhou**, e três defeitos meus vieram à tona:

| Defeito | Correção |
|---|---|
| `image_refs` só era definido no ramo do primeiro pedaço → **todo** pedaço encadeado morria com `You must provide at least one Reference Image` | passou a valer nos dois ramos |
| `gerar()` lia `resultado.error`, que não existe — o atributo é **`errors`**, uma lista de `GenerationError`. Todo erro virava "sem arquivo gerado" | `_extrair_erro()` |
| `provision.sh` tinha `\|\| true` nos downloads: reportou sucesso com **0 byte** baixado, e o primeiro job pagou os 33 GB | falha alto, com diagnóstico |
| `stdout_logfile=/dev/stdout` → `supervisorctl tail adapter` responde "unknown error reading log" | `/var/log/portal/adapter.log` |
| `gerar()` devolvia `generated_files[0]`. O Wan2GP grava **um arquivo por janela deslizante**, e o último é o completo — o pedaço de 29,5 s virava um `.mp4` de **3,24 s** (81 frames, a 1ª janela) | `generated_files[-1]` |
| A saída encadeada **inclui o vídeo de origem** colado na frente (fonte 3,24 s + áudio 29,1 s = saída 32,36 s). Concatenar direto duplicaria cada pedaço | `pipeline.cortar_inicio()` remove o prefixo; o resultado cortado vira a origem do pedaço seguinte, o que também impede a origem de crescer a cada pedaço |

Confirmado na mesma sessão: com `image_refs` presente, o encadeamento retorna
`success: True`. E o InfiniteTalk deriva a duração do **áudio**, não do
`video_length` que mandamos.

Medição: pedaço 0 levou 970 s **incluindo** os 33 GB de download; o pedaço 1
rodou ~900 s para ~29,5 s de vídeo, ou seja **~30x tempo real** (Fase 0b:
25,5x num host diferente).

### Segunda sessão — 2026-08-17, RTX 5090, **pela plataforma**

Primeira geração disparada pela esteira em vez de à mão. Mais três defeitos:

| Defeito | Correção |
|---|---|
| `wgp.py` faz `os.mkdir("settings")` sem `exist_ok`, e importar `shared.api` importa o wgp. Funcionou na 1ª máquina por sorte (o diretório não existia); no primeiro restart da 2ª, o adapter nunca mais ficou pronto | `_mkdir_tolerante()` durante o import |
| O Caddy da imagem do Vast aceita `Authorization: Bearer <OPEN_BUTTON_TOKEN>` — **o mesmo header** que o adapter usa. As duas credenciais brigavam e o proxy devolvia 401 sem o pedido chegar ao adapter | plataforma manda o token do proxy na **query** (`?token=`), que o Caddyfile também aceita, e o do adapter no header |
| O Wan2GP grava as saídas em `outputs/` **relativo ao cwd**, e procura pelo mesmo caminho. Rodando de `/opt/node/adapter`, o job morria com `ffprobe skipped; file not found` — depois de 12 min de GPU paga | `adapter.sh` roda a partir de `$WAN2GP_ROOT` com `PYTHONPATH` para o adapter; `gerar()` também resolve caminho relativo |

⚠️ `init(root=...)` diz onde estão os **modelos**, não onde ele escreve. Assumi o
contrário duas vezes.

Ritmo medido na 5090: **~25x tempo real** — praticamente igual à 4090, apesar
do dobro de `dlperf`. O gargalo é o offload dos pesos do modelo de 14B, não o
poder bruto da GPU. Placa mais cara não compra velocidade aqui; o que importa é
VRAM suficiente e PCIe rápido.

### Ainda não verificado

- Se a emenda entre pedaços é **invisível**. A correção do `image_refs` foi
  validada isoladamente, mas nenhum vídeo com 2+ pedaços concatenados foi
  assistido ainda.
- Se o Wan2GP aceita **mais de 737 frames** pela API. O teto pode ser só do
  slider do Gradio; se for, o encadeamento some e o adapter simplifica muito.
- O progresso **dentro** de um pedaço: `_bombear_eventos` não recebeu nenhum
  evento — a barra pula de 25 em 25%. Os nomes `evento.kind` / `data.progress`
  provavelmente estão errados, do mesmo jeito que `error`/`errors` estava.
