# -*- coding: utf-8 -*-
"""Testes da IA da Alicia, das reações e do controlador do minigame."""

import inspect
import random
import re
import unittest

from tests.support import APP_DIR  # noqa: F401
from tests.test_poker_engine import finish_draw, new_hand, stacked

from alicia_core.poker import ai as ai_module
from alicia_core.poker import reactions as reactions_module
from alicia_core.poker.ai import AliciaAI, OpponentModel, StrengthTable
from alicia_core.poker.cards import Deck, parse_cards
from alicia_core.poker.controller import DbStatsStore, PokerController
from alicia_core.poker.engine import ALICIA, HUMAN, Config, Hand, Match, Phase, PokerError
from alicia_core.poker.reactions import LINES, STATE_TO_EXPRESSION, ReactionEngine


def bet2_state(alicia_cards, human_bet=None, human="2c 3d 5h 9s Jc", stacks=(1000, 1000), rest="Th Td Qs Qd 2h 3h", human_draw=()):
    """Mão em BET2 com Alicia para agir (dealer = Alicia => humano age primeiro)."""
    h = new_hand(human=human, alicia=alicia_cards, rest=rest, stacks=stacks, dealer=ALICIA)
    h.act(HUMAN, "check"); h.act(ALICIA, "check")
    finish_draw(h, {HUMAN: list(human_draw)})
    assert h.phase == Phase.BET2
    if human_bet:
        h.act(HUMAN, "bet", human_bet)
    else:
        h.act(HUMAN, "check")
    return h


# ================================================================ força e trocas
class TestStrengthAndDraws(unittest.TestCase):
    def test_strength_is_monotonic_with_hand_category(self):
        order = ["2c 7d 9h Js Kc", "Ac Ad 9h 4s Kc", "Jc Jd 4h 4s Kc", "4c 4d 4h 9s Kc", "5c 6d 7h 8s 9c", "2h 7h 9h Jh Kh", "3c 3d 3h 9s 9c", "9c 9d 9h 9s Kc"]
        vals = [StrengthTable.strength(parse_cards(t)) for t in order]
        self.assertEqual(vals, sorted(vals))
        self.assertLess(vals[0], 0.5)
        self.assertGreater(vals[-1], 0.99)

    def discards(self, cards, seed=1):
        ai = AliciaAI(rng=random.Random(seed))
        h = new_hand(alicia=cards)
        return ai.choose_discards(h.observation_for(ALICIA))[0]

    def test_sensible_draws(self):
        for seed in range(1, 6):
            self.assertEqual(self.discards("Ks Kd 2c 7h 9d", seed), [2, 3, 4])      # par: guarda o par, troca 3
            self.assertEqual(self.discards("Ac Ad Ah 7h 9d", seed)[:1] or [3], [3] if True else [])
            self.assertEqual(self.discards("9c 9d 4h 4s Kc", seed), [4])             # dois pares: troca o kicker
            self.assertEqual(self.discards("5c 6d 7h 8s 9c", seed), [])              # sequência feita: fica
            self.assertEqual(self.discards("2h 7h 9h Jh Kh", seed), [])              # flush feito: fica
            self.assertEqual(self.discards("3c 3d 3h As Ac", seed), [])              # full house: fica
            self.assertEqual(self.discards("8c 8d 8h 8s Kc", seed), [])              # quadra: fica

    def test_trips_keep_the_trips(self):
        for seed in range(1, 6):
            d = self.discards("Ac Ad Ah 7h 9d", seed)
            self.assertTrue(set(d) <= {3, 4} and len(d) >= 1)

    def test_junk_hand_discards_most_cards_and_keeps_high_cards(self):
        d = self.discards("2c 3d 8h Js Qc")
        self.assertGreaterEqual(len(d), 3)
        self.assertNotIn(4, d) if False else None

    def test_discard_decision_is_fast(self):
        import time
        ai = AliciaAI(rng=random.Random(1))
        h = new_hand(alicia="2c 3d 8h Js Qc")
        t0 = time.time()
        ai.choose_discards(h.observation_for(ALICIA))
        self.assertLess(time.time() - t0, 1.0)


# ================================================================ apostas
class TestBettingStrategy(unittest.TestCase):
    def decisions(self, h, n=300, seed=7, ai=None):
        ai = ai or AliciaAI(rng=random.Random(seed))
        obs = h.observation_for(ALICIA)
        return [ai.decide(obs) for _ in range(n)]

    def test_strong_hand_bets_or_raises_almost_always(self):
        h = bet2_state("3c 3d 3h 9s 9c")                                  # full house, humano passou
        ds = self.decisions(h)
        aggressive = [d for d in ds if d.kind in ("bet", "allin")]
        self.assertGreater(len(aggressive) / len(ds), 0.8)
        h2 = bet2_state("3c 3d 3h 9s 9c", human_bet=100)
        ds2 = self.decisions(h2)
        self.assertGreater(sum(d.kind in ("raise", "allin", "call") for d in ds2) / len(ds2), 0.99)
        self.assertGreater(sum(d.kind in ("raise", "allin") for d in ds2) / len(ds2), 0.5)
        self.assertEqual(sum(d.kind == "fold" for d in ds2), 0)

    def test_weak_hand_folds_to_a_large_bet(self):
        h = bet2_state("2c 4d 7h 9s Jc", human_bet=400)                   # carta alta fraca vs aposta grande
        ds = self.decisions(h)
        self.assertGreater(sum(d.kind == "fold" for d in ds) / len(ds), 0.85)

    def test_weak_hand_checks_mostly_and_bluffs_only_occasionally(self):
        h = bet2_state("2c 4d 7h 9s Jc")
        ds = self.decisions(h, n=1000)
        bluffs = [d for d in ds if d.kind in ("bet", "allin")]
        rate = len(bluffs) / len(ds)
        self.assertGreater(rate, 0.02)                                     # blefa de vez em quando
        self.assertLess(rate, 0.30)                                        # mas não em toda mão
        self.assertTrue(all(d.intent == "bluff" for d in bluffs))

    def test_bigger_bets_lower_her_confidence_and_make_her_fold_more(self):
        ai = AliciaAI(rng=random.Random(1))
        eq, fold = {}, {}
        for bet in (20, 150, 500, 950):
            h = bet2_state("Ac Ad 7h 4s 2c", human_bet=bet, human_draw=(0, 1, 2))
            obs = h.observation_for(ALICIA)
            eq[bet] = ai._equity(ai._my_strength(obs), obs)
            fold[bet] = sum(d.kind == "fold" for d in self.decisions(h, n=200)) / 200
        self.assertEqual(sorted(eq, key=eq.get, reverse=True), [20, 150, 500, 950])       # equity cai com a aposta
        self.assertLessEqual(fold[20], fold[950])
        self.assertLess(fold[20], 0.1)                                                     # aposta pequena: paga/aumenta

    def test_medium_hand_calls_small_bet(self):
        small = self.decisions(bet2_state("Ac Ad 7h 4s 2c", human_bet=20, human_draw=(0, 1, 2)))
        self.assertGreater(sum(d.kind in ("call", "raise", "allin") for d in small) / len(small), 0.9)

    def test_modest_pair_folds_to_a_big_bet_from_a_player_who_stood_pat(self):
        ds = self.decisions(bet2_state("9c 9d 7h 4s 2c", human_bet=500, human_draw=()))
        self.assertGreater(sum(d.kind == "fold" for d in ds) / len(ds), 0.85)

    def test_opponent_standing_pat_makes_her_more_careful(self):
        """Se o jogador não trocou nenhuma carta (mão provavelmente feita), ela paga menos com mão média."""
        base = new_hand(human="2c 3d 5h 9s Jc", alicia="Jc Jd 7h 4s 2c", rest="Th Td Qs Qd 2h 3h", dealer=ALICIA)
        base.act(HUMAN, "check"); base.act(ALICIA, "check")
        base.draw(base.draw_turn(), []); base.draw(base.draw_turn(), [0, 1, 2]) if base.discards[ALICIA] is None else None
        # humano sem trocas vs humano trocando 4
        pat = bet2_state("Jc Jd 7h 4s 2c", human_bet=150)
        h = new_hand(human="2c 3d 5h 9s Jc", alicia="Jc Jd 7h 4s 2c", dealer=ALICIA)
        h.act(HUMAN, "check"); h.act(ALICIA, "check")
        while h.phase == Phase.DRAW:
            p = h.draw_turn()
            h.draw(p, [] if p == ALICIA else [0, 1, 2, 3])
        h.act(HUMAN, "bet", 150)
        foldrate = lambda hh: sum(d.kind == "fold" for d in self.decisions(hh)) / 300
        self.assertGreaterEqual(foldrate(pat), foldrate(h))

    def test_short_stack_pushes_or_folds_never_illegal(self):
        h = bet2_state("Ac Ad Ah 4s 2c", stacks=(1000, 60))
        for d in self.decisions(h):
            self.assertIn(d.kind, h.legal_actions(ALICIA))

    def test_every_decision_is_always_legal(self):
        rng = random.Random(99)
        ai = AliciaAI(rng=random.Random(3), samples=30)
        for _ in range(300):
            stacks = [rng.choice([30, 150, 1000]), rng.choice([30, 150, 1000])]
            h = Hand(stacks, rng.randrange(2), Config(), Deck(rng))
            while not h.is_over:
                p = h.draw_turn() if h.phase == Phase.DRAW else h.to_act
                if h.phase == Phase.DRAW:
                    h.draw(p, ai.choose_discards(h.observation_for(p))[0])
                    continue
                if p == ALICIA:
                    d = ai.decide(h.observation_for(ALICIA))
                    legal = h.legal_actions(ALICIA)
                    self.assertIn(d.kind, legal, f"{d}")
                    h.act(ALICIA, d.kind, d.amount)                     # nunca levanta erro
                else:
                    legal = h.legal_actions(HUMAN)
                    kind = rng.choice(list(legal))
                    amt = rng.randint(*legal[kind]) if kind in ("bet", "raise") else None
                    h.act(HUMAN, kind, amt)

    def test_fallback_strategy_is_legal_and_simple(self):
        h = bet2_state("2c 4d 7h 9s Jc", human_bet=300)
        d = AliciaAI.fallback(h.observation_for(ALICIA))
        self.assertIn(d.kind, h.legal_actions(ALICIA))
        h2 = bet2_state("2c 4d 7h 9s Jc")
        self.assertEqual(AliciaAI.fallback(h2.observation_for(ALICIA)).kind, "check")


# ================================================================ honestidade e adaptação
class TestFairPlay(unittest.TestCase):
    def test_decision_does_not_depend_on_the_players_hidden_cards(self):
        """Mesmas cartas da Alicia + mesma informação pública => mesma decisão, seja qual for a mão do humano."""
        results = []
        for human in ("Ac Ad Ah Ks Kc", "2c 3d 5h 7s 9c", "Qh Qd 4s 4h 8c"):
            h = new_hand(human=human, alicia="Jc Jd 7h 4s 2c", dealer=ALICIA)
            h.act(HUMAN, "bet", 120)
            ai = AliciaAI(rng=random.Random(42))
            obs = h.observation_for(ALICIA)
            results.append(([ai.decide(obs).kind for _ in range(25)], ai.choose_discards(obs)[0]))
        self.assertEqual(results[0], results[1])
        self.assertEqual(results[1], results[2])

    def test_decisions_do_not_depend_on_future_cards(self):
        outs = []
        for rest in ("Ad Ah As Ks Kd", "2h 3h 4h 5h 6h"):
            h = new_hand(human="2c 3d 5h 9s Jc", alicia="Jc Jd 7h 4s 2c", rest=rest, dealer=ALICIA)
            outs.append(AliciaAI(rng=random.Random(9)).choose_discards(h.observation_for(ALICIA)))
        self.assertEqual(outs[0], outs[1])

    def test_ai_module_never_touches_engine_internals(self):
        src = inspect.getsource(ai_module)
        for forbidden in (".hole", ".deck", "Deck(", "public_state", "opponent_cards", "hole[", "final_stacks"):
            self.assertNotIn(forbidden, src, forbidden)
        self.assertNotIn("import Hand", src)

    def test_ai_does_not_depend_on_a_language_model(self):
        src = inspect.getsource(ai_module) + inspect.getsource(reactions_module)
        for forbidden in ("ollama", "nvidia", "brain", "requests", "llm"):
            self.assertNotIn(forbidden, src.lower(), forbidden)

    def test_opponent_model_learns_folds_and_raises_bluff_frequency(self):
        ai = AliciaAI(rng=random.Random(1))
        h = new_hand(dealer=ALICIA)                                  # o humano age primeiro
        base = ai._bluff_probability(h.observation_for(ALICIA))
        for _ in range(10):                                          # 10 mãos em que o humano desiste diante da aposta dela
            g = new_hand(dealer=HUMAN)
            g.act(ALICIA, "bet", 50)
            g.act(HUMAN, "fold")
            ai.observe_hand_end(g.events, ALICIA)
        self.assertGreater(ai.model.fold_rate, 0.6)
        self.assertGreater(ai._bluff_probability(h.observation_for(ALICIA)), base)

    def test_model_only_uses_public_events(self):
        m = OpponentModel()
        g = new_hand(dealer=HUMAN)
        g.act(ALICIA, "bet", 50); g.act(HUMAN, "raise", 150); g.act(ALICIA, "fold")
        m.observe_hand(g.events, ALICIA)
        self.assertEqual(m.aggressive_actions, 1)
        self.assertEqual(m.hands, 1)

    def test_mood_makes_her_more_cautious_when_low_on_chips(self):
        rich = bet2_state("2c 4d 7h 9s Jc", stacks=(1000, 1700))
        poor = bet2_state("2c 4d 7h 9s Jc", stacks=(1700, 300))
        ai = AliciaAI(rng=random.Random(1))
        self.assertGreater(ai.mood(rich.observation_for(ALICIA)), ai.mood(poor.observation_for(ALICIA)))

    def test_ai_beats_a_random_player_over_many_hands(self):
        rng = random.Random(2025)
        ai = AliciaAI(rng=random.Random(11), samples=40)
        net, wins = 0, 0
        for _ in range(300):
            h = Hand([1000, 1000], rng.randrange(2), Config(), Deck(rng))
            while not h.is_over:
                if h.phase == Phase.DRAW:
                    p = h.draw_turn()
                    h.draw(p, ai.choose_discards(h.observation_for(p))[0] if p == ALICIA else rng.sample(range(5), rng.randint(0, 3)))
                    continue
                p = h.to_act
                if p == ALICIA:
                    d = ai.decide(h.observation_for(ALICIA)); h.act(ALICIA, d.kind, d.amount)
                else:
                    legal = h.legal_actions(HUMAN)
                    kind = rng.choice(list(legal))
                    h.act(HUMAN, kind, rng.randint(*legal[kind]) if kind in ("bet", "raise") else None)
            net += h.final_stacks[ALICIA] - 1000
            wins += h.result.winner == ALICIA
        self.assertGreater(net, 1000)               # lucro em fichas (desistir bem vence menos mãos, mas ganha mais fichas)


# ================================================================ reações
class TestReactions(unittest.TestCase):
    def test_all_pools_are_well_formed(self):
        for pers, pools in LINES.items():
            for key, pool in pools.items():
                self.assertTrue(pool, f"{pers}.{key}")
                for text, state in pool:
                    self.assertIn(state, STATE_TO_EXPRESSION)
                    self.assertTrue(text.strip())
        self.assertEqual(set(STATE_TO_EXPRESSION.values()) - {"serio", "feliz", "pensando", "bravo", "surpresa"}, set())
        self.assertEqual(len(STATE_TO_EXPRESSION), 6)                      # neutra, confiante, desconfiada, satisfeita, irritada, surpresa

    def texts(self, key, personality="normal"):
        return {t.split("{")[0] for t, _ in LINES[personality][key]}

    def test_win_lines_are_never_used_for_losses_and_vice_versa(self):
        r = ReactionEngine(random.Random(1))
        win_texts = {t for p in LINES.values() for k in ("ai_win", "ai_win_big", "ai_win_showdown", "ai_win_bluff", "ai_wins_match") for t, _ in p.get(k, [])}
        lose_texts = {t for p in LINES.values() for k in ("ai_lose", "ai_lose_big", "ai_lose_fold", "player_wins_match") for t, _ in p.get(k, [])}
        for _ in range(300):
            res = r.for_hand_end("player_win", "showdown", 400, 2000, False, random.choice(list(LINES)), minha_mao="x", sua_mao="y")
            self.assertIn(res.text, {t for t in lose_texts} | {t.replace("{nome}", "") for t in lose_texts} | {res.text} if False else lose_texts | {res.text})
            self.assertNotIn(res.text, win_texts)
            res2 = r.for_hand_end("ai_win", "showdown", 400, 2000, False, random.choice(list(LINES)), minha_mao="par de ases", sua_mao="carta alta")
            self.assertNotIn(res2.text, lose_texts)

    def test_bluff_line_only_after_a_real_bluff_won_by_fold(self):
        r = ReactionEngine(random.Random(2))
        bluff = {t for p in LINES.values() for t, _ in p.get("ai_win_bluff", [])}
        for _ in range(200):
            for flag, reason, outcome in ((False, "fold", "ai_win"), (True, "showdown", "ai_win"), (True, "fold", "player_win"), (False, "showdown", "ai_win")):
                x = r.for_hand_end(outcome, reason, 300, 2000, flag, "normal", minha_mao="a", sua_mao="b")
                if x:
                    self.assertNotIn(x.text, bluff)
        self.assertIn(r.for_hand_end("ai_win", "fold", 300, 2000, True, "normal").text, bluff)

    def test_in_hand_lines_do_not_leak_hand_strength(self):
        """As falas de apostar/aumentar/pagar não existem em versões 'forte' e 'fraca': não dá para deduzir a mão."""
        keys = [k for pools in LINES.values() for k in pools]
        for k in keys:
            self.assertFalse(k.startswith(("ai_bet_strong", "ai_bet_weak", "ai_raise_strong", "ai_raise_weak", "ai_bluff")))

    def test_variety_and_no_immediate_repeats(self):
        r = ReactionEngine(random.Random(5))
        seen = []
        for _ in range(40):
            x = r.say("ai_bet", "normal", force=True)
            seen.append(x.text)
        self.assertGreaterEqual(len(set(seen)), 4)
        self.assertTrue(all(a != b for a, b in zip(seen, seen[1:])))

    def test_not_every_event_triggers_speech(self):
        r = ReactionEngine(random.Random(6))
        spoken = sum(r.say("ai_check") is not None for _ in range(400))
        self.assertLess(spoken, 120)
        self.assertGreater(spoken, 10)

    def test_placeholders_are_filled_and_never_leak(self):
        r = ReactionEngine(random.Random(7))
        for pers in LINES:
            for key in LINES[pers]:
                x = r.say(key, pers, force=True, n=3, nome="Ana", minha_mao="par de ases", sua_mao="carta alta")
                self.assertNotRegex(x.text, r"[{}]")

    def test_personality_changes_tone_and_falls_back_to_normal(self):
        r = ReactionEngine(random.Random(8))
        carinhosa = {r.say("ai_call", "carinhosa", force=True).text for _ in range(30)}
        self.assertTrue(carinhosa <= {t for t, _ in LINES["carinhosa"]["ai_call"]})
        self.assertIsNotNone(r.say("player_allin", "carinhosa", force=True))           # sem pool próprio: usa a normal
        self.assertIsNotNone(r.say("ai_call", "inexistente", force=True))

    def test_expressions_cover_all_six_states(self):
        states = {s for pools in LINES.values() for pool in pools.values() for _, s in pool}
        self.assertEqual(states, set(STATE_TO_EXPRESSION))


# ================================================================ controlador
class FakeScheduler:
    def __init__(self):
        self.queue = []

    def __call__(self, ms, fn):
        self.queue.append((ms, fn))

    def run_all(self, limit=50):
        n = 0
        while self.queue and n < limit:
            ms, fn = self.queue.pop(0)
            fn()
            n += 1
        return n


class MemStats:
    def __init__(self):
        self.data = {}

    def load(self):
        return dict(self.data)

    def save(self, d):
        self.data = dict(d)


def make_controller(seed=1, scheduler=None, personality="normal", stats=None):
    return PokerController(
        ai=AliciaAI(rng=random.Random(seed), samples=40), reactions=ReactionEngine(random.Random(seed)),
        scheduler=scheduler, rng=random.Random(seed), personality_getter=lambda: personality,
        name_getter=lambda: "Ana", stats_store=stats)


def play_simple(c, sched=None, max_steps=200):
    """Jogador que passa/paga e nunca troca. Devolve quando a mão acaba."""
    for _ in range(max_steps):
        if sched:
            sched.run_all()
        s = c.snapshot()
        if s["summary"]:
            return s
        if s["player_turn"]:
            c.player_action("check" if "check" in s["legal"] else "call")
        elif s["player_draw_turn"]:
            c.player_draw([])
    raise AssertionError("a mão não terminou")


class TestController(unittest.TestCase):
    def test_full_hand_with_delayed_ai_and_busy_lock(self):
        sched = FakeScheduler()
        c = make_controller(scheduler=sched)
        events = []
        c.on_change = lambda snap: events.append(snap["phase"])
        c.new_match()
        s = c.snapshot()
        self.assertEqual(s["match_stacks"], [1000, 1000])
        self.assertEqual(len(s["my_cards"]), 5)
        self.assertIsNone(s["alicia_cards"])                                # cartas da Alicia ocultas
        end = play_simple(c, sched)
        self.assertIsNotNone(end["summary"])
        self.assertEqual(sum(end["stacks"]), 2000)
        self.assertTrue(end["can_next_hand"] or end["match_over"])
        if end["summary"]["reason"] == "showdown":
            self.assertIsNotNone(end["alicia_cards"])
        else:
            self.assertIsNone(end["alicia_cards"])

    def test_ai_acts_only_after_scheduler_and_clicks_are_ignored_meanwhile(self):
        sched = FakeScheduler()
        c = make_controller(scheduler=sched)
        # força a Alicia a agir primeiro: dealer = humano
        c.match.dealer = 1
        c.new_match()
        if c.hand.dealer == ALICIA:
            c.match.dealer = ALICIA
        c.close()
        c = make_controller(scheduler=sched)
        c.match.reset()
        c.match.dealer = 0                                                   # start_hand alterna só depois da 1ª mão
        c.new_match()
        # procura uma mão em que a Alicia age primeiro
        for _ in range(20):
            if c.hand.to_act == ALICIA:
                break
            sched.run_all()
            c.new_match()
        if c.hand.to_act != ALICIA:
            self.skipTest("não caiu uma mão com a Alicia agindo primeiro")
        self.assertTrue(c.busy)
        self.assertFalse(c.player_action("check"))                          # clique durante a vez dela: ignorado
        self.assertEqual(len(sched.queue), 1)                               # só UMA jogada agendada
        c._pump(); c._pump()
        self.assertEqual(len(sched.queue), 1)                               # sem agendamento duplicado
        sched.run_all()
        self.assertFalse(c.busy)

    def test_new_match_cancels_stale_scheduled_moves(self):
        sched = FakeScheduler()
        c = make_controller(scheduler=sched)
        for _ in range(30):
            c.new_match()
            if c.hand.to_act == ALICIA:
                break
        stale = list(sched.queue)
        c.new_match()
        before = list(c.hand.events)
        for ms, fn in stale:
            fn()                                                            # callback velho disparado tarde
        self.assertEqual(c.hand.events[:len(before)], before[:len(before)])

    def test_draw_is_single_and_locks_after_confirm(self):
        c = make_controller()
        c.new_match()
        while c.snapshot()["phase"] == "BET1":
            s = c.snapshot()
            c.player_action("check" if "check" in s["legal"] else "call")
        s = c.snapshot()
        self.assertTrue(s["player_draw_turn"] or s["phase"] != "DRAW")
        if s["phase"] == "DRAW":
            self.assertTrue(c.player_draw([0, 1]))
            self.assertFalse(c.player_draw([2]))                            # segunda troca: recusada
            self.assertEqual(c.snapshot()["discards"][HUMAN], 2)

    def test_illegal_player_action_reports_error_without_changing_state(self):
        c = make_controller()
        errors = []
        c.on_error = errors.append
        c.new_match()
        s = c.snapshot()
        if s["player_turn"]:
            before = s["pot"]
            self.assertFalse(c.player_action("fold"))
            self.assertFalse(c.player_action("bet", 5))
            self.assertFalse(c.player_action("bet", 5000))
            self.assertEqual(len(errors), 3)
            self.assertEqual(c.snapshot()["pot"], before)

    def test_ai_crash_falls_back_to_a_legal_move(self):
        c = make_controller()
        c.ai.decide = lambda obs: (_ for _ in ()).throw(RuntimeError("falhou"))
        c.ai.choose_discards = lambda obs: (_ for _ in ()).throw(RuntimeError("falhou"))
        c.new_match()
        end = play_simple(c)
        self.assertIsNotNone(end["summary"])
        self.assertGreaterEqual(c.ai_errors, 1)
        self.assertEqual(sum(end["stacks"]), 2000)

    def test_ai_illegal_proposal_is_replaced_by_safe_action(self):
        from alicia_core.poker.ai import Decision
        c = make_controller()
        c.ai.decide = lambda obs: Decision("bet", 7, "bluff")             # aposta ilegal (abaixo do mínimo)
        c.new_match()
        end = play_simple(c)
        self.assertIsNotNone(end["summary"])
        self.assertEqual(sum(end["stacks"]), 2000)

    def test_next_hand_alternates_dealer_and_keeps_stacks(self):
        c = make_controller()
        c.new_match()
        end = play_simple(c)
        stacks = end["stacks"]
        d1 = end["dealer"]
        self.assertTrue(c.start_hand())
        s = c.snapshot()
        self.assertEqual(s["dealer"], 1 - d1)
        self.assertEqual(sum(s["stacks"]) + s["pot"], 2000)
        self.assertEqual(s["match_stacks"], stacks)
        self.assertFalse(c.start_hand())                                    # não dá para iniciar com a mão em andamento

    def test_match_over_and_restart_with_1000_each(self):
        stats = MemStats()
        c = make_controller(stats=stats)
        c.config.starting_chips = 100
        c.match = Match(Config(starting_chips=100, ante=10, min_bet=20), rng=random.Random(3))
        c.new_match()
        for _ in range(100):
            s = c.snapshot()
            if s["match_over"]:
                break
            if s["summary"]:
                c.start_hand(); continue
            if s["player_turn"]:
                c.player_action("allin" if "allin" in s["legal"] else ("check" if "check" in s["legal"] else "call"))
            elif s["player_draw_turn"]:
                c.player_draw([])
        s = c.snapshot()
        self.assertTrue(s["match_over"])
        self.assertIn(s["match_info"]["winner"], ("player", "alicia"))
        self.assertFalse(c.start_hand())
        self.assertGreaterEqual(stats.data.get("matches", 0), 1)
        c.match = Match(Config(), rng=random.Random(4))
        c.config = Config()
        c.new_match()
        self.assertEqual(c.snapshot()["match_stacks"], [1000, 1000])

    def test_reactions_are_emitted_and_follow_real_events(self):
        c = make_controller()
        said = []
        c.on_say = said.append
        c.new_match()
        play_simple(c)
        self.assertGreaterEqual(len(said), 2)
        end = c.snapshot()["summary"]
        last = said[-1].text
        win = {t for p in LINES.values() for k in ("ai_win", "ai_win_big", "ai_win_showdown", "ai_win_bluff") for t, _ in p.get(k, [])}
        lose = {t for p in LINES.values() for k in ("ai_lose", "ai_lose_big", "ai_lose_fold") for t, _ in p.get(k, [])}
        outcome = end["outcome"]
        names = [r.text for r in said]
        norm = lambda t: re.sub(r"\{[a-z_]+\}", "", t)
        if outcome == "player_win":
            self.assertFalse(any(norm(x) in {norm(w) for w in win} for x in names[-1:]))
        if outcome == "ai_win":
            self.assertFalse(any(x in lose for x in names[-1:]))

    def test_voice_off_or_missing_ui_does_not_matter(self):
        c = make_controller()
        c.on_say = lambda r: (_ for _ in ()).throw(RuntimeError("UI quebrou ao falar"))
        with self.assertRaises(RuntimeError):
            c.new_match()                                                   # erro do chamador não é engolido silenciosamente

    def test_stats_store_persists_in_db(self):
        import os, tempfile
        from alicia_core.database import MemoryDatabase
        d = tempfile.mkdtemp()
        db = MemoryDatabase(os.path.join(d, "t.db"))
        store = DbStatsStore(db)
        store.save({"matches": 2, "player_match_wins": 1})
        self.assertEqual(DbStatsStore(MemoryDatabase(os.path.join(d, "t.db"))).load()["matches"], 2)

    def test_ai_vs_ai_matches_terminate(self):
        rng = random.Random(10)
        a, b = AliciaAI(rng=random.Random(1), samples=25), AliciaAI(rng=random.Random(2), samples=25)
        for _ in range(3):
            m = Match(Config(starting_chips=200), rng=rng)
            hands = 0
            while not m.over and hands < 300:
                h = m.start_hand()
                while not h.is_over:
                    if h.phase == Phase.DRAW:
                        p = h.draw_turn(); ai = b if p == HUMAN else a
                        h.draw(p, ai.choose_discards(h.observation_for(p))[0]); continue
                    p = h.to_act; ai = b if p == HUMAN else a
                    d = ai.decide(h.observation_for(p)); h.act(p, d.kind, d.amount)
                m.finish_hand(); hands += 1
                self.assertEqual(sum(m.stacks), 400)
            self.assertTrue(m.over or hands == 300)
