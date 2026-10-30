"""
Contexto de uma call online (Discord etc.): quem falou o quê, em janela deslizante.

Fica só em memória (RAM) e nunca entra no histórico da conversa normal: serve para a
Alicia saber do que se está falando quando alguém a chama.
"""

from __future__ import annotations

import re
import threading
import time
import unicodedata
from collections import deque
from difflib import SequenceMatcher
from typing import Callable, Deque, List, Tuple

# O Google nem sempre escreve "Alicia": no começo da frase cola na palavra anterior ("talicia resume...",
# "anicia o que acha...") ou erra uma letra. Por isso comparamos palavra a palavra por semelhança.
_NAME = "alicia"
_WAKE_EXTRA = {"alice", "alise", "alisia", "alicea"}
_NOT_WAKE = {"malicia", "malicias", "alicias", "policia", "delicia"}      # palavras reais parecidas
_WAKE_RATIO = 0.8


def _strip_accents(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", text) if unicodedata.category(c) != "Mn")


def has_wake_word(text: str) -> bool:
    if not text:
        return False
    for tok in re.findall(r"[a-z]+", _strip_accents(text.lower())):
        if len(tok) < 4 or tok in _NOT_WAKE:
            continue
        if tok == _NAME or tok in _WAKE_EXTRA or SequenceMatcher(None, tok, _NAME).ratio() >= _WAKE_RATIO:
            return True
    return False


class CallContext:
    def __init__(self, max_age: float = 600.0, max_items: int = 60, clock: Callable[[], float] = time.time):
        self.max_age = float(max_age)
        self._items: Deque[Tuple[float, str, str]] = deque(maxlen=int(max_items))
        self._lock = threading.Lock()
        self._clock = clock

    def add(self, speaker: str, text: str):
        text = " ".join((text or "").split())
        if not text:
            return
        with self._lock:
            self._items.append((self._clock(), speaker, text))

    def _fresh(self) -> List[Tuple[float, str, str]]:
        limit = self._clock() - self.max_age
        return [i for i in self._items if i[0] >= limit]

    def recent(self) -> List[Tuple[float, str, str]]:
        with self._lock:
            return self._fresh()

    def format_for_prompt(self, max_chars: int = 2500) -> str:
        """'Amigo: ...' do mais antigo ao mais novo; se passar do limite, descarta as falas mais antigas."""
        with self._lock:
            lines = [f"{who}: {text}" for _, who, text in self._fresh()]
        while lines and sum(len(l) + 1 for l in lines) > max_chars:
            lines.pop(0)
        return "\n".join(lines)

    def clear(self):
        with self._lock:
            self._items.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._fresh())
