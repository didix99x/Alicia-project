# -*- coding: utf-8 -*-
"""
Alicia menos robótica: persona de fala natural, aberturas que não se repetem, saudações humanas e voz com prosódia por emoção.
(O tom real das respostas do modelo só dá para avaliar conversando; aqui se confere o que o código manda para ele e para a voz.)
"""

import asyncio
import json
import random
import tempfile
import unittest
from pathlib import Path

from tests.support import APP_DIR  # noqa: F401
from tests.test_alicia import CoreCase

from alicia_core import tts as tts_mod
from alicia_core.enums import AliciaExpression as E
from alicia_core.prompts import PromptEngine


class TestSpeechPreparation(unittest.TestCase):
    def test_removes_everything_that_should_not_be_read_aloud(self):
        out = tts_mod.prepare_for_speech("[FELIZ] Oi!!! *sorri* Olha isso — é **bom** 😄 https://x.com/a e `código`")
        self.assertEqual(out, "Oi! Olha isso, é bom link e código")
        self.assertEqual(tts_mod.prepare_for_speech("[SERIO] \n- item um\n2) item dois"), "item um item dois")
        self.assertEqual(tts_mod.prepare_for_speech("Hum... tá bom"), "Hum... tá bom")      # reticências = pausa, ficam

    def test_prosody_follows_emotion_and_stays_in_safe_range(self):
        rng = random.Random(3)
        feliz = tts_mod.prosody_for(E.FELIZ, 100, True, rng)
        pensando = tts_mod.prosody_for(E.PENSANDO, 100, True, rng)
        self.assertGreater(int(feliz[0].rstrip("%")), int(pensando[0].rstrip("%")))        # feliz mais ligeira que pensativa
        self.assertGreater(int(feliz[1].rstrip("Hz")), int(pensando[1].rstrip("Hz")))      # e mais aguda
        for e in E:
            for speed in (70, 100, 130):
                rate, pitch, vol = tts_mod.prosody_for(e, speed, True, rng)
                self.assertTrue(-35 <= int(rate.rstrip("%")) <= 35 and rate[0] in "+-")
                self.assertTrue(pitch.endswith("Hz") and pitch[0] in "+-")
                self.assertTrue(-20 <= int(vol.rstrip("%")) <= 20)
        flat = tts_mod.prosody_for(E.BRAVO, 100, False)
        self.assertEqual(flat, ("+0%", "+0Hz", "+0%"))                                       # expressiva desligada = voz neutra

    def test_varies_a_little_between_sentences(self):
        seen = {tts_mod.prosody_for(E.SERIO, 100, True, random.Random(i)) for i in range(30)}
        self.assertGreater(len(seen), 3)


class FakeEdge:
    """Substitui edge_tts: registra as chamadas e falha onde o teste mandar."""
    calls = []
    fail_if = staticmethod(lambda voice, kwargs: False)

    class Communicate:
        def __init__(self, text, voice, **kwargs):
            self.text, self.voice, self.kwargs = text, voice, kwargs
            FakeEdge.calls.append((voice, dict(kwargs)))

        async def save(self, path):
            if FakeEdge.fail_if(self.voice, self.kwargs):
                raise RuntimeError("recusado")
            Path(path).write_bytes(b"mp3")


class TestVoiceFallbacks(unittest.TestCase):
    def setUp(self):
        self.saved = tts_mod.edge_tts
        tts_mod.edge_tts = FakeEdge
        FakeEdge.calls = []
        FakeEdge.fail_if = staticmethod(lambda voice, kwargs: False)

    def tearDown(self):
        tts_mod.edge_tts = self.saved

    def run_gen(self, engine, expr=E.FELIZ):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "a.mp3"
            asyncio.run(engine._generate_audio_async("oi", str(out), expr))
            return out.exists()

    def engine(self, **kw):
        e = tts_mod.TextToSpeechEngine.__new__(tts_mod.TextToSpeechEngine)
        e.voice_speed, e.voice_name, e.expressive = 100, kw.get("voice", tts_mod.DEFAULT_VOICE), kw.get("expressive", True)
        return e

    def test_first_attempt_carries_emotion_prosody(self):
        self.assertTrue(self.run_gen(self.engine(), E.SURPRESA))
        voice, kw = FakeEdge.calls[0]
        self.assertEqual(voice, tts_mod.DEFAULT_VOICE)
        self.assertEqual(set(kw), {"rate", "pitch", "volume"})

    def test_pitch_rejected_falls_back_to_rate_only(self):
        FakeEdge.fail_if = staticmethod(lambda voice, kw: "pitch" in kw)
        self.assertTrue(self.run_gen(self.engine()))
        self.assertEqual(set(FakeEdge.calls[-1][1]), {"rate"})

    def test_unknown_custom_voice_falls_back_to_default(self):
        FakeEdge.fail_if = staticmethod(lambda voice, kw: voice == "pt-BR-NaoExisteNeural")
        self.assertTrue(self.run_gen(self.engine(voice="pt-BR-NaoExisteNeural")))
        self.assertEqual(FakeEdge.calls[-1][0], tts_mod.DEFAULT_VOICE)

    def test_everything_failing_raises_so_speak_logs_it(self):
        FakeEdge.fail_if = staticmethod(lambda voice, kw: True)
        with self.assertRaises(RuntimeError):
            self.run_gen(self.engine())


class TestHumanPrompt(unittest.TestCase):
    def test_chat_prompt_has_the_human_speech_rules_and_no_stiff_wording(self):
        sys = PromptEngine.chat_system_prompt("Ana", "agora", [], [])
        self.assertIn("[FALA HUMANA", sys)
        self.assertIn("ROBÓTICO:", sys)
        self.assertIn("Ótima pergunta", sys)                       # listado como coisa a EVITAR
        self.assertNotIn("Elegante ao falar", sys)
        self.assertNotIn("Interessante. Você escolheu violência", PromptEngine.BASE_PERSONALITY)

    def test_avoid_openers_block_only_when_there_are_openers(self):
        self.assertNotIn("ABERTURAS PARA NÃO REPETIR", PromptEngine.chat_system_prompt("Ana", "agora", [], []))
        sys = PromptEngine.chat_system_prompt("Ana", "agora", [], [], avoid_openers=["Ah entendi", "Hum"])
        self.assertIn('"Ah entendi", "Hum"', sys)
        self.assertLess(sys.index("ABERTURAS PARA NÃO REPETIR"), sys.index("[ESTILO ATUAL"))   # estilo continua por último

    def test_greetings_sound_like_a_person(self):
        seen = {PromptEngine.random_greeting("Ana") for _ in range(60)}
        self.assertGreater(len(seen), 2)
        for g in seen:
            self.assertTrue(g.startswith("["), g)
            self.assertNotIn("Sistemas online", g)
            self.assertNotIn("Pronta para começar", g)
        self.assertNotIn("None", PromptEngine.random_greeting(""))


class TestOpenersInConversation(CoreCase):
    def test_recent_openers_reach_the_next_request(self):
        self.srv.reply_fn = lambda msgs: "[FELIZ] Ah, entendi o que você quis dizer."
        self.ask("primeira mensagem sobre cachorros")
        self.ask("segunda mensagem sobre gatos e outros bichos")
        self.ask("terceira mensagem sobre passarinhos")
        system = self.srv.requests[-1]["messages"][0]["content"]
        self.assertIn("ABERTURAS PARA NÃO REPETIR", system)
        self.assertIn('"Ah entendi"', system)

    def test_recent_openers_helper_dedupes_and_ignores_user_lines(self):
        from alicia_core.brain import ConversationBrain
        hist = [{"role": "user", "content": "Oi tudo bem"}, {"role": "assistant", "content": "Hum, deixa ver isso"},
                {"role": "assistant", "content": "[FELIZ] hum, outra coisa"}, {"role": "assistant", "content": "Nossa, sério?"},
                {"role": "assistant", "content": "[SERIO] NOSSA, sério mesmo"}]
        self.assertEqual(ConversationBrain._recent_openers(hist), ["NOSSA sério", "hum outra", "Hum deixa"])   # repetida entra uma vez só


class TestVoiceConfig(unittest.TestCase):
    def test_new_options_have_safe_defaults(self):
        from alicia_core.config import SystemConfig
        with tempfile.TemporaryDirectory() as tmp:
            c = SystemConfig(Path(tmp) / "config.json")
        self.assertEqual(c.voice_name, "pt-BR-FranciscaNeural")
        self.assertTrue(c.voice_expressive)


if __name__ == "__main__":
    unittest.main()
