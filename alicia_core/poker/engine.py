"""
Motor do Five-Card Draw (pôquer fechado de 5 cartas), cabeça a cabeça. SEM interface, SEM IA, SEM acaso escondido:
as cartas só mudam pelas regras abaixo. As fichas são puramente virtuais.

REGRAS (explícitas e consistentes)
  * Baralho de 52 cartas, sem curingas. Cada jogador recebe 5 cartas.
  * Ante: cada jogador paga `ante` antes de ver as apostas (dinheiro "morto" no pote). Quem tiver menos que o ante paga o que tem (all-in).
  * 1ª rodada de apostas -> TROCA (0 a 5 cartas, UMA vez por mão) -> 2ª rodada de apostas -> showdown.
  * Quem NÃO é o dealer age primeiro em cada rodada (o botão alterna a cada mão). O número de cartas trocadas é público.
  * Sem limite (no-limit): aposta mínima = `min_bet`; aumento mínimo = o tamanho da última aposta/aumento.
    No máximo `max_raises` aumentos por rodada (depois, só pagar ou desistir; all-in para pagar sempre vale).
  * Passar (check) só se não há aposta a pagar. Desistir (fold) só se há aposta a pagar (evita desistir sem querer).
  * ALL-IN: quem vai all-in por menos que o aumento mínimo pode, mas isso NÃO reabre a ação (o oponente só paga ou desiste).
  * POTES PARALELOS: são cabeça a cabeça, então basta uma regra: a parte da aposta que o oponente não conseguiu pagar
    (excesso de contribuição) VOLTA ao dono antes do showdown; só o valor igualado é disputado.
  * Showdown: vence a melhor mão (evaluator.py). Empate: o pote é dividido; a ficha ímpar vai para quem age primeiro (o não-dealer).
  * Quem desiste perde o pote; o vencedor NÃO é obrigado a revelar suas cartas.
  * Conservação: stacks + pote = constante durante toda a mão (testado).
"""

from __future__ import annotations

import enum
import random
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .cards import Card, Deck
from .evaluator import HandRank, describe, evaluate, winning_cards

HUMAN, ALICIA = 0, 1


class PokerError(Exception):
    """Ação ilegal ou fora de hora. A mensagem explica o motivo."""


class Phase(enum.Enum):
    BET1 = "apostas iniciais"
    DRAW = "troca de cartas"
    BET2 = "apostas finais"
    SHOWDOWN = "showdown"
    COMPLETE = "mão encerrada"


@dataclass
class Config:
    starting_chips: int = 1000
    ante: int = 10
    min_bet: int = 20
    max_raises: int = 3

    def validate(self):
        if self.starting_chips <= 0 or self.ante < 0 or self.min_bet <= 0 or self.max_raises < 0:
            raise ValueError("Configuração de fichas inválida.")


@dataclass
class HandResult:
    reason: str                              # 'fold' ou 'showdown'
    winner: Optional[int]                    # None = empate (pote dividido)
    amounts: List[int]                       # fichas recebidas de volta do pote por cada jogador (inclui reembolso de excesso)
    pot: int                                 # valor disputado
    ranks: Optional[List[HandRank]] = None   # só no showdown
    descriptions: Optional[List[str]] = None
    best_cards: Optional[List[List[Card]]] = None
    refunded: int = 0                        # excesso devolvido (aposta não paga)
    folder: Optional[int] = None


class Hand:
    def __init__(self, stacks: List[int], dealer: int, config: Config, deck: Optional[Deck] = None):
        config.validate()
        if len(stacks) != 2 or min(stacks) <= 0:
            raise PokerError("Os dois jogadores precisam ter fichas para jogar uma mão.")
        self.config = config
        self.dealer = dealer
        self.first = 1 - dealer                      # age primeiro
        self.stacks = list(stacks)                   # fichas que cada um ainda tem FORA do pote
        self.start_stacks = list(stacks)
        self.deck = deck or Deck()
        self.hole: List[List[Card]] = [self.deck.deal(5), self.deck.deal(5)]
        self.contrib = [0, 0]                        # total colocado no pote nesta mão (ante + apostas)
        self.round_bet = [0, 0]                      # colocado na rodada de apostas atual
        self.folded = [False, False]
        self.all_in = [False, False]
        self.drawn = [False, False]
        self.discards: List[Optional[int]] = [None, None]    # nº de cartas trocadas (público)
        self.events: List[Dict] = []
        self.result: Optional[HandResult] = None
        self.final_stacks: Optional[List[int]] = None
        self.to_act: Optional[int] = None
        self.phase = Phase.BET1
        for p in (HUMAN, ALICIA):
            pay = min(config.ante, self.stacks[p])
            self._commit(p, pay, count_round=False)
            if pay:
                self._log("ante", p, pay)
        self._start_betting_round(Phase.BET1)

    # ------------------------------------------------------------------ utilidades
    @property
    def pot(self) -> int:
        return sum(self.contrib)

    @property
    def is_over(self) -> bool:
        return self.phase == Phase.COMPLETE

    def _log(self, kind: str, player: int, amount: int = 0, **extra):
        self.events.append({"type": kind, "player": player, "amount": amount, "phase": self.phase.name, **extra})

    def _commit(self, p: int, amount: int, count_round: bool = True):
        amount = min(amount, self.stacks[p])
        self.stacks[p] -= amount
        self.contrib[p] += amount
        if count_round:
            self.round_bet[p] += amount
        if self.stacks[p] == 0:
            self.all_in[p] = True

    def _opp(self, p: int) -> int:
        return 1 - p

    # ------------------------------------------------------------------ rodada de apostas
    def _start_betting_round(self, phase: Phase):
        self.phase = phase
        self.round_bet = [0, 0]
        self.current_bet = 0
        self.last_raise = self.config.min_bet
        self.raises = 0
        self.no_reraise = False
        self.acted = [False, False]
        self._next_actor(self.first)

    def _settled(self, p: int) -> bool:
        if self.folded[p] or self.all_in[p]:
            return True
        opp = self._opp(p)
        nothing_to_do = self.all_in[opp] or self.folded[opp]
        return self.round_bet[p] == self.current_bet and (self.acted[p] or nothing_to_do)

    def _next_actor(self, prefer: int):
        """Define quem age agora; se ninguém precisa agir, encerra a rodada."""
        for p in (prefer, self._opp(prefer)):
            if not self._settled(p):
                self.to_act = p
                return
        self.to_act = None
        self._end_betting_round()

    def _end_betting_round(self):
        if self.phase == Phase.BET1:
            self.phase = Phase.DRAW
            self.to_act = self.first
        else:
            self._showdown()

    def to_call(self, p: int) -> int:
        return max(0, self.current_bet - self.round_bet[p])

    def legal_actions(self, p: int) -> Dict:
        """Ações permitidas AGORA para o jogador p (vazio se não é a vez dele)."""
        if self.phase not in (Phase.BET1, Phase.BET2) or p != self.to_act:
            return {}
        stack, to_call, opp = self.stacks[p], self.to_call(p), self._opp(p)
        acts: Dict = {}
        opp_blocked = self.all_in[opp]
        if to_call == 0:
            acts["check"] = True
            if stack > 0 and not opp_blocked:
                acts["bet"] = (min(self.config.min_bet, stack), stack)
        else:
            acts["fold"] = True
            acts["call"] = min(to_call, stack)
            if stack > to_call and not opp_blocked and not self.no_reraise and self.raises < self.config.max_raises:
                max_to = self.round_bet[p] + stack
                min_to = min(self.current_bet + max(self.last_raise, self.config.min_bet), max_to)
                acts["raise"] = (min_to, max_to)           # valores "aumentar PARA"
        if stack > 0 and (("bet" in acts) or ("raise" in acts) or ("call" in acts and acts["call"] == stack)):
            acts["allin"] = stack
        return acts

    def act(self, p: int, kind: str, amount: Optional[int] = None) -> None:
        legal = self.legal_actions(p)
        if not legal:
            raise PokerError("Não é a vez deste jogador ou a rodada de apostas não está aberta.")
        if kind == "allin":
            stack = self.stacks[p]
            if "call" in legal and legal["call"] == stack and self.to_call(p) >= stack:
                kind = "call"
            elif "raise" in legal:
                kind, amount = "raise", self.round_bet[p] + stack
            elif "bet" in legal:
                kind, amount = "bet", stack
            else:
                raise PokerError("All-in não é permitido agora.")
        if kind not in legal:
            raise PokerError(f"Ação '{kind}' ilegal agora. Permitidas: {', '.join(legal)}.")

        opp = self._opp(p)
        if kind == "fold":
            self._log("fold", p)
            self.folded[p] = True
            self._finish_by_fold(p)
            return
        if kind == "check":
            self.acted[p] = True
            self._log("check", p)
        elif kind == "call":
            pay = legal["call"]
            self._commit(p, pay)
            self.acted[p] = True
            self._log("call", p, pay, all_in=self.all_in[p])
        elif kind == "bet":
            lo, hi = legal["bet"]
            if amount is None or not (lo <= amount <= hi):
                raise PokerError(f"Aposta inválida: use um valor entre {lo} e {hi}.")
            self._commit(p, amount)
            self.current_bet = self.round_bet[p]
            if amount >= self.config.min_bet:
                self.last_raise = amount
            else:
                self.no_reraise = True                       # all-in curto não reabre a ação
            self.acted[p] = True
            self.acted[opp] = False
            self._log("bet", p, amount, all_in=self.all_in[p])
        elif kind == "raise":
            lo, hi = legal["raise"]
            if amount is None or not (lo <= amount <= hi):
                raise PokerError(f"Aumento inválido: use um valor total entre {lo} e {hi}.")
            added = amount - self.round_bet[p]
            previous = self.current_bet
            self._commit(p, added)
            self.current_bet = self.round_bet[p]
            raise_size = self.current_bet - previous
            if raise_size >= max(self.last_raise, self.config.min_bet):
                self.last_raise = raise_size
                self.raises += 1
            else:
                self.no_reraise = True                       # all-in abaixo do mínimo: não reabre
            self.acted[p] = True
            self.acted[opp] = False
            self._log("raise", p, added, to=self.current_bet, all_in=self.all_in[p])
        self._next_actor(opp)

    # ------------------------------------------------------------------ troca de cartas
    def draw_turn(self) -> Optional[int]:
        """Quem deve trocar agora (None se a troca acabou)."""
        if self.phase != Phase.DRAW:
            return None
        for p in (self.first, self._opp(self.first)):
            if not self.folded[p] and not self.drawn[p]:
                return p
        return None

    def draw(self, p: int, discard_indices: List[int]) -> List[Card]:
        """Troca as cartas nas posições indicadas (0 a 5 cartas). Só UMA vez por mão. Devolve as cartas novas."""
        if self.phase != Phase.DRAW:
            raise PokerError("Não é a fase de troca de cartas.")
        if self.drawn[p]:
            raise PokerError("A troca de cartas só pode ser feita uma vez por mão.")
        if p != self.draw_turn():
            raise PokerError("Ainda não é a sua vez de trocar cartas.")
        idx = list(discard_indices)
        if len(set(idx)) != len(idx) or any((not isinstance(i, int)) or i < 0 or i > 4 for i in idx):
            raise PokerError("Índices de descarte inválidos (use posições de 0 a 4, sem repetir).")
        new = self.deck.deal(len(idx))
        hand = list(self.hole[p])
        for i, card in zip(sorted(idx), new):
            hand[i] = card
        self.hole[p] = hand
        self.drawn[p] = True
        self.discards[p] = len(idx)
        self._log("draw", p, len(idx))
        if self.draw_turn() is None:
            self._start_betting_round(Phase.BET2)
        else:
            self.to_act = self.draw_turn()
        return new

    # ------------------------------------------------------------------ fim da mão
    def _finish_by_fold(self, folder: int):
        winner = self._opp(folder)
        pot = self.pot
        stacks = list(self.stacks)
        stacks[winner] += pot
        amounts = [0, 0]
        amounts[winner] = pot
        self.result = HandResult("fold", winner, amounts, pot, folder=folder)
        self._complete(stacks)

    def _showdown(self):
        self.phase = Phase.SHOWDOWN
        m = min(self.contrib)
        refund = [c - m for c in self.contrib]            # excesso não igualado volta ao dono
        stacks = [self.stacks[i] + refund[i] for i in (0, 1)]
        pot = 2 * m
        ranks = [evaluate(self.hole[0]), evaluate(self.hole[1])]
        amounts = list(refund)
        if ranks[0] > ranks[1]:
            winner = HUMAN
            stacks[HUMAN] += pot
            amounts[HUMAN] += pot
        elif ranks[1] > ranks[0]:
            winner = ALICIA
            stacks[ALICIA] += pot
            amounts[ALICIA] += pot
        else:
            winner = None
            half, odd = divmod(pot, 2)
            for i in (0, 1):
                stacks[i] += half
                amounts[i] += half
            stacks[self.first] += odd                      # ficha ímpar: quem age primeiro
            amounts[self.first] += odd
        self.result = HandResult(
            "showdown", winner, amounts, pot, ranks,
            [describe(r) for r in ranks],
            [winning_cards(self.hole[i], ranks[i]) for i in (0, 1)],
            refunded=sum(refund),
        )
        self._log("showdown", -1, pot)
        self._complete(stacks)

    def _complete(self, stacks: List[int]):
        self.final_stacks = stacks
        self.phase = Phase.COMPLETE
        self.to_act = None
        assert sum(stacks) == sum(self.start_stacks), "Fichas não conservadas!"

    # ------------------------------------------------------------------ visão pública / da IA
    @property
    def reveals_opponent(self) -> bool:
        return self.result is not None and self.result.reason == "showdown"

    def public_state(self, viewer: int) -> Dict:
        """Tudo que `viewer` pode ver. As cartas do oponente só aparecem se foram reveladas em um showdown."""
        opp = self._opp(viewer)
        return {
            "phase": self.phase.name,
            "phase_label": self.phase.value,
            "pot": self.pot,
            "stacks": list(self.stacks if self.final_stacks is None else self.final_stacks),
            "round_bet": list(self.round_bet),
            "contrib": list(self.contrib),
            "current_bet": self.current_bet,
            "to_call": self.to_call(viewer) if self.phase in (Phase.BET1, Phase.BET2) else 0,
            "to_act": self.to_act,
            "draw_turn": self.draw_turn(),
            "dealer": self.dealer,
            "folded": list(self.folded),
            "all_in": list(self.all_in),
            "discards": list(self.discards),
            "legal": self.legal_actions(viewer),
            "my_cards": list(self.hole[viewer]),
            "opponent_cards": list(self.hole[opp]) if self.reveals_opponent else None,
            "events": [dict(e) for e in self.events],
            "result": self.result,
            "min_bet": self.config.min_bet,
        }

    def observation_for(self, viewer: int) -> "Observation":
        """O que a IA tem direito de saber. Não contém o baralho nem as cartas do oponente."""
        opp = self._opp(viewer)
        return Observation(
            my_cards=tuple(self.hole[viewer]),
            my_index=viewer,
            phase=self.phase.name,
            pot=self.pot,
            my_stack=self.stacks[viewer],
            opp_stack=self.stacks[opp],
            to_call=self.to_call(viewer) if self.phase in (Phase.BET1, Phase.BET2) else 0,
            current_bet=self.current_bet,
            my_round_bet=self.round_bet[viewer],
            opp_round_bet=self.round_bet[opp],
            legal=self.legal_actions(viewer),
            opp_discards=self.discards[opp],
            my_discards=self.discards[viewer],
            events=tuple(dict(e) for e in self.events),
            min_bet=self.config.min_bet,
            raises=self.raises,
            i_am_dealer=(self.dealer == viewer),
            starting_chips=sum(self.start_stacks) // 2,
        )


@dataclass(frozen=True)
class Observation:
    """Informação pública + as próprias cartas. É TUDO que a IA recebe."""
    my_cards: Tuple[Card, ...]
    my_index: int
    phase: str
    pot: int
    my_stack: int
    opp_stack: int
    to_call: int
    current_bet: int
    my_round_bet: int
    opp_round_bet: int
    legal: Dict
    opp_discards: Optional[int]
    my_discards: Optional[int]
    events: Tuple[Dict, ...]
    min_bet: int
    raises: int
    i_am_dealer: bool
    starting_chips: int


class Match:
    """Partida: 1000 fichas para cada um, mãos em sequência com o botão alternando, até alguém ficar sem fichas."""

    def __init__(self, config: Optional[Config] = None, rng=None):
        self.config = config or Config()
        self.config.validate()
        self._rng = rng
        self.hand: Optional[Hand] = None
        self.reset()

    def reset(self):
        self.stacks = [self.config.starting_chips, self.config.starting_chips]
        self.hands_played = 0
        self.dealer = random.SystemRandom().randrange(2) if self._rng is None else self._rng.randrange(2)
        self.hand = None
        self.wins = [0, 0]
        self.ties = 0

    @property
    def over(self) -> bool:
        return min(self.stacks) <= 0

    @property
    def winner(self) -> Optional[int]:
        if not self.over:
            return None
        return HUMAN if self.stacks[HUMAN] > 0 else ALICIA

    def start_hand(self, deck: Optional[Deck] = None) -> Hand:
        if self.over:
            raise PokerError("A partida terminou. Comece uma nova partida.")
        if self.hand is not None and not self.hand.is_over:
            raise PokerError("A mão anterior ainda não terminou.")
        if self.hands_played > 0:
            self.dealer = 1 - self.dealer
        self.hand = Hand(self.stacks, self.dealer, self.config, deck or Deck(self._rng))
        return self.hand

    def finish_hand(self):
        """Aplica o resultado da mão aos saldos da partida (chamar uma vez quando a mão acabar)."""
        h = self.hand
        if h is None or not h.is_over or h.final_stacks is None:
            raise PokerError("Não há mão encerrada para contabilizar.")
        if getattr(h, "_counted", False):
            return
        self.stacks = list(h.final_stacks)
        self.hands_played += 1
        if h.result.winner is None:
            self.ties += 1
        else:
            self.wins[h.result.winner] += 1
        h._counted = True
