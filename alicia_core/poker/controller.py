"""
Controlador do minigame: liga motor, IA da Alicia, reações e interface. SEM Qt (a janela só chama métodos e escuta callbacks).

Garantias:
  * Só o jogador humano age na vez dele; durante a vez da Alicia (`busy`) cliques são ignorados (sem ação dupla).
  * A jogada da Alicia é agendada UMA vez (`_ai_pending`) e cancelada se a partida for reiniciada/fechada (`_epoch`).
  * Se a IA lançar erro ou propuser ação ilegal, entra a estratégia de contingência: a mão nunca trava.
  * Nenhuma informação oculta chega à interface: o snapshot só mostra as cartas da Alicia após um showdown.
"""

from __future__ import annotations

import json
import random
from typing import Callable, Dict, List, Optional

from .ai import AliciaAI
from .engine import ALICIA, HUMAN, Config, Hand, Match, Phase, PokerError
from .reactions import Reaction, ReactionEngine


def format_event(e: Dict, you: str = "Você", me: str = "Alicia") -> str:
    who = you if e["player"] == HUMAN else me
    t, a = e["type"], e["amount"]
    if t == "ante":
        return f"{who} pagou o ante ({a})"
    if t == "bet":
        return f"{who} apostou {a}" + (" (all-in)" if e.get("all_in") else "")
    if t == "raise":
        return f"{who} aumentou para {e.get('to', a)}" + (" (all-in)" if e.get("all_in") else "")
    if t == "call":
        return f"{who} pagou {a}" + (" (all-in)" if e.get("all_in") else "")
    if t == "check":
        return f"{who} passou"
    if t == "fold":
        return f"{who} desistiu"
    if t == "draw":
        return f"{who} trocou {a} carta(s)" if a else f"{who} ficou com as cinco"
    if t == "showdown":
        return "Showdown"
    return t


class DbStatsStore:
    """Guarda placar vitalício no banco da Alicia (perfil 'poker_stats')."""

    def __init__(self, db):
        self.db = db

    def load(self) -> Dict:
        try:
            return json.loads(self.db.get_profile("poker_stats") or "{}")
        except Exception:
            return {}

    def save(self, stats: Dict):
        try:
            self.db.set_profile("poker_stats", json.dumps(stats))
        except Exception:
            pass


class PokerController:
    def __init__(
        self,
        ai: Optional[AliciaAI] = None,
        reactions: Optional[ReactionEngine] = None,
        scheduler: Optional[Callable[[int, Callable], None]] = None,
        rng: Optional[random.Random] = None,
        config: Optional[Config] = None,
        personality_getter: Callable[[], str] = lambda: "normal",
        name_getter: Callable[[], str] = lambda: "",
        stats_store=None,
        think_ms=(700, 1500),
    ):
        self.rng = rng
        self.ai = ai or AliciaAI(rng=random.Random())
        self.reactions = reactions or ReactionEngine()
        self.schedule = scheduler or (lambda ms, fn: fn())
        self.config = config or Config()
        self.personality = personality_getter
        self.name = name_getter
        self.stats_store = stats_store
        self.think_ms = think_ms
        self.on_change: Callable[[Dict], None] = lambda snap: None
        self.on_say: Callable[[Reaction], None] = lambda r: None
        self.on_error: Callable[[str], None] = lambda msg: None
        # Narrador opcional (conversa livre durante a partida): recebe (evento, fatos, fala_pronta) e fica responsável por
        # entregar a fala. Sem narrador, vale a fala pronta de reactions.py, como sempre foi.
        self.narrator: Optional[Callable[[str, str, Reaction], None]] = None
        self.context_text: str = ""         # resumo da mesa (só informação pública) para a Alicia conversar
        self.match = Match(self.config, rng=rng)
        self.hand: Optional[Hand] = None
        self.busy = False
        self._ai_pending = False
        self._epoch = 0
        self._summary: Optional[Dict] = None
        self._ai_bluffed = False
        self._hand_started = False
        self.match_over_info: Optional[Dict] = None
        self.speech_log: List[Reaction] = []
        self.ai_errors = 0

    # ------------------------------------------------------------------ ciclo da partida
    def new_match(self):
        self._epoch += 1                               # cancela jogadas agendadas da partida anterior
        self._ai_pending = False
        self.match.reset()
        self.ai.new_match()
        self.hand, self.busy, self._summary, self.match_over_info = None, False, None, None
        self._hand_started = False
        self._say(self.reactions.say("match_start", self.personality(), nome=self.name()), "match_start",
                  f"Uma partida nova começou: {self.config.starting_chips} fichas para cada um.")
        self.start_hand()

    def start_hand(self):
        if self.match.over or (self.hand is not None and not self.hand.is_over) or self._ai_pending:
            return False
        self._summary = None
        self._ai_bluffed = False
        self.hand = self.match.start_hand()
        self._hand_started = True
        cls = self.ai.hand_class(self.hand.hole[ALICIA])          # a PRÓPRIA mão dela: informação legítima
        rotulo = {"strong": "forte", "medium": "média", "weak": "fraca"}.get(cls, "média")
        self._say(self.reactions.for_deal(cls, self.personality(), nome=self.name()), "deal",
                  f"Mão {self.match.hands_played + 1} distribuída. Sua mão é {rotulo} (segredo: reaja de forma vaga, sem citar cartas).")
        self._pump()
        return True

    def close(self):
        self._epoch += 1
        self._ai_pending = False

    # ------------------------------------------------------------------ ações do jogador
    def player_action(self, kind: str, amount: Optional[int] = None) -> bool:
        h = self.hand
        if h is None or h.is_over or self.busy or self._ai_pending or h.to_act != HUMAN:
            return False
        pot_before = h.pot
        try:
            h.act(HUMAN, kind, amount)
        except PokerError as e:
            self.on_error(str(e))
            return False
        ev = h.events[-1] if h.events else {}
        if ev.get("player") == HUMAN:
            self._say(self.reactions.for_player_action(
                ev["type"], ev.get("amount", 0), pot_before, bool(ev.get("all_in")), self.personality(), nome=self.name()),
                "player_action", f"{self._fmt(ev).capitalize()}. Pote antes da jogada: {pot_before}.")
        self._pump()
        return True

    def player_draw(self, indices: List[int]) -> bool:
        h = self.hand
        if h is None or h.is_over or self.busy or self._ai_pending or h.draw_turn() != HUMAN:
            return False
        try:
            h.draw(HUMAN, list(indices))
        except PokerError as e:
            self.on_error(str(e))
            return False
        self._say(self.reactions.for_player_draw(len(indices), self.personality(), nome=self.name()), "player_draw",
                  f"O jogador trocou {len(indices)} carta(s)." if indices else "O jogador ficou com as cinco cartas, sem trocar.")
        self._pump()
        return True

    # ------------------------------------------------------------------ fluxo
    def _pump(self):
        h = self.hand
        if h is None:
            self._emit()
            return
        if h.is_over:
            self._finish_hand()
            return
        ai_turn = (h.phase in (Phase.BET1, Phase.BET2) and h.to_act == ALICIA) or \
                  (h.phase == Phase.DRAW and h.draw_turn() == ALICIA)
        self.busy = ai_turn
        self._emit()
        if ai_turn and not self._ai_pending:
            self._ai_pending = True
            epoch = self._epoch
            delay = self.rng.randint(*self.think_ms) if self.rng else random.randint(*self.think_ms)
            self.schedule(delay, lambda: self._ai_step(epoch))

    def _ai_step(self, epoch: int):
        if epoch != self._epoch or self.hand is None or self.hand.is_over:
            self._ai_pending = False
            return
        self._ai_pending = False
        h = self.hand
        if h.phase == Phase.DRAW and h.draw_turn() == ALICIA:
            self._ai_draw()
        elif h.phase in (Phase.BET1, Phase.BET2) and h.to_act == ALICIA:
            self._ai_act()
        self._pump()

    def _ai_draw(self):
        h = self.hand
        obs = h.observation_for(ALICIA)
        try:
            idx, _ = self.ai.choose_discards(obs)
            h.draw(ALICIA, idx)
        except Exception:
            self.ai_errors += 1
            h.draw(ALICIA, [])                          # contingência: fica com as cinco
        n_ai = h.discards[ALICIA] or 0
        self._say(self.reactions.for_ai_draw(n_ai, self.personality(), nome=self.name()), "ai_draw",
                  f"Você (Alicia) trocou {n_ai} carta(s)." if n_ai else "Você (Alicia) ficou com as cinco cartas.")

    def _ai_act(self):
        h = self.hand
        obs = h.observation_for(ALICIA)
        try:
            d = self.ai.decide(obs)
            if d.intent in ("bluff", "raise_bluff"):
                self._ai_bluffed = True
            h.act(ALICIA, d.kind, d.amount)
            kind = d.kind
        except Exception:
            self.ai_errors += 1
            fb = AliciaAI.fallback(h.observation_for(ALICIA))
            h.act(ALICIA, fb.kind, fb.amount)
            kind = fb.kind
        last = h.events[-1] if h.events else {}
        fatos = (f"Você (Alicia) fez: {self._fmt(last)}. Pote agora: {h.pot}." if last.get("player") == ALICIA
                 else f"Você (Alicia) agiu ({kind}). Pote agora: {h.pot}.")
        self._say(self.reactions.for_ai_action(kind if kind in ("bet", "raise", "call", "check", "fold", "allin") else "call",
                                               self.personality(), nome=self.name()), "ai_action", fatos)

    def _finish_hand(self):
        h = self.hand
        self.busy = False
        self.match.finish_hand()
        res = h.result
        revealed = h.hole[HUMAN] if res.reason == "showdown" else None
        self.ai.observe_hand_end(h.events, ALICIA, revealed)
        outcome = "split" if res.winner is None else ("player_win" if res.winner == HUMAN else "ai_win")
        total = 2 * self.config.starting_chips
        won = res.amounts[res.winner] if res.winner is not None else 0
        ctx = {"nome": self.name()}
        if res.reason == "showdown":
            ctx["minha_mao"] = res.descriptions[ALICIA].lower()
            ctx["sua_mao"] = res.descriptions[HUMAN].lower()
        ai_bluffed_win = self._ai_bluffed and res.winner == ALICIA and res.reason == "fold"
        self._say(self.reactions.for_hand_end(outcome, res.reason, res.pot, total, ai_bluffed_win, self.personality(), **ctx),
                  "hand_end", "A mão acabou. " + self._result_text(res)
                  + (" Você (Alicia) tinha blefado e o jogador desistiu." if ai_bluffed_win else ""))
        self._summary = self._build_summary(res, outcome)
        stats = self.stats_store.load() if self.stats_store else {}
        stats["hands"] = stats.get("hands", 0) + 1
        if self.match.over:
            winner = "player" if self.match.winner == HUMAN else "alicia"
            stats["matches"] = stats.get("matches", 0) + 1
            stats[f"{winner}_match_wins"] = stats.get(f"{winner}_match_wins", 0) + 1
            self.match_over_info = {"winner": winner, "stats": dict(stats)}
            self._say(self.reactions.for_match_end("player" if winner == "player" else "ai", self.personality(), nome=self.name()),
                      "match_end", "A partida acabou: venceu " + ("o jogador." if winner == "player" else "Alicia (você)."))
        else:
            start = self.config.starting_chips
            if self.match.stacks[ALICIA] < 0.25 * start:
                self._say(self.reactions.say("ai_low_chips", self.personality(), nome=self.name()), "ai_low_chips",
                          f"Você (Alicia) está com poucas fichas: {self.match.stacks[ALICIA]}.")
            elif self.match.stacks[HUMAN] < 0.2 * start:
                self._say(self.reactions.say("ai_near_win", self.personality(), nome=self.name()), "ai_near_win",
                          f"O jogador está com poucas fichas: {self.match.stacks[HUMAN]}.")
        if self.stats_store:
            self.stats_store.save(stats)
        self._emit()

    def _build_summary(self, res, outcome: str) -> Dict:
        if res.winner is None:
            text = f"Empate! O pote de {res.pot} foi dividido."
        else:
            who = "Você ganhou" if res.winner == HUMAN else "Alicia ganhou"
            how = " (o outro desistiu)" if res.reason == "fold" else f" com {res.descriptions[res.winner]}"
            text = f"{who} o pote de {res.pot} fichas{how}."
        out = {"outcome": outcome, "reason": res.reason, "pot": res.pot, "text": text, "refunded": res.refunded}
        if res.reason == "showdown":
            out["descriptions"] = list(res.descriptions)
            hole = self.hand.hole
            out["win_idx"] = [[i for i, c in enumerate(hole[p]) if c in res.best_cards[p]] for p in (HUMAN, ALICIA)]
        return out

    def _say(self, reaction: Optional[Reaction], event: str = "", facts: str = ""):
        if not reaction:
            return
        self.speech_log.append(reaction)
        if self.narrator is not None and facts:
            try:
                self.narrator(event, facts, reaction)
                return
            except Exception:
                pass                                   # narrador com defeito: cai na fala pronta
        self.on_say(reaction)

    # ------------------------------------------------------------------ contexto para a conversa (SÓ o que é público/legítimo)
    NO_ONE = "o jogador"

    def _fmt(self, e: Dict) -> str:
        return format_event(e, you=self.NO_ONE, me="Alicia")

    def _result_text(self, res) -> str:
        if res.winner is None:
            txt = f"Empate: o pote de {res.pot} fichas foi dividido."
        else:
            who = self.NO_ONE if res.winner == HUMAN else "Alicia"
            how = " porque o outro desistiu" if res.reason == "fold" else f" com {res.descriptions[res.winner]}"
            txt = f"{who.capitalize()} ganhou o pote de {res.pot} fichas{how}."
        if res.reason == "showdown":
            txt += f" Showdown: {self.NO_ONE} tinha {res.descriptions[HUMAN]}; Alicia tinha {res.descriptions[ALICIA]}."
        return txt

    def table_context(self) -> str:
        """Resumo da mesa para a Alicia falar sobre a partida. NUNCA inclui as cartas do jogador nem as dela."""
        m, h = self.match, self.hand
        nome = (self.name() or "").strip()
        lines = [f"Jogador: {nome or 'sem nome'}. Fichas: jogador {m.stacks[HUMAN]}, Alicia {m.stacks[ALICIA]}. "
                 f"Mãos jogadas: {m.hands_played}. Mãos vencidas: jogador {m.wins[HUMAN]}, Alicia {m.wins[ALICIA]}."]
        if h is None:
            lines.append("A mão ainda não começou.")
        else:
            lines.append(f"Mão atual: fase '{h.phase.value}'. Pote: {h.pot}.")
            if not h.is_over:
                lines.append(f"Vez de: {'Alicia' if h.to_act == ALICIA else self.NO_ONE}.")
            evs = [self._fmt(e) for e in h.events if e.get("type") != "ante"]
            if evs:
                lines.append("Jogadas desta mão: " + "; ".join(evs[-12:]) + ".")
            if not h.is_over:
                cls = {"strong": "forte", "medium": "média", "weak": "fraca"}.get(self.ai.hand_class(h.hole[ALICIA]), "média")
                lines.append(f"Sua mão (SEGREDO, não revele nem cite cartas): {cls}.")
            elif h.result is not None:
                lines.append("Resultado da mão: " + self._result_text(h.result))
        if self.match_over_info:
            lines.append("A partida terminou: venceu " + ("o jogador." if self.match_over_info["winner"] == "player" else "Alicia."))
        return "\n".join(lines)

    # ------------------------------------------------------------------ snapshot para a interface
    def _emit(self):
        try:
            self.context_text = self.table_context()
        except Exception:
            pass                                           # contexto é um extra: nunca pode travar a mesa
        self.on_change(self.snapshot())

    def snapshot(self) -> Dict:
        h = self.hand
        m = self.match
        snap: Dict = {
            "match_stacks": list(m.stacks), "hands_played": m.hands_played, "wins": list(m.wins),
            "match_over": m.over, "match_info": self.match_over_info, "busy": self.busy,
            "has_hand": h is not None, "summary": self._summary, "config": self.config,
            "can_next_hand": bool(h is not None and h.is_over and not m.over and self._summary is not None),
            "personality": self.personality(),
        }
        if h is None:
            snap.update(phase="NONE", phase_label="aguardando", pot=0, stacks=list(m.stacks), legal={}, my_cards=[], alicia_cards=None,
                        history=[], to_act=None, draw_turn=None, discards=[None, None], dealer=m.dealer, round_bet=[0, 0],
                        to_call=0, all_in=[False, False], folded=[False, False], player_turn=False, player_draw_turn=False)
            return snap
        pub = h.public_state(HUMAN)
        snap.update(
            phase=pub["phase"], phase_label=pub["phase_label"], pot=pub["pot"], stacks=pub["stacks"], legal=pub["legal"],
            my_cards=pub["my_cards"], alicia_cards=pub["opponent_cards"], history=[format_event(e) for e in pub["events"]],
            to_act=pub["to_act"] if pub["phase"] in ("BET1", "BET2") else None, draw_turn=pub["draw_turn"], discards=pub["discards"], dealer=pub["dealer"],
            round_bet=pub["round_bet"], to_call=pub["to_call"], all_in=pub["all_in"], folded=pub["folded"],
            player_turn=(pub["phase"] in ("BET1", "BET2") and pub["to_act"] == HUMAN and not self.busy and not h.is_over),
            player_draw_turn=(pub["draw_turn"] == HUMAN and not self.busy and not h.is_over),
            current_bet=pub["current_bet"],
        )
        return snap
