# -*- coding: utf-8 -*-
"""
Conversa da Alicia durante o pôquer: contexto só com informação pública, falas presas à partida, nada de mistura com a
conversa normal / tela / modo gamer, e fallback para as falas prontas quando a API falha ou demora.
(A API é um servidor local de teste; a janela é o Qt falso. Isto não prova a qualidade das respostas do modelo real.)
"""

import random
import threading
import types
import unittest

from tests.support import APP_DIR  # noqa: F401
from tests.fake_qt import QTimer
from tests.test_alicia import CoreCase
from tests.test_poker_ui import make_core, make_window, settle, pw

from alicia_core.nvidia_client import BrainError
from alicia_core.poker.controller import PokerController
from alicia_core.poker.engine import ALICIA, HUMAN
from alicia_core.poker.reactions import Reaction, ReactionEngine
from alicia_core.poker.talk import PokerTalk, clean_line, grounded, split_expression


class FakeClient:
    has_key = True

    def __init__(self, reply="[FELIZ] Boa jogada.", error=None, gate=None):
        self.reply, self.error, self.gate, self.calls = reply, error, gate, []

    def chat(self, messages, **kw):
        self.calls.append(messages)
        if self.gate is not None:
            self.gate.wait(5)
        if self.error:
            raise self.error
        r = self.reply
        return r(messages) if callable(r) else r


class TestTextHelpers(unittest.TestCase):
    def test_split_expression_and_clean_line(self):
        self.assertEqual(split_expression("[BRAVO] Eita."), ("Eita.", "bravo"))
        self.assertEqual(split_expression("[SÉRIO] ok"), ("ok", "serio"))
        self.assertEqual(split_expression("sem tag"), ("sem tag", None))
        out = clean_line('"Aposto *sorri* agora (risos). Que susto! E mais uma frase. E outra."')
        self.assertNotIn("*", out)
        self.assertNotIn("(", out)
        self.assertLessEqual(out.count("."), 2)
        self.assertLessEqual(len(clean_line("palavra " * 100)), 201)

    def test_grounded_blocks_invented_numbers_hands_and_cards(self):
        ctx = "Pote: 120. Jogadas: o jogador apostou 60."
        self.assertTrue(grounded("Esses 60 me assustaram.", ctx))
        self.assertFalse(grounded("Tenho 500 fichas.", ctx))
        self.assertFalse(grounded("Que full house!", ctx))
        self.assertTrue(grounded("Que full house!", ctx + " Showdown: Alicia tinha full house."))
        self.assertFalse(grounded("Tenho o ás de copas.", ctx))
        self.assertTrue(grounded("Sem números nem cartas, só provocação.", ctx))


class TestPokerTalk(unittest.TestCase):
    def talk(self, client, personality="normal"):
        return PokerTalk(client, personality_getter=lambda: personality, name_getter=lambda: "Ana")

    def test_chat_uses_context_personality_and_strips_tag(self):
        c = FakeClient("[FELIZ] Esses 60 foram corajosos, Ana.")
        t = self.talk(c, "brincalhona")
        text, expr = t.chat("apostei 60", "Pote: 120. o jogador apostou 60.")
        self.assertEqual((text, expr), ("Esses 60 foram corajosos, Ana.", "feliz"))
        system = c.calls[0][0]["content"]
        self.assertIn("[ESTADO DA MESA]", system)
        self.assertIn("Pote: 120", system)
        self.assertIn("brincalhona", system.lower())
        self.assertIn("SOMENTE da partida", system)
        self.assertEqual([m["role"] for m in c.calls[0]], ["system", "user"])

    def test_chat_retries_then_falls_back_when_inventing_facts(self):
        c = FakeClient("[FELIZ] Você tem 999 fichas e um full house!")
        text, expr = self.talk(c).chat("e aí?", "Pote: 20.")
        self.assertEqual(len(c.calls), 2)                              # uma nova tentativa
        self.assertIn("Refaça", c.calls[1][-1]["content"])
        self.assertNotIn("999", text)
        self.assertEqual(expr, "serio")

    def test_chat_api_error_propagates_and_history_stays_alternating(self):
        t = self.talk(FakeClient(error=BrainError("timeout")))
        with self.assertRaises(BrainError):
            t.chat("oi", "ctx")
        c = FakeClient("[SERIO] Certo.")
        t = self.talk(c)
        t.remember_alicia("Fala pronta anterior.")                     # começa por 'assistant': não pode ir antes do 1º 'user'
        t.chat("primeira", "ctx")
        t.chat("segunda", "ctx")
        roles = [m["role"] for m in c.calls[1]]
        self.assertEqual(roles, ["system", "user", "assistant", "user"])

    def test_narrate_returns_none_when_ungrounded(self):
        t = self.talk(FakeClient("[SURPRESA] Apostou 777? Ousado!"))
        self.assertIsNone(t.narrate("O jogador apostou 60.", "Pote: 120.", "surprised"))
        t = self.talk(FakeClient("[SURPRESA] Apostou 60? Ousado!"))
        text, expr = t.narrate("O jogador apostou 60.", "Pote: 120.", "surprised")
        self.assertEqual((text, expr), ("Apostou 60? Ousado!", "surpresa"))


class TestControllerContextAndNarrator(unittest.TestCase):
    def make(self, seed=3):
        rng = random.Random(seed)
        c = PokerController(reactions=ReactionEngine(random.Random(seed)), rng=rng, name_getter=lambda: "Ana")
        c.on_say = lambda r: None
        return c

    def test_context_has_only_public_information(self):
        c = self.make()
        c.new_match()
        ctx = c.table_context()
        self.assertIn("Fichas", ctx)
        self.assertIn("SEGREDO", ctx)
        for card in list(c.hand.hole[HUMAN]) + list(c.hand.hole[ALICIA]):
            self.assertNotIn(card.symbol, ctx)                           # nenhum naipe: nenhuma carta vaza
        self.assertEqual(c.context_text, ctx if c.context_text == ctx else c.context_text)
        self.assertTrue(c.context_text)

    def test_narrator_receives_facts_and_no_narrator_keeps_old_behavior(self):
        c = self.make()
        said, got = [], []
        c.on_say = said.append
        c.narrator = lambda ev, facts, fb: got.append((ev, facts, fb))
        c.new_match()
        self.assertTrue(any(ev == "match_start" for ev, _, _ in got))
        self.assertEqual(said, [])                                       # com narrador, quem fala é o narrador
        c2 = self.make()
        said2 = []
        c2.on_say = said2.append
        c2.new_match()
        self.assertTrue(said2)                                           # sem narrador: fala pronta, como sempre

    def test_broken_narrator_falls_back_to_ready_made_line(self):
        c = self.make()
        said = []
        c.on_say = said.append

        def boom(*a):
            raise RuntimeError("falhou")
        c.narrator = boom
        c.new_match()
        self.assertTrue(said)


def make_talk_core(client, **kw):
    core = make_core(**kw)
    core.nvidia = client
    return core


def join_narrations():
    for t in threading.enumerate():
        if t.name == "PokerNarrate":
            t.join(5)


class TestWindowTalk(unittest.TestCase):
    """A decisão de "falar ou ficar calada" em cada evento é sorteada; aqui a reação é disparada à mão para ser determinística."""

    FALLBACK = Reaction("Fala pronta de teste.", "neutral")

    def window(self, client, join=True):
        win = make_window(make_talk_core(client))
        if join:
            join_narrations()
        settle(win)
        QTimer.queue.clear()
        win.bubble.setText("")
        return win

    def test_narration_replaces_ready_made_line_when_api_answers(self):
        win = self.window(FakeClient("[FELIZ] Começou a brincadeira. Boa sorte."))
        win._narrate("teste", "Uma partida nova começou.", self.FALLBACK)
        join_narrations()
        self.assertEqual(win.bubble.text(), "Começou a brincadeira. Boa sorte.")

    def test_slow_api_uses_ready_made_line_once_and_drops_late_answer(self):
        gate = threading.Event()
        win = self.window(FakeClient("[FELIZ] Resposta tardia.", gate=gate), join=False)
        win._narrate("teste", "O jogador apostou 60.", self.FALLBACK)
        self.assertEqual(win.bubble.text(), "")                          # ainda esperando a API
        QTimer.run_pending(1)                                            # estoura o tempo máximo
        self.assertEqual(win.bubble.text(), "Fala pronta de teste.")
        gate.set()
        join_narrations()
        self.assertEqual(win.bubble.text(), "Fala pronta de teste.")     # a resposta atrasada não fala por cima

    def test_newer_event_makes_the_older_narration_obsolete(self):
        gate = threading.Event()
        win = self.window(FakeClient("[FELIZ] Fala tardia.", gate=gate), join=False)
        win._narrate("a", "Primeiro fato.", Reaction("Fala antiga.", "neutral"))
        win._narrate("b", "Segundo fato.", Reaction("Fala nova.", "neutral"))
        QTimer.run_pending(2)                                            # os dois tempos máximos estouram
        self.assertEqual(win.bubble.text(), "Fala nova.")                 # a antiga nunca aparece
        gate.set()
        join_narrations()
        self.assertEqual(win.bubble.text(), "Fala nova.")

    def test_api_failure_uses_ready_made_line(self):
        win = self.window(FakeClient(error=BrainError("network")))
        win._narrate("teste", "Uma jogada.", self.FALLBACK)
        join_narrations()
        self.assertEqual(win.bubble.text(), "Fala pronta de teste.")

    def test_chat_handler_registered_answers_in_bubble_and_clears_on_close(self):
        client = FakeClient("[BRAVO] Eita, esse 60 doeu.")
        core = make_talk_core(client)
        win = make_window(core)
        self.assertEqual(core.poker_chat_handler, win.handle_user_chat)
        text, face = win.handle_user_chat("apostei 60 agora")
        self.assertEqual(text, "Eita, esse 60 doeu.")
        self.assertEqual(face.value, "bravo")
        self.assertEqual(win.bubble.text(), "Eita, esse 60 doeu.")
        self.assertEqual(core.spoken, [])                                # a voz da resposta é de quem respondeu (voz/teclado)
        win.close()
        self.assertIsNone(core.poker_chat_handler)

    def test_without_api_key_window_keeps_ready_made_lines(self):
        win = make_window(make_core())
        self.assertIsNone(win.talk)
        self.assertIsNone(win.controller.narrator)
        self.assertNotEqual(win.bubble.text(), "")
        text, face = win.handle_user_chat("oi")
        self.assertIn("partida", text)


class TestCoreRoutingDuringPoker(CoreCase):
    def open_table(self):
        win = make_window(self.core)
        for t in threading.enumerate():                                      # a narração de "partida nova" já pediu à API
            if t.name == "PokerNarrate":
                t.join(5)
        self.core.poker_active = True
        self.srv.requests.clear()
        return win

    def test_chat_goes_to_poker_and_not_to_normal_memory(self):
        self.srv.reply_fn = lambda msgs: "[FELIZ] Esses 60 foram corajosos."
        win = self.open_table()
        before = len(self.core.db.get_chat_messages(100))
        out = self.core.interact("apostei 60, tá com medo?")
        self.assertIn("corajosos", out)
        self.assertTrue(out.startswith("[FELIZ]"))
        self.assertEqual(len(self.core.db.get_chat_messages(100)), before)   # nada vai para o histórico normal
        last = self.srv.requests[-1]["messages"][0]["content"]
        self.assertIn("[ESTADO DA MESA]", last)
        self.assertIn("SOMENTE da partida", last)
        self.assertNotIn("MEMÓRIAS CONFIRMADAS", last)

    def test_memory_commands_and_screen_requests_do_not_leak_into_poker(self):
        self.srv.reply_fn = lambda msgs: "[SERIO] Concentrada na mesa."
        self.core.db.add_memory("gosto de café", category="pedido do usuário")
        self.open_table()
        for phrase in ("esquece café, vou blefar", "olha a tela", "lembra que eu odeio perder"):
            self.assertTrue(self.core.interact(phrase))
        self.assertEqual(len(self.core.db.list_memories(limit=10)), 1)       # nada apagado nem criado
        for req in self.srv.requests:
            self.assertIn("[ESTADO DA MESA]", req["messages"][0]["content"])

    def test_personality_command_still_works_in_poker(self):
        self.open_table()
        self.core.interact("/personalidade seria")
        self.assertEqual(self.core.get_personality(), "seria")

    def test_api_error_during_poker_is_controlled(self):
        self.open_table()
        self.srv.script = [("status", 401, "chave inválida")]
        out = self.core.interact("e aí?")
        self.assertTrue(out.startswith("[BRAVO]"))
        self.assertEqual(len(self.core.db.get_chat_messages(100)), 0)

    def test_gamer_mode_is_blocked_while_table_is_open(self):
        self.core.poker_active = True
        self.core.entrar_modo_gamer(manual=True)
        self.assertFalse(self.core.modo_gamer)
        self.core.poker_active = False
        self.core.entrar_modo_gamer(manual=True)
        self.assertTrue(self.core.modo_gamer)

    def test_normal_conversation_returns_after_table_closes(self):
        self.srv.reply_fn = lambda msgs: "[FELIZ] Conversa normal de volta."
        win = self.open_table()
        win.close()
        self.core.poker_active = False                                       # a janela do avatar faz isto ao fechar
        out = self.core.interact("me explique o que é uma lista em python")
        self.assertIn("Conversa normal", out)
        self.assertEqual(len(self.core.db.get_chat_messages(100)), 2)


if __name__ == "__main__":
    unittest.main()
