"""
Escuta de microfone e reconhecimento de voz.

ARQUITETURA (corrige o "perde o começo da frase"):

  thread de CAPTURA  ── lê o microfone SEM PARAR ──► segmentador (VAD) ──► fila
                         (guarda 1,6 s de áudio "de trás" para não cortar o começo)
  thread de RECONHECIMENTO ◄── fila ── envia ao Google, chama a Alicia, fala a resposta

Por que a versão anterior cortava o começo:
  * reconhecimento, espera de 3 s depois da fala da Alicia e sleep(0.2) aconteciam NO MESMO laço
    que lia o microfone; nesses momentos nada era lido e o buffer da placa de som transbordava
    (o áudio novo era descartado ou chegava atrasado);
  * a gravação só começava quando o volume passava do limite, então início suave era perdido.

Este módulo usa `'AliciaAssistantCore'` só como anotação de tipo (sem import circular).
"""

from __future__ import annotations

import array
import math
import queue
import re
import threading
import time
from collections import deque
from typing import TYPE_CHECKING, List, Optional

from .audio_input import open_microphone
from .call_context import has_wake_word
from .deps import sr, HAS_SPEECH, HAS_MIC, MIC_ERROR
from .logging_setup import logger
from .winperf import boost_current_thread

if TYPE_CHECKING:
    from .core import AliciaAssistantCore


def rms_int16(chunk: bytes) -> float:
    """Volume (RMS) de um bloco de áudio PCM 16 bits mono."""
    n = len(chunk) // 2
    if n == 0:
        return 0.0
    samples = array.array("h")
    samples.frombytes(chunk[: n * 2])
    return math.sqrt(sum(x * x for x in samples) / n)


class UtteranceSegmenter:
    """
    Detector de fala por energia, SEM threads nem relógio (mede tudo em número de blocos),
    por isso é totalmente testável. Recebe blocos de áudio e devolve frases completas.
    """

    def __init__(self, sample_rate: int, chunk_samples: int, *, floor: float = 250.0,
                 start_mult: float = 2.5, preroll_s: float = 1.6, end_silence_s: float = 1.0,
                 max_phrase_s: float = 25.0, min_voiced_s: float = 0.30, start_chunks: int = 2,
                 calibration_s: float = 1.0):
        self.chunk_s = chunk_samples / float(sample_rate)
        self.min_thr = floor * 0.6          # nunca abaixo disto (ruído de ventoinha, teclado...)
        self.start_mult = start_mult
        self.preroll = deque(maxlen=max(1, math.ceil(preroll_s / self.chunk_s)))
        self.end_chunks = max(1, math.ceil(end_silence_s / self.chunk_s))
        self.max_chunks = max(1, math.ceil(max_phrase_s / self.chunk_s))
        self.min_voiced = max(1, math.ceil(min_voiced_s / self.chunk_s))
        self.start_chunks = start_chunks
        self.noise = floor / start_mult / 2.0
        self._calib_total = max(1, math.ceil(calibration_s / self.chunk_s))
        self._calib_left = self._calib_total
        self._calib_acc: List[float] = []
        self._cap = floor * 4               # a calibração nunca aceita ruído maior que isto
        self._reset_state()

    # -- estado ----------------------------------------------------------
    def _reset_state(self):
        self.recording = False
        self._hot = 0
        self._buf: List[bytes] = []
        self._voiced = 0
        self._silence = 0

    @property
    def threshold(self) -> float:
        return max(self.min_thr, self.noise * self.start_mult)

    @property
    def calibrating(self) -> bool:
        return self._calib_left > 0

    @property
    def idle(self) -> bool:
        return not self.recording and self._hot == 0 and not self.calibrating

    def recalibrate(self, duration_s: float = 0.6):
        """Mede o ruído de fundo de novo (só deve ser chamado quando está ocioso)."""
        self._calib_total = self._calib_left = max(1, math.ceil(duration_s / self.chunk_s))
        self._calib_acc = []

    def block(self):
        """A Alicia está falando: descarta o que estiver pendente (não grava o eco dela)."""
        self.preroll.clear()
        self._reset_state()

    # -- processamento ---------------------------------------------------
    def feed(self, chunk: bytes) -> Optional[bytes]:
        """Processa um bloco. Devolve os bytes de UMA frase completa quando ela termina, senão None."""
        rms = rms_int16(chunk)

        if self.calibrating:
            self._calib_acc.append(rms)
            self.preroll.append(chunk)
            self._calib_left -= 1
            if self._calib_left == 0:
                mean = sum(self._calib_acc) / len(self._calib_acc)
                self.noise = min(mean, self._cap / self.start_mult)
            return None

        thr = self.threshold
        if not self.recording:
            self.preroll.append(chunk)
            if rms > thr:
                self._hot += 1
            else:
                self._hot = 0
                self.noise = 0.97 * self.noise + 0.03 * min(rms, thr)   # acompanha o ambiente devagar
            if self._hot >= self.start_chunks:
                # Começou a fala: leva junto o que veio ANTES (início suave incluído).
                self.recording = True
                self._buf = list(self.preroll)
                self.preroll.clear()
                self._voiced = self._hot
                self._silence = 0
            return None

        # gravando
        self._buf.append(chunk)
        if rms > thr * 0.7:        # histerese: finais de palavra mais fracos ainda contam como fala
            self._voiced += 1
            self._silence = 0
        else:
            self._silence += 1
        if self._silence >= self.end_chunks or len(self._buf) >= self.max_chunks:
            keep_tail = math.ceil(0.4 / self.chunk_s)
            trim = max(0, self._silence - keep_tail)
            chunks = self._buf[: len(self._buf) - trim] if trim else self._buf
            voiced = self._voiced
            self._reset_state()
            self.preroll.clear()
            if voiced >= self.min_voiced:
                return b"".join(chunks)
        return None


class VoiceInputEngine:
    SEGMENT_MAX_AGE = 20.0     # frase que ficou mais que isto na fila é velha demais para responder

    def __init__(self, core_engine: 'AliciaAssistantCore'):
        self.core = core_engine
        self.enabled = bool(HAS_SPEECH and HAS_MIC)
        if not HAS_MIC:
            logger.warning(f"🎙️ Microfone desligado: {MIC_ERROR}. A Alicia continua por teclado.")
        self.thread_ativa = False
        self._gen = 0                                  # identifica a "geração" das threads ativas
        self._threads: List[threading.Thread] = []
        self._queue: "queue.Queue" = queue.Queue(maxsize=3)
        self.last_calibration_time: float = 0.0
        self.force_recalibration: bool = False
        self.consecutive_recognition_fails: int = 0
        if self.enabled:
            self.recognizer = sr.Recognizer()

    # ------------------------------------------------------------------
    def _cfg(self, name: str, default):
        return getattr(self.core.config, name, default)

    def request_recalibration(self):
        """Pede nova medição do ruído (ex.: o volume do jogo mudou)."""
        self.force_recalibration = True

    def _should_recalibrate(self) -> bool:
        if self.force_recalibration:
            return True
        intervalo = (self.core.config.mic_recalibration_interval_gamer
                     if self.core.modo_gamer else self.core.config.mic_recalibration_interval)
        return time.time() - self.last_calibration_time >= intervalo

    @staticmethod
    def _transcricao_parece_lixo(text: str) -> bool:
        if not text:
            return True
        texto_limpo = text.strip()
        if len(texto_limpo) < 2:
            return True
        if not re.search(r"[a-zA-ZÀ-ÿ]", texto_limpo):
            return True
        return False

    def _alicia_is_speaking(self) -> bool:
        """True enquanto a Alicia fala e por um curto intervalo depois (eco da sala)."""
        tts = self.core.tts
        cooldown = float(self._cfg("mic_post_speech_cooldown", 1.0))
        return bool(tts.is_speaking) or (time.time() - tts.last_speech_finish_time < cooldown)

    # ------------------------------------------------------------------
    def start_listening(self):
        if not self.enabled or self.thread_ativa:
            return
        for t in self._threads:                      # espera as threads antigas largarem o microfone
            if t.is_alive():
                t.join(timeout=2.0)
        self._threads = []
        self.thread_ativa = True
        self._gen += 1
        gen = self._gen
        self._drain_queue()
        for target, name in ((self._capture_loop, "AliciaMicCapture"), (self._recognize_loop, "AliciaMicRecognize")):
            t = threading.Thread(target=target, args=(gen,), daemon=True, name=name)
            t.start()
            self._threads.append(t)

    def stop_listening(self):
        self.thread_ativa = False
        self._gen += 1                               # as threads atuais percebem e encerram

    def _alive(self, gen: int) -> bool:
        return self.core.is_running and self.thread_ativa and self._gen == gen

    def _drain_queue(self):
        try:
            while True:
                self._queue.get_nowait()
        except queue.Empty:
            pass

    # ------------------------------------------------------------------
    # THREAD 1: captura contínua
    # ------------------------------------------------------------------
    def _capture_loop(self, gen: int):
        logger.info(f"🎙️ Microfone ativado (índice: {self.core.config.mic_index}). Medindo o ruído de fundo...")
        boost_current_thread()
        restarted_elsewhere = False
        try:
            with open_microphone(self.core.config.mic_index) as source:
                seg = UtteranceSegmenter(
                    source.SAMPLE_RATE, source.CHUNK,
                    floor=float(self.core.config.mic_energy_floor),
                    start_mult=float(self._cfg("mic_start_multiplier", 2.5)),
                    preroll_s=float(self._cfg("mic_preroll_seconds", 1.6)),
                    end_silence_s=float(self._cfg("mic_end_silence", 1.0)),
                )
                self._rate = source.SAMPLE_RATE
                was_blocked = True
                last_read = time.monotonic()
                slow_reads = 0
                warned = False
                while self._alive(gen):
                    chunk = source.stream.read(source.CHUNK)
                    now_m = time.monotonic()
                    gap, last_read = now_m - last_read, now_m
                    self.max_read_gap = max(getattr(self, "max_read_gap", 0.0), gap)
                    if gap > 0.5:               # o laço ficou > 0,5 s sem ler: o Windows atrasou esta thread
                        slow_reads += 1
                        if slow_reads >= 3 and not warned:
                            warned = True
                            logger.warning(
                                "🎙️ A captura do microfone está sendo atrasada pelo Windows (pausas de "
                                f"{gap:.1f}s). Isso corta o começo das frases. Desative o 'modo de eficiência' "
                                "do Windows Terminal/Python no Gerenciador de Tarefas, ou rode o Teste_Completo "
                                "com --foco para medir.")
                    if not chunk:
                        continue

                    # A cada bloco continuamos LENDO (nunca deixa o buffer encher);
                    # só não aproveitamos o áudio enquanto a Alicia fala.
                    if self._alicia_is_speaking():
                        seg.block()
                        was_blocked = True
                        continue
                    if was_blocked:
                        was_blocked = False
                        if not seg.calibrating:
                            logger.debug("🎙️ Voltando a escutar.")

                    done = seg.feed(chunk)
                    if seg.calibrating is False and self.last_calibration_time == 0.0:
                        self.last_calibration_time = time.time()
                        logger.info(f"🎙️ Microfone pronto (limiar de voz: {seg.threshold:.0f}).")
                    if done is not None:
                        try:
                            self._queue.put_nowait((time.time(), done, source.SAMPLE_RATE))
                        except queue.Full:                      # fila cheia: descarta a mais velha
                            try:
                                self._queue.get_nowait()
                            except queue.Empty:
                                pass
                            self._queue.put_nowait((time.time(), done, source.SAMPLE_RATE))
                    if seg.idle and self._should_recalibrate():
                        seg.recalibrate(0.6)
                        self.force_recalibration = False
                        self.last_calibration_time = time.time()
        except Exception as e:
            logger.error(
                "Não foi possível acessar o microfone selecionado: "
                f"{e} (confira Configurações do Windows > Privacidade > Microfone)"
            )
            tinha_microfone_especifico = self.core.config.mic_index is not None
            self.thread_ativa = False
            if tinha_microfone_especifico and self.core.is_running:
                self.core.config.mic_index = None
                self.core.config.save()
                time.sleep(2)
                restarted_elsewhere = True
                self.start_listening()
            else:
                logger.warning(
                    "🎙️ Microfone indisponível. A Alicia continua por teclado; "
                    "escolha outro microfone no menu do avatar e reinicie."
                )
        finally:
            if not restarted_elsewhere and self._gen == gen:
                self.thread_ativa = False

    # ------------------------------------------------------------------
    # THREAD 2: reconhecimento + resposta (pode demorar; a captura continua)
    # ------------------------------------------------------------------
    def _transcribe(self, data: bytes, rate: int) -> str:
        audio = sr.AudioData(data, rate, 2)
        return self.recognizer.recognize_google(audio, language="pt-BR").strip()

    def _recognize_loop(self, gen: int):
        while self._alive(gen):
            try:
                queued_at, data, rate = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if time.time() - queued_at > self.SEGMENT_MAX_AGE:
                logger.info("🎙️ Fala antiga demais na fila; descartada.")
                continue
            try:
                self._handle_segment(data, rate)
            except Exception as e:
                logger.error(f"Erro ao tratar a fala: {type(e).__name__}: {e}")
                time.sleep(0.5)

    def _handle_segment(self, data: bytes, rate: int):
        try:
            text = self._transcribe(data, rate)
            self.consecutive_recognition_fails = 0
        except Exception as e:
            name = type(e).__name__
            if name == "UnknownValueError":
                # Soou como fala mas não deu para transcrever. Se repetir muito, o ruído de fundo mudou.
                self.consecutive_recognition_fails += 1
                if self.consecutive_recognition_fails >= self.core.config.mic_fails_before_recalibration:
                    self.force_recalibration = True
                return
            if name == "RequestError":
                logger.error(f"Erro no serviço de reconhecimento (internet?): {e}")
                time.sleep(2)
                return
            raise

        if self._transcricao_parece_lixo(text):
            return
        label = (self.core.db.get_user_name() if hasattr(self.core, "db") else None) or "Você"
        print(f"\n🎙️ {label} (Voz): {text}")

        in_call = self.core.call_listener.active
        if in_call:
            # Seu microfone também conta para o contexto; na call ela só responde se você a chamar pelo nome.
            self.core.call_context.add(label, text)
            called = has_wake_word(text)
            logger.info(f"📞 {label}: {text!r} -> {'CHAMOU a Alicia' if called else 'sem chamada'}")
            if not called:
                return

        if self.core.modo_descanso:
            palavras = ["acordar", "acorda", "despertar", "voltar", "alicia acorda"]
            if any(p in text.lower() for p in palavras):
                self.core.sair_modo_descanso()
            else:
                logger.info("😴 [Modo Descanso] Ignorando voz...")
            return

        response = self.core.interact(text, source="voice")
        if not response:
            return
        clean_response, expr = self.core.parse_expression_tags(response)
        if in_call:
            self.core.call_context.add("Alicia", clean_response)
        print(f"🤖 Alicia [{expr.value.upper()}]: {clean_response}")
        self.core.tts.speak(clean_response, expr)
