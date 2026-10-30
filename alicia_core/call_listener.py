"""
Modo call: a Alicia ouve o áudio do computador (a voz de quem está na call), guarda o contexto
e só responde quando alguém a chama pelo nome.

  thread de CAPTURA  ── parec (monitor do destino do Discord) ──► segmentador (VAD) ──► fila
  thread de RECONHECIMENTO ◄── fila ── Google ── registra no CallContext ── se disseram "Alicia": responde

O microfone do próprio usuário continua no VoiceInputEngine (ele também alimenta o CallContext).
"""

from __future__ import annotations

import queue
import threading
import time
from typing import TYPE_CHECKING, Optional, Tuple

from . import audio_output
from .call_audio import CAPTURE_HINT, HAS_CAPTURE, HAS_PACTL, IS_WINDOWS, ParecSource, open_source
from .call_context import has_wake_word
from .deps import HAS_SPEECH, sr
from .logging_setup import logger
from .virtual_audio import DEFAULT_DISCORD_APPS, SINK_VOICE, SOURCE_MIC, PactlError, VirtualAudio, run_pactl
from .voice_id import VoiceID
from .voice_input import UtteranceSegmenter

if TYPE_CHECKING:
    from .core import AliciaAssistantCore

REMOTE_SPEAKER = "Amigo"


class CallListener:
    SEGMENT_MAX_AGE = 15.0
    MOVE_INTERVAL = 5.0

    def __init__(self, core: "AliciaAssistantCore", virtual: Optional[VirtualAudio] = None):
        self.core = core
        self.enabled = bool(HAS_SPEECH and HAS_CAPTURE)
        cfg = core.config
        self.virtual = virtual or VirtualAudio(
            mic_source=getattr(cfg, "call_mic_source", None),
            discord_apps=getattr(cfg, "call_discord_apps", None) or DEFAULT_DISCORD_APPS,
        )
        self.active = False
        self._gen = 0
        self._threads = []
        self._queue: "queue.Queue[Tuple[float, bytes]]" = queue.Queue(maxsize=4)
        self._last_reply = 0.0
        self._isolated = False
        self.recognizer = sr.Recognizer() if self.enabled else None

    def _cfg(self, name: str, default):
        return getattr(self.core.config, name, default)

    # ------------------------------------------------------------------
    def start(self) -> Tuple[bool, str]:
        if self.active:
            return True, "O modo call já está ligado."
        if not self.enabled:
            return False, f"Modo call indisponível: {CAPTURE_HINT} e do SpeechRecognition."
        device = self._cfg("call_source", None)
        self._isolated = False
        if self._cfg("call_virtual_audio", True) and HAS_PACTL:
            try:
                self.virtual.setup()
                device = device or self.virtual.capture_device
                self._isolated = True
                audio_output.set_output_sink(SINK_VOICE)
            except Exception as e:
                logger.warning(f"Áudio virtual da call não foi criado ({e}); usando o áudio geral do sistema.")
        if not device and IS_WINDOWS:
            device = None          # o LoopbackSource usa o alto-falante padrão
        elif not device:
            try:
                device = run_pactl(["get-default-sink"]).strip() + ".monitor"
            except PactlError as e:
                return False, f"Não achei de onde ouvir o áudio do sistema: {e}"
        self.device = device
        self.active = True
        self._gen += 1
        gen = self._gen
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break
        self._threads = []
        loops = [(self._capture_loop, "AliciaCallCapture"), (self._recognize_loop, "AliciaCallRecognize")]
        if self._isolated:
            loops.append((self._route_loop, "AliciaCallRoute"))
        for target, name in loops:
            t = threading.Thread(target=target, args=(gen,), daemon=True, name=name)
            t.start()
            self._threads.append(t)
        if self._isolated:
            msg = (f"Modo call ligado. No Discord, em Configurações de Voz, escolha como ENTRADA '{SOURCE_MIC}' "
                   "(Alicia_Mic) para seu amigo ouvir você e a Alicia. A saída do Discord vai sozinha para o canal "
                   "dela (você continua ouvindo).")
        else:
            msg = (f"Modo call ligado (ouvindo '{device or 'o alto-falante padrão'}'). Sem áudio virtual: seu amigo NÃO ouve a Alicia, "
                   "e ela pausa a escuta enquanto fala.")
        logger.info(f"📞 {msg}")
        return True, msg

    def stop(self):
        was_active = self.active
        self.active = False
        self._gen += 1
        for t in self._threads:
            if t is not threading.current_thread():
                t.join(timeout=2.0)
        self._threads = []
        if was_active or self.virtual.active:
            self.virtual.teardown()
            audio_output.set_output_sink(None)

    def _alive(self, gen: int) -> bool:
        return self.core.is_running and self.active and self._gen == gen

    # ------------------------------------------------------------------
    def _route_loop(self, gen: int):
        """Cada pessoa que entra na call cria um stream novo no Discord: leva todos para o canal da Alicia."""
        while self._alive(gen):
            try:
                self.virtual.move_discord_streams()
            except Exception as e:
                logger.debug(f"Roteamento do Discord: {e}")
            end = time.monotonic() + self.MOVE_INTERVAL
            while self._alive(gen) and time.monotonic() < end:
                time.sleep(0.25)

    def _alicia_is_speaking(self) -> bool:
        tts = self.core.tts
        return bool(tts.is_speaking) or (time.time() - tts.last_speech_finish_time
                                         < float(self._cfg("mic_post_speech_cooldown", 1.0)))

    def _capture_loop(self, gen: int):
        logger.info(f"📞 Ouvindo a call em '{self.device or 'alto-falante padrão'}'...")
        try:
            with open_source(self.device) as source:
                seg = UtteranceSegmenter(
                    source.SAMPLE_RATE, source.CHUNK,
                    floor=float(self._cfg("call_energy_floor", 150)),
                    start_mult=float(self._cfg("mic_start_multiplier", 2.5)),
                    preroll_s=float(self._cfg("mic_preroll_seconds", 1.6)),
                    end_silence_s=float(self._cfg("call_end_silence", 0.9)),
                )
                while self._alive(gen):
                    chunk = source.read(0.5)
                    if chunk is None:
                        continue
                    if not chunk:
                        if self._alive(gen):
                            raise RuntimeError("a captura encerrou (o destino de áudio sumiu?)")
                        break
                    # Sem o canal isolado, o que a Alicia fala volta pelo monitor: ignora enquanto ela fala.
                    if not self._isolated and self._alicia_is_speaking():
                        seg.block()
                        continue
                    done = seg.feed(chunk)
                    if done is not None:
                        try:
                            self._queue.put_nowait((time.time(), done))
                        except queue.Full:
                            try:
                                self._queue.get_nowait()
                            except queue.Empty:
                                pass
                            self._queue.put_nowait((time.time(), done))
        except Exception as e:
            logger.error(f"📞 A captura da call parou: {e}")
            if self._gen == gen:
                self.active = False

    def _transcribe(self, data: bytes) -> str:
        audio = sr.AudioData(data, ParecSource.SAMPLE_RATE, 2)
        return self.recognizer.recognize_google(audio, language="pt-BR").strip()

    def _recognize_loop(self, gen: int):
        while self._alive(gen):
            try:
                queued_at, data = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if time.time() - queued_at > self.SEGMENT_MAX_AGE:
                continue
            try:
                try:
                    text = self._transcribe(data)
                except Exception as e:
                    if type(e).__name__ == "RequestError":
                        logger.error(f"Reconhecimento da call falhou (internet/limite do Google?): {e}")
                        time.sleep(2)
                    continue
                ident = None
                vid = getattr(self.core, "voice_id", None)
                if vid is not None and vid.enabled:
                    try:
                        if vid.enrolling:        # cadastro de voz em andamento: a fala é do cadastro, não da conversa
                            reply = vid.feed_enrollment(text, data, ParecSource.SAMPLE_RATE, "call")
                            if reply:
                                self.core.say_voice_reply(reply)
                                continue
                        ident = vid.identify(data, ParecSource.SAMPLE_RATE, "call")
                    except Exception as e:     # um erro de voz nunca derruba a escuta da call
                        logger.error(f"Reconhecimento de voz na call falhou: {type(e).__name__}: {e}")
                        ident = None
                self.on_remote_text(text, speaker=VoiceID.label(ident, REMOTE_SPEAKER), identity=ident)
            except Exception as e:
                logger.error(f"Erro ao tratar a fala da call: {type(e).__name__}: {e}")

    # ------------------------------------------------------------------
    def on_remote_text(self, text: str, speaker: str = REMOTE_SPEAKER, identity=None):
        """Texto já transcrito de quem está na call: entra no contexto e, se a chamaram, ela responde."""
        if not text or len(text.strip()) < 2:
            return
        self.core.call_context.add(speaker, text)
        print(f"\n📞 {speaker} (call): {text}")
        called = has_wake_word(text)
        logger.info(f"📞 {speaker}: {text!r} -> {'CHAMOU a Alicia' if called else 'sem chamada'}")
        if not called:
            return
        now = time.time()
        if self._alicia_is_speaking():
            logger.info("📞 Chamaram a Alicia enquanto ela falava; ignorado.")
            return
        if now - self._last_reply < float(self._cfg("call_reply_cooldown", 2.0)):
            logger.info("📞 Chamaram a Alicia de novo em menos de 2 s (mesma frase repartida?); ignorado.")
            return
        self._last_reply = now
        if self.core.modo_descanso:
            logger.info("📞 Chamaram a Alicia, mas ela está em modo descanso (/acordar).")
            return
        self.core.responder_na_call(text, speaker, identity)
        vid = getattr(self.core, "voice_id", None)
        if vid is not None and identity is not None:
            offer = vid.maybe_offer(identity, source="call")      # voz desconhecida que chamou: oferece o cadastro
            if offer:
                self.core.say_voice_reply(offer)
