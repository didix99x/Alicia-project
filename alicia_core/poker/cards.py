"""Cartas e baralho padrão de 52 cartas (sem curingas)."""

from __future__ import annotations

import random
import secrets
from dataclasses import dataclass
from typing import List, Optional

RANK_CHARS = {2: "2", 3: "3", 4: "4", 5: "5", 6: "6", 7: "7", 8: "8", 9: "9", 10: "10", 11: "J", 12: "Q", 13: "K", 14: "A"}
SUITS = ("c", "d", "h", "s")                        # paus, ouros, copas, espadas
SUIT_SYMBOLS = {"c": "♣", "d": "♦", "h": "♥", "s": "♠"}
SUIT_NAMES_PT = {"c": "paus", "d": "ouros", "h": "copas", "s": "espadas"}
RED_SUITS = ("d", "h")


@dataclass(frozen=True)
class Card:
    rank: int       # 2..14 (14 = Ás)
    suit: str       # 'c', 'd', 'h', 's'

    def __post_init__(self):
        if self.rank not in RANK_CHARS or self.suit not in SUITS:
            raise ValueError(f"Carta inválida: {self.rank!r}{self.suit!r}")

    @property
    def rank_char(self) -> str:
        return RANK_CHARS[self.rank]

    @property
    def symbol(self) -> str:
        return SUIT_SYMBOLS[self.suit]

    @property
    def is_red(self) -> bool:
        return self.suit in RED_SUITS

    @property
    def code(self) -> str:
        return f"{self.rank_char}{self.suit}"

    def __str__(self) -> str:
        return f"{self.rank_char}{self.symbol}"

    def __repr__(self) -> str:
        return f"Card({self.code})"


def full_deck() -> List[Card]:
    return [Card(r, s) for s in SUITS for r in range(2, 15)]


def parse_cards(text: str) -> List[Card]:
    """'As Kh 10d' -> [Card...]  (usado nos testes)."""
    out = []
    for token in text.split():
        rank_part, suit = token[:-1].upper(), token[-1].lower()
        rank = {**{v: k for k, v in RANK_CHARS.items()}, "T": 10}[rank_part]
        out.append(Card(rank, suit))
    return out


class Deck:
    """Baralho embaralhado com a fonte de aleatoriedade do sistema operacional (imprevisível)."""

    def __init__(self, rng: Optional[random.Random] = None, cards: Optional[List[Card]] = None):
        self._rng = rng or secrets.SystemRandom()
        self._cards = list(cards) if cards is not None else full_deck()
        if cards is None:
            self._rng.shuffle(self._cards)

    def deal(self, n: int) -> List[Card]:
        if n < 0 or n > len(self._cards):
            raise ValueError("Cartas insuficientes no baralho.")
        dealt, self._cards = self._cards[:n], self._cards[n:]
        return dealt

    def __len__(self) -> int:
        return len(self._cards)
