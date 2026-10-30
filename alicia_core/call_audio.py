"""
Captura do áudio que SAI do computador (a voz de quem está na call).

No Linux (PipeWire/PulseAudio) todo destino de som tem uma fonte "<nome>.monitor" com o que ele toca.
Lemos essa fonte com o `parec`, que entrega PCM int16 mono 16 kHz — o formato que o segmentador
de voz (voice_input.UtteranceSegmenter) e o reconhecimento já esperam.
"""

from __future__ import annotations

import os
import select
import shutil
import subprocess
from typing import Optional

HAS_PAREC = shutil.which("parec") is not None
HAS_PACTL = shutil.which("pactl") is not None


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
