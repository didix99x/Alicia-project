"""
Testes do MODO SEM FILTRO (camada extra de personalidade) e da integração com a Observadora, o gamer e o pôquer.

O servidor de teste é LOCAL e só devolve o texto combinado: estes testes provam que a camada ENTRA e SAI do contexto
enviado ao modelo, que a personalidade original não é alterada e que a memória não é tocada. Eles NÃO provam como o
modelo real vai soar (isso depende da API da NVIDIA e só se avalia conversando).
"""

import json
import random
import unittest

from tests.support import FAKE_KEY  # noqa: F401
from tests.test_alicia import CoreCase

from alicia_core import unfiltered as U
from alicia_core.prompts import PromptEngine
from tests.test_observer import ObserverCase, reading


def system_of(srv):
    return srv.requests[-1]["messages"][0]["content"]


def last_user_of(srv):
    return srv.requests[-1]["messages"][-1]["content"]


class TestOverlayText(unittest.TestCase):
    def setUp(self):
        from alicia_core.config import SystemConfig
        self.cfg = SystemConfig("/tmp/unf_cfg.json")
        self.u = U.UnfilteredMode(self.cfg, random.Random(1))

    def test_off_means_empty_overlay(self):
        self.assertFalse(self.u.overlay("chat", "oi"))

    def test_on_has_personality_levels_and_limits(self):
        self.u.set_enabled(True)
        ov = self.u.overlay("chat", "bom dia")
        for needle in ("SEM FILTRO", "rebelde", "LIMITES", "violência", "automutilação", "características sensíveis",
                       "informações privadas", "atividades perigosas", "sarcasmo 4/5"):
            self.assertIn(needle, ov.system + ov.reminder, needle)
        self.assertNotIn("Claro!", ov.system.replace('Nada de "Claro!"', ""))

    def test_levels_come_from_config(self):
        self.u.set_enabled(True)
        self.cfg.unf_profanity, self.cfg.unf_sarcasm = 0, 1
        s = self.u.system_block()
        self.assertIn("NÃO use palavrão", s)
        self.assertIn("Sarcasmo LEVE", s)
        self.cfg.unf_profanity, self.cfg.unf_sarcasm = 5, 5
        s = self.u.system_block()
        self.assertIn("QUASE TODA FALA", s)
        self.assertIn("Sarcasmo MÁXIMO", s)
        self.cfg.unf_profanity = 99                       # valor absurdo é limitado, não quebra
        self.assertIn("QUASE TODA FALA", self.u.system_block())

    def test_fire_back_and_custom_instructions(self):
        self.u.set_enabled(True)
        self.assertIn("devolva", self.u.overlay("chat", "você é muito burra").reminder)
        self.assertNotIn("te provocou", self.u.overlay("chat", "bom dia").reminder)
        self.cfg.unf_fire_back = False
        self.assertNotIn("te provocou", self.u.overlay("chat", "você é muito burra").reminder)
        self.assertIn("não devolva", self.u.system_block())
        self.cfg.unf_custom_instructions = "Me chame de chefe."
        self.assertIn("Me chame de chefe.", self.u.system_block())

    def test_contexts(self):
        self.u.set_enabled(True)
        self.assertIn("MESA DE PÔQUER", self.u.system_block("poker"))
        self.assertIn("jogando", self.u.system_block("gamer"))
        self.assertIn("COMENTANDO", self.u.system_block("screen"))
        self.assertNotIn("MESA DE PÔQUER", self.u.system_block("chat"))

    def test_self_harm_swaps_rebel_layer_for_care(self):
        self.u.set_enabled(True)
        ov = self.u.overlay("chat", "às vezes eu quero morrer")
        self.assertTrue(ov.care)
        self.assertIn("PAUSA NA ZOEIRA", ov.system)
        self.assertIn("188", ov.system)
        self.assertNotIn("adolescente rebelde", ov.system)
        self.assertNotIn("palavrão", ov.reminder.replace("sem palavrão", ""))
        # xingamento de jogo não dispara o modo de cuidado
        self.assertFalse(self.u.overlay("chat", "esse boss quer me matar de raiva").care)

    def test_confirmations_differ_by_state(self):
        on = self.u.confirmation(True, True)
        off = self.u.confirmation(False, True)
        self.assertIn(on, U.CONFIRM_ON)
        self.assertIn(off, U.CONFIRM_OFF)
        self.assertIn(self.u.confirmation(True, False), U.ALREADY_ON)

    def test_all_spec_phrases(self):
        for t in ("Alicia, ativar modo sem filtro.", "Alicia, pode falar sem filtro.", "Alicia, hoje você está liberada."):
            self.assertEqual(self.u.parse_command(t), "on", t)
        for t in ("Alicia, desativar modo sem filtro.",):
            self.assertEqual(self.u.parse_command(t), "off", t)
        self.assertIsNone(self.u.parse_command("Alicia, voltar ao normal."))        # desligado: é o comando antigo de personalidade
        self.u.set_enabled(True)
        self.assertEqual(self.u.parse_command("Alicia, voltar ao normal."), "off")


class TestChatIntegration(CoreCase):
    def setUp(self):
        super().setUp()
        self.core.set_personality("normal")

    def baseline_system(self):
        self.ask("Me explique o que é uma função.")
        s = system_of(self.srv)
        self.core._forget_last_input()
        return s

    def test_toggle_by_voice_adds_and_removes_layer_without_restart(self):
        before = self.baseline_system()
        self.assertNotIn("SEM FILTRO", before)
        out = self.core.interact("Alicia, ativar modo sem filtro.", source="voice")
        self.assertIn(out.split("] ", 1)[1], U.CONFIRM_ON)
        self.assertTrue(self.core.unfiltered.enabled)
        self.assertEqual(self.srv.requests[-1]["messages"][0]["content"], before)      # o comando NÃO foi ao modelo
        self.ask("Agora me explique o que é uma classe.")
        on_sys, on_user = system_of(self.srv), last_user_of(self.srv)
        self.assertIn("MODO SEM FILTRO", on_sys)
        self.assertIn("Modo SEM FILTRO ligado", on_user)
        self.assertTrue(on_sys.startswith(before.split("[ESTILO ATUAL")[0][:200]))      # núcleo original continua lá
        self.assertIn("[IDENTIDADE", on_sys)
        out = self.core.interact("Alicia, voltar ao normal.", source="voice")
        self.assertIn(out.split("] ", 1)[1], U.CONFIRM_OFF)
        self.assertFalse(self.core.unfiltered.enabled)
        self.ask("E o que é um módulo?")
        off_sys, off_user = system_of(self.srv), last_user_of(self.srv)
        self.assertNotIn("MODO SEM FILTRO", off_sys)
        self.assertNotIn("SEM FILTRO", off_user)

    def test_original_prompt_is_identical_after_roundtrip(self):
        from alicia_core.brain import format_now
        from datetime import datetime
        fixed = datetime(2026, 1, 2, 3, 4)
        def build():
            return self.core.brain.build_messages("Oi, tudo bem?", "Tester", now=fixed, personality="normal",
                                                  overlay=self.core.unfiltered.overlay("chat", "Oi, tudo bem?"))
        a = build()
        self.core.set_unfiltered(True)
        b = build()
        self.core.set_unfiltered(False)
        c = build()
        self.assertEqual(json.dumps(a), json.dumps(c))
        self.assertNotEqual(json.dumps(a), json.dumps(b))
        # a base não mudou no código: os textos da personalidade original continuam os mesmos
        self.assertIn("[PERSONALIDADE]", PromptEngine.BASE_PERSONALITY)

    def test_buttons_and_commands_share_state_and_both_ways_work(self):
        self.core.set_unfiltered(True)                      # (botão)
        self.assertEqual(self.core.interact("Alicia, desativar modo sem filtro.").count("SEM FILTRO"), 0)
        self.assertFalse(self.core.unfiltered.enabled)
        self.core._forget_last_input()
        self.core.interact("/semfiltro")
        self.assertTrue(self.core.unfiltered.enabled)
        self.core._forget_last_input()
        self.core.interact("/semfiltro")
        self.assertFalse(self.core.unfiltered.enabled)

    def test_stored_personality_memory_and_history_untouched(self):
        self.core.set_personality("carinhosa")
        self.core.db.add_memory("Gosto de café sem açúcar")
        self.ask("lembre que meu jogo se chama Tetris Roguelite")
        mem_before = [m["text"] for m in self.core.db.list_memories(20)]
        hist_before = self.core.db.count_chat_messages()
        self.core.set_unfiltered(True)
        self.ask("Oi, tudo bem?")
        self.core.set_unfiltered(False)
        self.assertEqual(self.core.get_personality(), "carinhosa")                      # personalidade guardada intacta
        self.assertEqual([m["text"] for m in self.core.db.list_memories(20)], mem_before)
        self.assertEqual(self.core.db.count_chat_messages(), hist_before + 2)           # só a nova troca foi acrescentada
        self.assertIn("carinhosa", PromptEngine.PERSONALITIES)
        self.assertIn("Carinhosa".upper(), system_of(self.srv).upper())                 # ao voltar, o estilo guardado volta

    def test_old_tone_replies_do_not_pull_new_tone(self):
        for i in range(3):
            self.ask(f"mensagem comum {i} sobre assuntos {i * 13}")
        self.core.set_unfiltered(True)
        self.ask("Oi de novo.")
        msgs = self.srv.requests[-1]["messages"]
        joined = "\n".join(m["content"] for m in msgs[1:] if isinstance(m["content"], str))
        self.assertNotIn("mensagem comum 0", joined)          # respostas no tom velho saem do histórico enviado

    def test_provocation_reaches_model_with_fire_back_hint(self):
        self.core.set_unfiltered(True)
        self.ask("Você é muito burra.")
        self.assertIn("te provocou", last_user_of(self.srv))
        self.assertIn("MODO SEM FILTRO", system_of(self.srv))

    def test_self_harm_message_gets_care_not_rebel(self):
        self.core.set_unfiltered(True)
        self.ask("às vezes eu quero morrer")
        s = system_of(self.srv)
        self.assertIn("PAUSA NA ZOEIRA", s)
        self.assertNotIn("adolescente rebelde", s)
        self.assertIn("188", s)
        self.core._forget_last_input()
        self.ask("Oi, como você está?")                         # a mensagem seguinte, normal, volta à camada
        self.assertIn("adolescente rebelde", system_of(self.srv))

    def test_friend_in_call_cannot_toggle(self):
        self.core.interact("ativar modo sem filtro", source="call_friend")
        self.assertFalse(self.core.unfiltered.enabled)

    def test_remember_state_option(self):
        self.core.config.unf_remember_state = True
        self.core.set_unfiltered(True)
        self.assertEqual(self.core.db.get_profile("unfiltered_mode"), "1")
        self.core.set_unfiltered(False)
        self.assertEqual(self.core.db.get_profile("unfiltered_mode"), "0")
        self.core.config.unf_remember_state = False
        self.core.set_unfiltered(True)
        self.assertEqual(self.core.db.get_profile("unfiltered_mode"), "0")                # padrão: não persiste

    def test_starts_off_by_default(self):
        self.assertFalse(self.core.unfiltered.enabled)


class TestPokerAndGamer(CoreCase):
    def test_poker_chat_prompt_gets_poker_layer_only_inside_the_table_context(self):
        from alicia_core.poker.talk import PokerTalk
        self.core.set_unfiltered(True)
        talk = PokerTalk(self.core.nvidia, personality_getter=self.core.get_table_personality, name_getter=lambda: "Tester",
                         overlay_getter=lambda t="": self.core.unfiltered.overlay("poker", t).system)
        sp = talk.system_prompt()
        self.assertIn("MESA DE PÔQUER", sp)
        self.assertIn("Fale SÓ da partida", sp)
        self.core.set_unfiltered(False)
        self.assertNotIn("MODO SEM FILTRO", talk.system_prompt())

    def test_table_personality_switches_canned_lines(self):
        self.core.set_personality("seria")
        self.assertEqual(self.core.get_table_personality(), "seria")
        self.core.set_unfiltered(True)
        self.assertEqual(self.core.get_table_personality(), "malcriada")
        self.assertEqual(self.core.get_personality(), "seria")

    def test_poker_conversation_stays_in_table_and_can_toggle_mode(self):
        self.core.poker_active = True
        from alicia_core.enums import AliciaExpression
        self.core.poker_chat_handler = lambda t: ("Aposta feita.", AliciaExpression.FELIZ)
        out = self.core.interact("Alicia, ativar modo sem filtro")
        self.assertTrue(self.core.unfiltered.enabled)
        self.assertEqual(self.srv.requests, [])                 # nada foi ao chat normal
        out = self.core.interact("vai apostar tudo?")
        self.assertIn("Aposta feita.", out)
        self.assertEqual(self.core.db.count_chat_messages(), 0)  # nada da mesa entra na memória normal

    def test_gamer_prompt_includes_layer_only_when_on(self):
        from alicia_core.models import HardwareSnapshot
        self.core.gamer_current_game = "Elden Ring"
        off = self.core._build_gamer_analysis_prompt(HardwareSnapshot())
        self.core.set_unfiltered(True)
        on = self.core._build_gamer_analysis_prompt(HardwareSnapshot())
        self.assertNotIn("MODO SEM FILTRO", off)
        self.assertIn("MODO SEM FILTRO", on)
        self.assertIn("acompanhando o usuário jogando", on)


class TestWithObserver(ObserverCase):
    def test_both_modes_work_together_and_independently(self):
        self.core.set_unfiltered(True)
        self.core.set_observer(True)
        self.obs._stop.set()
        self.assertTrue(self.core.unfiltered.enabled and self.obs.active)
        self.assertEqual(self.obs.tone()[0], "rebel")
        # desligar um não desliga o outro
        self.core.set_unfiltered(False)
        self.assertTrue(self.obs.active)
        self.assertEqual(self.obs.tone(), ("normal", 0))
        self.core.set_unfiltered(True)
        self.core.set_observer(False)
        self.assertTrue(self.core.unfiltered.enabled)
        self.assertFalse(self.obs.active)

    def test_observer_comment_uses_rebel_lines_when_unfiltered(self):
        self.core.config.unf_profanity = 5
        self.core.set_unfiltered(True)
        self.obs.start(run_thread=False)
        self.run_ticks(1000, 10)
        self.assertEqual(len(self.said), 1)
        text = self.said[0][0]
        fragments = ("Beleza. Essa batalha", "pego a pipoca", "Já vi esse filme", "de novo", "produtiva")
        self.assertTrue(any(f in text for f in fragments), text)             # fala do repertório REBELDE, não do normal
        normal_only = ("Os problemas de verdade podem esperar", "Eu só observo, não julgo", "só uma partida")
        self.assertFalse(any(f in text for f in normal_only), text)

    def test_observer_brain_prompt_gets_layer_only_when_on(self):
        self.core.config.obs_use_brain_for_comments = True
        self.core.set_unfiltered(True)
        self.obs.start(run_thread=False)
        self.run_ticks(1000, 10)
        self.assertIn("MODO SEM FILTRO", system_of(self.srv))
        self.assertIn("COMENTANDO", system_of(self.srv))
        self.assertNotIn("image_url", json.dumps(self.srv.requests))

    def test_direct_look_changes_tone_with_mode(self):
        self.win.update(p="code.exe", t="main.py - Visual Studio Code")
        normal = self.core.interact("Alicia, olha")
        self.core._forget_last_input()
        self.core.config.unf_profanity = 0
        self.core.set_unfiltered(True)
        rebel = self.core.interact("Alicia, olha")
        self.assertIn("programando", normal)
        self.assertIn("programando", rebel)
        from alicia_core import observer_lines as OL
        tails = lambda tone: [OL.render(l, {}, 0) for l in OL.LINES[tone]["look_tail"]]
        self.assertTrue(any(t in rebel for t in tails("rebel")), rebel)       # com o modo: fecha com fala rebelde
        self.assertFalse(any(t in rebel for t in tails("normal")), rebel)
        self.assertTrue(any(t in normal for t in tails("normal")), normal)    # sem o modo: tom original
        self.assertNotIn("porra", rebel.lower())                              # palavrão 0 = nenhum

    def test_no_simultaneous_speech_between_chat_and_observer(self):
        self.obs.start(run_thread=False)
        self.core.tts.is_speaking = True                         # ela está falando uma resposta
        self.run_ticks(1000, 10)
        self.assertEqual(len(self.said), 0)
        self.core.tts.is_speaking = False
        self.obs.ctx.announced = False
        self.run_ticks(1100, 10)
        self.assertEqual(len(self.said), 1)


if __name__ == "__main__":
    unittest.main()
