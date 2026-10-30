# -*- coding: utf-8 -*-
"""
Testes da LÓGICA da janela do pôquer e da integração com a Alicia, usando um Qt falso (tests/fake_qt.py).
Isto valida o comportamento (botões, seleção, troca única, apostas, banners, fechamento, voz, comando), NÃO a aparência
nem a API real do PyQt6 — isso só se verifica abrindo o jogo no seu computador.
"""

import sys
import types
import unittest

from tests.support import APP_DIR  # noqa: F401
from tests.fake_qt import QPixmap, QTimer, _Signal, load_poker_window_with_fake_qt
from tests.test_alicia import CoreCase

from alicia_core.poker.engine import ALICIA, HUMAN
from alicia_core.poker.reactions import Reaction

pw = load_poker_window_with_fake_qt()


class FakeDb:
    def get_user_name(self): return "Ana"
    def get_profile(self, k): return getattr(self, "_p", {}).get(k)
    def set_profile(self, k, v): self.__dict__.setdefault("_p", {})[k] = v


def make_core(tts_enabled=False, voice=True):
    spoken = []
    core = types.SimpleNamespace(
        db=FakeDb(), config=types.SimpleNamespace(poker_voice=voice), get_personality=lambda: "normal",
        tts=types.SimpleNamespace(enabled=tts_enabled, audio_playing=False, is_speaking=False),
        poker_active=False, spoken=spoken)
    core._speak_async = lambda text, expr: spoken.append((text, expr.value))
    return core


def make_window(core=None, avatar_ui=None):
    QTimer.queue.clear()
    return pw.PokerWindow(core or make_core(), avatar_ui)


def settle(win, rounds=30):
    for _ in range(rounds):
        if not QTimer.run_pending():
            break


def play_until_hand_over(win, trade=()):
    """Joga só com os botões da janela: passa/paga; troca as cartas indicadas na troca."""
    for _ in range(80):
        settle(win)
        if win.btn_next.isVisible() or win._last_snap.get("match_over"):
            return
        if win.btn_draw.isEnabled():
            for i in trade:
                win.my_cards[i].click()
            win.btn_draw.click()
        elif win.btn_call.isEnabled():
            win.btn_call.click()
    raise AssertionError("a mão não terminou usando só os botões")


class TestPokerWindowFlow(unittest.TestCase):
    def test_initial_state_after_new_match(self):
        w = make_window()
        settle(w)
        self.assertEqual(len(w.controller.snapshot()["my_cards"]), 5)
        self.assertTrue(all(c.text().strip() and c.text() != "♠" for c in w.my_cards))       # minhas cartas viradas para cima
        self.assertTrue(all(c.text() == "♠" for c in w.ai_cards))                              # cartas dela viradas para baixo
        self.assertIn("🪙", w.my_chips.text())
        self.assertEqual(w.pot_label.text(), f"Pote: {w.controller.snapshot()['pot']}")
        self.assertGreaterEqual(w.controller.snapshot()["pot"], 20)                           # no mínimo os dois antes
        self.assertIn("Mão 1", w.phase_label.text())
        self.assertFalse(w.banner.isVisible())
        self.assertGreaterEqual(w.history.count(), 2)
        self.assertIn("ante", w.history.items[0])                                              # começa pelos dois antes

    def test_buttons_follow_the_real_turn(self):
        w = make_window()
        h = w.controller.hand
        if h.to_act == ALICIA:                                                                  # vez dela: tudo bloqueado
            self.assertFalse(w.btn_call.isEnabled())
            self.assertFalse(w.btn_bet.isEnabled())
            self.assertFalse(w.btn_fold.isEnabled())
            self.assertIn("pensando", w.ai_status.text())
            settle(w)
        else:
            self.assertTrue(w.btn_call.isEnabled())
        # vez do jogador: passar/apostar liberados; desistir só se houver aposta
        for _ in range(30):
            snap = w.controller.snapshot()
            if snap["player_turn"]:
                break
            settle(w)
        snap = w.controller.snapshot()
        self.assertTrue(snap["player_turn"])
        self.assertEqual(w.btn_fold.isEnabled(), "fold" in snap["legal"])
        self.assertEqual(w.btn_bet.isEnabled(), "bet" in snap["legal"] or "raise" in snap["legal"])
        self.assertFalse(w.btn_draw.isEnabled())                                               # troca só na fase de troca
        self.assertIn(w.btn_call.text()[:6], ("Passar", "Pagar "))

    def test_draw_selection_marks_cards_and_confirms_once(self):
        w = make_window()
        for _ in range(60):
            settle(w)
            if w.controller.snapshot()["phase"] == "DRAW":
                break
            if w.btn_call.isEnabled():
                w.btn_call.click()
        for _ in range(10):
            settle(w)
            if w.btn_draw.isEnabled():
                break
        self.assertEqual(w.controller.snapshot()["phase"], "DRAW")
        self.assertTrue(w.btn_draw.isEnabled())
        self.assertEqual(w.btn_draw.text(), "Ficar com as cinco")                              # confirmar sem trocar é permitido
        before = list(w.controller.hand.hole[HUMAN])
        w.my_cards[1].click(); w.my_cards[3].click()
        self.assertEqual(w.selected, {1, 3})
        self.assertIn("TROCAR", w.my_cards[1].text())
        self.assertNotIn("TROCAR", w.my_cards[0].text())
        self.assertIn("2", w.sel_label.text())
        self.assertEqual(w.btn_draw.text(), "Confirmar troca (2)")
        w.my_cards[1].click()                                                                  # desmarca
        self.assertEqual(w.selected, {3})
        w.my_cards[1].click()
        w.btn_draw.click()
        after = w.controller.hand.hole[HUMAN]
        self.assertEqual(w.controller.hand.discards[HUMAN], 2)
        self.assertEqual([after[i] for i in (0, 2, 4)], [before[i] for i in (0, 2, 4)])        # só as marcadas mudam
        self.assertNotEqual({after[1], after[3]}, {before[1], before[3]})
        self.assertEqual(w.selected, set())
        self.assertFalse(w.btn_draw.isEnabled())                                               # bloqueada depois de confirmar
        w.my_cards[0].click()
        self.assertEqual(w.selected, set())                                                    # novas seleções bloqueadas
        w.btn_draw.click()
        self.assertEqual(w.controller.hand.discards[HUMAN], 2)                                 # nada mudou de novo

    def test_bet_controls_use_legal_range_and_presets(self):
        w = make_window()
        for _ in range(30):
            settle(w)
            if w.controller.snapshot()["player_turn"]:
                break
        snap = w.controller.snapshot()
        legal = snap["legal"]
        lo, hi = legal.get("bet") or legal["raise"]
        self.assertEqual((w.spin.minimum(), w.spin.maximum()), (lo, hi))
        w.btn_pot.click()
        self.assertTrue(lo <= w.spin.value() <= hi)
        w.btn_half.click()
        self.assertTrue(lo <= w.spin.value() <= hi)
        w.btn_min.click()
        self.assertEqual(w.spin.value(), lo)
        pot_before = snap["pot"]
        stack_before = snap["stacks"][HUMAN]
        w.btn_bet.click()
        h = w.controller.hand
        self.assertEqual(sum(h.contrib) + sum(h.stacks), 2000)
        self.assertLess(h.stacks[HUMAN], stack_before)
        self.assertIn("🪙", w.my_chips.text())
        self.assertEqual(w.my_chips.text(), f"🪙 {h.stacks[HUMAN]}")
        self.assertGreater(h.pot, pot_before)

    def test_all_in_button(self):
        w = make_window()
        for _ in range(30):
            settle(w)
            if w.controller.snapshot()["player_turn"]:
                break
        self.assertTrue(w.btn_allin.isEnabled())
        w.btn_allin.click()
        self.assertEqual(w.controller.hand.stacks[HUMAN], 0)

    def test_hand_over_shows_banner_and_next_hand_button(self):
        w = make_window()
        play_until_hand_over(w)
        snap = w.controller.snapshot()
        if snap["match_over"]:
            return
        self.assertTrue(w.banner.isVisible())
        self.assertIn(snap["summary"]["text"], w.banner.text())
        self.assertTrue(w.btn_next.isVisible())
        self.assertFalse(w.btn_draw.isVisible())
        if snap["summary"]["reason"] == "showdown":
            self.assertIn("Você:", w.banner.text())
            self.assertTrue(all(c.text() != "♠" for c in w.ai_cards))                           # cartas reveladas só no showdown
        else:
            self.assertTrue(all(c.text() == "♠" for c in w.ai_cards))
        d1 = snap["dealer"]
        w.btn_next.click()
        self.assertFalse(w.banner.isVisible())
        self.assertEqual(w.controller.snapshot()["dealer"], 1 - d1)
        self.assertEqual(sum(w.controller.snapshot()["stacks"]) + w.controller.snapshot()["pot"], 2000)

    def test_stacks_labels_match_controller_after_every_step(self):
        w = make_window()
        for _ in range(40):
            settle(w)
            snap = w.controller.snapshot()
            self.assertEqual(w.my_chips.text(), f"🪙 {snap['stacks'][HUMAN]}")
            self.assertEqual(w.ai_chips.text(), f"🪙 {snap['stacks'][ALICIA]}")
            self.assertEqual(w.pot_label.text(), f"Pote: {snap['pot']}")
            if snap["summary"]:
                break
            if w.btn_draw.isEnabled():
                w.btn_draw.click()
            elif w.btn_call.isEnabled():
                w.btn_call.click()

    def test_match_over_banner_and_new_match_resets_chips(self):
        from alicia_core.poker.engine import Config, Match
        import random
        w = make_window()
        w.controller.close()
        w.controller.config = Config(starting_chips=100, ante=10, min_bet=20)
        w.controller.match = Match(w.controller.config, rng=random.Random(2))
        w.controller.new_match()
        for _ in range(120):
            settle(w)
            if w._last_snap.get("match_over"):
                break
            if w.btn_next.isVisible():
                w.btn_next.click()
            elif w.btn_allin.isEnabled():
                w.btn_allin.click()
            elif w.btn_draw.isEnabled():
                w.btn_draw.click()
            elif w.btn_call.isEnabled():
                w.btn_call.click()
        self.assertTrue(w._last_snap["match_over"])
        self.assertTrue(w.banner.isVisible())
        self.assertIn("Nova partida", w.banner.text())
        self.assertFalse(w.btn_next.isVisible())
        w.controller.config = Config()
        w.controller.match = Match(Config(), rng=random.Random(3))
        w.btn_new_match.click()
        settle(w)
        self.assertEqual(w.controller.snapshot()["match_stacks"], [1000, 1000])

    def test_closing_cancels_pending_ai_and_emits_closed(self):
        w = make_window()
        closed = []
        w.closed.connect(lambda: closed.append(1))
        w.close()
        self.assertEqual(closed, [1])
        events_before = len(w.controller.hand.events)
        settle(w)                                                                              # callbacks velhos disparam
        self.assertEqual(len(w.controller.hand.events), events_before)                         # e não fazem nada
        self.assertFalse(w.anim_timer.active)

    def test_exit_button_closes_without_exception(self):
        w = make_window()
        closed = []
        w.closed.connect(lambda: closed.append(1))
        w.btn_exit.click()
        self.assertEqual(closed, [1])

    def test_ui_errors_do_not_propagate_into_the_game(self):
        w = make_window()
        settle(w)
        w.refresh = lambda snap: (_ for _ in ()).throw(RuntimeError("bug de interface"))
        w.controller.on_change = w._safe(w.refresh)
        w.controller._emit()                                                                   # não levanta
        w.controller.on_say = w._safe(lambda r: 1 / 0)
        w.controller._say(Reaction("oi", "neutral"))                                           # não levanta


class TestPokerWindowAliciaPresence(unittest.TestCase):
    def test_reaction_sets_bubble_and_expression(self):
        w = make_window()
        w.say(Reaction("Hm. Interessante.", "suspicious"))
        self.assertTrue(w.bubble.isVisible())
        self.assertEqual(w.bubble.text(), "Hm. Interessante.")
        self.assertEqual(w._avatar_expr, "pensando")
        w.say(Reaction("Ah, que raiva.", "irritated"))
        self.assertEqual(w._avatar_expr, "bravo")

    def test_voice_speaks_only_when_enabled_and_idle(self):
        core = make_core(tts_enabled=True)
        w = make_window(core)
        core.spoken.clear()
        w.say(Reaction("Pago.", "neutral"))
        self.assertEqual(core.spoken[-1][0], "Pago.")
        n = len(core.spoken)
        core.tts.is_speaking = True
        w.say(Reaction("Outra fala.", "neutral"))
        self.assertEqual(len(core.spoken), n)                                                  # não empilha vozes
        core.tts.is_speaking = False
        core.config.poker_voice = False
        w.say(Reaction("Sem voz.", "neutral"))
        self.assertEqual(len(core.spoken), n)
        self.assertEqual(w.bubble.text(), "Sem voz.")                                         # mas o balão sempre aparece

    def test_game_works_with_voice_disabled(self):
        w = make_window(make_core(tts_enabled=False))
        play_until_hand_over(w)
        self.assertTrue(w.btn_next.isVisible() or w._last_snap.get("match_over"))

    def test_avatar_uses_animation_frames_and_static_expressions(self):
        ui = types.SimpleNamespace()
        keys = [f"idle_{i:02d}" for i in range(1, 19)] + [f"blink_{i}" for i in range(1, 5)] + [f"talk_{i}" for i in range(1, 5)]
        ui.anim_pixmaps = {k: QPixmap(k) for k in keys}
        from alicia_core.enums import AliciaExpression
        ui.pixmaps = {e: QPixmap(e.value) for e in AliciaExpression}
        ui.anim_manifest = {"expression": "serio", "idle": {"frames": 18, "fps": 10},
                            "blink": {"frames": 4, "sequence": [[2, .05], [3, .08], [4, .06]]}, "talk": {"frames": 4}}
        w = make_window(avatar_ui=ui)
        w._avatar_expr, w._expr_until = "serio", 0
        w._tick_avatar()
        self.assertTrue(w.avatar.pixmap.name.startswith(("idle_", "blink_", "talk_")))
        w._avatar_expr = "bravo"
        w._expr_until = 1e12
        w._tick_avatar()
        self.assertEqual(w.avatar.pixmap.name, "bravo")                                        # outras expressões: imagem estática

    def test_avatar_animates_every_emotion_when_ui_has_per_emotion_animations(self):
        from alicia_core.enums import AliciaExpression
        ui = types.SimpleNamespace()
        man = {n: {"expression": n, "idle": {"frames": 6, "fps": 10},
                   "blink": {"frames": 4, "sequence": [[2, .05], [3, .08], [4, .06]]}, "talk": {"frames": 4}}
               for n in ("serio", "bravo")}
        ui.anim_manifests = man
        ui.anim_pixmaps = {f"{n}/{k}": QPixmap(f"{n}/{k}") for n in man
                           for k in [f"idle_{i:02d}" for i in range(1, 7)] + [f"blink_{i}" for i in range(1, 5)] + [f"talk_{i}" for i in range(1, 5)]}
        ui.pixmaps = {e: QPixmap(e.value) for e in AliciaExpression}
        w = make_window(avatar_ui=ui)
        w._avatar_expr, w._expr_until = "bravo", 1e12
        w._tick_avatar()
        self.assertTrue(w.avatar.pixmap.name.startswith("bravo/"), w.avatar.pixmap.name)          # a emoção também anima
        w._avatar_expr = "feliz"                                                                    # sem animação: estática
        w._tick_avatar()
        self.assertEqual(w.avatar.pixmap.name, "feliz")

    def test_window_works_without_avatar_assets(self):
        w = make_window(avatar_ui=None)
        w.say(Reaction("oi", "confident"))
        w._tick_avatar()                                                                       # não levanta


class TestIntegrationWithAlicia(CoreCase):
    def test_text_or_voice_command_opens_the_table(self):
        opened = []
        sig = _Signal()
        sig.connect(lambda: opened.append(1))
        self.core.ui = types.SimpleNamespace(request_poker=sig)
        out = self.core.interact("vamos jogar pôquer", source="voice")
        self.assertEqual(opened, [1])
        self.assertIn("Senta", out)
        self.assertEqual(self.srv.requests, [])                                                # comando local: sem API
        self.assertEqual(self.core.db.count_chat_messages(), 2)

    def test_command_variants_and_no_false_positives(self):
        opened = []
        sig = _Signal(); sig.connect(lambda: opened.append(1))
        self.core.ui = types.SimpleNamespace(request_poker=sig)
        for phrase in ("/poker", "Alicia, bora jogar poker?", "quero jogar uma partida de poker"):
            self.core.interact(phrase)
        self.assertEqual(len(opened), 3)
        n = len(self.srv.requests)
        self.core.interact("como jogar poker?")                                                # pergunta: vai para a conversa
        self.core.interact("quais as regras do poker")
        self.assertEqual(len(opened), 3)
        self.assertEqual(len(self.srv.requests), n + 2)

    def test_without_window_gives_friendly_message(self):
        self.core.ui = None
        out = self.core.interact("/poker")
        self.assertIn("janela", out)

    def test_poker_pauses_spontaneous_commentary_and_other_features_remain(self):
        self.assertFalse(self.core.poker_active)
        import inspect
        src = inspect.getsource(type(self.core).screen_observation_loop)
        self.assertIn("self.poker_active", src)
        self.core.poker_active = True
        antes = len(self.srv.requests)
        out = self.core.interact("me explique o que é uma lista em python")                    # com a mesa aberta, não vai para o chat normal
        self.assertTrue(out)
        self.assertEqual(len(self.srv.requests), antes)
        self.assertEqual(len(self.core.db.get_chat_messages(100)), 0)
        out2 = self.core.interact("/personalidade seria")                                      # personalidade continua valendo
        self.assertEqual(self.core.get_personality(), "seria")

    def test_avatar_window_opens_and_restores(self):
        from alicia_core.ui import desktop_pet
        shown = []
        fake_module = types.ModuleType("alicia_core.ui.poker_window")

        class FakeWindow:
            def __init__(self, core, ui):
                self.closed = _Signal(); self.visible = False
            def show(self): self.visible = True
            def isVisible(self): return self.visible
            def raise_(self): pass
            def activateWindow(self): pass
        fake_module.PokerWindow = FakeWindow
        saved = sys.modules.get("alicia_core.ui.poker_window")
        sys.modules["alicia_core.ui.poker_window"] = fake_module
        try:
            ui = desktop_pet.AliciaDesktopPetUI.__new__(desktop_pet.AliciaDesktopPetUI)
            ui.core = types.SimpleNamespace(poker_active=False)
            ui.poker = None
            ui.hide = lambda: shown.append("hide")
            ui.show = lambda: shown.append("show")
            ui.abrir_poker()
            self.assertTrue(ui.core.poker_active)
            self.assertEqual(shown, ["hide"])
            first = ui.poker
            ui.abrir_poker()                                                                    # reabrir: só traz para frente
            self.assertIs(ui.poker, first)
            first.closed.emit()
            self.assertFalse(ui.core.poker_active)
            self.assertIsNone(ui.poker)
            self.assertEqual(shown[-1], "show")                                                 # o avatar volta
        finally:
            if saved is None:
                sys.modules.pop("alicia_core.ui.poker_window", None)
            else:
                sys.modules["alicia_core.ui.poker_window"] = saved


if __name__ == "__main__":
    unittest.main()
