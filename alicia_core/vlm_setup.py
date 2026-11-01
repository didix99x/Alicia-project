# -*- coding: utf-8 -*-
"""
Instalação do modelo de visão LOCAL (usado por `Iniciar_Alicia.bat --vision-local`). Só biblioteca padrão.

  * Descobre os arquivos do modelo pela API pública do Hugging Face (nome, tamanho e SHA-256 oficiais).
  * Baixa com progresso, em arquivo .part retomável, e CONFERE o SHA-256 antes de aceitar.
  * Nada é baixado sem a autorização do usuário (quem chama pergunta antes).
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import re
import time
import urllib.request
from typing import Callable, Dict, List, Optional, Tuple

# Modelo padrão: pequeno o bastante para PCs comuns (≈ 3 GB em disco, ≈ 4–6 GB de RAM em uso).
DEFAULT_REPO = "unsloth/Qwen2.5-VL-3B-Instruct-GGUF"
HF = "https://huggingface.co"
PIP_ARGS = ["--only-binary=:all:", "--extra-index-url", "https://abetlen.github.io/llama-cpp-python/whl/cpu"]
PIP_PACKAGE = "llama-cpp-python"


def _get_json(url: str, timeout: float = 30.0):
    req = urllib.request.Request(url, headers={"User-Agent": "Alicia-installer"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def pick_files(siblings: List[Dict]) -> Optional[Tuple[Dict, Dict]]:
    """Escolhe (modelo Q4_K_M, projetor mmproj) entre os arquivos do repositório. None se não achar."""
    ggufs = [s for s in siblings if str(s.get("rfilename", "")).lower().endswith(".gguf")]
    mm = [s for s in ggufs if "mmproj" in s["rfilename"].lower()]
    main = [s for s in ggufs if "mmproj" not in s["rfilename"].lower()]
    if not mm or not main:
        return None
    main_pick = next((s for s in main if "q4_k_m" in s["rfilename"].lower()), None) or \
        next((s for s in main if "q4" in s["rfilename"].lower()), None) or main[0]
    mm_pick = next((s for s in mm if re.search(r"(?<![a-z0-9])f16", s["rfilename"].lower())), None) or \
        next((s for s in mm if "bf16" not in s["rfilename"].lower()), None) or mm[0]
    return main_pick, mm_pick


def describe_repo(repo: str = DEFAULT_REPO) -> Tuple[Dict, Dict]:
    """Consulta o Hugging Face: devolve dois dicts {name, size, sha256, url}. Levanta exceção se falhar."""
    info = _get_json(f"{HF}/api/models/{repo}?blobs=true")
    picked = pick_files(info.get("siblings", []))
    if picked is None:
        raise RuntimeError("o repositório do modelo não tem um arquivo de modelo e um mmproj")
    out = []
    for s in picked:
        lfs = s.get("lfs") or {}
        out.append({"name": pathlib.PurePosixPath(s["rfilename"]).name,
                    "size": int(lfs.get("size") or s.get("size") or 0),
                    "sha256": str(lfs.get("sha256") or ""),
                    "url": f"{HF}/{repo}/resolve/main/{s['rfilename']}"})
    return out[0], out[1]


def human(n: float) -> str:
    return f"{n / 1e9:.2f} GB" if n >= 1e9 else f"{n / 1e6:.0f} MB"


def sha256_of(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def download(file: Dict, dest_dir: pathlib.Path, progress: Optional[Callable[[int, int], None]] = None,
             chunk: int = 1 << 20) -> pathlib.Path:
    """Baixa (retomando o .part se existir) e confere o SHA-256. Levanta RuntimeError se algo não bater."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    final = dest_dir / file["name"]
    if final.exists() and (not file["sha256"] or sha256_of(final) == file["sha256"]):
        return final
    part = dest_dir / (file["name"] + ".part")
    have = part.stat().st_size if part.exists() else 0
    headers = {"User-Agent": "Alicia-installer"}
    if have:
        headers["Range"] = f"bytes={have}-"
    req = urllib.request.Request(file["url"], headers=headers)
    with urllib.request.urlopen(req, timeout=60) as r:
        resumed = getattr(r, "status", 200) == 206
        mode = "ab" if (have and resumed) else "wb"
        done = have if mode == "ab" else 0
        total = file["size"] or (done + int(r.headers.get("Content-Length") or 0))
        with open(part, mode) as f:
            while True:
                buf = r.read(chunk)
                if not buf:
                    break
                f.write(buf)
                done += len(buf)
                if progress:
                    progress(done, total)
    if file["size"] and part.stat().st_size != file["size"]:
        raise RuntimeError("o download terminou incompleto (rode de novo: ele continua de onde parou)")
    if file["sha256"] and sha256_of(part) != file["sha256"]:
        part.unlink()
        raise RuntimeError("o arquivo baixado não confere com a assinatura (SHA-256) oficial; foi descartado")
    part.replace(final)
    return final
