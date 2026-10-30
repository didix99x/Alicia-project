"""
Captura do microfone para o reconhecimento de fala.

Backend principal: sounddevice (não exige PyAudio nem compilador).
Alternativa: PyAudio via sr.Microphone, se já estiver instalado.

Exporta:
  - list_microphones() -> [(índice, nome), ...]  (só dispositivos de entrada, sem duplicatas de API)
  - open_microphone(indice) -> fonte de áudio compatível com sr.Recognizer.listen()
"""

from __future__ import annotations

from typing import List, Optional, Tuple

from .deps import HAS_PYAUDIO, HAS_SOUNDDEVICE, HAS_SPEECH, sd, sr
from .logging_setup import logger

_SD_CLASS = None


def _sample_rate_for(device: Optional[int]) -> int:
    """16 kHz se o dispositivo aceitar (ideal para reconhecimento); senão, a taxa padrão dele."""
    try:
        sd.check_input_settings(device=device, samplerate=16000, channels=1, dtype="int16")
        return 16000
    except Exception:
        info = sd.query_devices(device, "input")
        return int(info["default_samplerate"])


def _sd_microphone_class():
    """Cria (uma vez) a classe da fonte de áudio. Precisa herdar de sr.AudioSource."""
    global _SD_CLASS
    if _SD_CLASS is not None:
        return _SD_CLASS

    class _Reader:
        def __init__(self, raw):
            self._raw = raw
            self.overflows = 0      # quantas vezes o Windows descartou áudio porque lemos tarde demais

        def read(self, size):
            data, overflowed = self._raw.read(size)
            if overflowed:
                self.overflows += 1
            return bytes(data)

    class SoundDeviceMicrophone(sr.AudioSource):
        def __init__(self, device_index: Optional[int] = None, chunk_size: int = 1024):
            self.device_index = device_index
            self.SAMPLE_WIDTH = 2          # int16
            self.CHUNK = chunk_size
            self.stream = None
            self._raw = None
            resolved = device_index
            if resolved is None:
                resolved = sd.default.device[0]
                if resolved is None or resolved < 0:
                    raise OSError("Nenhum microfone (dispositivo de entrada) foi encontrado no Windows.")
            self.SAMPLE_RATE = _sample_rate_for(resolved)
            self._device = resolved

        def __enter__(self):
            assert self.stream is None, "Fonte de áudio já está aberta"
            self._raw = sd.RawInputStream(
                samplerate=self.SAMPLE_RATE, channels=1, dtype="int16",
                blocksize=self.CHUNK, device=self._device, latency="high",
            )
            self._raw.start()
            self.stream = _Reader(self._raw)
            return self

        def __exit__(self, exc_type, exc, tb):
            try:
                if self._raw is not None:
                    self._raw.stop()
                    self._raw.close()
            finally:
                self._raw = None
                self.stream = None

    _SD_CLASS = SoundDeviceMicrophone
    return _SD_CLASS


def list_microphones() -> List[Tuple[int, str]]:
    if HAS_SOUNDDEVICE:
        try:
            devices = sd.query_devices()
            try:
                allowed = set(sd.query_hostapis(sd.default.hostapi)["devices"])
            except Exception:
                allowed = set(range(len(devices)))
            found = [(i, d["name"]) for i, d in enumerate(devices)
                     if d["max_input_channels"] > 0 and i in allowed]
            if not found:
                found = [(i, d["name"]) for i, d in enumerate(devices) if d["max_input_channels"] > 0]
            return found
        except Exception as e:
            logger.warning(f"Não consegui listar os microfones (sounddevice): {e}")
            return []
    if HAS_SPEECH and HAS_PYAUDIO:
        try:
            return list(enumerate(sr.Microphone.list_microphone_names()))
        except Exception as e:
            logger.warning(f"Não consegui listar os microfones (PyAudio): {e}")
    return []


def open_microphone(device_index: Optional[int] = None):
    """Fonte de áudio pronta para `with ... as source:` e recognizer.listen(source)."""
    if HAS_SOUNDDEVICE and HAS_SPEECH:
        return _sd_microphone_class()(device_index=device_index)
    if HAS_SPEECH and HAS_PYAUDIO:
        return sr.Microphone(device_index=device_index)
    raise OSError("Nenhuma biblioteca de captura de áudio disponível (instale o sounddevice).")


def measure_capture(source, seconds: float) -> dict:
    """
    Lê o microfone por `seconds` e mede a regularidade da leitura (usado para provar se o Windows está
    atrasando a Alicia quando a janela perde o foco). `source` já deve estar aberto (`with ... as source`).
    """
    import array
    import time as _t
    chunks, max_gap, peak = 0, 0.0, 0
    start = last = _t.monotonic()
    while _t.monotonic() - start < seconds:
        data = source.stream.read(source.CHUNK)
        now = _t.monotonic()
        max_gap, last = max(max_gap, now - last), now
        chunks += 1
        samples = array.array("h")
        samples.frombytes(data[: len(data) // 2 * 2])
        if samples:
            peak = max(peak, max(abs(x) for x in samples))
    expected = seconds * source.SAMPLE_RATE / source.CHUNK
    return {"chunks": chunks, "expected": round(expected), "max_gap": round(max_gap, 3),
            "overflows": getattr(source.stream, "overflows", 0), "peak": peak}
