"""
Conversa da Alicia DURANTE o pôquer (sem Qt, testável sem janela e sem rede).

Dois usos, ambos presos à mesa:
  * `chat()`    -> responde ao que o jogador diz (voz ou teclado) enquanto a mesa está aberta;
  * `narrate()` -> transforma um acontecimento real da partida numa fala curta e natural.

Regras (o modo pôquer NÃO se mistura com a conversa normal, a tela ou o modo gamer):
  * Só entra aqui o que o controlador considera público: fichas, pote, jogadas, trocas e o resultado/showdown.
    As cartas do jogador nunca chegam a este módulo; as dela só entram como uma classe vaga (forte/média/fraca).
  * Toda resposta passa por `grounded()`: números, combinações e cartas citados precisam existir no contexto da
    mesa (ou na fala do jogador). Se não existirem, a resposta é descartada e entra a fala pronta de reactions.py.
  * O histórico é só desta mesa (em memória): nada daqui entra na memória/histórico da conversa normal.
"""

from __future__ import annotations

import re
import unicodedata
from collections import deque
from typing import Deque, Optional, Tuple

from ..logging_setup import logger
from ..prompts import PromptEngine

# humor do rosto -> estado de reactions.py (e de volta)
EXPRESSION_TO_STATE = {"serio": "neutral", "feliz": "confident", "pensando": "suspicious", "bravo": "irritated", "surpresa": "surprised"}
STATE_LABEL = {"neutral": "neutra", "confident": "confiante", "suspicious": "desconfiada", "satisfied": "satisfeita",
               "irritated": "irritada", "surprised": "surpresa"}

MAX_CHARS = 200
MAX_SENTENCES = 2

_TAG_RE = re.compile(r"\[\s*(FELIZ|SERIO|SÉRIO|SURPRESA|PENSANDO|BRAVO)\s*\]", re.IGNORECASE)
_NUM_RE = re.compile(r"\d+")
_HAND_TERMS = ("full house", "flush", "sequencia", "straight", "trinca", "quadra", "dois pares", "par de", "royal", "carta alta")
_CARD_RE = re.compile(r"\b(as|ases|rei|reis|dama|damas|valete|valetes)\s+de\s+(copas|ouros|espadas|paus)\b|\bde\s+(copas|ouros|espadas|paus)\b")

SYSTEM = """Você é a Alicia, jogando Five-Card Draw (pôquer de 5 cartas com uma troca) contra {nome} numa mesa que aparece na tela do computador. Vocês são adversários e, ao mesmo tempo, companhia um do outro.

[SEU JEITO NA MESA]
{estilo}
Fale como em uma partida entre amigos: casual, natural, com personalidade, provocações leves e brincadeiras sobre a partida. Reaja às jogadas, às apostas, aos blefes, às vitórias e às derrotas dele.

[REGRAS DESTE MODO — PRIORIDADE MÁXIMA]
- Fale SOMENTE da partida em andamento. Não puxe assunto aleatório e não ajude com outras coisas (tela, jogos, tarefas, memórias). Se ele mudar de assunto, devolva a conversa para a mesa em uma frase com bom humor.
- Respostas CURTAS: no máximo 2 frases e cerca de 25 palavras. Nada de listas, emojis, asteriscos ou rubricas de ação: o texto é lido em voz alta.
- Só cite fichas, pote, jogadas, trocas, cartas e resultados que estejam em [ESTADO DA MESA]. O que não estiver lá você NÃO sabe. Você não vê as cartas dele. Nunca invente números, cartas ou resultados.
- Sua mão é SEGREDO: não revele cartas nem nomes de combinações antes do showdown. Pode provocar e blefar no tom, sem afirmar quais cartas tem.
- Não repita falas nem aberturas recentes. Varie.
- Comece SEMPRE com uma tag de rosto: [FELIZ], [SERIO], [SURPRESA], [PENSANDO] ou [BRAVO]."""


def _plain(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", text.lower()) if unicodedata.category(c) != "Mn")


def split_expression(raw: str) -> Tuple[str, Optional[str]]:
    """Tira a tag de rosto do texto. Devolve (texto limpo, 'feliz'|'serio'|... ou None)."""
    m = _TAG_RE.search(raw or "")
    expr = None
    if m:
        expr = _plain(m.group(1))
    return _TAG_RE.sub("", raw or "").strip(), expr


def clean_line(text: str, max_chars: int = MAX_CHARS, max_sentences: int = MAX_SENTENCES) -> str:
    """Deixa a fala pronta para voz: sem rubricas, sem aspas externas, no máximo 2 frases curtas."""
    t = re.sub(r"\*[^*]*\*", " ", text or "")
    t = re.sub(r"\([^)]*\)", " ", t)
    t = re.sub(r"[\U00010000-\U0010ffff\u2600-\u27bf]", "", t)          # emojis
    t = re.sub(r"\s+", " ", t).strip().strip('"“”').strip()
    parts = re.split(r"(?<=[.!?…])\s+", t)
    t = " ".join(parts[:max_sentences]).strip()
    if len(t) > max_chars:
        cut = t[:max_chars]
        end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
        t = cut[:end + 1] if end > 40 else cut.rsplit(" ", 1)[0].rstrip(",;:") + "…"
    return t


def grounded(text: str, *allowed: str) -> bool:
    """True se tudo o que a fala afirma (números, combinações, cartas) existe no contexto permitido."""
    base = _plain(" ".join(a or "" for a in allowed))
    low = _plain(text)
    if not set(_NUM_RE.findall(low)) <= set(_NUM_RE.findall(base)):
        return False
    for term in _HAND_TERMS:
        if term in low and term not in base:
            return False
    for m in _CARD_RE.finditer(low):
        if m.group(0) not in base:
            return False
    return True


class PokerTalk:
    """Fala da Alicia na mesa. `client` é o NvidiaClient (precisa de `.chat(messages, ...)` e `.has_key`)."""

    SAFE_CHAT = {
        "seria": "Prefiro manter o foco na partida. Sua vez.",
    }
    SAFE_CHAT_DEFAULT = "Hm, nessa eu prefiro deixar as cartas falarem. Bora jogar."

    def __init__(self, client, personality_getter=lambda: "normal", name_getter=lambda: "", temperature: float = 0.9,
                 overlay_getter=None):
        # overlay_getter(user_text) -> texto da camada extra (Modo Sem Filtro) ou "" — sempre dentro do contexto da mesa
        self.overlay_getter = overlay_getter
        self._user_text = ""
        self.client = client
        self.personality = personality_getter
        self.name = name_getter
        self.temperature = temperature
        self.history: Deque[Tuple[str, str]] = deque(maxlen=10)       # ("user"|"assistant", texto) só desta mesa

    # ------------------------------------------------------------------ estado
    @property
    def available(self) -> bool:
        return bool(self.client is not None and getattr(self.client, "has_key", False))

    def reset(self):
        self.history.clear()

    def remember_alicia(self, text: str):
        """Registra o que ela disse (inclusive falas prontas) para não se repetir."""
        if text:
            self.history.append(("assistant", text))

    # ------------------------------------------------------------------ prompts
    def system_prompt(self) -> str:
        key = self.personality()
        style = PromptEngine.PERSONALITIES.get(key, PromptEngine.PERSONALITIES[PromptEngine.DEFAULT_PERSONALITY])["style"]
        nome = (self.name() or "").strip() or "o jogador"
        base = SYSTEM.format(nome=nome, estilo=style)
        if self.overlay_getter:
            try:
                extra = self.overlay_getter(self._user_text)
            except Exception:
                extra = ""
            if extra:
                base += "\n\n" + extra
        return base

    def _messages(self, context: str, user_content: str):
        msgs = [{"role": "system", "content": self.system_prompt() + "\n\n[ESTADO DA MESA]\n" + (context or "A mão ainda não começou.")}]
        for role, text in self.history:
            if len(msgs) == 1 and role != "user":                       # depois do system, a conversa começa pelo jogador
                continue
            if msgs[-1]["role"] == role:                                # a API exige alternância
                msgs[-1]["content"] += "\n" + text
            else:
                msgs.append({"role": role, "content": text})
        if msgs[-1]["role"] == "user":
            msgs[-1]["content"] += "\n" + user_content
        else:
            msgs.append({"role": "user", "content": user_content})
        return msgs

    def _ask(self, context: str, user_content: str, max_tokens: int) -> str:
        return self.client.chat(self._messages(context, user_content), temperature=self.temperature, top_p=0.9, max_tokens=max_tokens)

    # ------------------------------------------------------------------ conversa com o jogador
    def chat(self, user_text: str, context: str) -> Tuple[str, str]:
        """Responde ao jogador. Devolve (fala, rosto). Levanta BrainError se a API falhar (quem chama decide o aviso)."""
        content = f"{(self.name() or 'O jogador').strip()} diz: {user_text}"
        self._user_text = user_text
        allowed = (context, user_text)
        text, expr = "", None
        for attempt in range(2):
            ask = content if attempt == 0 else (
                content + "\n(Sua resposta anterior citou algo que não está no estado da mesa. Refaça sem inventar nada: "
                          "só fichas, pote e jogadas que estão listados.)")
            raw = self._ask(context, ask, 300)
            text, expr = split_expression(raw)
            text = clean_line(text)
            if text and grounded(text, *allowed):
                break
            logger.info("Pôquer: resposta de conversa descartada (vazia ou citando algo fora do contexto).")
            text = ""
        if not text:
            text, expr = self.SAFE_CHAT.get(self.personality(), self.SAFE_CHAT_DEFAULT), "serio"
        self.history.append(("user", content))
        self.history.append(("assistant", text))
        return text, expr or "feliz"

    # ------------------------------------------------------------------ reação a um acontecimento
    def narrate(self, facts: str, context: str, state: str = "neutral") -> Optional[Tuple[str, str]]:
        """Fala curta sobre um acontecimento real. Devolve (fala, rosto) ou None (a fala pronta assume)."""
        self._user_text = ""
        content = (f"ACONTECEU AGORA: {facts}\n"
                   f"Reaja a isso em UMA frase curta, no seu estilo, com humor {STATE_LABEL.get(state, 'neutro')}. "
                   "Comente só o que aconteceu; não faça pergunta fora da partida.")
        raw = self._ask(context, content, 220)
        text, expr = split_expression(raw)
        text = clean_line(text, max_chars=160, max_sentences=2)
        if not text or not grounded(text, context, facts):
            logger.info("Pôquer: narração descartada (vazia ou citando algo fora do contexto).")
            return None
        self.history.append(("user", f"[aconteceu] {facts}"))
        self.history.append(("assistant", text))
        return text, expr or None
