"""
Avaliação de mãos de pôquer de 5 cartas (classificação oficial, sem curingas).

`evaluate()` devolve uma TUPLA comparável: (categoria, desempates...). Comparar duas tuplas com > < ==
já resolve categoria, par/trinca/quadra mais alta e kickers (inclusive o "wheel" A-2-3-4-5, que vale 5 alto).

Categorias: 0 carta alta, 1 par, 2 dois pares, 3 trinca, 4 sequência, 5 flush, 6 full house, 7 quadra, 8 straight flush.
"""

from __future__ import annotations

from collections import Counter
from typing import List, Sequence, Tuple

from .cards import Card, RANK_CHARS

HandRank = Tuple[int, ...]

CATEGORY_NAMES = ["Carta alta", "Par", "Dois pares", "Trinca", "Sequência", "Flush", "Full house", "Quadra", "Straight flush"]
RANK_NAMES_SING = {2: "dois", 3: "três", 4: "quatro", 5: "cinco", 6: "seis", 7: "sete", 8: "oito", 9: "nove", 10: "dez",
                   11: "valete", 12: "dama", 13: "rei", 14: "ás"}
RANK_NAMES_PLUR = {2: "dois", 3: "três", 4: "quatros", 5: "cincos", 6: "seis", 7: "setes", 8: "oitos", 9: "noves", 10: "dez",
                   11: "valetes", 12: "damas", 13: "reis", 14: "ases"}
RANK_NAMES_PLUR[2] = "dois"
RANK_NAMES_PLUR[3] = "três"


def evaluate(cards: Sequence[Card]) -> HandRank:
    if len(cards) != 5 or len(set(cards)) != 5:
        raise ValueError("Uma mão precisa ter exatamente 5 cartas distintas.")
    ranks = sorted((c.rank for c in cards), reverse=True)
    counts = Counter(ranks)
    groups = sorted(counts.items(), key=lambda kv: (kv[1], kv[0]), reverse=True)   # por quantidade, depois valor
    is_flush = len({c.suit for c in cards}) == 1
    uniq = sorted(set(ranks), reverse=True)
    straight_high = None
    if len(uniq) == 5:
        if uniq[0] - uniq[4] == 4:
            straight_high = uniq[0]
        elif uniq == [14, 5, 4, 3, 2]:
            straight_high = 5                                  # wheel: o ás vale 1
    if straight_high and is_flush:
        return (8, straight_high)
    if groups[0][1] == 4:
        return (7, groups[0][0], groups[1][0])
    if groups[0][1] == 3 and groups[1][1] == 2:
        return (6, groups[0][0], groups[1][0])
    if is_flush:
        return (5, *ranks)
    if straight_high:
        return (4, straight_high)
    if groups[0][1] == 3:
        return (3, groups[0][0], *sorted((r for r, _ in groups[1:]), reverse=True))
    if groups[0][1] == 2 and groups[1][1] == 2:
        hi, lo = sorted((groups[0][0], groups[1][0]), reverse=True)
        return (2, hi, lo, groups[2][0])
    if groups[0][1] == 2:
        return (1, groups[0][0], *sorted((r for r, _ in groups[1:]), reverse=True))
    return (0, *ranks)


def category(rank: HandRank) -> int:
    return rank[0]


def describe(rank: HandRank) -> str:
    """Descrição em português, ex.: 'Dois pares: ases e oitos', 'Sequência até o dez'."""
    cat = rank[0]
    if cat == 8:
        return "Royal flush" if rank[1] == 14 else f"Straight flush até {_n(rank[1])}"
    if cat == 7:
        return f"Quadra de {RANK_NAMES_PLUR[rank[1]]}"
    if cat == 6:
        return f"Full house: {RANK_NAMES_PLUR[rank[1]]} e {RANK_NAMES_PLUR[rank[2]]}"
    if cat == 5:
        return f"Flush, {_n(rank[1])} alto"
    if cat == 4:
        return f"Sequência até {_n(rank[1])}"
    if cat == 3:
        return f"Trinca de {RANK_NAMES_PLUR[rank[1]]}"
    if cat == 2:
        return f"Dois pares: {RANK_NAMES_PLUR[rank[1]]} e {RANK_NAMES_PLUR[rank[2]]}"
    if cat == 1:
        return f"Par de {RANK_NAMES_PLUR[rank[1]]}"
    return f"Carta alta: {_n(rank[1])}"


def _n(rank: int) -> str:
    return {14: "o ás", 13: "o rei", 12: "a dama", 11: "o valete"}.get(rank, f"o {rank}" if rank <= 10 else str(rank))


def winning_cards(cards: Sequence[Card], rank: HandRank) -> List[Card]:
    """As cartas que FORMAM a combinação (para destacar no showdown). Kickers ficam de fora."""
    cat = rank[0]
    if cat in (4, 5, 6, 8):
        return list(cards)
    if cat == 7:
        return [c for c in cards if c.rank == rank[1]]
    if cat == 3:
        return [c for c in cards if c.rank == rank[1]]
    if cat == 2:
        return [c for c in cards if c.rank in (rank[1], rank[2])]
    if cat == 1:
        return [c for c in cards if c.rank == rank[1]]
    top = max(cards, key=lambda c: c.rank)
    return [top]


def compare(a: HandRank, b: HandRank) -> int:
    """1 se a vence, -1 se b vence, 0 se empatam."""
    return (a > b) - (a < b)
