"""
Motor de texto-para-voz (Edge TTS gera o áudio; audio_output.py toca).
"""

import asyncio
import os
import random
import re
import tempfile
import threading
import time

from .audio_output import play_mp3
from .deps import edge_tts, HAS_AUDIO, AUDIO_ERROR
from .enums import AliciaExpression
from .logging_setup import logger


DEFAULT_VOICE = "pt-BR-FranciscaNeural"

# (ritmo %, tom Hz, volume %) somados à velocidade do config: a voz acompanha a emoção, em vez de falar tudo no mesmo tom.
EXPRESSION_PROSODY = {
    AliciaExpression.FELIZ: (6, 4, 0),
    AliciaExpression.SERIO: (0, 0, 0),
    AliciaExpression.SURPRESA: (10, 8, 5),
    AliciaExpression.PENSANDO: (-8, -2, -3),
    AliciaExpression.BRAVO: (8, -3, 8),
}

_EMOJI_RE = re.compile("[\U00010000-\U0010ffff\u2600-\u27bf\ufe0f]")


def prepare_for_speech(text: str) -> str:
    """Texto -> o que de fato deve ser falado: sem tags, rubricas, emojis, markdown ou links; pausas mais naturais."""
    t = re.sub(r"\[[^\]]*\]", "", text or "")                 # [FELIZ] e similares
    t = re.sub(r"\*[^*]*\*", "", t).replace("*", "")           # *sorri*
    t = re.sub(r"```.*?```", " ", t, flags=re.DOTALL)
    t = t.replace("`", "").replace("_", " ")
    t = re.sub(r"https?://\S+", "link", t)
    t = _EMOJI_RE.sub("", t)
    t = re.sub(r"^\s*(?:#+|[-•]|\d+[.)])\s+", "", t, flags=re.MULTILINE)
    t = re.sub(r"\s*[—–]\s*", ", ", t)                         # travessão vira pausa curta
    t = re.sub(r"([!?])\1+", r"\1", t)                         # "!!!" -> "!" (as reticências ficam: são pausa)
    t = re.sub(r"\.{4,}", "...", t)
    t = re.sub(r"\s+", " ", t)
    return t.strip()


def prosody_for(expression, voice_speed: int = 100, expressive: bool = True, rng=random):
    """('+6%', '+4Hz', '+0%'): ritmo, tom e volume. Com pequena variação aleatória para nunca soar igual."""
    base = max(70, min(130, int(voice_speed))) - 100
    rate_d, pitch_d, vol_d = EXPRESSION_PROSODY.get(expression, (0, 0, 0)) if expressive else (0, 0, 0)
    if expressive:
        rate_d += rng.choice((-2, -1, 0, 1, 2))
        pitch_d += rng.choice((-1, 0, 1))
    rate = max(-35, min(35, base + rate_d))
    return f"{rate:+d}%", f"{pitch_d:+d}Hz", f"{max(-20, min(20, vol_d)):+d}%"


class TextToSpeechEngine:

    def __init__(
        self,
        enabled: bool = True,
        voice_speed: int = 100,
        voice_name: str = DEFAULT_VOICE,
        expressive: bool = True,
    ):
        self.enabled = bool(enabled and HAS_AUDIO)
        if enabled and not HAS_AUDIO:
            logger.warning(f"Voz desativada: {AUDIO_ERROR}. A Alicia continua por texto.")
        self.voice_speed = voice_speed
        self.voice_name = voice_name or DEFAULT_VOICE
        self.expressive = bool(expressive)
        self.is_speaking = False
        self.audio_playing = False      # True só enquanto o som toca (a boca do avatar acompanha isto)
        self.last_speech_finish_time = 0.0
        self._speak_lock = threading.Lock()

    async def _generate_audio_async(
        self,
        text: str,
        output_path: str,
        expression: AliciaExpression = AliciaExpression.SERIO
    ):
        rate, pitch, volume = prosody_for(expression, self.voice_speed, self.expressive)
        base_rate = prosody_for(AliciaExpression.SERIO, self.voice_speed, False)[0]
        # tentativas, da mais expressiva para a mais segura: o áudio sai mesmo se a voz/tom não forem aceitos
        attempts = [(self.voice_name, dict(rate=rate, pitch=pitch, volume=volume)),
                    (self.voice_name, dict(rate=base_rate))]
        if self.voice_name != DEFAULT_VOICE:
            attempts.append((DEFAULT_VOICE, dict(rate=base_rate)))
        last = None
        for voice, kwargs in attempts:
            try:
                await edge_tts.Communicate(text, voice, **kwargs).save(output_path)
                if os.path.exists(output_path) and os.path.getsize(output_path) > 0:
                    return
            except Exception as e:           # voz inexistente, parâmetro recusado, rede...
                last = e
                logger.warning(f"TTS: tentativa com {voice} {kwargs} falhou ({type(e).__name__}); tentando algo mais simples.")
        if last:
            raise last

    def speak(
        self,
        text: str,
        expression: AliciaExpression = AliciaExpression.FELIZ
    ):
        if not self.enabled or not text:
            return

        with self._speak_lock:
            self.is_speaking = True
            temp_path = None

            try:
                clean_text = prepare_for_speech(text)

                if not clean_text:
                    return

                with tempfile.NamedTemporaryFile(
                    delete=False,
                    suffix=".mp3"
                ) as tmp:
                    temp_path = tmp.name

                asyncio.run(
                    self._generate_audio_async(clean_text, temp_path, expression)
                )

                if (
                    os.path.exists(temp_path)
                    and os.path.getsize(temp_path) > 0
                ):
                    self.audio_playing = True
                    try:
                        play_mp3(temp_path)
                    finally:
                        self.audio_playing = False

            except Exception as e:
                logger.error(f"Erro no TTS: {e}")

            finally:
                self.is_speaking = False
                self.last_speech_finish_time = time.time()

                if temp_path and os.path.exists(temp_path):
                    try:
                        os.remove(temp_path)
                    except OSError:
                        pass
