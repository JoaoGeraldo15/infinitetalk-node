#!/usr/bin/env python3
"""Teste de fumaça do nó — roda do seu notebook contra a máquina alugada.

    export ADAPTER_TOKEN=<o mesmo token do template>
    python3 smoke_test.py \
        --url https://<IP>:<PORTA_8000> \
        --audio /workspace/teste.wav \
        --image /workspace/avatar.png

⚠️ O token vem da variável de ambiente, NUNCA escrito aqui: este arquivo mora
num repositório público. Um token no código seria lido por qualquer um, e quem
o tiver enfileira jobs na sua GPU alugada e baixa seus vídeos.
(`--token` ainda existe, mas fica no histórico do shell — prefira o export.)

`--audio` e `--image` são caminhos DENTRO da máquina (envie pelo Jupyter para
/workspace) ou URLs https. O adapter aceita os dois.

Só biblioteca padrão de propósito: precisa rodar em qualquer lugar, sem venv.
"""

from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request

# A base-image do Vast serve com certificado auto-assinado. Verificar aqui não
# agregaria: o que autentica o nó é o ADAPTER_TOKEN, não a cadeia de confiança.
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE


def chamar(url: str, token: str, metodo: str = "GET", corpo: dict | None = None,
           bruto: bool = False, timeout: int = 60):
    dados = json.dumps(corpo).encode() if corpo is not None else None
    req = urllib.request.Request(url, data=dados, method=metodo)
    req.add_header("Authorization", f"Bearer {token}")
    if dados:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, context=CTX, timeout=timeout) as r:
        return r.read() if bruto else json.loads(r.read())


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--url", required=True, help="https://IP:PORTA (porta externa 8000)")
    p.add_argument("--token", default=os.environ.get("ADAPTER_TOKEN", ""),
                   help="padrão: variável de ambiente ADAPTER_TOKEN")
    p.add_argument("--audio", required=True)
    p.add_argument("--image", required=True)
    p.add_argument("--aspect", default="9:16")
    p.add_argument("--prompt", default=None, help="comportamento do avatar")
    p.add_argument("--saida", default="resultado.mp4")
    a = p.parse_args()
    base = a.url.rstrip("/")

    if not a.token:
        print("✗ token ausente. Rode:  export ADAPTER_TOKEN=<token do template>")
        return 1

    # ── 1. o nó está pronto? ──────────────────────────────────────────
    print("1) /healthz ...", end=" ", flush=True)
    try:
        # Sem token de propósito: /healthz é sonda pública. Se ISTO pedir
        # autenticação, quem está pedindo é o Caddy, não o adapter — anote,
        # porque muda o cliente da plataforma na Fase 2.
        h = chamar(f"{base}/healthz", "", timeout=30)
    except urllib.error.HTTPError as e:
        print(f"HTTP {e.code}")
        if e.code in (401, 403):
            print("   ⚠️  o Caddy está exigindo autenticação própria, ANTES do "
                  "adapter.\n   Isso é informação importante para a Fase 3 — "
                  "anote e siga com --token.")
        return 1
    except Exception as e:  # noqa: BLE001
        print(f"falhou: {e}")
        print("   A máquina terminou de subir? Veja: supervisorctl tail -f adapter")
        return 1

    print(json.dumps(h, ensure_ascii=False))
    if h.get("status") != "ready":
        print(f"   ainda em '{h.get('status')}' — espere o provisionamento acabar")
        return 1

    # ── 2. enfileirar ─────────────────────────────────────────────────
    settings = {"aspect_ratio": a.aspect}
    if a.prompt:
        settings["prompt"] = a.prompt
    pedido = {"task": "render", "audio_url": a.audio,
              "image_url": a.image, "settings": settings}

    print("2) POST /jobs ...", end=" ", flush=True)
    try:
        j = chamar(f"{base}/jobs", a.token, "POST", pedido)
    except urllib.error.HTTPError as e:
        print(f"HTTP {e.code}: {e.read().decode()[:300]}")
        if e.code == 401:
            print("   token errado. É o ADAPTER_TOKEN do template — ou, se você "
                  "não definiu um,\n   o sorteado que aparece no log do adapter.")
        return 1
    job_id = j["job_id"]
    print(f"job {job_id}")

    # ── 3. acompanhar ─────────────────────────────────────────────────
    print("3) aguardando (Ctrl-C não cancela o job na máquina)")
    t0 = time.time()
    ultimo = ""
    while True:
        time.sleep(10)
        try:
            s = chamar(f"{base}/jobs/{job_id}", a.token)
        except Exception as e:  # noqa: BLE001
            print(f"   erro ao consultar: {e}")
            continue

        linha = f"   [{time.time()-t0:6.0f}s] {s['status']:<10} " \
                f"{s.get('progress', 0)*100:5.1f}%  {s.get('message', '')}"
        if linha != ultimo:
            print(linha)
            ultimo = linha

        if s["status"] == "completed":
            break
        if s["status"] == "failed":
            print(f"\n✗ FALHOU: {s.get('error')}")
            return 1

    # ── 4. baixar ─────────────────────────────────────────────────────
    decorrido = time.time() - t0
    print(f"4) concluído em {decorrido:.0f}s · GPU: {s.get('gpu_seconds', 0):.0f}s")
    art = (s.get("artifacts") or {}).get("master") or {}
    if art:
        # Chaves em inglês: é o contrato que a plataforma consome (INTEGRATION.md §2).
        print(f"   {art.get('width')}x{art.get('height')} · "
              f"{art.get('duration_seconds', 0):.1f}s · "
              f"{art.get('bytes', 0)/1e6:.1f} MB")
        dur = art.get("duration_seconds") or 0
        if dur:
            print(f"   ⇒ {decorrido/dur:.1f}x tempo real "
                  f"(a Fase 0b mediu 25,5x num RTX 4090)")

    print(f"5) baixando para {a.saida} ...", end=" ", flush=True)
    dados = chamar(f"{base}/jobs/{job_id}/artifact/master", a.token, bruto=True,
                   timeout=600)
    with open(a.saida, "wb") as f:
        f.write(dados)
    print(f"{len(dados)/1e6:.1f} MB")
    print("\n✓ Agora ASSISTA o vídeo. O que importa nesta primeira sessão é se a\n"
          "  emenda entre os pedaços aparece — procure saltos de enquadramento\n"
          "  ou de aparência a cada ~29 s.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
