"""
Reprodução do áudio da voz da Alicia (mp3 gerado pelo Edge TTS).

Windows: usa o MCI do próprio sistema (winmm) via ctypes — não precisa instalar NADA
(o pygame não tem pacote pronto para o Python 3.14 e tenta compilar).
Outros sistemas: usa o pygame, se estiver instalado.

  play_mp3(caminho)  -> toca e só retorna quando termina (ou quando stop_playback() é chamado)
  stop_playback()    -> interrompe o que estiver tocando
"""

from __future__ import annotations

import os
import sys
import threading
import time

from .deps import AUDIO_BACKEND, pygame
from .logging_setup import logger

_ALIAS = "alicia_voice"
_lock = threading.Lock()
_stop_flag = threading.Event()


def _mci(command: str) -> str:
    """Envia um comando ao MCI do Windows. Devolve o texto de retorno; levanta OSError em erro."""
    import ctypes
    buf = ctypes.create_unicode_buffer(256)
    winmm = ctypes.windll.winmm
    err = winmm.mciSendStringW(command, buf, 255, 0)
    if err:
        msg = ctypes.create_unicode_buffer(256)
        winmm.mciGetErrorStringW(err, msg, 255)
        raise OSError(f"MCI '{command.split()[0]}' falhou: {msg.value or err}")
    return buf.value


def _play_mci(path: str):
    try:
        _mci(f"close {_ALIAS}")
    except OSError:
        pass
    _mci(f'open "{path}" type mpegvideo alias {_ALIAS}')
    try:
        _mci(f"play {_ALIAS}")
        time.sleep(0.15)
        while not _stop_flag.is_set():
            if _mci(f"status {_ALIAS} mode").strip().lower() != "playing":
                break
            time.sleep(0.1)
    finally:
        try:
            _mci(f"stop {_ALIAS}")
        except OSError:
            pass
        try:
            _mci(f"close {_ALIAS}")
        except OSError:
            pass


def _play_pygame(path: str):
    pygame.mixer.music.load(path)
    pygame.mixer.music.play()
    while pygame.mixer.music.get_busy() and not _stop_flag.is_set():
        time.sleep(0.1)
    pygame.mixer.music.stop()


def play_mp3(path: str):
    with _lock:
        _stop_flag.clear()
        if AUDIO_BACKEND == "mci":
            _play_mci(path)
        elif AUDIO_BACKEND == "pygame":
            _play_pygame(path)
        else:
            raise OSError("Nenhum player de áudio disponível.")


def stop_playback():
    _stop_flag.set()


def set_output_sink(sink: str | None) -> bool:
    """Linux/pygame: faz a voz sair por um destino específico do PulseAudio/PipeWire (None = padrão do sistema).
    O libpulse lê PULSE_SINK quando o SDL abre o dispositivo, então reabrimos o mixer."""
    if AUDIO_BACKEND != "pygame":
        return False
    with _lock:
        try:
            pygame.mixer.quit()
            if sink:
                os.environ["PULSE_SINK"] = sink
            else:
                os.environ.pop("PULSE_SINK", None)
            pygame.mixer.init()
            return True
        except Exception as e:
            logger.warning(f"Não consegui trocar o destino da voz para '{sink}': {e}")
            return False
