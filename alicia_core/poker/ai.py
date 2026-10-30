"""
Estratégia da Alicia no Five-Card Draw. SEM modelo de linguagem: é cálculo local, rápido e sempre legal.

HONESTIDADE: a IA só recebe uma `Observation` (as próprias cartas + informação pública: apostas, pote, fichas,
quantas cartas o oponente trocou). Ela nunca vê o baralho, as cartas do jogador nem o resultado do embaralhamento;
as cartas "ainda não vistas" que ela usa nas simulações são todas as 47 que não estão na mão dela.

ARQUITETURA (cada parte é independente)
  StrengthTable   força de uma mão como percentil entre mãos aleatórias (tabela amostrada uma vez)
  OpponentModel   estilo do jogador (frequência de desistir, de aumentar, força mostrada nos showdowns)
  AliciaAI        troca de cartas (valor esperado de cada combinação mantida) e decisão de apostas
A "personalidade" (falas, expressões) fica em reactions.py, separada da estratégia.
"""

from __future__ import annotations

import bisect
import itertools
import math
import random
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from .cards import Card, full_deck
from .engine import Observation
from .evaluator import evaluate


# ---------------------------------------------------------------------- força da mão
class StrengthTable:
    """Percentil de uma mão entre 6000 mãos aleatórias (semente fixa: tabela de referência, não informação oculta)."""
    _scores: Optional[List[Tuple[int, ...]]] = None

    @classmethod
    def _build(cls):
        rng = random.Random(20240601)
        deck = full_deck()
        cls._scores = sorted(evaluate(rng.sample(deck, 5)) for _ in range(6000))

    @classmethod
    def strength(cls, cards: Sequence[Card]) -> float:
        if cls._scores is None:
            cls._build()
        lo = bisect.bisect_left(cls._scores, evaluate(cards))
        hi = bisect.bisect_right(cls._scores, evaluate(cards))
        return (lo + hi) / 2.0 / len(cls._scores)


# ---------------------------------------------------------------------- modelo do oponente
class OpponentModel:
    """Aprende o estilo do jogador SÓ com o que é público (ações e showdowns revelados)."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.hands = 0
        self.faced_aggression = 0      # vezes que o jogador enfrentou uma aposta/aumento da Alicia
        self.folds_to_aggression = 0
        self.aggressive_actions = 0    # apostas/aumentos do jogador
        self.total_actions = 0
        self.showdown_strengths: List[float] = []

    def observe_hand(self, events: Sequence[Dict], my_index: int):
        self.hands += 1
        opp = 1 - my_index
        facing = False
        for e in events:
            if e["type"] in ("ante", "draw", "showdown"):
                continue
            if e["player"] == opp:
                self.total_actions += 1
                if facing:
                    self.faced_aggression += 1
                    if e["type"] == "fold":
                        self.folds_to_aggression += 1
                if e["type"] in ("bet", "raise"):
                    self.aggressive_actions += 1
                facing = False
            elif e["player"] == my_index:
                facing = e["type"] in ("bet", "raise")

    def observe_showdown(self, opp_cards: Sequence[Card]):
        self.showdown_strengths.append(StrengthTable.strength(opp_cards))

    @property
    def fold_rate(self) -> float:
        return (self.folds_to_aggression + 1.0) / (self.faced_aggression + 4.0)       # prior 0.25

    @property
    def aggression(self) -> float:
        return (self.aggressive_actions + 1.0) / (self.total_actions + 4.0)

    @property
    def avg_showdown(self) -> Optional[float]:
        if len(self.showdown_strengths) < 3:
            return None
        return sum(self.showdown_strengths) / len(self.showdown_strengths)

    @property
    def reliable(self) -> bool:
        return self.hands >= 6


# ---------------------------------------------------------------------- decisão
@dataclass
class Decision:
    kind: str                       # fold, check, call, bet, raise, allin
    amount: Optional[int] = None
    intent: str = ""                # value, bluff, check, call, cheap_call, fold, raise_value, raise_bluff, pot_control
    equity: float = 0.0
    strength: float = 0.0


def _sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))



class OpponentDistribution:
    """
    Distribuição da força FINAL de um oponente típico, por número de cartas trocadas.
    Simulada uma vez: mãos aleatórias + uma política de trocas simples (guarda pares/trincas, 4 para flush/sequência,
    senão as cartas altas). É um modelo de referência do que se vê na mesa, não informação sobre a mão real dele.
    """
    _by_discards: Optional[Dict[int, List[float]]] = None

    @staticmethod
    def _policy(hand: List[Card]) -> List[int]:
        """Posições que um jogador comum descartaria."""
        counts: Dict[int, int] = {}
        for c in hand:
            counts[c.rank] = counts.get(c.rank, 0) + 1
        rank = evaluate(hand)
        if rank[0] in (4, 5, 6, 7, 8):
            return []
        if max(counts.values()) >= 2:
            return [i for i, c in enumerate(hand) if counts[c.rank] == 1]
        for suit in {c.suit for c in hand}:
            if sum(1 for c in hand if c.suit == suit) == 4:
                return [i for i, c in enumerate(hand) if sum(1 for x in hand if x.suit == c.suit) == 1]
        ranks = sorted({c.rank for c in hand})
        for window in range(len(ranks) - 3):
            if ranks[window + 3] - ranks[window] == 3:
                keep = set(ranks[window:window + 4])
                return [i for i, c in enumerate(hand) if c.rank not in keep]
        order = sorted(range(5), key=lambda i: -hand[i].rank)
        keep = order[:2] if hand[order[0]].rank >= 13 else order[:1]
        return [i for i in range(5) if i not in keep]

    @classmethod
    def _build(cls):
        rng = random.Random(777)
        deck = full_deck()
        buckets: Dict[int, List[float]] = {d: [] for d in range(6)}
        for _ in range(9000):
            cards = rng.sample(deck, 15)
            hand, extra = cards[:5], cards[5:]
            discard = cls._policy(hand)
            final = list(hand)
            for i, new in zip(discard, extra):
                final[i] = new
            buckets[len(discard)].append(StrengthTable.strength(final))
        cls._by_discards = {d: sorted(v) for d, v in buckets.items() if v}

    @classmethod
    def cdf(cls, discards: Optional[int], s: float) -> float:
        """Fração das mãos do oponente (com essa quantidade de trocas) que a força `s` supera."""
        if discards is None:
            return s                                             # sem troca ainda: qualquer mão (percentil = próprio s)
        if cls._by_discards is None:
            cls._build()
        data = cls._by_discards.get(discards) or cls._by_discards.get(max(cls._by_discards, key=lambda d: -abs(d - discards)))
        lo, hi = bisect.bisect_left(data, s), bisect.bisect_right(data, s)
        return (lo + hi) / 2.0 / len(data)


class AliciaAI:
    def __init__(self, rng: Optional[random.Random] = None, samples: int = 150):
        self.rng = rng or random.Random()
        self.samples = samples
        self.model = OpponentModel()
        self._potential_cache: Dict[Tuple, float] = {}

    # ------------------------------------------------------------------ aprendizado
    def new_match(self):
        self.model.reset()
        self._potential_cache.clear()

    def observe_hand_end(self, events: Sequence[Dict], my_index: int, opp_cards_if_revealed: Optional[Sequence[Card]] = None):
        self.model.observe_hand(events, my_index)
        if opp_cards_if_revealed:
            self.model.observe_showdown(opp_cards_if_revealed)
        self._potential_cache.clear()

    # ------------------------------------------------------------------ troca de cartas
    def _unseen(self, cards: Sequence[Card]) -> List[Card]:
        mine = set(cards)
        return [c for c in full_deck() if c not in mine]

    def _future_draws(self, cards: Sequence[Card], samples: int) -> List[List[Card]]:
        """Sequências de cartas futuras sorteadas UMA vez e compartilhadas por todos os planos (comparação pareada)."""
        unseen = self._unseen(cards)
        return [self.rng.sample(unseen, 5) for _ in range(samples)]

    def plan_ev(self, cards: Sequence[Card], keep: Sequence[bool], futures: Optional[List[List[Card]]] = None) -> float:
        kept = [c for c, k in zip(cards, keep) if k]
        n = 5 - len(kept)
        if n == 0:
            return StrengthTable.strength(cards)
        futures = futures or self._future_draws(cards, self.samples)
        return sum(StrengthTable.strength(kept + f[:n]) for f in futures) / len(futures)

    def choose_discards(self, obs: Observation) -> Tuple[List[int], float]:
        """Qual combinação manter maximiza a força esperada depois da troca. Devolve (posições a descartar, EV)."""
        cards = list(obs.my_cards)
        futures = self._future_draws(cards, self.samples)
        counts: Dict[int, int] = {}
        for c in cards:
            counts[c.rank] = counts.get(c.rank, 0) + 1

        def score(mask, ev_):
            # desempate: guardar uma carta baixa sozinha quase nunca ajuda (e denuncia o que ela tem)
            lonely_low = sum(1 for c, k in zip(cards, mask) if k and counts[c.rank] == 1 and c.rank <= 9)
            return ev_ - 0.004 * lonely_low

        stand = StrengthTable.strength(cards)
        best_mask, best_ev, best_score = (True,) * 5, stand, score((True,) * 5, stand)
        for mask in itertools.product((True, False), repeat=5):
            if all(mask):
                continue
            ev_ = self.plan_ev(cards, mask, futures)
            sc = score(mask, ev_)
            if sc > best_score + 1e-9:
                best_mask, best_ev, best_score = mask, ev_, sc
        return [i for i, keep in enumerate(best_mask) if not keep], best_ev

    def potential(self, obs: Observation) -> float:
        key = (tuple(sorted(c.code for c in obs.my_cards)),)
        if key not in self._potential_cache:
            self._potential_cache[key] = self.choose_discards(obs)[1]
        return self._potential_cache[key]

    # ------------------------------------------------------------------ estimativas
    @staticmethod
    def hand_class(cards: Sequence[Card]) -> str:
        s = StrengthTable.strength(cards)
        return "strong" if s >= 0.85 else "medium" if s >= 0.55 else "weak"

    def _my_strength(self, obs: Observation) -> float:
        current = StrengthTable.strength(obs.my_cards)
        if obs.my_discards is not None or obs.phase == "BET2":
            return current                               # já trocou: o que tem é o que vale
        return 0.35 * current + 0.65 * self.potential(obs)

    def _opp_cut(self, obs: Observation) -> float:
        """Fração das mãos mais fracas do oponente que são improváveis dado o que ele apostou (0 = nenhuma)."""
        q = 0.0
        if obs.to_call > 0:
            ratio = obs.to_call / max(1, obs.pot - obs.to_call)
            commit = obs.to_call / float(max(1, obs.my_stack + obs.to_call))
            # aposta grande (para o pote e para as fichas dele): raramente é lixo
            q = 0.30 * (1 - math.exp(-ratio)) + 0.30 * commit
            if obs.opp_discards in (0, 1):
                q *= 0.5                                # a faixa de quem trocou 0-1 cartas já é forte: aposta diz menos
        m = self.model
        if m.reliable:
            if m.aggression > 0.55 and (m.avg_showdown is None or m.avg_showdown < 0.6):
                q -= 0.08                               # agressivo e blefador: pago mais leve
            if m.avg_showdown is not None:
                q += (m.avg_showdown - 0.6) * 0.15
        return max(0.0, min(0.75, q))

    def _equity(self, s: float, obs: Observation) -> float:
        """Chance aproximada de vencer: posição da minha mão na distribuição provável da mão dele."""
        q = self._opp_cut(obs)
        beat = OpponentDistribution.cdf(obs.opp_discards, s)
        return min(0.98, max(0.02, (beat - q) / (1.0 - q)))

    def warm_up(self):
        """Constrói as tabelas de referência (≈0,5 s) para a primeira decisão não demorar."""
        StrengthTable.strength(full_deck()[:5])
        OpponentDistribution.cdf(0, 0.5)

    @staticmethod
    def _my_committed(obs: Observation) -> int:
        return sum(e["amount"] for e in obs.events if e["player"] == obs.my_index and e["type"] in ("ante", "bet", "raise", "call"))

    def mood(self, obs: Observation) -> float:
        """-1 (muito atrás) .. +1 (muito à frente), pelas fichas no começo da mão. Muda a postura, não as regras."""
        start = obs.my_stack + self._my_committed(obs)
        return max(-0.6, min(0.6, (start - obs.starting_chips) / float(obs.starting_chips)))

    def _bluff_probability(self, obs: Observation) -> float:
        p = 0.08
        if self.model.reliable:
            p += 0.30 * (self.model.fold_rate - 0.30)
        mood = self.mood(obs)
        if mood < -0.35:
            p *= 0.6
        elif mood > 0.35:
            p *= 1.3
        if obs.phase == "BET1":
            p *= 0.4
        return max(0.03, min(0.28, p))

    # ------------------------------------------------------------------ apostas
    def decide(self, obs: Observation) -> Decision:
        legal = obs.legal
        if not legal:
            raise ValueError("Não há ação legal para decidir.")
        s = self._my_strength(obs)
        equity = self._equity(s, obs)
        mood = self.mood(obs)
        short_stack = obs.my_stack <= 4 * obs.min_bet
        d = self._decide_core(obs, s, equity, mood, short_stack)
        d.equity, d.strength = equity, s
        return self._legalize(d, obs)

    def _decide_core(self, obs, s, equity, mood, short_stack) -> Decision:
        legal, rng = obs.legal, self.rng
        pot = max(obs.pot, 1)
        tighten = 0.04 if mood < -0.3 else (-0.02 if mood > 0.35 else 0.0)

        if obs.to_call > 0:
            pot_odds = obs.to_call / float(pot + obs.to_call)
            commit = obs.to_call / float(max(1, obs.my_stack + obs.to_call))
            required = pot_odds + 0.02 + 0.06 * commit + tighten
            if short_stack:
                if equity >= max(0.40, pot_odds + 0.02):
                    return Decision("call", legal.get("call"), "call")
            if equity >= required:
                monster = s >= 0.98 and equity >= 0.5               # mão quase imbatível: extrai valor
                if "raise" in legal and ((equity >= 0.72 and rng.random() < 0.5 + 0.4 * (equity - 0.72) / 0.28)
                                         or (monster and rng.random() < 0.8)):
                    return self._raise(obs, equity, "raise_value")
                return Decision("call", legal["call"], "call")
            if obs.to_call <= 0.06 * pot and equity >= 0.25:
                return Decision("call", legal["call"], "cheap_call")
            if "raise" in legal and obs.phase == "BET2" and s < 0.35 and rng.random() < self._bluff_probability(obs) * 0.35:
                return self._raise(obs, equity, "raise_bluff")
            return Decision("fold", None, "fold")

        # sem aposta a pagar
        if "bet" in legal:
            if short_stack and equity >= 0.55:
                return Decision("allin", legal["allin"], "value")
            if equity >= 0.68:
                if rng.random() < 0.88:
                    frac = rng.uniform(0.6, 1.1) if equity >= 0.85 else rng.uniform(0.5, 0.9)
                    return self._bet(obs, frac, "value")
                return Decision("check", None, "pot_control")           # slowplay ocasional
            if equity >= 0.5:
                if rng.random() < 0.40:
                    return self._bet(obs, rng.uniform(0.33, 0.5), "value")
                return Decision("check", None, "check")
            if rng.random() < self._bluff_probability(obs):
                return self._bet(obs, rng.uniform(0.5, 0.75), "bluff")
        return Decision("check", None, "check")

    def _bet(self, obs, frac_of_pot, intent) -> Decision:
        lo, hi = obs.legal["bet"]
        amount = int(round(frac_of_pot * obs.pot / 5.0) * 5) or lo       # múltiplos de 5 ficam mais naturais
        amount = max(lo, min(hi, amount))
        if amount >= 0.85 * hi:
            return Decision("allin", obs.legal.get("allin", hi), intent)
        return Decision("bet", amount, intent)

    def _raise(self, obs, equity, intent) -> Decision:
        lo, hi = obs.legal["raise"]
        frac = self.rng.uniform(0.6, 1.0)
        target = obs.current_bet + int(round(frac * (obs.pot + obs.to_call) / 5.0) * 5)
        target = max(lo, min(hi, target))
        if target >= 0.8 * hi:
            return Decision("allin", obs.legal.get("allin", hi), intent)
        return Decision("raise", target, intent)

    # ------------------------------------------------------------------ segurança
    def _legalize(self, d: Decision, obs: Observation) -> Decision:
        """Garante uma ação PERMITIDA. Qualquer coisa fora do permitido vira a opção segura."""
        legal = obs.legal
        k, a = d.kind, d.amount
        if k == "allin" and "allin" in legal:
            return d
        if k in ("check", "fold") and k in legal:
            return d
        if k == "call" and "call" in legal:
            d.amount = legal["call"]
            return d
        if k in ("bet", "raise") and k in legal and a is not None:
            lo, hi = legal[k]
            d.amount = max(lo, min(hi, int(a)))
            return d
        return self.fallback(obs, d)

    @staticmethod
    def fallback(obs: Observation, d: Optional[Decision] = None) -> Decision:
        """Estratégia de contingência simples (usada se algo der errado): passa; ou paga barato; ou desiste."""
        legal = obs.legal
        eq, st = (d.equity, d.strength) if d else (0.0, 0.0)
        if "check" in legal:
            return Decision("check", None, "check", eq, st)
        if "call" in legal and legal["call"] <= max(obs.pot * 0.25, obs.min_bet):
            return Decision("call", legal["call"], "cheap_call", eq, st)
        if "fold" in legal:
            return Decision("fold", None, "fold", eq, st)
        return Decision("call", legal.get("call"), "call", eq, st)
