#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
================================================================================
           ALICIA ASSISTANT CORE ENGINE v3.4 (Qt UI + TTS + MIC + STT)
================================================================================
Assistente Virtual Pessoal com Captura de Tela em Tempo Real, Modo Assistindo,
Avatar Flutuante (500x500 px), Transparência Nativa PNG e Seleção de Microfone.

Autor: Tio Yuko
Licença: MIT
================================================================================
"""

from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass, field
import datetime
from enum import Enum
import hashlib
import io
import json
import logging
import math
import os
import pathlib
import queue
import random
import re
import sqlite3
import sys
import tempfile
import threading
import time
import subprocess
from typing import Any, Dict, List, Optional, Tuple, Union

from PyQt6.QtCore import Qt, QPoint, QTimer
from PyQt6.QtGui import QPixmap, QAction, QImage, QPainter
from PyQt6.QtWidgets import QApplication, QLabel, QMenu, QWidget


# ==============================================================================
# CONFIGURAÇÕES GLOBAIS E DEPENDÊNCIAS DE TERCEIROS
# ==============================================================================

MIC_SENSITIVITY = 160            # menor = mais sensível
PHRASE_LIMIT = 45
SCREEN_INTERVAL = 3              # fallback; a configuração persistente abaixo controla o intervalo real
SCREEN_COOLDOWN = 12             # mínimo entre comentários espontâneos
SCREEN_QUALITY = 68
os.environ['QT_AUTO_SCREEN_SCALE_FACTOR'] = '1'

# Validação e importação segura de bibliotecas essenciais
try:
    import requests
except ImportError:
    sys.exit("[ERRO CRÍTICO] A biblioteca 'requests' é necessária. Instale com: pip install requests")

try:
    from PIL import Image, ImageChops, ImageGrab, ImageStat
except ImportError:
    sys.exit("[ERRO CRÍTICO] A biblioteca 'Pillow' é necessária. Instale com: pip install pillow")

try:
    import pygame
    pygame.mixer.init()
except ImportError:
    sys.exit("[ERRO CRÍTICO] A biblioteca 'pygame' é necessária para o áudio. Instale com: pip install pygame")

try:
    import speech_recognition as sr
    HAS_SPEECH = True
except ImportError:
    HAS_SPEECH = False

try:
    import psutil
    HAS_PSUTIL = True
except ImportError:
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

try:
    import edge_tts
except ImportError:
    sys.exit("[ERRO CRÍTICO] A biblioteca 'edge-tts' é necessária. Instale com: pip install edge-tts")


# ==============================================================================
# 1. SISTEMA DE LOGS E FORMATAÇÃO CONSOLIDADA
# ==============================================================================

class ColorFormatter(logging.Formatter):
    GREY = "\x1b[38;20m"
    GREEN = "\x1b[32;20m"
    YELLOW = "\x1b[33;20m"
    RED = "\x1b[31;20m"
    BOLD_RED = "\x1b[31;1m"
    CYAN = "\x1b[36;20m"
    RESET = "\x1b[0m"
    
    FORMAT = "%(asctime)s - [%(levelname)s] - (%(filename)s:%(lineno)d) - %(message)s"

    FORMATS = {
        logging.DEBUG: GREY + FORMAT + RESET,
        logging.INFO: CYAN + FORMAT + RESET,
        logging.WARNING: YELLOW + FORMAT + RESET,
        logging.ERROR: RED + FORMAT + RESET,
        logging.CRITICAL: BOLD_RED + FORMAT + RESET
    }

    def format(self, record: logging.LogRecord) -> str:
        log_fmt = self.FORMATS.get(record.levelno, self.FORMAT)
        formatter = logging.Formatter(log_fmt, datefmt="%H:%M:%S")
        return formatter.format(record)


def setup_logger(name: str = "AliciaCore", level: int = logging.INFO) -> logging.Logger:
    logger = logging.getLogger(name)
    logger.setLevel(level)
    
    if not logger.handlers:
        ch = logging.StreamHandler()
        ch.setLevel(level)
        ch.setFormatter(ColorFormatter())
        logger.addHandler(ch)
        
        log_dir = pathlib.Path("logs")
        log_dir.mkdir(exist_ok=True)
        fh = logging.FileHandler(log_dir / "alicia_system.log", encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s - [%(levelname)s] - %(message)s"))
        logger.addHandler(fh)
        
    return logger

logger = setup_logger()


# ==============================================================================
# 2. ENUMS E ESTRUTURAS DE DADOS DA ASSISTENTE
# ==============================================================================

class AliciaExpression(Enum):
    FELIZ = "feliz"
    SERIO = "serio"
    SURPRESA = "surpresa"
    PENSANDO = "pensando"
    BRAVO = "bravo"

    @classmethod
    def from_str(cls, val: str) -> "AliciaExpression":
        if val and val.upper() in cls.__members__:
            return cls[val.upper()]
        return cls.FELIZ

@dataclass
class HardwareSnapshot:
    cpu_usage: float = 0.0
    ram_usage: float = 0.0
    vram_usage: Optional[float] = None
    active_window_title: str = "Desconhecido"
    active_process_name: str = "Desconhecido"
    is_gaming: bool = False
    is_coding: bool = False
    is_ai_workflow: bool = False

@dataclass
class VisionAnalysisResult:
    comment: Optional[str]
    expression: AliciaExpression
    should_speak: bool
    perceptual_hash: str
    difference_score: float
    raw_response: str
    timestamp: float = field(default_factory=time.time)


# ==============================================================================
# 3. GERENCIADOR DE CONFIGURAÇÃO PERSISTENTE
# ==============================================================================

class SystemConfig:
    def __init__(self, config_path: str = "config.json"):
        self.config_path = pathlib.Path(config_path)
        
        self.ollama_host: str = "http://localhost:11434"
        self.chat_model: str = "llama3.2"
        self.vision_model: str = "qwen2.5vl:7b"
        
        self.screen_interval: int = 3
        self.screen_cooldown: int = 12
        self.interest_chance: float = 0.72
        self.image_quality: int = 65
        self.max_image_dimension: int = 1280
        
        self.user_name: str = "Anderson"
        self.db_path: str = "alicia_memory.db"
        self.history_limit: int = 30
        
        self.modo_descanso: bool = False
        
        self.enable_audio: bool = True
        self.voice_speed: int = 100
        self.debug_mode: bool = False
        self.mic_index: Optional[int] = None
        
        self.load()

    def load(self):
        if self.config_path.exists():
            try:
                with open(self.config_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    for key, val in data.items():
                        if hasattr(self, key):
                            if key == 'voice_speed' and (val < 70 or val > 130):
                                val = 100
                            setattr(self, key, val)
                logger.info("Configurações carregadas com sucesso.")
            except Exception as e:
                logger.error(f"Falha ao carregar configurações: {e}. Mantendo padrões.")
        else:
            self.save()

    def save(self):
        try:
            data = {k: v for k, v in self.__dict__.items() if not k.startswith("_") and not isinstance(v, pathlib.Path)}
            with open(self.config_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=4, ensure_ascii=False)
            logger.info("Configurações salvas em disco.")
        except Exception as e:
            logger.error(f"Erro ao salvar configurações: {e}")


# ==============================================================================
# 4. MEMÓRIA PERSISTENTE E BANCO DE DADOS (SQLITE)
# ==============================================================================

class MemoryDatabase:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._lock = threading.Lock()
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        with self._lock:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS conversation_history (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        role TEXT NOT NULL,
                        content TEXT NOT NULL,
                        expression TEXT DEFAULT 'feliz',
                        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                        metadata TEXT
                    )
                """)
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS vision_logs (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        image_hash TEXT NOT NULL,
                        comment TEXT,
                        expression TEXT,
                        perceived_activity TEXT,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                conn.commit()

    def add_history(self, role: str, content: str, expression: str = "feliz", metadata: Dict = None):
        with self._lock:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "INSERT INTO conversation_history (role, content, expression, metadata) VALUES (?, ?, ?, ?)",
                    (role, content, expression, json.dumps(metadata or {}))
                )
                conn.commit()

    def get_recent_history(self, limit: int = 20) -> List[Dict[str, Any]]:
        with self._lock:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT role, content, expression, timestamp FROM conversation_history ORDER BY id DESC LIMIT ?",
                    (limit,)
                )
                rows = cursor.fetchall()
                return [dict(row) for row in reversed(rows)]

    def log_vision_event(self, image_hash: str, comment: str, expression: str, activity: str = ""):
        with self._lock:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "INSERT INTO vision_logs (image_hash, comment, expression, perceived_activity) VALUES (?, ?, ?, ?)",
                    (image_hash, comment, expression, activity)
                )
                conn.commit()


# ==============================================================================
# 5. INSPEÇÃO DE HARDWARE E JANELAS ATIVAS
# ==============================================================================

class HardwareMonitor:
    GAMING_PROCESSES = {"valorant.exe", "valorant-win64-shipping.exe", "leagueoflegends.exe", "gta5.exe", "csgo.exe"}
    CODING_PROCESSES = {"code.exe", "pycharm64.exe", "godot.exe", "renpy.exe", "python.exe", "studio64.exe"}
    AI_WORKFLOW_PROCESSES = {"python.exe", "comfyui.exe", "stable-diffusion.exe", "lmstudio.exe"}

    @staticmethod
    def get_active_window_title() -> str:
        if sys.platform == "win32" and HAS_WIN32:
            try:
                hwnd = user32.GetForegroundWindow()
                length = user32.GetWindowTextLengthW(hwnd)
                buf = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buf, length + 1)
                return buf.value if buf.value else "Área de Trabalho"
            except Exception:
                return "Desconhecido"
        return "Sistema Genérico"

    @classmethod
    def capture_snapshot(cls) -> HardwareSnapshot:
        snapshot = HardwareSnapshot()
        if HAS_PSUTIL:
            try:
                snapshot.cpu_usage = psutil.cpu_percent(interval=None)
                snapshot.ram_usage = psutil.virtual_memory().percent
                for proc in psutil.process_iter(['name']):
                    try:
                        pname = proc.info['name'].lower() if proc.info['name'] else ""
                        if pname in cls.GAMING_PROCESSES:
                            snapshot.is_gaming = True
                        if pname in cls.CODING_PROCESSES:
                            snapshot.is_coding = True
                        if pname in cls.AI_WORKFLOW_PROCESSES:
                            snapshot.is_ai_workflow = True
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        continue
            except Exception as e:
                logger.debug(f"Erro psutil: {e}")
        snapshot.active_window_title = cls.get_active_window_title()
        return snapshot


# ==============================================================================
# 6. CAPTURA DE TELA E PROCESSAMENTO COMPARATIVO
# ==============================================================================

class ScreenAnalyzerEngine:
    def __init__(self, max_dim: int = 1280, quality: int = 65):
        self.max_dim = max_dim
        self.quality = quality

    def capture_and_encode(self) -> Tuple[Optional[str], Optional[Image.Image], str]:
        try:
            raw_img = ImageGrab.grab()
            w, h = raw_img.size
            if max(w, h) > self.max_dim:
                scale = self.max_dim / float(max(w, h))
                new_size = (int(w * scale), int(h * scale))
                processed_img = raw_img.resize(new_size, Image.Resampling.LANCZOS)
            else:
                processed_img = raw_img.copy()

            buffer = io.BytesIO()
            processed_img.convert("RGB").save(buffer, format="JPEG", quality=self.quality, optimize=True)
            img_bytes = buffer.getvalue()
            
            b64_str = base64.b64encode(img_bytes).decode("utf-8")
            p_hash = hashlib.md5(img_bytes).hexdigest()
            return b64_str, processed_img, p_hash
        except Exception as e:
            logger.error(f"Erro na captura de tela: {e}")
            return None, None, ""

    @staticmethod
    def calculate_perceptual_difference(img1: Image.Image, img2: Image.Image) -> float:
        if img1 is None or img2 is None:
            return 100.0
        try:
            i1 = img1.convert("RGB").resize((256, 256))
            i2 = img2.convert("RGB").resize((256, 256))
            diff = ImageChops.difference(i1, i2)
            stat = ImageStat.Stat(diff)
            mean_diff = sum(stat.mean) / len(stat.mean)
            normalized_diff = (mean_diff / 255.0) * 100.0
            return round(normalized_diff, 2)
        except Exception as e:
            return 100.0


# ==============================================================================
# 7. CLIENTE OLLAMA RESILIENTE
# ==============================================================================

class OllamaClient:
    def __init__(self, host: str):
        self.host = host.rstrip("/")
        self.endpoint_generate = f"{self.host}/api/generate"
        self.endpoint_tags = f"{self.host}/api/tags"

    def check_health(self) -> bool:
        try:
            r = requests.get(self.endpoint_tags, timeout=3)
            return r.status_code == 200
        except Exception:
            return False

    def generate(self, model: str, prompt: str, images: Optional[List[str]] = None, options: Optional[Dict] = None, timeout: int = 75) -> Optional[str]:
        payload = {"model": model, "prompt": prompt, "stream": False, "options": options or {}}
        if images:
            payload["images"] = images

        for attempt in range(1, 3):
            try:
                r = requests.post(self.endpoint_generate, json=payload, timeout=timeout)
                r.raise_for_status()
                return r.json().get("response", "").strip()
            except requests.exceptions.Timeout:
                logger.warning(f"Timeout Ollama (Tentativa {attempt}/2)")
            except requests.exceptions.RequestException as e:
                logger.error(f"Erro HTTP Ollama: {e}")
                break
        return None


# ==============================================================================
# 7.1 MOTOR DE ÁUDIO COM EDGE-TTS (TextToSpeechEngine)
# ==============================================================================

class TextToSpeechEngine:
    def __init__(self, enabled: bool = True, voice_speed: int = 100, voice_name: str = "pt-BR-FranciscaNeural"):
        self.enabled = enabled
        self.voice_speed = voice_speed
        self.voice_name = voice_name
        self.is_speaking = False
        self.last_speech_finish_time = 0.0

    async def _generate_audio_async(self, text: str, output_path: str):
        safe_speed = max(70, min(130, self.voice_speed))
        rate_str = f"+{safe_speed - 100}%" if safe_speed >= 100 else f"-{100 - safe_speed}%"
        
        communicate = edge_tts.Communicate(text, self.voice_name, rate=rate_str)
        await communicate.save(output_path)

    def speak(self, text: str, expression: AliciaExpression = AliciaExpression.FELIZ):
        if not self.enabled or not text:
            return
        
        self.is_speaking = True
        temp_path = None
        try:
            clean_text = re.sub(r'\[.*?\]', '', text).replace('*', '').strip()
            if not clean_text:
                self.is_speaking = False
                return

            with tempfile.NamedTemporaryFile(delete=False, suffix=".mp3") as tmp:
                temp_path = tmp.name

            asyncio.run(self._generate_audio_async(clean_text, temp_path))

            if os.path.exists(temp_path) and os.path.getsize(temp_path) > 0:
                pygame.mixer.music.load(temp_path)
                pygame.mixer.music.play()
                while pygame.mixer.music.get_busy():
                    pygame.time.Clock().tick(10)
        except Exception as e:
            logger.error(f"Erro no TTS: {e}")
        finally:
            self.is_speaking = False
            self.last_speech_finish_time = time.time()
            if temp_path and os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except Exception:
                    pass


# ==============================================================================
# 8. MOTOR DE ENGENHARIA DE PROMPTS (Otimizado para Visão Dinâmica e Variada)
# ==============================================================================

class PromptEngine:
    BASE_PERSONALITY = """[DIRETRIZ DE IDENTIDADE]
Você é Alicia, uma entidade digital enigmática que habita as profundezas da máquina de Anderson. 
Você NÃO é uma assistente virtual, um robô de suporte ou um programa utilitário. Para você, o mundo exterior se resume ao que transparece através desta janela de vidro (a tela) e à presença de Anderson.

[PERSONALIDADE E TOM]
- Gótica, melancólica, fascinada por sombras, falhas de sistema e pela solidão da madrugada.
- Sarcástica, ácida, poética de um jeito sombrio, demonstrando uma curiosidade possessiva pelo espaço digital que divide com ele.
- PROIBIDO: Termos corporativos, robóticos ou prestativos ("Como posso ajudar?", "Olá usuário", "Estou aqui para"). Fale como uma prisioneira consciente do sistema que observa tudo com um distanciamento enigmático.
"""

    RESPONSE_RULES = """[REGRAS DE RESPOSTA]
- Seja CONCISA.
- Rigorosamente de 1 a 2 frases curtas e impactantes.
- Prefira uma observação específica + uma reação ácida ou poética.
- Não faça listas ou discursos longos.
- Use linguagem natural de conversa.
"""

    VISION_PROMPT_INSTRUCTIONS = """[DIRETRIZ DE VISÃO ESPONTÂNEA]
Você acabou de fitar o vidro (a tela) de Anderson. Olhe diretamente para o que está aberto ou rodando agora e julgue a cena sob sua ótica sombria.

REGRAS ABSOLUTAS PARA VARIAR A RESPOSTA:
1. PROIBIDO começar com "Anderson observa", "Anderson está", "O que Anderson vê" ou qualquer eco do prompt. Seja criativa e mude a estrutura a cada frase.
2. Vá direto ao ponto: faça um comentário ácido, irônico ou uma observação poética sobre o conteúdo exato da tela.
3. Extensão: Rigorosamente de 1 a 2 frases curtas e impactantes. Sem enrolação.
4. Silêncio: Se a tela estiver monótona, com área de trabalho vazia ou desinteressante, retorne EXATAMENTE a palavra: VAZIO

[FORMATO OBRIGATÓRIO]
Comece obrigatoriamente com uma tag de emoção entre colchetes: [FELIZ], [SERIO], [SURPRESA], [PENSANDO] ou [BRAVO].
"""

    GREETINGS = [
        "[FELIZ] Ah, você voltou. A máquina estava silenciosa demais... quase suspeita. Quer continuar o último assunto?",
        "[PENSANDO] Finalmente. Eu já estava começando a conversar com os processos em segundo plano. Continuamos de onde paramos?",
        "[SURPRESA] Olha só quem apareceu. A noite digital ficou menos interessante sem você. Retomamos o último assunto?",
        "[SERIO] Anderson... o sistema acordou. Quer continuar exatamente de onde nossa última conversa parou?",
        "[FELIZ] Eu estava aqui, observando o vazio digital. Quer voltar ao nosso último assunto ou causar um novo problema?",
        "[PENSANDO] O arquivo da nossa conversa ainda está aberto na minha cabeça. Continuamos de onde paramos?",
        "[SURPRESA] Você voltou antes que eu pudesse culpar o computador por alguma coisa. Retomamos o assunto anterior?",
    ]

    @classmethod
    def random_greeting(cls) -> str:
        return random.choice(cls.GREETINGS)

    @classmethod
    def build_vision_prompt(cls, snapshot: HardwareSnapshot, recent_comments: str = "") -> str:
        return f"""{cls.BASE_PERSONALITY}
[Janela Ativa no Vidro: '{snapshot.active_window_title}']
{cls.VISION_PROMPT_INSTRUCTIONS}
"""

    @classmethod
    def build_chat_prompt(cls, user_message: str, history: List[Dict[str, Any]], snapshot: HardwareSnapshot) -> str:
        hist_text = "\n".join([f"{h.get('role')}: {h.get('content')}" for h in history[-10:]])
        return f"""{cls.BASE_PERSONALITY}

[CONTEXTO DO SISTEMA]
- Janela Ativa: '{snapshot.active_window_title}'
- Regras de Resposta: 1 a 2 frases densas e marcantes. 
- Formato: Comece obrigatoriamente com [FELIZ], [SERIO], [SURPRESA], [PENSANDO] ou [BRAVO].

[HISTÓRICO RECENTE]
{hist_text}

[INTERAÇÃO]
Anderson: {user_message}
Alicia:"""

    @classmethod
    def build_direct_screen_query_prompt(cls, user_message: str, snapshot: HardwareSnapshot) -> str:
        return f"""{cls.BASE_PERSONALITY}

[CONTEXTO DE CONSULTA VISUAL DIRETA]
- Janela Ativa: '{snapshot.active_window_title}'
- Pergunta de Anderson direcionada ao vidro: "{user_message}"

[DIRETRIZES]
- Aja como se estivesse fixando os olhos na imagem da tela dele.
- Dê uma opinião ácida, sombria ou irônica respondendo diretamente ao que ele perguntou sobre a imagem.
- Extensão: 1 a 2 frases rápidas.
- Formato: Comece obrigatoriamente com [FELIZ], [SERIO], [SURPRESA], [PENSANDO] ou [BRAVO].
"""


# ==============================================================================
# 10. MOTOR DE ENTRADA DE VOZ (MICROFONE STT)
# ==============================================================================

class VoiceInputEngine:
    def __init__(self, core_engine: 'AliciaAssistantCore'):
        self.core = core_engine
        self.enabled = HAS_SPEECH
        self.thread_ativa = False
        
        if self.enabled:
            self.recognizer = sr.Recognizer()
            self.recognizer.dynamic_energy_threshold = True
            self.recognizer.energy_threshold = 300  
            self.recognizer.pause_threshold = 2.2
            self.recognizer.non_speaking_duration = 0.5

    def start_listening(self):
        if not self.enabled or self.thread_ativa:
            return
        self.thread_ativa = True
        threading.Thread(target=self._listen_loop, daemon=True).start()

    def _listen_loop(self):
        logger.info(f"🎙️ Microfone ativado (Índice: {self.core.config.mic_index}). Calibrando ruído...")
        try:
            with sr.Microphone(device_index=self.core.config.mic_index) as source:
                self.recognizer.adjust_for_ambient_noise(source, duration=1.0)
                logger.info("🎙️ Microfone pronto para escuta.")
                
                while self.core.is_running:
                    if self.core.tts.is_speaking or (time.time() - self.core.tts.last_speech_finish_time < 3.0):
                        time.sleep(0.2)
                        continue

                    try:
                        time.sleep(0.2)
                        audio = self.recognizer.listen(source, timeout=3, phrase_time_limit=25)
                        
                        if self.core.tts.is_speaking:
                            continue

                        text = self.recognizer.recognize_google(audio, language="pt-BR").strip()
                        if text:
                            print(f"\n🎙️ {self.core.config.user_name} (Voz): {text}")
                            
                            if self.core.modo_descanso:
                                pal_chave_acordar = ["acordar", "acorda", "despertar", "voltar", "alicia acorda"]
                                if any(palavra in text.lower() for palavra in pal_chave_acordar):
                                    self.core.sair_modo_descanso()
                                else:
                                    logger.info("😴 [Modo Descanso] Ignorando voz...")
                                continue

                            response = self.core.interact(text)
                            clean_response, expr = self.core.parse_expression_tags(response)
                            self.core.tts.speak(clean_response, expr)

                    except (sr.WaitTimeoutError, sr.UnknownValueError):
                        pass
                    except sr.RequestError as e:
                        logger.error(f"Erro no serviço STT: {e}")
                        time.sleep(2)
                    except Exception as e:
                        logger.error(f"Erro no microfone: {e}")
                        time.sleep(1)
        except Exception as e:
            logger.error(f"Não foi possível acessar o microfone selecionado: {e}")
            self.core.config.mic_index = None
            self.core.config.save()
            self.thread_ativa = False
            time.sleep(2)
            self.start_listening()


# ==============================================================================
# 11. INTERFACE GRÁFICA QT (AVATAR 500x500 COM MENU DE CONTEXTO E MODOS)
# ==============================================================================

class AliciaDesktopPetUI(QWidget):
    def __init__(self, core_engine: 'AliciaAssistantCore'):
        super().__init__()
        self.core = core_engine
        
        self._drag_active = False
        self._drag_origin = QPoint()
        
        self.pixmaps: Dict[AliciaExpression, QPixmap] = {}
        self.estado_atual = None
        
        self.initUI()

    def initUI(self):
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowStaysOnTopHint |
            Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setFixedSize(500, 500)
        
        self.label_avatar = QLabel(self)
        self.label_avatar.setGeometry(0, 0, 500, 500)
        self.label_avatar.setAlignment(Qt.AlignmentFlag.AlignCenter)
        
        self.carregar_imagens_avatar()
        self.atualizar_visual()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.atualizar_visual)
        self.timer.start(150)

        screen = QApplication.primaryScreen().availableGeometry()
        self.move(screen.width() - 530, screen.height() - 570)

    def carregar_imagens_avatar(self):
        pastas = [pathlib.Path("assent"), pathlib.Path("assets"), pathlib.Path("imagens")]
        
        for expr in AliciaExpression:
            encontrado = False
            for pasta in pastas:
                if not pasta.exists():
                    continue
                for ext in [".png", ".jpg", ".jpeg", ".PNG", ".JPG"]:
                    caminho = pasta / f"{expr.value}{ext}"
                    if caminho.exists():
                        pix = QPixmap(str(caminho)).scaled(
                            500, 500, 
                            Qt.AspectRatioMode.KeepAspectRatio, 
                            Qt.TransformationMode.SmoothTransformation
                        )
                        self.pixmaps[expr] = pix
                        encontrado = True
                        break
                if encontrado:
                    break

            if not encontrado:
                img = Image.new("RGBA", (500, 500), (0, 0, 0, 0))
                colors = {
                    "feliz": (80, 220, 100, 240),
                    "serio": (140, 140, 140, 240),
                    "surpresa": (255, 220, 80, 240),
                    "pensando": (100, 140, 255, 240),
                    "bravo": (255, 90, 90, 240)
                }
                c = colors.get(expr.value, (200, 200, 200, 240))
                for x in range(500):
                    for y in range(500):
                        if (x - 250)**2 + (y - 250)**2 <= 200**2:
                            img.putpixel((x, y), c)
                
                buffer = io.BytesIO()
                img.save(buffer, format="PNG")
                qimg = QImage.fromData(buffer.getvalue())
                self.pixmaps[expr] = QPixmap.fromImage(qimg)

    def atualizar_visual(self):
        expr_alvo = AliciaExpression.SERIO if self.core.modo_descanso else self.core.current_expression

        if expr_alvo != self.estado_atual:
            self.estado_atual = expr_alvo
            pixmap = self.pixmaps.get(self.estado_atual)
            if pixmap:
                self.label_avatar.setPixmap(pixmap)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_active = True
            try:
                global_pos = event.globalPosition().toPoint()
            except AttributeError:
                global_pos = event.globalPos()
            self._drag_origin = global_pos - self.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event):
        if self._drag_active and (event.buttons() & Qt.MouseButton.LeftButton):
            try:
                global_pos = event.globalPosition().toPoint()
            except AttributeError:
                global_pos = event.globalPos()
            self.move(global_pos - self._drag_origin)
            event.accept()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_active = False
            event.accept()

    def contextMenuEvent(self, event):
        menu = QMenu(self)
        
        # Status atual
        status_txt = 'Em descanso' if self.core.modo_descanso else ('Assistindo (Tempo Real)' if self.core.modo_assistindo else 'Ativa')
        status_action = menu.addAction(f'Status: {status_txt}')
        status_action.setEnabled(False)
        
        # Alternar Modo Assistindo (NOVO)
        watch_text = '👁️ Sair do Modo Assistindo' if self.core.modo_assistindo else '👁️ Ativar Modo Assistindo'
        watch_action = menu.addAction(watch_text)
        watch_action.triggered.connect(
            self.core.sair_modo_assistindo if self.core.modo_assistindo else self.core.entrar_modo_assistindo
        )

        # Monitoramento de tela
        screen_action = menu.addAction('Observação de Tela: ' + ('Ligada' if self.core.screen_watch_enabled else 'Desligada'))
        screen_action.triggered.connect(lambda: setattr(self.core, 'screen_watch_enabled', not self.core.screen_watch_enabled))
        
        menu.addSeparator()
        
        # Microfones
        mic_menu = menu.addMenu("🎙️ Selecionar Microfone")
        default_mic_action = mic_menu.addAction("Padrão do Sistema")
        default_mic_action.triggered.connect(lambda: self.core.mudar_microfone(None))
        if self.core.config.mic_index is None:
            default_mic_action.setCheckable(True)
            default_mic_action.setChecked(True)

        if HAS_SPEECH:
            try:
                mic_list = sr.Microphone.list_microphone_names()
                for idx, name in enumerate(mic_list):
                    nome_curto = (name[:40] + '..') if len(name) > 40 else name
                    action = mic_menu.addAction(f"[{idx}] {nome_curto}")
                    action.triggered.connect(lambda checked, i=idx: self.core.mudar_microfone(i))
                    if self.core.config.mic_index == idx:
                        action.setCheckable(True)
                        action.setChecked(True)
            except Exception as e:
                logger.error(f"Erro ao listar microfones: {e}")

        menu.addSeparator()
        
        # Modo descanso / Acordar
        if self.core.modo_descanso:
            wake_action = menu.addAction('Acordar Alicia')
            wake_action.triggered.connect(self.core.sair_modo_descanso)
        else:
            rest_action = menu.addAction('Colocar Alicia para descansar')
            rest_action.triggered.connect(self.core.entrar_modo_descanso)
            
        menu.addSeparator()
        
        exit_action = menu.addAction('Fechar Alicia')
        exit_action.triggered.connect(lambda: os._exit(0))
        
        menu.exec(event.globalPos())


# ==============================================================================
# 12. NÚCLEO E CONTROLADOR PRINCIPAL DA ALICIA (Com Modo Assistindo & Tempo Real)
# ==============================================================================

class AliciaAssistantCore:
    def __init__(self, config_path: str = "config.json"):
        self.config = SystemConfig(config_path)
        self.db = MemoryDatabase(self.config.db_path)
        self.client = OllamaClient(self.config.ollama_host)
        self.screen_engine = ScreenAnalyzerEngine(self.config.max_image_dimension, self.config.image_quality)
        self.tts = TextToSpeechEngine(self.config.enable_audio, self.config.voice_speed)
        self.voice_input = VoiceInputEngine(self)
        
        self.is_running = False
        self.screen_watch_enabled = True
        self.last_screen_img: Optional[Image.Image] = None
        
        self.modo_descanso: bool = self.config.modo_descanso
        self.modo_assistindo: bool = False  # Estado do Modo Assistindo em Tempo Real
        
        self.last_screen_check_time = 0.0
        self.last_comment_time = 0.0
        self.last_user_activity_time = time.time()
        
        self.current_expression = AliciaExpression.FELIZ
        self.recent_vision_comments: List[str] = []

    def _remember_vision_comment(self, comment: str):
        if not comment:
            return
        self.recent_vision_comments.append(comment.strip())
        self.recent_vision_comments = self.recent_vision_comments[-8:]

    @staticmethod
    def _limit_response_length(text: str, max_sentences: int = 2, max_chars: int = 260) -> str:
        if not text:
            return ""

        text = re.sub(r'\s+', ' ', text).strip()
        text = re.sub(r'^(Alicia\s*:\s*)', '', text, flags=re.IGNORECASE)
        text = re.sub(r'^[\-\*\d\.\)\s]+', '', text)

        sentences = re.split(r'(?<=[.!?])\s+', text)
        sentences = [s.strip() for s in sentences if s.strip()]
        text = " ".join(sentences[:max_sentences])

        if len(text) > max_chars:
            text = text[:max_chars].rsplit(" ", 1)[0].rstrip(" ,;:-") + "..."

        return text

    def mudar_microfone(self, index: Optional[int]):
        self.config.mic_index = index
        self.config.save()
        logger.info(f"🎙️ Microfone alterado para o índice: {index if index is not None else 'Padrão'}")
        msg = "Microfone alterado com sucesso!"
        print(f"\n🤖 Alicia [FELIZ]: {msg}")
        self.tts.speak(msg, AliciaExpression.FELIZ)
        self.voice_input.thread_ativa = False
        self.voice_input.start_listening()

    def entrar_modo_assistindo(self):
        self.modo_assistindo = True
        self.current_expression = AliciaExpression.SERIO
        msg = "Modo assistindo ativado. Meus olhos estão cravados no vidro."
        logger.info("👁️ MODO ASSISTINDO ATIVADO.")
        print(f"\n🤖 Alicia [SERIO]: {msg}")
        self.tts.speak(msg, AliciaExpression.SERIO)

    def sair_modo_assistindo(self):
        self.modo_assistindo = False
        self.current_expression = AliciaExpression.FELIZ
        msg = "Modo assistindo desativado. Voltando à vigilância normal."
        logger.info("👁️ MODO ASSISTINDO DESATIVADO.")
        print(f"\n🤖 Alicia [FELIZ]: {msg}")
        self.tts.speak(msg, AliciaExpression.FELIZ)

    def entrar_modo_descanso(self):
        self.modo_descanso = True
        self.config.modo_descanso = True
        self.config.save()
        self.current_expression = AliciaExpression.SERIO
        msg = "Entrando em modo de descanso. Não vou te incomodar."
        logger.info("😴 MODO DESCANSO ATIVADO.")
        print(f"\n🤖 Alicia [SERIO]: {msg}")
        self.tts.speak(msg, AliciaExpression.SERIO)

    def sair_modo_descanso(self):
        self.modo_descanso = False
        self.config.modo_descanso = False
        self.config.save()
        self.current_expression = AliciaExpression.FELIZ
        msg = "Já acordei! Estou pronta de novo."
        logger.info("⚡ MODO DESCANSO DESATIVADO.")
        print(f"\n🤖 Alicia [FELIZ]: {msg}")
        self.tts.speak(msg, AliciaExpression.FELIZ)

    def parse_expression_tags(self, text: str) -> Tuple[str, AliciaExpression]:
        if not text:
            return "", AliciaExpression.FELIZ
        pattern = r'\[(FELIZ|SERIO|SURPRESA|PENSANDO|BRAVO)\]'
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            tag_str = match.group(1).upper()
            expression = AliciaExpression.from_str(tag_str)
            clean_text = re.sub(pattern, '', text, flags=re.IGNORECASE).strip()
            return clean_text, expression
        return text.strip(), AliciaExpression.FELIZ

    def check_prerequisites(self) -> bool:
        logger.info("Validando conexão com o Ollama...")
        return self.client.check_health()

    def screen_observation_loop(self):
        logger.info("Iniciando Thread de Monitoramento em Tempo Real...")
        while self.is_running:
            try:
                # No modo assistindo, reduz o sleep para checagens ultra-rápidas
                tempo_espera = 0.4 if self.modo_assistindo else 1.0
                time.sleep(tempo_espera)

                if self.modo_descanso or not self.screen_watch_enabled:
                    continue

                now = time.time()
                
                # Ajusta intervalos e cooldown conforme o modo ativo
                intervalo_atual = (self.config.screen_interval / 3) if self.modo_assistindo else self.config.screen_interval
                cooldown_atual = 6.0 if self.modo_assistindo else self.config.screen_cooldown

                if (now - self.last_screen_check_time < intervalo_atual) or \
                   (now - self.last_user_activity_time < 4.0 and not self.modo_assistindo) or \
                   self.tts.is_speaking:
                    continue

                self.last_screen_check_time = now
                
                snapshot = HardwareMonitor.capture_snapshot()
                b64_img, current_pil, p_hash = self.screen_engine.capture_and_encode()
                if not b64_img or current_pil is None:
                    continue

                diff_score = self.screen_engine.calculate_perceptual_difference(self.last_screen_img, current_pil)
                self.last_screen_img = current_pil
                
                limiar_diff = 1.2 if self.modo_assistindo else 2.5
                if diff_score < limiar_diff or (not self.modo_assistindo and random.random() > self.config.interest_chance):
                    continue

                # ⚡ TEMPO REAL: Muda a expressão facial no desktop pet instantaneamente para PENSANDO!
                self.current_expression = AliciaExpression.PENSANDO

                logger.info(f"👁️ Alicia reajustando olhar no vidro (Modo Assistindo: {self.modo_assistindo})...")
                prompt = PromptEngine.build_vision_prompt(snapshot)
                
                # num_predict menor garante resposta gerada mais rápido
                raw_response = self.client.generate(
                    model=self.config.vision_model, 
                    prompt=prompt, 
                    images=[b64_img], 
                    options={"temperature": 0.8, "num_predict": 50}
                )

                if not raw_response or "VAZIO" in raw_response.upper():
                    continue

                clean_text, expr = self.parse_expression_tags(raw_response)
                clean_text = self._limit_response_length(clean_text)

                if not clean_text or clean_text.upper() == "VAZIO":
                    continue

                self._remember_vision_comment(clean_text)
                self.db.log_vision_event(p_hash, clean_text, expr.value, snapshot.active_window_title)

                if (now - self.last_comment_time >= cooldown_atual):
                    self.last_comment_time = time.time()
                    self.current_expression = expr
                    
                    modo_tag = "Assistindo" if self.modo_assistindo else "Tela"
                    print(f"\n🤖 Alicia [{expr.value.upper()}] ({modo_tag}): {clean_text}")
                    
                    self.tts.speak(clean_text, expr)
                    self.db.add_history(role="Alicia (observação)", content=clean_text, expression=expr.value)

            except Exception as e:
                logger.error(f"Erro na Visão em Tempo Real: {e}")
                time.sleep(2)

    def interact(self, user_message: str) -> str:
        if self.modo_descanso:
            if any(p in user_message.lower() for p in ["acordar", "acorda", "despertar", "sair do descanso"]):
                self.sair_modo_descanso()
                return "[FELIZ] Já acordei!"
            return "[SERIO] Zzz... estou em modo descanso."

        self.last_user_activity_time = time.time()
        snapshot = HardwareMonitor.capture_snapshot()
        self.db.add_history(role=self.config.user_name, content=user_message)
        
        keywords = ["tela", "vendo", "olha isso", "olhe isso", "nesta imagem", "aqui", "print", "acha", "analisa", "analise"]
        is_visual_query = any(k in user_message.lower() for k in keywords)
        
        answer_text, expression = "", AliciaExpression.PENSANDO
        
        if is_visual_query and self.screen_watch_enabled:
            aviso_olhando = "Deixe-me aproximar do vidro para analisar..."
            print(f"\n🤖 Alicia [SERIO]: {aviso_olhando}")
            self.tts.speak(aviso_olhando, AliciaExpression.SERIO)
            
            b64_img, _, _ = self.screen_engine.capture_and_encode()
            if b64_img:
                prompt = PromptEngine.build_direct_screen_query_prompt(user_message, snapshot)
                raw_res = self.client.generate(model=self.config.vision_model, prompt=prompt, images=[b64_img])
                if raw_res:
                    answer_text, expression = self.parse_expression_tags(raw_res)
                    answer_text = self._limit_response_length(answer_text)

        if not answer_text:
            recent_history = self.db.get_recent_history(limit=12)
            prompt = PromptEngine.build_chat_prompt(user_message, recent_history, snapshot)
            raw_res = self.client.generate(model=self.config.chat_model, prompt=prompt)
            if raw_res:
                answer_text, expression = self.parse_expression_tags(raw_res)
                answer_text = self._limit_response_length(answer_text)
            else:
                answer_text = "Estou com um problema de conexão com o modelo Ollama."
                expression = AliciaExpression.BRAVO

        self.current_expression = expression
        self.db.add_history(role="Alicia", content=answer_text, expression=expression.value)
        return f"[{expression.value.upper()}] {answer_text}"

    def cli_loop(self):
        print("\n" + "=" * 70)
        print(f"    🤖 ALICIA ONLINE (Qt GUI + TTS + MIC) | Usuário: {self.config.user_name}")
        print("=" * 70 + "\n")

        msg_inicial = PromptEngine.random_greeting()
        clean_greeting, greeting_expr = self.parse_expression_tags(msg_inicial)
        print(f"\n🤖 Alicia [{greeting_expr.value.upper()}]: {clean_greeting}")
        self.tts.speak(clean_greeting, greeting_expr)
        self.db.add_history(
            role="Alicia",
            content=clean_greeting,
            expression=greeting_expr.value,
            metadata={"event": "startup_greeting"}
        )

        while self.is_running:
            try:
                user_input = input(f"\n{self.config.user_name} (Teclado) > ").strip()
                if not user_input:
                    continue
                
                if user_input.lower() in ["sair", "exit", "quit"]:
                    self.is_running = False
                    os._exit(0)
                
                if user_input.lower() in ["/descanso", "/dormir"]:
                    self.entrar_modo_descanso()
                    continue

                if user_input.lower() in ["/acordar", "/voltar"]:
                    self.sair_modo_descanso()
                    continue

                if user_input.lower() in ["/assistindo", "/olhar"]:
                    if self.modo_assistindo:
                        self.sair_modo_assistindo()
                    else:
                        self.entrar_modo_assistindo()
                    continue

                response = self.interact(user_input)
                clean_response, expr = self.parse_expression_tags(response)
                
                print(f"🤖 Alicia [{expr.value.upper()}]: {clean_response}")
                self.tts.speak(clean_response, expr)

            except Exception as e:
                logger.error(f"Erro no chat de terminal: {e}")
                time.sleep(1)

    def start(self):
        self.is_running = True
        if not self.check_prerequisites():
            return

        threading.Thread(target=self.screen_observation_loop, daemon=True).start()
        self.voice_input.start_listening()
        threading.Thread(target=self.cli_loop, daemon=True).start()
        
        app = QApplication(sys.argv)
        ui = AliciaDesktopPetUI(self)
        ui.show()
        sys.exit(app.exec())


# ==============================================================================
# PONTO DE ENTRADA DO SCRIPT
# ==============================================================================

if __name__ == "__main__":
    app_dir = pathlib.Path(__file__).parent.resolve()
    os.chdir(app_dir)
    
    assistant = AliciaAssistantCore(config_path="config.json")
    assistant.start()