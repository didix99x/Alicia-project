"""
Testes da Modo Observadora (visão local, contexto temporário, política de comentários, privacidade, comandos, thread).

Tudo aqui é local. A captura real de tela só existe no Windows/X11; aqui as imagens são desenhadas com Pillow
e injetadas (o teste de captura REAL roda à parte, com Xvfb — ver README/relatório).
"""

import time
import unittest

from tests.support import FAKE_KEY  # noqa: F401  (também prepara o sys.path e o Qt de teste)
from tests.test_alicia import CoreCase

from PIL import Image, ImageDraw, ImageFont

from alicia_core.enums import AliciaExpression
from alicia_core.local_vision import (ActivityClassifier, FrameAnalyzer, LocalVision, OcrEngine, PrivacyGuard,
                                      ScreenReading, detect_events, norm)
from alicia_core.observer import VisualContext, parse_observer_command, too_similar
from alicia_core import observer_lines

FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"


def solid(color=(30, 30, 30), size=(800, 450)):
    return Image.new("RGB", size, color)


def text_image(lines, size=(1400, 700), bg=(255, 255, 255), fg=(0, 0, 0)):
    img = Image.new("RGB", size, bg)
    d = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype(FONT, 24)
    except OSError:
        font = ImageFont.load_default()
    for i, line in enumerate(lines):
        d.text((30, 30 + i * 36), line, font=font, fill=fg)
    return img


ERROR_LINES = ["def calcular(a, b):", "    return a - dano", "", "Traceback (most recent call last):",
               '  File "main.py", line 42, in <module>', "NameError: name 'dano' is not defined"]


def reading(category="game", app="Elden Ring", process="eldenring.exe", **kw):
    r = ScreenReading(ts=kw.pop("ts", 0.0), process=process, title=kw.pop("title", app), category=category,
                      app_label=app, activity=f"jogando {app}")
    for k, v in kw.items():
        setattr(r, k, v)
    return r


class ObserverCase(CoreCase):
    def setUp(self):
        super().setUp()
        c = self.core
        cfg = c.config
        cfg.obs_use_brain_for_comments = False        # falas locais: determinístico (o cérebro é testado à parte)
        cfg.obs_talkativeness = 1.0
        cfg.obs_settle_seconds = 20.0
        cfg.obs_comment_cooldown = 90.0
        cfg.obs_event_cooldown = 40.0
        cfg.obs_quiet_after_user_seconds = 20.0
        c.last_user_activity_time = 0.0
        self.win = {"p": "eldenring.exe", "t": "ELDEN RING"}
        self.captures = 0
        self.image = solid()

        def capture_raw(mode="active", rect=None):
            self.captures += 1
            return self.image

        c.screen_engine.capture_raw = capture_raw
        c.observer.vision = LocalVision(cfg, c.screen_engine, lambda: (self.win["p"], self.win["t"]),
                                        game_detector=lambda t, p: (c._normalize_game_name(t) if p == "eldenring.exe" else None))
        self.said = []
        c._speak_async = lambda text, expr, should_play=None: self.said.append((text, expr, should_play))
        self.obs = c.observer
        self.obs.rng.seed(1)

    def run_ticks(self, start, count, step=4.0, image=None):
        out = []
        for i in range(count):
            out.append(self.obs.tick(now=start + i * step, image=image))
        return out


# ======================================================================
class TestContinuity(unittest.TestCase):
    def setUp(self):
        from alicia_core.config import SystemConfig
        self.cfg = SystemConfig("/tmp/obs_ctx_cfg.json")
        self.ctx = VisualContext(self.cfg)

    def test_menu_and_return_is_same_activity(self):
        # jogo -> jogo -> (menu do mesmo processo) -> jogo: continua a MESMA atividade
        t = 1000.0
        phases = [self.ctx.update(reading(title=title, ts=t + i * 4), t + i * 4)
                  for i, title in enumerate(["Elden Ring", "Elden Ring", "Elden Ring - Menu", "Elden Ring"])]
        self.assertEqual(phases, ["first", "continue", "continue", "continue"])
        self.assertEqual(self.ctx.switches, 0)
        self.assertEqual(len(self.ctx.entries), 4)

    def test_short_alt_tab_does_not_switch_but_long_stay_does(self):
        self.ctx.update(reading(), 1000)
        self.assertEqual(self.ctx.update(reading("browsing", "Chrome", "chrome.exe"), 1004), "pending")
        self.assertEqual(self.ctx.update(reading(), 1008), "continue")        # voltou: nada mudou
        self.assertEqual(self.ctx.current_key, reading().activity_key)
        self.ctx.update(reading("coding", "Visual Studio Code", "code.exe"), 1100)
        self.assertEqual(self.ctx.update(reading("coding", "Visual Studio Code", "code.exe"), 1125), "switch")
        self.assertEqual(self.ctx.switches, 1)
        self.assertEqual(self.ctx.current_key, "coding|visual studio code")

    def test_clear_wipes_everything(self):
        self.ctx.update(reading(), 1000)
        self.ctx.remember_comment("oi", "start", 1000)
        self.ctx.clear()
        self.assertEqual((len(self.ctx.entries), self.ctx.current_key, self.ctx.last_comment, len(self.ctx.comment_log)), (0, "", "", 0))

    def test_entries_are_pruned_by_age_and_count(self):
        self.cfg.obs_context_minutes = 1.0
        self.cfg.obs_context_max_items = 5
        for i in range(20):
            self.ctx.update(reading(), 1000 + i * 2)
        self.assertLessEqual(len(self.ctx.entries), 5)
        self.ctx.update(reading(), 5000)
        self.assertEqual(len(self.ctx.entries), 1)


class TestFrameAnalyzer(unittest.TestCase):
    def test_identical_frames_have_zero_change_and_big_change_is_detected(self):
        fa = FrameAnalyzer()
        a = solid((20, 20, 20))
        self.assertTrue(fa.analyze(a).first)
        s = fa.analyze(a)
        self.assertEqual((s.change, s.motion), (0.0, 0.0))
        s = fa.analyze(solid((220, 220, 220)))
        self.assertGreater(s.change, 50)
        self.assertGreater(s.motion, 90)

    def test_small_change_is_small(self):
        fa = FrameAnalyzer()
        base = solid((50, 50, 50))
        fa.analyze(base)
        img = base.copy()
        ImageDraw.Draw(img).rectangle((0, 0, 40, 20), fill=(255, 255, 255))
        s = fa.analyze(img)
        self.assertLess(s.change, 5)
        self.assertGreater(s.change, 0)


class TestClassifier(unittest.TestCase):
    def setUp(self):
        self.c = ActivityClassifier()

    def test_known_apps_and_files(self):
        r = self.c.classify("code.exe", "main.py - Alicia - Visual Studio Code")
        self.assertEqual((r.category, r.file_hint), ("coding", "main.py"))
        self.assertIn("programando", r.activity)
        self.assertEqual(self.c.classify("spotify.exe", "Spotify").category, "music")
        self.assertEqual(self.c.classify("discord.exe", "Discord").category, "chat")

    def test_browser_uses_site(self):
        r = self.c.classify("chrome.exe", "Aula de Python - YouTube - Google Chrome")
        self.assertEqual((r.category, r.app_label), ("video", "YouTube"))
        r = self.c.classify("msedge.exe", "como usar pygame - Pesquisa Google - Microsoft Edge")
        self.assertEqual(r.category, "searching")
        r = self.c.classify("firefox.exe", "Alguma coisa qualquer - Mozilla Firefox")
        self.assertEqual(r.category, "browsing")

    def test_unknown_window_is_unknown_not_invented(self):
        r = self.c.classify("coisa_estranha.exe", "Janela X")
        self.assertEqual((r.category, r.known), ("unknown", False))

    def test_game_detector_is_used(self):
        c = ActivityClassifier(lambda t, p: "Elden Ring" if p == "eldenring.exe" else None)
        self.assertEqual(c.classify("eldenring.exe", "ELDEN RING").category, "game")

    def test_events(self):
        ev, sig = detect_events("coding", norm("Traceback (most recent call last):\nNameError: x"))
        self.assertEqual(ev, ["erro_visivel"])
        self.assertTrue(sig)
        self.assertEqual(detect_events("game", norm("YOU DIED"))[0], ["jogo_derrota"])
        self.assertEqual(detect_events("game", norm("VICTORY"))[0], ["jogo_vitoria"])
        self.assertEqual(detect_events("browsing", norm("Traceback error: coisa"))[0], [])      # erro só vale em código/terminal
        self.assertEqual(detect_events("coding", norm("Ran 5 tests in 0.1s\n\nOK"))[0], ["testes_ok"])


class TestPrivacyGuard(unittest.TestCase):
    def setUp(self):
        from alicia_core.config import SystemConfig
        self.g = PrivacyGuard(SystemConfig("/tmp/obs_priv_cfg.json"))

    def test_windows_blocked(self):
        self.assertTrue(self.g.check_window("keepassxc.exe", "KeePassXC"))
        self.assertTrue(self.g.check_window("chrome.exe", "Internet Banking - Nubank - Google Chrome"))
        self.assertTrue(self.g.check_window("chrome.exe", "Entrar - Login - Google Chrome"))
        self.assertTrue(self.g.check_window("chrome.exe", "Sessão - Navegação anônima"))
        self.assertIsNone(self.g.check_window("code.exe", "main.py - Visual Studio Code"))
        self.assertIsNone(self.g.check_window("aseprite.exe", "pixel art - Aseprite"))      # "pix" não pode pegar "pixel"

    def test_sensitive_text(self):
        self.assertTrue(self.g.text_is_sensitive("Cartão 4111 1111 1111 1111 validade"))
        self.assertTrue(self.g.text_is_sensitive("CPF 123.456.789-09"))
        self.assertTrue(self.g.text_is_sensitive("sua senha: abc"))
        self.assertTrue(self.g.text_is_sensitive("NVIDIA_API_KEY=nvapi-abcdef"))
        self.assertFalse(self.g.text_is_sensitive("def calcular(a, b): return a + b"))
        self.assertFalse(self.g.text_is_sensitive("Número 1234 5678 1234 5678 não é cartão válido"))   # Luhn inválido


class TestOcr(unittest.TestCase):
    def test_ocr_reads_text_and_detects_error(self):
        cfg = __import__("alicia_core.config", fromlist=["SystemConfig"]).SystemConfig("/tmp/obs_ocr_cfg.json")
        ocr = OcrEngine(cfg)
        if not ocr.available:
            self.skipTest("nenhum motor de OCR instalado neste ambiente")
        text, backend = ocr.read(text_image(ERROR_LINES))
        self.assertTrue(backend)
        self.assertIn("Traceback", text)
        self.assertIn("NameError", text)

    def test_ocr_reads_dark_theme(self):
        cfg = __import__("alicia_core.config", fromlist=["SystemConfig"]).SystemConfig("/tmp/obs_ocr_cfg.json")
        ocr = OcrEngine(cfg)
        if not ocr.available:
            self.skipTest("nenhum motor de OCR instalado neste ambiente")
        text, _ = ocr.read(text_image(ERROR_LINES, bg=(25, 25, 30), fg=(230, 230, 230)))
        self.assertIn("Traceback", text)


# ======================================================================
class TestCommands(unittest.TestCase):
    def test_observer_commands(self):
        on = ["Alicia, observar minha tela.", "Alicia, comece a observar.", "Alicia, comece a observar", "Alícia, fica observando",
              "ativar modo observadora", "Alicia, observa a minha tela"]
        off = ["Alicia, pare de observar.", "Alicia, para de observar", "alicia chega de observar", "desligar modo observadora",
               "Alicia, desativa o modo observadora"]
        what = ["Alicia, o que estou fazendo?", "Alicia, o que eu estou fazendo agora?", "o que eu to fazendo"]
        for t in on:
            self.assertEqual(parse_observer_command(t), "on", t)
        for t in off:
            self.assertEqual(parse_observer_command(t), "off", t)
        for t in what:
            self.assertEqual(parse_observer_command(t), "what", t)

    def test_not_commands(self):
        for t in ["Alicia, olha minha tela.", "Alicia, olha", "como funciona o modo observadora?", "o que é observar?",
                  "bom dia", "eu estou observando um passarinho"]:
            self.assertIsNone(parse_observer_command(t), t)

    def test_similarity(self):
        self.assertTrue(too_similar("Você está jogando de novo hoje", ["Você está jogando de novo hoje!"]))
        self.assertFalse(too_similar("Esse erro está te perseguindo", ["Vitória na partida"]))


class TestLines(unittest.TestCase):
    def test_profanity_levels(self):
        import random
        tpl = "Voltou pro código[[, porra]]. Boa."
        self.assertNotIn("porra", observer_lines.render(tpl, {}, 0, random.Random(1)))
        self.assertIn("porra", observer_lines.render(tpl, {}, 5, random.Random(1)))
        hits = sum("porra" in observer_lines.render(tpl, {}, 3, random.Random(i)) for i in range(400))
        self.assertTrue(150 < hits < 330, hits)        # ~60%

    def test_every_tone_has_every_key_or_falls_back(self):
        for tone in ("normal", "rebel", "gentle"):
            for key in ("start:game", "start:coding", "start:xyz", "long:video", "long:xyz", "event:erro_visivel",
                        "event:jogo_vitoria", "look_tail", "silence_privacy"):
                self.assertTrue(observer_lines.pool_for(tone, key), (tone, key))

    def test_no_repeat_until_pool_exhausted(self):
        import random
        rng = random.Random(3)
        seen = []
        for _ in range(3):
            seen.append(observer_lines.pick("normal", "start:coding", {"app": "X"}, 0, seen, rng))
        self.assertEqual(len(set(seen)), 3)


# ======================================================================
class TestObserverPolicy(ObserverCase):
    def start(self):
        return self.obs.start(run_thread=False)     # sem thread de fundo: os testes dirigem tick() à mão

    def test_inactive_does_not_capture(self):
        self.assertEqual(self.obs.tick(now=1000), "inactive")
        self.assertEqual(self.captures, 1 if False else 0)

    def test_start_probes_capture_and_reports_cause_on_failure(self):
        def broken(*a, **k):
            raise RuntimeError("monitor indisponível")
        self.core.screen_engine.capture_raw = broken
        msg = self.obs.start(run_thread=False)
        self.assertIn("monitor indisponível", msg)
        self.assertFalse(self.obs.active)

    def test_new_activity_comments_once_after_settle(self):
        self.start()
        outs = self.run_ticks(1000, 10, step=4)        # 36 s de jogo estável
        self.assertEqual(outs.count("commented"), 1, outs)
        self.assertEqual(len(self.said), 1)
        text, expr, should_play = self.said[0]
        self.assertTrue(text)
        self.assertTrue(should_play())
        # continua no mesmo jogo: nada de novo comentário (cooldown + já anunciado)
        self.assertNotIn("commented", self.run_ticks(1100, 10, step=4))

    def test_long_activity_marks(self):
        self.start()
        self.core.config.obs_comment_cooldown = 5.0
        self.run_ticks(1000, 8, step=4)
        n0 = len(self.said)
        outs = []
        t = 1100.0
        for _ in range(40):                            # salta até passar de 20 min no mesmo jogo
            t += 40
            outs.append(self.obs.tick(now=t))
        self.assertGreater(len(self.said), n0)
        self.assertIn("minutos", " ".join(s[0] for s in self.said) + " minutos")   # texto presente
        # o marco de 20 min só dispara uma vez
        count = len(self.said)
        for _ in range(5):
            t += 10
            self.obs.tick(now=t)
        self.assertEqual(len(self.said), count)

    def test_quiet_when_user_just_talked_and_when_tts_busy(self):
        self.start()
        self.core.last_user_activity_time = 1030.0
        outs = self.run_ticks(1000, 10, step=4)       # 1000..1036: o usuário falou em 1030 (conversa tem prioridade)
        self.assertNotIn("commented", outs[7:])
        self.core.last_user_activity_time = 0.0
        self.core.tts.is_speaking = True
        self.obs.ctx.announced = False
        self.assertEqual(self.obs.tick(now=1100), "observed")
        self.assertIn("falando", self.obs.last_block)
        self.core.tts.is_speaking = False

    def test_turn_lock_blocks_comment(self):
        self.start()
        self.core._turn_lock.acquire()
        try:
            outs = self.run_ticks(1000, 10, step=4)
            self.assertNotIn("commented", outs)
            self.assertIn("conversa", self.obs.last_block)
        finally:
            self.core._turn_lock.release()

    def test_error_event_comments_once_per_error(self):
        self.win.update(p="code.exe", t="main.py - Alicia - Visual Studio Code")
        self.core.config.obs_ocr_every = 0.0
        if not self.obs.vision.ocr.available:
            self.skipTest("sem OCR neste ambiente")
        reasons = []
        real = self.obs._comment
        self.obs._comment = lambda d, r, now, gen: (reasons.append(d.reason + ":" + d.event), real(d, r, now, gen))[1]
        self.start()
        img = text_image(ERROR_LINES)
        outs = [self.obs.tick(now=1000 + i * 4, image=img) for i in range(7)]
        self.assertEqual(outs.count("commented"), 1, outs)
        self.assertEqual(reasons, ["event:erro_visivel"])
        # o MESMO erro continua na tela por muito tempo: não gera outro comentário de EVENTO
        for i in range(40):
            self.obs.tick(now=1100 + i * 4, image=img)
        self.assertEqual([r for r in reasons if r.startswith("event")], ["event:erro_visivel"])

    def test_error_event_uses_event_cooldown_not_activity_cooldown(self):
        self.core.config.obs_ocr_every = 0.0
        self.win.update(p="code.exe", t="main.py - VS Code")
        if not self.obs.vision.ocr.available:
            self.skipTest("sem OCR neste ambiente")
        self.start()
        self.obs.ctx.remember_comment("comentário antigo qualquer", "start", 1000.0)     # há 50 s: > 40 s de evento, < 90 s de atividade
        self.core.last_comment_time = 1000.0
        out = self.obs.tick(now=1050, image=text_image(ERROR_LINES))
        self.assertEqual(out, "commented")

    def test_comments_are_not_repeated_in_a_row(self):
        self.start()
        texts = []
        self.core.config.obs_comment_cooldown = 1.0
        self.core.config.obs_settle_seconds = 1.0
        for i in range(8):
            self.win.update(p="code.exe" if i % 2 == 0 else "spotify.exe", t="x")
            for k in range(3):
                self.obs.tick(now=2000 + i * 100 + k * 50)
        texts = [s[0] for s in self.said]
        self.assertEqual(len(texts), len(set(texts)))

    def test_hourly_cap(self):
        self.start()
        self.core.config.obs_max_comments_per_hour = 1
        self.core.config.obs_comment_cooldown = 1.0
        self.core.config.obs_settle_seconds = 1.0
        for i in range(6):
            self.win.update(p=["code.exe", "spotify.exe", "chrome.exe", "discord.exe", "winword.exe", "blender.exe"][i], t="t")
            for k in range(3):
                self.obs.tick(now=3000 + i * 60 + k * 20)
        self.assertEqual(len(self.said), 1)

    def test_stale_comment_is_dropped_by_should_play(self):
        self.start()
        self.run_ticks(1000, 10, step=4)
        should_play = self.said[0][2]
        self.assertTrue(should_play())
        self.core.poker_active = True
        self.assertFalse(should_play())
        self.core.poker_active = False
        self.obs.stop()
        self.assertFalse(should_play())

    def test_poker_and_rest_pause_capturing_entirely(self):
        self.start()
        self.obs.tick(now=1000)
        before = self.captures
        self.core.poker_active = True
        self.assertEqual(self.obs.tick(now=1004), "paused")
        self.core.poker_active = False
        self.core.modo_descanso = True
        self.assertEqual(self.obs.tick(now=1008), "paused")
        self.core.modo_descanso = False
        self.assertEqual(self.captures, before)

    def test_private_window_is_not_even_captured(self):
        self.start()
        base = self.captures
        self.win.update(p="chrome.exe", t="Internet Banking - Nubank - Google Chrome")
        self.assertEqual(self.obs.tick(now=1000), "private")
        self.assertEqual(self.captures, base)
        self.assertEqual(len(self.obs.ctx.entries), 0)
        self.win.update(p="keepassxc.exe", t="")
        self.assertEqual(self.obs.tick(now=1004), "private")
        self.assertEqual(len(self.said), 0)

    def test_sensitive_text_is_discarded_and_not_commented(self):
        self.core.config.obs_ocr_every = 0.0
        if not self.obs.vision.ocr.available:
            self.skipTest("sem OCR neste ambiente")
        self.win.update(p="chrome.exe", t="Loja - Google Chrome")
        self.start()
        out = self.obs.tick(now=1000, image=text_image(["Numero do cartao 4111 1111 1111 1111  CVV 123"]))
        self.assertEqual(out, "private")
        self.assertEqual(self.obs.vision._last_ocr_text, "")
        self.assertEqual(len(self.obs.ctx.entries), 0)

    def test_stop_clears_context_and_stops_captures(self):
        self.start()
        self.run_ticks(1000, 3)
        self.assertGreater(len(self.obs.ctx.entries), 0)
        self.obs.stop(wait=True)
        self.assertFalse(self.obs.active)
        self.assertEqual((len(self.obs.ctx.entries), self.obs.ctx.current_key), (0, ""))
        self.assertIsNone(self.obs.vision.frames._prev)
        self.assertFalse(self.obs.vision.ocr._probed)
        c = self.captures
        self.assertEqual(self.obs.tick(now=2000), "inactive")
        self.assertEqual(self.captures, c)

    def test_stop_during_read_discards_result(self):
        self.start()
        real = self.obs.vision.read

        def read_then_stop(**k):
            r = real(**k)
            self.obs.stop()
            return r

        self.obs.vision.read = read_then_stop
        self.assertEqual(self.obs.tick(now=1000), "stale")
        self.assertEqual(len(self.obs.ctx.entries), 0)

    def test_gamer_mode_with_vision_owns_game_comments(self):
        self.start()
        self.core.modo_gamer = True
        self.core.vision_spontaneous = True
        self.assertNotIn("commented", self.run_ticks(1000, 10))
        self.assertIn("Modo Gamer", self.obs.last_block)
        self.core.vision_spontaneous = False            # gamer sem análise de imagem: a Observadora assume
        self.obs.ctx.announced = False
        self.assertIn("commented", self.run_ticks(1100, 10))

    def test_observer_never_touches_database(self):
        n = self.core.db.count_chat_messages()
        self.start()
        self.run_ticks(1000, 10)
        self.assertEqual(self.core.db.count_chat_messages(), n)
        import sqlite3
        with sqlite3.connect(self.core.config.db_path) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM vision_logs").fetchone()[0], 0)

    def test_brain_receives_only_abstract_facts(self):
        self.core.config.obs_use_brain_for_comments = True
        self.core.config.obs_ocr_every = 0.0
        self.win.update(p="code.exe", t="segredo_do_projeto.py - chave_interna_xyz - Visual Studio Code")
        self.srv.reply_fn = lambda m: "[FELIZ] Comentário vindo do cérebro, bem específico."
        self.start()
        self.run_ticks(1000, 10)
        payload = str(self.srv.requests)
        self.assertEqual(len(self.said), 1)
        self.assertIn("Comentário vindo do cérebro", self.said[0][0])
        self.assertNotIn("segredo_do_projeto", payload)            # título da janela NÃO vai
        self.assertNotIn("chave_interna_xyz", payload)
        self.assertNotIn("image_url", payload)                   # nenhuma imagem
        self.assertIn("Visual Studio Code", payload)             # só o nome do programa

    def test_brain_failure_falls_back_to_local_lines(self):
        self.core.config.obs_use_brain_for_comments = True
        self.srv.script = [("status", 500, "falhou")] * 6
        self.start()
        outs = self.run_ticks(1000, 10)
        self.assertIn("commented", outs)
        self.assertEqual(len(self.said), 1)
        self.assertGreater(self.obs._brain_block_until, 0)


class TestImmediateLook(ObserverCase):
    """'Alicia, olha' / 'o que estou fazendo?': análise real da imagem, só local e sem observadora ligada."""

    def setUp(self):
        super().setUp()

    def test_look_describes_activity_without_remote_vision(self):
        self.win.update(p="code.exe", t="main.py - Alicia - Visual Studio Code")
        out = self.core.interact("Alicia, olha minha tela", source="voice")
        self.assertIn("programando", out)
        self.assertIn("main.py", out)
        self.assertEqual(self.srv.requests, [])                  # nem cérebro, nem visão remota
        self.assertFalse(self.core.vision.available)

    def test_look_reads_error_on_screen(self):
        if not self.obs.vision.ocr.available:
            self.skipTest("sem OCR neste ambiente")
        self.win.update(p="code.exe", t="main.py - Visual Studio Code")
        self.image = text_image(ERROR_LINES)
        out = self.core.interact("Alicia, olha")
        self.assertIn("erro", out.lower())

    def test_what_am_i_doing_uses_context_duration(self):
        self.win.update(p="code.exe", t="main.py - Visual Studio Code")
        self.obs.start(run_thread=False)
        now = time.time()
        self.obs.ctx.update(reading("coding", "Visual Studio Code", "code.exe", activity="programando no Visual Studio Code"), now - 600)
        self.obs.ctx.current_since = now - 600
        out = self.core.interact("Alicia, o que estou fazendo?")
        self.assertIn("10 minutos", out)

    def test_look_on_private_window_refuses(self):
        self.win.update(p="chrome.exe", t="Banco do Brasil - Google Chrome")
        base = self.captures
        out = self.core.interact("Alicia, olha")
        self.assertTrue("sensível" in out.lower() or "privacidade" in out.lower(), out)
        self.assertEqual(self.captures, base)

    def test_look_admits_when_it_cannot_understand(self):
        self.win.update(p="coisa_estranha.exe", t="Janela X")
        self.core.config.obs_ocr = False
        out = self.core.interact("Alicia, olha")
        self.assertIn("não consegui identificar", out)
        self.assertEqual(self.core.db.count_chat_messages(), 0)  # erro/dúvida não vira fala gravada

    def test_look_reports_capture_failure_with_cause(self):
        def broken(*a, **k):
            raise RuntimeError("tela bloqueada")
        self.core.screen_engine.capture_raw = broken
        out = self.core.interact("Alicia, olha")
        self.assertIn("tela bloqueada", out)

    def test_look_frees_ocr_when_observer_off(self):
        self.win.update(p="code.exe", t="x.py - VS Code")
        self.core.interact("Alicia, olha")
        self.assertFalse(self.obs.vision.ocr._probed)
        self.assertIsNone(self.obs.vision.frames._prev)

    def test_wake_variants_still_trigger_look(self):
        self.win.update(p="code.exe", t="x.py - VS Code")
        for phrase in ("Alicia, olha", "Alícia, ver", "Alicia olhar isso", "Alicia, por favor olha"):
            out = self.core.interact(phrase, source="voice")
            self.assertIn("programando", out, phrase)
            self.core._forget_last_input()


class TestObserverViaChat(ObserverCase):
    def test_voice_commands_toggle_observer_and_dont_hit_brain(self):
        out = self.core.interact("Alicia, observar minha tela.", source="voice")
        self.assertIn("Observadora ligada", out)
        self.assertTrue(self.obs.active)
        out = self.core.interact("Alicia, pare de observar.", source="voice")
        self.assertIn("desligada", out)
        self.assertFalse(self.obs.active)
        self.assertEqual(self.srv.requests, [])

    def test_slash_command_toggles(self):
        self.core.interact("/observar")
        self.assertTrue(self.obs.active)
        self.core._forget_last_input()
        self.core.interact("/observar")
        self.assertFalse(self.obs.active)

    def test_starts_off_and_friend_in_call_cannot_toggle(self):
        self.assertFalse(self.core.observer.active)
        self.core.interact("Alicia, observar minha tela", source="call_friend")
        self.assertFalse(self.obs.active)

    def test_poker_blocks_observer_command_but_not_conversation(self):
        self.core.poker_active = True
        self.core.poker_chat_handler = lambda t: ("Só jogo.", "serio")
        out = self.core.interact("Alicia, observar minha tela")
        self.assertIn("cartas", out)
        self.assertFalse(self.obs.active)

    def test_normal_chat_unaffected_and_visual_context_not_mixed(self):
        self.core.interact("Alicia, observar minha tela")
        self.obs.ctx.update(reading(), 1000)
        self.core.last_visual_context = "(observadora) jogando Elden Ring"
        self.core.interact("Me explique o que é uma lista em Python.")
        system = self.srv.requests[-1]["messages"][0]["content"]
        self.assertNotIn("Elden Ring", system)                   # assunto sem relação com a tela: contexto visual não entra
        self.core._forget_last_input()
        self.core.interact("o que você está vendo na tela agora, jogo?")
        self.assertIn("Elden Ring", self.srv.requests[-1]["messages"][0]["content"])

    def test_thread_runs_and_stops(self):
        self.core.config.obs_interval = 1.0
        self.core.start_msg = self.obs.start()
        time.sleep(2.6)
        self.assertGreaterEqual(self.obs.stats["ticks"], 2)
        self.obs.stop(wait=True)
        ticks = self.obs.stats["ticks"]
        time.sleep(1.5)
        self.assertEqual(self.obs.stats["ticks"], ticks)
        self.assertFalse(self.obs._thread)

    def test_ui_buttons_toggle_modes(self):
        from tests.support import install_qt_stubs_if_needed  # noqa: F401
        from alicia_core.ui.desktop_pet import AliciaDesktopPetUI
        ui = object.__new__(AliciaDesktopPetUI)
        ui.core = self.core

        class Btn:
            text = ""
            style = ""
            def setText(s, t): s.text = t
            def setStyleSheet(s, t): s.style = t
        class Lbl:
            shown = False
            def show(s): s.shown = True
            def hide(s): s.shown = False
        ui.btn_obs, ui.btn_unf, ui.lbl_observando = Btn(), Btn(), Lbl()
        ui._modes_seen = (None, None)
        ui._refresh_mode_controls()
        self.assertIn("OFF", ui.btn_obs.text)
        self.assertFalse(ui.lbl_observando.shown)
        ui.alternar_observadora()
        self.assertTrue(self.obs.active)
        self.assertIn("ON", ui.btn_obs.text)
        self.assertTrue(ui.lbl_observando.shown)                 # indicador de privacidade visível
        ui.alternar_sem_filtro()
        self.assertTrue(self.core.unfiltered.enabled)
        self.assertIn("ON", ui.btn_unf.text)
        ui.alternar_observadora()
        self.assertFalse(ui.lbl_observando.shown)
        self.assertIn("ON", ui.btn_unf.text)                     # desligar um não desliga o outro


class TestWorksWithoutImageBackend(ObserverCase):
    def test_core_boots_and_observer_works_without_image_backend(self):
        self.core._last_vision_recheck = 0.0
        self.core._maybe_recheck_vision()
        self.assertTrue(self.core.vision_spontaneous)           # Modo Gamer analisa localmente (precisa só do NumPy)
        self.assertTrue(self.core.check_prerequisites())
        msg = self.obs.start(run_thread=False)
        self.assertIn("ligada", msg)
        outs = self.run_ticks(1000, 10)
        self.assertIn("commented", outs)

    def test_legacy_loop_no_longer_captures_on_its_own(self):
        self.assertFalse(self.core.config.legacy_screen_comments)
        self.assertFalse(self.core.observer.active)


class TestGamerLocalAnalysis(ObserverCase):
    """Modo Gamer 100% local: vitória/derrota lidas na tela e marcos de tempo; nada sai do PC."""

    def setUp(self):
        super().setUp()
        self.screen_text = ""
        ocr = self.obs.vision.ocr
        ocr._probed, ocr.backend = True, "fake"
        ocr.read = lambda img, timeout=15.0: (self.screen_text, "fake")
        self.core.vision.generate = lambda *a, **k: self.fail("o Modo Gamer não pode usar modelo remoto")

    def test_defeat_on_screen_is_commented_once(self):
        self.screen_text = "YOU DIED"
        out = self.obs.gamer_comment(self.image, "Elden Ring", 3.0)
        self.assertIsNotNone(out)
        text, expr, reading = out
        self.assertTrue(text)
        self.assertEqual(reading.events, ["jogo_derrota"])
        self.assertEqual(expr.value, "bravo")
        self.screen_text = "YOU DIED"
        self.obs.vision.reset()
        self.assertIsNone(self.obs.gamer_comment(self.image, "Elden Ring", 3.1))   # mesmo resultado: não repete

    def test_victory_is_commented_happily(self):
        self.screen_text = "VICTORY ACHIEVED"
        out = self.obs.gamer_comment(self.image, "Elden Ring", 3.0)
        self.assertIsNotNone(out)
        self.assertEqual(out[1].value, "feliz")

    def test_time_marks_fire_once_each(self):
        self.assertIsNone(self.obs.gamer_comment(self.image, "Elden Ring", 5.0))
        out = self.obs.gamer_comment(self.image, "Elden Ring", 21.0)
        self.assertIsNotNone(out)
        self.assertIsNone(self.obs.gamer_comment(self.image, "Elden Ring", 22.0))
        self.assertIsNotNone(self.obs.gamer_comment(self.image, "Elden Ring", 46.0))

    def test_sensitive_window_is_never_analyzed(self):
        self.win.update(p="keepass.exe", t="Cofre")
        self.screen_text = "YOU DIED"
        self.assertIsNone(self.obs.gamer_comment(self.image, "Elden Ring", 30.0))

    def test_nothing_is_sent_and_nothing_stays_loaded(self):
        self.screen_text = "YOU DIED"
        self.obs.gamer_comment(self.image, "Elden Ring", 3.0)
        self.assertEqual(self.srv.requests, [])
        self.assertFalse(self.obs.active)

    def test_full_gamer_cycle_speaks_via_local_analysis(self):
        c = self.core
        c.modo_gamer, c.gamer_current_game, c.screen_watch_enabled = True, "Elden Ring", True
        c.gamer_last_game_change_time = time.time() - 60
        c.screen_engine.capture_and_encode = lambda: ("x" * 100, self.image, "hash-1")
        c.last_screen_img = None
        self.screen_text = "YOU DIED"
        c._run_gamer_analysis()
        self.assertEqual(len(self.said), 1)
        self.assertEqual(self.srv.requests, [])


if __name__ == "__main__":
    unittest.main()
