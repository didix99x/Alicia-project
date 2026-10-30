"""
Ponto único de importação das bibliotecas de terceiros.

Qualquer outro módulo do pacote que precise de uma dessas libs importa
DAQUI (ex.: `from alicia_core.deps import psutil, HAS_PSUTIL`).

Essenciais (o programa não funciona sem elas): requests, Pillow, PyQt6.
Opcionais (o recurso correspondente fica desligado se faltarem):
  - edge-tts (+ player do Windows) -> voz da Alicia (HAS_AUDIO)
  - SpeechRecognition + sounddevice -> microfone (HAS_MIC)
  - mss -> captura de tela mais rápida (HAS_MSS; há fallback com Pillow)
  - psutil -> detecção de processos (HAS_PSUTIL)
"""

import os
import sys

os.environ['QT_AUTO_SCREEN_SCALE_FACTOR'] = '1'

try:
    import requests
except ImportError:
    sys.exit("[ERRO CRÍTICO] A biblioteca 'requests' é necessária. "
             "Rode Iniciar_Alicia.bat para instalar as dependências.")

try:
    from PIL import Image, ImageChops, ImageGrab, ImageStat
except ImportError:
    sys.exit("[ERRO CRÍTICO] A biblioteca 'Pillow' é necessária. "
             "Rode Iniciar_Alicia.bat para instalar as dependências.")

try:
    import mss
    HAS_MSS = True
except ImportError:
    mss = None
    HAS_MSS = False

# ---- Áudio de saída (voz): edge-tts gera o mp3; o Windows toca (MCI) ----
pygame = None
edge_tts = None
HAS_AUDIO = False
AUDIO_ERROR = ""
AUDIO_BACKEND = None
try:
    import edge_tts
except ImportError:
    AUDIO_ERROR = "biblioteca edge-tts não instalada (rode Iniciar_Alicia.bat --reinstall)"
if edge_tts is not None:
    if sys.platform == "win32":
        AUDIO_BACKEND = "mci"                       # nada a instalar
    else:
        try:
            import pygame
            pygame.mixer.init()
            AUDIO_BACKEND = "pygame"
        except Exception as _e:
            AUDIO_ERROR = f"sem player de áudio ({_e})"
    HAS_AUDIO = AUDIO_BACKEND is not None

try:
    import speech_recognition as sr
    HAS_SPEECH = True
except ImportError:
    sr = None
    HAS_SPEECH = False

# ---- Captura de áudio do microfone -------------------------------------
# Preferimos 'sounddevice' (traz o PortAudio dentro do pacote e funciona em qualquer
# versão do Python no Windows, inclusive 3.13/3.14). 'PyAudio' é só alternativa.
sd = None
HAS_SOUNDDEVICE = False
SD_ERROR = ""
try:
    import sounddevice as sd
    HAS_SOUNDDEVICE = True
except Exception as _e:  # ImportError ou OSError (PortAudio ausente)
    SD_ERROR = str(_e)

import importlib.util as _ilu
HAS_PYAUDIO = _ilu.find_spec("pyaudio") is not None
HAS_MIC = HAS_SPEECH and (HAS_SOUNDDEVICE or HAS_PYAUDIO)
if HAS_MIC:
    MIC_ERROR = ""
elif not HAS_SPEECH:
    MIC_ERROR = "SpeechRecognition não instalado (rode Iniciar_Alicia.bat --reinstall)"
else:
    MIC_ERROR = "biblioteca de captura de áudio ausente (rode Iniciar_Alicia.bat --reinstall para instalar o sounddevice)"

try:
    import psutil
    HAS_PSUTIL = True
except ImportError:
    psutil = None
    HAS_PSUTIL = False

try:
    import ctypes
    if sys.platform == "win32":
        user32 = ctypes.windll.user32
        HAS_WIN32 = True
    else:
        HAS_WIN32 = False
except Exception:
    HAS_WIN32 = False
