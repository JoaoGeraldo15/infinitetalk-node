# Comandos do dia a dia

Referência rápida. **Onde rodar cada coisa:**

| Símbolo | Onde |
|---|---|
| 🖥️ | terminal do Jupyter da **máquina alugada** |
| 💻 | seu **notebook** |
| ☁️ | **VPS** da Oracle, por SSH |

---

## Estado do nó

🖥️ **Está pronto para gerar?**
```bash
curl -s localhost:18000/healthz | python3 -m json.tool
```
`status: ready` e `models_loaded: true` = pode gerar.
`provisioning` = ainda baixando modelos ou carregando na VRAM.

💻 **O mesmo, pelo caminho que a plataforma usa:**
```bash
./status-gpu.sh        # uma leitura
./status-gpu.sh -w     # acompanha, atualiza a cada 20 s
```

🖥️ **O log, que é onde tudo aparece:**
```bash
tail -f /var/log/portal/adapter.log
```

---

## Acompanhar uma geração

🖥️ **A GPU está trabalhando?**
```bash
watch -n 5 nvidia-smi
```
~100% de uso e ~20 GB de VRAM = gerando. 0% com job na fila = travou antes.

🖥️ **Quantas janelas já saíram** (o Wan2GP grava uma por janela deslizante):
```bash
watch -n 30 'ls -1 /workspace/Wan2GP/outputs/*.mp4 | wc -l; ls -lht /workspace/Wan2GP/outputs/ | head -3'
```

🖥️ **Acabou?** O `master.mp4` só existe quando o job concluiu:
```bash
watch -n 15 'ls -lh /workspace/adapter-work/*/master.mp4 2>/dev/null || echo "ainda gerando..."'
```

🖥️ **Linhas que fecham um job:**
```bash
grep -a "concluído\|FALHOU\|Traceback\|enquadrando\|recortando" /var/log/portal/adapter.log | tail -10
```

---

## Download dos modelos

🖥️ **Quanto já veio** (~32 GB no total, medido em 2026-08-19):
```bash
watch -n 20 'du -sh /workspace/Wan2GP/ckpts; df -h /workspace | tail -1'
```

🖥️ **O total exato, em bytes** — só depois de uma geração completa, porque o
Wan2GP baixa ~5,7 GB a mais durante a primeira:
```bash
du -sb /workspace/Wan2GP/ckpts | cut -f1
```

🖥️ **Os dois pesos grandes chegaram?** O `provision.sh` os pré-baixa; se
faltarem, o Wan2GP busca sozinho na primeira geração (e ela leva ~5 min a mais):
```bash
ls -lh /workspace/Wan2GP/ckpts/*.safetensors
ls -lh /workspace/Wan2GP/loras/wan_i2v/
```
Esperado: `wan2.1_image2video_480p_14B_...` (17 GB) e
`wan2.1_infinitetalk_single_14B_...` (2,4 GB).

---

## Quando algo dá errado

🖥️ **O registro na plataforma falhou?**
```bash
grep -a "registr" /var/log/portal/adapter.log | tail -10
env | grep -E "BACKEND_URL|PUBLIC_IPADDR|VAST_TCP_PORT_8000"
printf %s "$BACKEND_TOKEN" | sha256sum | cut -c1-12   # compare com a VPS
```

☁️ **O mesmo hash, do outro lado:**
```bash
grep '^GPU_NODE_REGISTER_TOKEN=' ~/app/AI-Creator-Plataform/.env \
  | cut -d= -f2- | tr -d '"\n' | sha256sum | cut -c1-12
```
Diferentes = o 401 de "segredo de registro inválido".

🖥️ **O adapter está de pé?**
```bash
supervisorctl status
supervisorctl restart adapter    # recarrega o modelo, ~1 min
```

🖥️ **Qual Python e qual diretório ele usa** (as duas coisas que já quebraram):
```bash
cat /opt/node/.python
ls -l /proc/$(supervisorctl pid adapter)/cwd    # tem que ser /workspace/Wan2GP
```

---

## Manutenção

🖥️ **Aplicar código novo do repositório** — sem relançar a máquina:
```bash
source /etc/environment && bash /opt/node/bootstrap.sh
```
Ele rebaixa o código, detecta que o adapter estava rodando e reinicia.

🖥️ **Liberar disco de jobs já entregues:**
```bash
du -sh /workspace/adapter-work/
rm -rf /workspace/adapter-work/<id>
```

🖥️ **Descobrir a URL pública do nó** (para cadastro manual, se o registro falhar):
```bash
echo "https://$PUBLIC_IPADDR:$VAST_TCP_PORT_8000"
echo "portal: $OPEN_BUTTON_TOKEN"
```

---

## Na VPS

☁️ **Aplicar código novo:**
```bash
cd ~/app/AI-Creator-Plataform && git pull
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build api worker frontend
docker compose -f docker-compose.yml -f docker-compose.prod.yml exec api alembic upgrade head
```

⚠️ **`--build` sempre.** `restart` não pega código novo **nem** `.env` alterado —
o código vai dentro da imagem e as variáveis são fixadas na criação do
container. Para só o `.env`: `up -d --force-recreate`.

☁️ **Conferir que uma variável chegou:**
```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml exec worker env | grep GPU_NODE
```

☁️ **Logs:**
```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml logs -f worker
```

---

## Números de referência

| | |
|---|---|
| Velocidade de geração | **~25x tempo real** (RTX 4090 e 5090, praticamente iguais) |
| Áudio de 1 min | ~25 min de GPU |
| Peso dos modelos | **32,2 GiB** após a primeira geração |
| Disco no template | **100 GB** basta |
| Máximo por geração | 4 min de áudio (`MAX_FRAMES=6000`) |
| Preparação da máquina | ~25 min do boot até `ready` (pesos pré-baixados) |
| Primeira geração | igual às demais — o download já foi feito na preparação |

⚠️ **A foto do avatar define a proporção do vídeo.** O Wan2GP ignora a
resolução pedida e segue a imagem de referência. Use 9:16 para Reels, 16:9
para YouTube, 1:1 para feed — senão o nó precisa recortar.
