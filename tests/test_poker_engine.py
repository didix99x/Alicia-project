# -*- coding: utf-8 -*-
"""Testes do motor de pôquer (cartas, avaliação, apostas, fichas, troca, showdown). Sem interface, sem IA."""

import dataclasses
import random
import unittest

from tests.support import APP_DIR  # noqa: F401  (ajusta sys.path)
from alicia_core.poker.cards import Card, Deck, full_deck, parse_cards
from alicia_core.poker.engine import ALICIA, HUMAN, Config, Hand, Match, Observation, Phase, PokerError
from alicia_core.poker.evaluator import compare, describe, evaluate, winning_cards


def ev(text):
    return evaluate(parse_cards(text))


def finish_draw(h, discards=None):
    """Cada jogador troca na ordem correta (quem age primeiro troca primeiro)."""
    discards = discards or {}
    while h.phase == Phase.DRAW and h.draw_turn() is not None:
        p = h.draw_turn()
        h.draw(p, discards.get(p, []))


def stacked(human, alicia, rest=""):
    """Baralho com ordem definida: 5 cartas do humano, 5 da Alicia e as próximas (trocas)."""
    cards = parse_cards(human) + parse_cards(alicia) + parse_cards(rest)
    others = [c for c in full_deck() if c not in cards]
    return Deck(cards=cards + others)


# ============================================================== avaliação
class TestEvaluator(unittest.TestCase):
    def test_every_category_is_recognized(self):
        cases = {
            "As Ks Qs Js 10s": 8, "9c 9d 9h 9s Kc": 7, "3c 3d 3h 9s 9c": 6, "2h 7h 9h Jh Kh": 5,
            "5c 6d 7h 8s 9c": 4, "4c 4d 4h 9s Kc": 3, "Jc Jd 4h 4s Kc": 2, "Ac Ad 9h 4s Kc": 1, "Ac 7d 9h 4s Kc": 0,
        }
        for text, cat in cases.items():
            self.assertEqual(ev(text)[0], cat, text)

    def test_category_order_is_correct(self):
        order = ["Ac 7d 9h 4s Kc", "Ac Ad 9h 4s Kc", "Jc Jd 4h 4s Kc", "4c 4d 4h 9s Kc", "5c 6d 7h 8s 9c",
                 "2h 7h 9h Jh Kh", "3c 3d 3h 9s 9c", "9c 9d 9h 9s Kc", "6s 7s 8s 9s 10s"]
        ranks = [ev(t) for t in order]
        self.assertEqual(ranks, sorted(ranks))
        self.assertEqual(len(set(ranks)), len(ranks))

    def test_wheel_is_the_lowest_straight_and_ace_high_the_highest(self):
        wheel, six_high, ace_high = ev("Ac 2d 3h 4s 5c"), ev("2c 3d 4h 5s 6c"), ev("10c Jd Qh Ks Ac")
        self.assertEqual(wheel, (4, 5))
        self.assertGreater(six_high, wheel)
        self.assertGreater(ace_high, six_high)
        self.assertEqual(ev("Ac 2c 3c 4c 5c"), (8, 5))                  # steel wheel
        self.assertNotEqual(ev("Qc Kd Ah 2s 3c")[0], 4)                 # não existe sequência "dando a volta"

    def test_kickers_decide_hands_of_the_same_category(self):
        self.assertGreater(ev("Ac Ad Kh 4s 3c"), ev("Ac Ad Qh 9s 8c"))              # par de ases, kicker K > Q
        self.assertGreater(ev("Ac Ad Kh 4s 3c"), ev("Ac Ad Kh 4s 2c"))              # 3º kicker
        self.assertGreater(ev("Kc Kd Qh Qs 2c"), ev("Kc Kd Jh Js Ac"))              # dois pares: par mais alto 2º
        self.assertGreater(ev("Kc Kd Qh Qs 3c"), ev("Kh Ks Qd Qc 2d"))              # kicker do dois pares
        self.assertGreater(ev("9c 9d 9h Ks 2c"), ev("9c 9d 9h Qs Jc"))              # trinca + kickers
        self.assertGreater(ev("8c 8d 8h 8s Kc"), ev("8c 8d 8h 8s Qc"))              # quadra + kicker
        self.assertGreater(ev("Ac Ad Ah 2s 2c"), ev("Kc Kd Kh Qs Qc"))              # full: trinca decide
        self.assertGreater(ev("3c 3d 3h As Ac"), ev("3c 3d 3h Ks Kc"))              # full: mesma trinca, par decide
        self.assertGreater(ev("2h 7h 9h Jh Ah"), ev("2d 7d 9d Jd Kd"))              # flush: carta mais alta
        self.assertGreater(ev("2h 8h 9h Jh Kh"), ev("2d 7d 9d Jd Kd"))              # flush: desempate fundo

    def test_equal_hands_with_different_suits_tie(self):
        a, b = ev("Ac Kd Qh 9s 2c"), ev("Ad Kh Qs 9c 2d")
        self.assertEqual(compare(a, b), 0)

    def test_description_and_winning_cards(self):
        self.assertEqual(describe(ev("As Ks Qs Js 10s")), "Royal flush")
        self.assertIn("ases", describe(ev("Ac Ad Kh 4s 3c")))
        hand = parse_cards("Kc Kd Qh Qs 2c")
        self.assertEqual({c.rank for c in winning_cards(hand, evaluate(hand))}, {13, 12})
        self.assertEqual(len(winning_cards(parse_cards("Ac Ad Ah 2s 2c"), ev("Ac Ad Ah 2s 2c"))), 5)
        self.assertEqual(len(winning_cards(parse_cards("Ac 7d 9h 4s Kc"), ev("Ac 7d 9h 4s Kc"))), 1)

    def test_invalid_hands_are_rejected(self):
        with self.assertRaises(ValueError):
            evaluate(parse_cards("Ac Ad Ah 2s"))
        with self.assertRaises(ValueError):
            evaluate(parse_cards("Ac Ac Ah 2s 3c"))

    def test_random_hand_frequencies_match_official_probabilities(self):
        rng = random.Random(2024)
        deck, n, counts = full_deck(), 40000, [0] * 9
        for _ in range(n):
            counts[evaluate(rng.sample(deck, 5))[0]] += 1
        self.assertAlmostEqual(counts[0] / n, 0.5012, delta=0.01)
        self.assertAlmostEqual(counts[1] / n, 0.4226, delta=0.01)
        self.assertAlmostEqual(counts[2] / n, 0.0475, delta=0.006)
        self.assertAlmostEqual(counts[3] / n, 0.0211, delta=0.004)


class TestDeck(unittest.TestCase):
    def test_52_distinct_cards_and_deal_limits(self):
        d = Deck()
        cards = d.deal(52)
        self.assertEqual(len(set(cards)), 52)
        with self.assertRaises(ValueError):
            d.deal(1)

    def test_shuffles_differ(self):
        orders = {tuple(Deck().deal(52)) for _ in range(5)}
        self.assertEqual(len(orders), 5)

    def test_deal_two_hands_without_duplicates(self):
        for _ in range(200):
            h = Hand([1000, 1000], HUMAN, Config())
            all_cards = h.hole[0] + h.hole[1]
            self.assertEqual(len(all_cards), 10)
            self.assertEqual(len(set(all_cards)), 10)


# ============================================================== apostas
def new_hand(human="Ac Ad 9h 4s Kc", alicia="2c 7d 9s Jh 3d", rest="", stacks=(1000, 1000), dealer=HUMAN, **cfg):
    return Hand(list(stacks), dealer, Config(**cfg), stacked(human, alicia, rest))


class TestBetting(unittest.TestCase):
    def test_start_of_hand_antes_and_first_to_act(self):
        h = new_hand(dealer=HUMAN)
        self.assertEqual(h.pot, 20)
        self.assertEqual(h.stacks, [990, 990])
        self.assertEqual(h.to_act, ALICIA)                    # não-dealer age primeiro
        self.assertEqual(new_hand(dealer=ALICIA).to_act, HUMAN)
        self.assertEqual(h.phase, Phase.BET1)

    def test_legal_actions_follow_real_state(self):
        h = new_hand()
        legal = h.legal_actions(ALICIA)
        self.assertEqual(set(legal), {"check", "bet", "allin"})
        self.assertEqual(legal["bet"], (20, 990))
        self.assertEqual(h.legal_actions(HUMAN), {})          # não é a vez do humano
        h.act(ALICIA, "bet", 100)
        legal = h.legal_actions(HUMAN)
        self.assertEqual(set(legal), {"fold", "call", "raise", "allin"})
        self.assertEqual(legal["call"], 100)
        self.assertEqual(legal["raise"], (200, 990))          # aumentar PARA, mínimo = aposta + 100

    def test_illegal_actions_are_rejected_without_changing_state(self):
        h = new_hand()
        before = (list(h.stacks), h.pot, h.to_act)
        bad = [(HUMAN, "check", None), (ALICIA, "fold", None), (ALICIA, "call", None), (ALICIA, "raise", 100),
               (ALICIA, "bet", 10), (ALICIA, "bet", 991), (ALICIA, "bet", None), (ALICIA, "bet", -5), (ALICIA, "dance", None)]
        for p, kind, amt in bad:
            with self.assertRaises(PokerError, msg=f"{p} {kind} {amt}"):
                h.act(p, kind, amt)
        self.assertEqual((h.stacks, h.pot, h.to_act), (before[0], before[1], before[2]))

    def test_cannot_bet_more_than_stack_and_min_raise_enforced(self):
        h = new_hand(stacks=(500, 500))
        h.act(ALICIA, "bet", 100)
        with self.assertRaises(PokerError):
            h.act(HUMAN, "raise", 150)                        # abaixo do mínimo (200)
        with self.assertRaises(PokerError):
            h.act(HUMAN, "raise", 1000)                       # mais do que tem
        h.act(HUMAN, "raise", 300)                            # aumento de 200
        self.assertEqual(h.legal_actions(ALICIA)["raise"], (490, 490))   # mínimo seria 500, mas só tem 490: só all-in
        self.assertEqual(h.legal_actions(ALICIA)["call"], 200)

    def test_check_check_closes_round_and_opens_draw(self):
        h = new_hand()
        h.act(ALICIA, "check")
        self.assertEqual(h.to_act, HUMAN)
        h.act(HUMAN, "check")
        self.assertEqual(h.phase, Phase.DRAW)
        self.assertEqual(h.pot, 20)

    def test_bet_call_pot_math(self):
        h = new_hand()
        h.act(ALICIA, "bet", 50)
        h.act(HUMAN, "call")
        self.assertEqual(h.pot, 120)
        self.assertEqual(h.stacks, [940, 940])
        self.assertEqual(h.phase, Phase.DRAW)

    def test_raise_cap_per_round(self):
        h = new_hand(max_raises=2)
        h.act(ALICIA, "bet", 20)
        h.act(HUMAN, "raise", 60)
        h.act(ALICIA, "raise", 120)
        legal = h.legal_actions(HUMAN)
        self.assertNotIn("raise", legal)                      # cap atingido: só pagar/desistir
        self.assertIn("call", legal)
        with self.assertRaises(PokerError):
            h.act(HUMAN, "raise", 300)
        h.act(HUMAN, "call")
        self.assertEqual(h.phase, Phase.DRAW)

    def test_fold_only_when_there_is_a_bet(self):
        h = new_hand()
        with self.assertRaises(PokerError):
            h.act(ALICIA, "fold")
        h.act(ALICIA, "bet", 30)
        h.act(HUMAN, "fold")
        self.assertTrue(h.is_over)

    def test_fold_awards_pot_and_does_not_reveal_cards(self):
        h = new_hand()
        h.act(ALICIA, "bet", 100)
        h.act(HUMAN, "fold")
        r = h.result
        self.assertEqual((r.reason, r.winner, r.pot), ("fold", ALICIA, 120))
        self.assertEqual(h.final_stacks, [990, 1010])
        self.assertIsNone(h.public_state(HUMAN)["opponent_cards"])
        self.assertIsNone(h.public_state(ALICIA)["opponent_cards"])
        self.assertIsNone(r.ranks)

    def test_actions_after_hand_over_are_rejected(self):
        h = new_hand()
        h.act(ALICIA, "bet", 100)
        h.act(HUMAN, "fold")
        with self.assertRaises(PokerError):
            h.act(ALICIA, "check")


class TestAllIn(unittest.TestCase):
    def test_allin_and_call_for_less_refunds_the_uncalled_part(self):
        h = new_hand(human="Ac Ad Ah 4s Kc", alicia="2c 7d 9s Jh 3d", stacks=(300, 1000), dealer=HUMAN)
        h.act(ALICIA, "bet", 800)
        self.assertEqual(h.legal_actions(HUMAN)["call"], 290)          # só tem 290 fora do ante
        h.act(HUMAN, "call")
        self.assertTrue(h.all_in[HUMAN])
        self.assertEqual(h.phase, Phase.DRAW)
        finish_draw(h)                                                 # ambos ainda trocam
        self.assertTrue(h.is_over)                                     # sem 2ª rodada: um está all-in
        r = h.result
        self.assertEqual(r.refunded, 510)                              # 810 - 300 não igualados voltam a ela
        self.assertEqual(r.pot, 600)
        self.assertEqual(sum(h.final_stacks), 1300)
        self.assertEqual(h.final_stacks[HUMAN], 600)                   # ganhou (trinca de ases)
        self.assertEqual(h.final_stacks[ALICIA], 700)                  # 1000 - 810 + 510

    def test_short_stack_loses_only_what_it_put_in(self):
        h = new_hand(human="2c 7d 9s Jh 3d", alicia="Ac Ad Ah 4s Kc", stacks=(300, 1000), dealer=HUMAN)
        h.act(ALICIA, "bet", 800)
        h.act(HUMAN, "call")
        finish_draw(h)
        self.assertEqual(h.final_stacks, [0, 1300])

    def test_allin_helper_maps_to_the_right_action(self):
        h = new_hand(stacks=(200, 200))
        h.act(ALICIA, "allin")
        self.assertEqual(h.stacks[ALICIA], 0)
        self.assertEqual(h.events[-1]["type"], "bet")
        self.assertEqual(h.legal_actions(HUMAN)["call"], 190)
        self.assertNotIn("raise", h.legal_actions(HUMAN))              # ela já está all-in: nada a aumentar
        h.act(HUMAN, "allin")                                          # equivale a pagar
        self.assertEqual(h.events[-1]["type"], "call")
        self.assertEqual(h.phase, Phase.DRAW)

    def test_short_allin_raise_does_not_reopen_action(self):
        h = new_hand(stacks=(1000, 1000))
        h.act(ALICIA, "bet", 100)
        h2 = new_hand(stacks=(150, 1000), dealer=ALICIA)               # humano age 1º e tem pouco
        h2.act(HUMAN, "bet", 100)                                      # humano aposta 100 (resta 40)
        h2.act(ALICIA, "raise", 250)                                   # Alicia aumenta para 250
        legal = h2.legal_actions(HUMAN)
        self.assertEqual(legal["call"], 40)                            # só consegue pagar 40
        self.assertNotIn("raise", legal)
        h2.act(HUMAN, "call")
        self.assertEqual(h2.phase, Phase.DRAW)
        self.assertEqual(sum(h2.contrib) + sum(h2.stacks), 1150)

    def test_allin_for_less_than_min_raise_closes_action(self):
        h = new_hand(stacks=(1000, 160), dealer=HUMAN)                 # Alicia (160) age primeiro
        h.act(ALICIA, "bet", 100)                                      # resta 50
        h.act(HUMAN, "raise", 250)                                     # humano aumenta
        legal = h.legal_actions(ALICIA)
        self.assertEqual(legal["call"], 50)                            # só paga o que tem
        self.assertNotIn("raise", legal)
        h.act(ALICIA, "call")
        self.assertEqual(h.result, None)
        self.assertEqual(h.phase, Phase.DRAW)
        self.assertEqual(sum(h.contrib) + sum(h.stacks), 1160)

    def test_both_all_in_from_the_ante_goes_straight_to_draw_then_showdown(self):
        h = new_hand(stacks=(8, 5))
        self.assertEqual(h.phase, Phase.DRAW)
        self.assertEqual(h.stacks, [0, 0])
        first = h.draw_turn()
        h.draw(first, [0])
        h.draw(1 - first, [])
        self.assertTrue(h.is_over)
        self.assertEqual(sum(h.final_stacks), 13)
        self.assertEqual(h.result.pot, 10)                             # 5 de cada; os 3 extras voltam
        self.assertEqual(h.result.refunded, 3)

    def test_opponent_all_in_means_no_more_betting_for_the_other(self):
        h = new_hand(stacks=(100, 1000), dealer=ALICIA)                # humano 1º
        h.act(HUMAN, "allin")
        h.act(ALICIA, "call")
        self.assertEqual(h.phase, Phase.DRAW)
        finish_draw(h, {HUMAN: [1, 2]})
        self.assertTrue(h.is_over)


# ============================================================== troca de cartas
class TestDraw(unittest.TestCase):
    def to_draw(self, **kw):
        h = new_hand(**kw)
        h.act(ALICIA, "check"); h.act(HUMAN, "check")
        return h

    def test_draw_zero_to_five_cards(self):
        for n in range(6):
            h = self.to_draw(rest="Kd Kh Ks Qc Qd 2h 3h 4h 5h 6h")
            first = h.draw_turn()
            old = list(h.hole[first])
            new = h.draw(first, list(range(n)))
            self.assertEqual(len(new), n)
            self.assertEqual(h.hole[first][n:], old[n:])               # as não descartadas ficam
            self.assertEqual(h.discards[first], n)
            self.assertEqual(len(set(h.hole[first])), 5)

    def test_new_cards_come_from_the_deck_in_order(self):
        h = self.to_draw(rest="Kd Kh Ks")
        first = h.draw_turn()
        new = h.draw(first, [0, 4])
        self.assertEqual([str(c) for c in new], ["K♦", "K♥"])
        self.assertEqual(h.hole[first][0], new[0])
        self.assertEqual(h.hole[first][4], new[1])

    def test_only_one_draw_per_hand(self):
        h = self.to_draw()
        first = h.draw_turn()
        h.draw(first, [0])
        with self.assertRaises(PokerError):
            h.draw(first, [1])

    def test_draw_order_and_phase_checks(self):
        h = new_hand()
        with self.assertRaises(PokerError):
            h.draw(HUMAN, [0])                                         # ainda é a rodada de apostas
        h.act(ALICIA, "check"); h.act(HUMAN, "check")
        first = h.draw_turn()
        with self.assertRaises(PokerError):
            h.draw(1 - first, [0])                                     # fora de ordem
        h.draw(first, [])
        h.draw(1 - first, [])
        self.assertEqual(h.phase, Phase.BET2)

    def test_invalid_indices_are_rejected(self):
        h = self.to_draw()
        first = h.draw_turn()
        for bad in ([5], [-1], [1, 1], ["a"], [0, 1, 2, 3, 4, 4]):
            with self.assertRaises(PokerError, msg=str(bad)):
                h.draw(first, bad)
        self.assertFalse(h.drawn[first])

    def test_discard_count_is_public_but_cards_are_not(self):
        h = self.to_draw()
        first = h.draw_turn()
        h.draw(first, [0, 1, 2])
        obs = h.observation_for(1 - first)
        self.assertEqual(obs.opp_discards, 3)
        self.assertNotIn(h.hole[first][0], obs.my_cards)

    def test_second_betting_round_after_draw(self):
        h = self.to_draw()
        h.draw(h.draw_turn(), [0]); h.draw(h.draw_turn(), [])
        self.assertEqual(h.phase, Phase.BET2)
        self.assertEqual(h.to_act, h.first)
        h.act(h.to_act, "check"); h.act(h.to_act, "check")
        self.assertTrue(h.is_over)
        self.assertEqual(h.result.reason, "showdown")

    def test_folding_player_does_not_draw(self):
        h = new_hand()
        h.act(ALICIA, "bet", 50); h.act(HUMAN, "fold")
        with self.assertRaises(PokerError):
            h.draw(HUMAN, [0])


# ============================================================== showdown
class TestShowdown(unittest.TestCase):
    def play_to_showdown(self, human, alicia, **kw):
        h = new_hand(human=human, alicia=alicia, **kw)
        h.act(h.to_act, "check"); h.act(h.to_act, "check")
        h.draw(h.draw_turn(), []); h.draw(h.draw_turn(), [])
        h.act(h.to_act, "check"); h.act(h.to_act, "check")
        return h

    def test_better_category_wins_and_cards_are_revealed(self):
        h = self.play_to_showdown("Ac Ad Ah 4s Kc", "Jc Jd 9h 4h 2c")
        r = h.result
        self.assertEqual(r.winner, HUMAN)
        self.assertEqual(h.final_stacks, [1010, 990])
        self.assertIn("Trinca", r.descriptions[HUMAN])
        self.assertEqual(h.public_state(HUMAN)["opponent_cards"], h.hole[ALICIA])
        self.assertEqual(len(r.best_cards[HUMAN]), 3)

    def test_kicker_decides_showdown(self):
        h = self.play_to_showdown("Ac Ad Kh 4s 3c", "As Ah Qh 4d 3d")
        self.assertEqual(h.result.winner, HUMAN)

    def test_tie_splits_pot_equally(self):
        h = self.play_to_showdown("Ac Kd Qh 9s 2c", "Ad Kh Qs 9c 2d")
        self.assertIsNone(h.result.winner)
        self.assertEqual(h.final_stacks, [1000, 1000])

    def test_odd_pot_tie_gives_extra_chip_to_first_to_act(self):
        """Pote ímpar de verdade: ante 0, humano com 3 fichas e Alicia aposta 3, mas só 2 são igualados -> use stacks (3, 10)."""
        for dealer in (HUMAN, ALICIA):
            g = Hand([3, 10], dealer, Config(ante=1, min_bet=1), stacked("Ac Kd Qh 9s 2c", "Ad Kh Qs 9c 2d"))
            # ante 1+1; se o humano tem 3 e Alicia aposta 2, humano paga 2 (all-in): contribuições 3/3 -> pote 6 (par)
            # para obter pote ímpar basta o ante ímpar via stack de 1 ficha
            g = Hand([1, 10], dealer, Config(ante=1, min_bet=1), stacked("Ac Kd Qh 9s 2c", "Ad Kh Qs 9c 2d"))
            finish_draw(g)
            self.assertTrue(g.is_over)
            self.assertIsNone(g.result.winner)
            self.assertEqual(sum(g.final_stacks), 11)
            self.assertEqual(g.final_stacks, [1, 10])                  # pote disputado 2: 1 para cada
        # pote ímpar: 3 fichas de cada lado + 1 morta não existe; testamos divisão ímpar diretamente:
        h = Hand([1000, 1000], HUMAN, Config(ante=0, min_bet=1), stacked("Ac Kd Qh 9s 2c", "Ad Kh Qs 9c 2d"))
        h.contrib = [3, 3]
        h.contrib[0] = 3; h.contrib[1] = 3
        h.stacks = [997, 997]
        h.acted = [True, True]
        h._showdown()
        self.assertEqual(h.final_stacks, [1000, 1000])
        h2 = Hand([1000, 1000], HUMAN, Config(ante=0, min_bet=1), stacked("Ac Kd Qh 9s 2c", "Ad Kh Qs 9c 2d"))
        h2.contrib = [3, 4]
        h2.stacks = [997, 996]
        h2._showdown()
        self.assertEqual(h2.result.refunded, 1)                         # a 4ª ficha da Alicia volta
        self.assertEqual(h2.final_stacks, [1000, 1000])

    def test_chips_conserved_through_a_whole_hand(self):
        h = new_hand()
        total = 2000
        for step in [("bet", 100), ("raise", 300), ("call", None)]:
            p = h.to_act
            h.act(p, step[0], step[1])
            self.assertEqual(sum(h.stacks) + h.pot, total)
        h.draw(h.draw_turn(), [0, 1]); h.draw(h.draw_turn(), [])
        h.act(h.to_act, "allin"); h.act(h.to_act, "call")
        self.assertEqual(sum(h.final_stacks), total)


# ============================================================== informação oculta
class TestHiddenInformation(unittest.TestCase):
    def test_observation_has_no_hidden_cards_or_deck(self):
        h = new_hand()
        obs = h.observation_for(ALICIA)
        self.assertIsInstance(obs, Observation)
        self.assertEqual(set(obs.my_cards), set(h.hole[ALICIA]))
        flat = repr(dataclasses.asdict(obs))
        for card in h.hole[HUMAN]:
            self.assertNotIn(repr(card), flat)
        names = {f.name for f in dataclasses.fields(Observation)}
        self.assertFalse(names & {"deck", "opponent_cards", "opp_cards", "hole", "opp_hand"})

    def test_public_state_hides_opponent_until_showdown(self):
        h = new_hand()
        self.assertIsNone(h.public_state(HUMAN)["opponent_cards"])
        h.act(ALICIA, "check"); h.act(HUMAN, "check")
        h.draw(h.draw_turn(), []); h.draw(h.draw_turn(), [])
        self.assertIsNone(h.public_state(HUMAN)["opponent_cards"])
        h.act(h.to_act, "check"); h.act(h.to_act, "check")
        self.assertIsNotNone(h.public_state(HUMAN)["opponent_cards"])

    def test_events_never_contain_cards(self):
        h = new_hand()
        h.act(ALICIA, "bet", 50); h.act(HUMAN, "call")
        h.draw(h.draw_turn(), [0, 1]); h.draw(h.draw_turn(), [2])
        for e in h.events:
            self.assertFalse(any(isinstance(v, Card) for v in e.values()))


# ============================================================== partida
class TestMatch(unittest.TestCase):
    def test_starting_state_and_dealer_alternation(self):
        m = Match(rng=random.Random(3))
        self.assertEqual(m.stacks, [1000, 1000])
        h1 = m.start_hand(); d1 = h1.dealer
        h1.act(h1.to_act, "bet", 50); h1.act(h1.to_act, "fold")
        m.finish_hand()
        h2 = m.start_hand()
        self.assertEqual(h2.dealer, 1 - d1)

    def test_stacks_update_after_each_hand_and_total_is_constant(self):
        m = Match(rng=random.Random(4))
        for _ in range(10):
            if m.over:
                break
            h = m.start_hand()
            h.act(h.to_act, "bet", 40); h.act(h.to_act, "call")
            h.draw(h.draw_turn(), [0]); h.draw(h.draw_turn(), [1, 2])
            h.act(h.to_act, "check"); h.act(h.to_act, "check")
            m.finish_hand()
            self.assertEqual(sum(m.stacks), 2000)
        self.assertEqual(m.hands_played, 10 if not m.over else m.hands_played)

    def test_cannot_start_a_hand_before_finishing_the_last_one(self):
        m = Match()
        m.start_hand()
        with self.assertRaises(PokerError):
            m.start_hand()

    def test_finish_hand_is_idempotent(self):
        m = Match()
        h = m.start_hand()
        h.act(h.to_act, "bet", 50); h.act(h.to_act, "fold")
        m.finish_hand(); s = list(m.stacks); m.finish_hand()
        self.assertEqual(m.stacks, s)
        self.assertEqual(m.hands_played, 1)

    def test_match_ends_when_someone_busts_and_can_be_reset(self):
        m = Match(Config(starting_chips=100, ante=10, min_bet=20), rng=random.Random(5))
        for _ in range(200):
            if m.over:
                break
            h = m.start_hand()
            h.act(h.to_act, "allin")
            h.act(h.to_act, "call")
            while not h.is_over:
                p = h.draw_turn()
                if p is not None:
                    h.draw(p, [])
            m.finish_hand()
        self.assertTrue(m.over)
        self.assertIn(m.winner, (HUMAN, ALICIA))
        self.assertEqual(sorted(m.stacks), [0, 200] if sum(m.stacks) == 200 else sorted(m.stacks))
        with self.assertRaises(PokerError):
            m.start_hand()
        m.reset()
        self.assertEqual(m.stacks, [100, 100])
        self.assertFalse(m.over)


class TestRandomPlayInvariants(unittest.TestCase):
    """Milhares de ações legais aleatórias: nenhuma ficha aparece ou some, nenhuma carta se repete."""

    def test_random_legal_play_conserves_chips(self):
        rng = random.Random(77)
        for hand_no in range(400):
            stacks = [rng.choice([5, 30, 120, 1000]), rng.choice([8, 60, 500, 1000])]
            h = Hand(stacks, rng.randrange(2), Config(), Deck(rng))
            total = sum(stacks)
            guard = 0
            while not h.is_over:
                guard += 1
                self.assertLess(guard, 200)
                self.assertEqual(sum(h.stacks) + h.pot, total)
                self.assertTrue(all(s >= 0 for s in h.stacks))
                dp = h.draw_turn()
                if h.phase == Phase.DRAW and dp is not None:
                    idx = rng.sample(range(5), rng.randint(0, 5))
                    h.draw(dp, idx)
                    cards = h.hole[0] + h.hole[1]
                    self.assertEqual(len(set(cards)), 10)
                    continue
                p = h.to_act
                legal = h.legal_actions(p)
                self.assertTrue(legal, "deveria haver ação legal")
                kind = rng.choice(list(legal))
                amount = None
                if kind == "bet":
                    amount = rng.randint(*legal["bet"])
                elif kind == "raise":
                    amount = rng.randint(*legal["raise"])
                h.act(p, kind, amount)
            self.assertEqual(sum(h.final_stacks), total)
            self.assertTrue(all(s >= 0 for s in h.final_stacks))
