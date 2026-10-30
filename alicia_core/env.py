"""
Carregador mínimo de arquivo .env (sem dependências externas).

- Lê linhas CHAVE=VALOR (aceita aspas e comentários com #).
- NUNCA sobrescreve variáveis que já existam no ambiente do Windows.
- Nunca registra valores em log.
"""

import os
import pathlib
from typing import Dict

PLACEHOLDER_MARKERS = ("COLE_", "SUA_CHAVE", "XXXX", "your_key", "your-key", "changeme")


def parse_env_text(text: str) -> Dict[str, str]:
    values: Dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.lower().startswith("export "):
            line = line[7:].strip()
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        elif " #" in val:
            val = val.split(" #", 1)[0].strip()
        if key:
            values[key] = val
    return values


def load_env_file(path: str = ".env") -> bool:
    p = pathlib.Path(path)
    if not p.exists():
        return False
    try:
        data = parse_env_text(p.read_text(encoding="utf-8-sig"))
    except OSError:
        return False
    for k, v in data.items():
        os.environ.setdefault(k, v)
    return True


def is_placeholder(value: str) -> bool:
    v = (value or "").strip()
    return (not v) or any(m.lower() in v.lower() for m in PLACEHOLDER_MARKERS)


def set_env_value(path: str, key: str, value: str) -> None:
    """Grava/atualiza uma chave no .env preservando as demais linhas."""
    p = pathlib.Path(path)
    lines = p.read_text(encoding="utf-8-sig").splitlines() if p.exists() else []
    out, done = [], False
    for line in lines:
        if line.strip().startswith(f"{key}=") and not done:
            out.append(f"{key}={value}")
            done = True
        else:
            out.append(line)
    if not done:
        out.append(f"{key}={value}")
    p.write_text("\n".join(out) + "\n", encoding="utf-8")
