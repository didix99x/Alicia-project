"""
Captura do áudio que SAI do computador (a voz de quem está na call).

No Linux (PipeWire/PulseAudio) todo destino de som tem uma fonte "<nome>.monitor" com o que ele toca.
Lemos essa fonte com o `parec`, que entrega PCM int16 mono 16 kHz — o formato que o segmentador
de voz (voice_input.UtteranceSegmenter) e o reconhecimento já esperam.

No Windows usamos o loopback WASAPI (biblioteca `soundcard`) no dispositivo de saída padrão, com o mesmo formato.
Não há áudio virtual lá: a Alicia ouve o áudio geral do PC e pausa a escuta enquanto fala.
"""

from __future__ import annotations

import os
import queue
import select
import shutil
import subprocess
import sys
import threading
from typing import Optional

HAS_PAREC = shutil.which("parec") is not None
HAS_PACTL = shutil.which("pactl") is not None

IS_WINDOWS = sys.platform == "win32"
HAS_LOOPBACK = False
sc = None
if IS_WINDOWS:
    try:
        import numpy as np
        import soundcard as sc
        HAS_LOOPBACK = True
    except Exception:            # biblioteca ausente ou sem backend de áudio
        sc = None

HAS_CAPTURE = HAS_LOOPBACK if IS_WINDOWS else HAS_PAREC
CAPTURE_HINT = ("precisa das bibliotecas `soundcard` e `numpy` (rode Iniciar_Alicia.bat --reinstall)"
                if IS_WINDOWS else "precisa do `parec` (pacote libpulse)")


class ParecSource:
    """Fonte de áudio contínua. `read()` devolve bytes, None se nada chegou a tempo, b'' se acabou."""

    SAMPLE_RATE = 16000
    SAMPLE_WIDTH = 2          # int16
    CHUNK = 1024              # amostras por bloco

    def __init__(self, device: str):
        self.device = device
        self._proc: Optional[subprocess.Popen] = None
        self._buf = bytearray()

    def __enter__(self) -> "ParecSource":
        self._proc = subprocess.Popen(
            ["parec", f"--device={self.device}", "--format=s16le",
             f"--rate={self.SAMPLE_RATE}", "--channels=1", "--latency-msec=50"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0,
        )
        return self

    def __exit__(self, exc_type, exc, tb):
        proc, self._proc = self._proc, None
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
            if proc.stdout:
                proc.stdout.close()

    def read(self, timeout: float = 0.5) -> Optional[bytes]:
        if self._proc is None or self._proc.stdout is None:
            return b""
        need = self.CHUNK * self.SAMPLE_WIDTH
        fd = self._proc.stdout.fileno()
        while len(self._buf) < need:
            ready, _, _ = select.select([fd], [], [], timeout)
            if not ready:
                return None
            data = os.read(fd, need - len(self._buf))
            if not data:
                return b""
            self._buf += data
        chunk, self._buf = bytes(self._buf[:need]), self._buf[need:]
        return chunk


class LoopbackSource:
    """Mesma interface do ParecSource, lendo o que toca no alto-falante padrão do Windows (WASAPI loopback)."""

    SAMPLE_RATE = ParecSource.SAMPLE_RATE
    SAMPLE_WIDTH = ParecSource.SAMPLE_WIDTH
    CHUNK = ParecSource.CHUNK

    def __init__(self, device: Optional[str] = None):
        self.device = device
        self._q: "queue.Queue[Optional[bytes]]" = queue.Queue(maxsize=64)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def __enter__(self) -> "LoopbackSource":
        name = self.device or sc.default_speaker().name
        mic = sc.get_microphone(id=name, include_loopback=True)
        ready = threading.Event()
        self._error: Optional[Exception] = None
        self._thread = threading.Thread(target=self._run, args=(mic, ready), daemon=True, name="AliciaLoopback")
        self._thread.start()
        ready.wait(5.0)
        if self._error is not None:
            raise self._error
        return self

    def _run(self, mic, ready: threading.Event):
        try:
            with mic.recorder(samplerate=self.SAMPLE_RATE, channels=1, blocksize=self.CHUNK) as rec:
                ready.set()
                while not self._stop.is_set():
                    data = rec.record(numframes=self.CHUNK)
                    pcm = (np.clip(data[:, 0], -1.0, 1.0) * 32767).astype("<i2").tobytes()
                    try:
                        self._q.put_nowait(pcm)
                    except queue.Full:
                        pass
        except Exception as e:
            self._error = e
            ready.set()
        finally:
            self._q.put(None)     # sinaliza o fim

    def __exit__(self, exc_type, exc, tb):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)

    def read(self, timeout: float = 0.5) -> Optional[bytes]:
        try:
            chunk = self._q.get(timeout=timeout)
        except queue.Empty:
            return None
        return b"" if chunk is None else chunk


def open_source(device: Optional[str]):
    return LoopbackSource(device) if IS_WINDOWS else ParecSource(device)
