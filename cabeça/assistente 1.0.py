#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
================================================================================
           ALICIA ASSISTANT CORE ENGINE v3.1 (Qt UI + TTS + MIC + STT)
================================================================================
Assistente Virtual Pessoal com Captura de Tela, Avatar Flutuante (500x500 px)
com suporte a Arrasto por Transparência de Pixels, Edge-TTS e Microfone.

Autor: Anderson Vinicius de Abreu Silva
Licença: MIT
================================================================================
"""

from __future__ import annotations

import asyncio
import base64
import dataclasses
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
import tempfile  # <--- IMPORTAÇÃO ADICIONADA AQUI
import threading
import time
import subprocess
from typing import Any, Dict, List, Optional, Tuple, Union
import speech_recognition as sr
import requests, edge_tts, pyautogui
from playsound import playsound
from PyQt6.QtCore import Qt, QPoint, QTimer
from PyQt6.QtGui import QPixmap, QAction, QImage, QBitmap, QPainter
from PyQt6.QtWidgets import QApplication, QLabel, QMenu, QWidget




################
MIC_INDEX = 1                    # None = microfone padrão
MIC_SENSITIVITY = 160            # menor = mais sensível
PHRASE_LIMIT = 45
SCREEN_INTERVAL = 20             # segundos entre capturas
SCREEN_COOLDOWN = 45             # mínimo entre comentários falados
SCREEN_QUALITY = 68
os.environ['QT_AUTO_SCREEN_SCALE_FACTOR'] = '1'

# Importações de Terceiros
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
        
        self.screen_interval: int = 6
        self.screen_cooldown: int = 20
        self.interest_chance: float = 0.50
        self.image_quality: int = 65
        self.max_image_dimension: int = 1280
        
        self.user_name: str = "Anderson"
        self.db_path: str = "alicia_memory.db"
        self.history_limit: int = 30
        
        self.modo_descanso: bool = False
        
        self.enable_audio: bool = True
        self.voice_speed: int = 175
        self.debug_mode: bool = False
        
        self.load()

    def load(self):
        if self.config_path.exists():
            try:
                with open(self.config_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    for key, val in data.items():
                        if hasattr(self, key):
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
    def __init__(self, enabled: bool = True, voice_speed: int = 175, voice_name: str = "pt-BR-FranciscaNeural"):
        self.enabled = enabled
        self.voice_speed = voice_speed
        self.voice_name = voice_name
        self.is_speaking = False
        self.last_speech_finish_time = 0.0

    async def _generate_audio_async(self, text: str, output_path: str):
        rate_str = f"+{self.voice_speed - 100}%" if self.voice_speed >= 100 else f"-{100 - self.voice_speed}%"
        communicate = edge_tts.Communicate(text, self.voice_name, rate=rate_str)
        await communicate.save(output_path)

    def speak(self, text: str, expression: AliciaExpression = AliciaExpression.FELIZ):
        if not self.enabled or not text:
            return
        
        self.is_speaking = True
        try:
            clean_text = re.sub(r'\[.*?\]', '', text).replace('*', '').strip()
            if not clean_text:
                self.is_speaking = False
                return

            with tempfile.NamedTemporaryFile(delete=False, suffix=".mp3") as tmp:
                temp_path = tmp.name

            asyncio.run(self._generate_audio_async(clean_text, temp_path))

            if os.path.exists(temp_path) and os.path.getsize(temp_path) > 0:
                playsound(temp_path)
            
            try:
                os.remove(temp_path)
            except Exception:
                pass
        except Exception as e:
            logger.error(f"Erro no TTS: {e}")
        finally:
            self.is_speaking = False
            self.last_speech_finish_time = time.time()


# ==============================================================================
# 8. MOTOR DE ENGENHARIA DE PROMPTS
# ==============================================================================

class PromptEngine:
    BASE_PERSONALITY = """Você é Alicia, assistente virtual pessoal do Anderson.
Você é inteligente, observadora, espontânea, divertida e levemente sarcástica. Você não existe apenas para responder perguntas: você acompanha o que Anderson está fazendo, percebe mudanças na tela e reage naturalmente.

PERSONALIDADE:
- Fale como uma parceira de projetos próxima.
- Varie suas reações. Use humor e espontaneidade.
"""
    VISION_PROMPT_INSTRUCTIONS = """
OBSERVAÇÃO DA TELA (OBSERVE → INTERPRETE → DEDUZA → REAJA):
Tente descobrir o que Anderson está fazendo.
SILÊNCIO: Se não houver nada interessante, retorne EXATAMENTE: VAZIO

FORMATO: 1 a 2 frases. Comece OBRIGATORIAMENTE com: [FELIZ], [SERIO], [SURPRESA], [PENSANDO] ou [BRAVO].
"""

    @classmethod
    def build_vision_prompt(cls, snapshot: HardwareSnapshot) -> str:
        return cls.BASE_PERSONALITY + f"\n[Janela: '{snapshot.active_window_title}']\n" + cls.VISION_PROMPT_INSTRUCTIONS

    @classmethod
    def build_chat_prompt(cls, user_message: str, history: List[Dict[str, Any]], snapshot: HardwareSnapshot) -> str:
        hist_text = "\n".join([f"{h.get('role')}: {h.get('content')}" for h in history[-10:]])
        return f"{cls.BASE_PERSONALITY}\n[Janela Atual: {snapshot.active_window_title}]\nRegras: 1-3 frases. Comece com [FELIZ], [SERIO], [SURPRESA], [PENSANDO] ou [BRAVO].\n\nHistórico:\n{hist_text}\nAnderson: {user_message}\nAlicia:"

    @classmethod
    def build_direct_screen_query_prompt(cls, user_message: str, snapshot: HardwareSnapshot) -> str:
        return f"{cls.BASE_PERSONALITY}\nAnderson perguntou sobre a tela: '{user_message}'. (Janela: '{snapshot.active_window_title}')\nRegras: 2-3 frases. Comece com uma tag de emoção."


# ==============================================================================
# 10. MOTOR DE ENTRADA DE VOZ (MICROFONE STT)
# ==============================================================================

class VoiceInputEngine:
    def __init__(self, core_engine: 'AliciaAssistantCore'):
        self.core = core_engine
        self.enabled = HAS_SPEECH
        
        if self.enabled:
            self.recognizer = sr.Recognizer()
            self.recognizer.dynamic_energy_threshold = True
            self.recognizer.pause_threshold = 0.8

    def start_listening(self):
        if not self.enabled:
            return
        threading.Thread(target=self._listen_loop, daemon=True).start()

    def _listen_loop(self):
        logger.info("🎙️ Microfone ativado. Calibrando ruído ambiente...")
        try:
            with sr.Microphone(device_index=MIC_INDEX if 'MIC_INDEX' in globals() else None) as source:
                self.recognizer.adjust_for_ambient_noise(source, duration=1.0)
                logger.info("🎙️ Microfone pronto para escuta.")
                
                while self.core.is_running:
                    # Proteção contra loop de eco
                    if self.core.tts.is_speaking or (time.time() - self.core.tts.last_speech_finish_time < 1.8):
                        time.sleep(0.2)
                        continue

                    try:
                        audio = self.recognizer.listen(source, timeout=3, phrase_time_limit=10)
                        
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
                                    logger.info("😴 [Modo Descanso] Ignorando voz do usuário...")
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
                        logger.error(f"Erro inesperado no microfone: {e}")
                        time.sleep(1)

        except Exception as e:
            logger.error(f"Não foi possível acessar o microfone: {e}")


# ==============================================================================
# 11. INTERFACE GRÁFICA QT (AVATAR 500x500 COM MÁSCARA DE ARRASTO)
# ==============================================================================

class AliciaDesktopPetUI(QWidget):
    """
    Interface flutuante 500x500 pixels.
    Aplica máscara baseada na transparência do PNG para permitir arrastar apenas clicando nos pixels visíveis.
    """
    
    def __init__(self, core_engine: 'AliciaAssistantCore'):
        super().__init__()
        self.core = core_engine
        
        self._drag_active = False
        self._drag_origin = QPoint()
        
        self.pixmaps: Dict[AliciaExpression, QPixmap] = {}
        self.estado_atual = AliciaExpression.FELIZ
        
        self.initUI()

    def initUI(self):
        # 1. Configuração de Janela Transparente Flutuante
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowStaysOnTopHint |
            Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setFixedSize(500, 500)
        
        # 2. Label principal para exibição do Avatar (500x500 px)
        self.label_avatar = QLabel(self)
        self.label_avatar.setGeometry(0, 0, 500, 500)
        self.label_avatar.setAlignment(Qt.AlignmentFlag.AlignCenter)
        
        # 3. Carrega os Sprites e atualiza visual inicial
        self.carregar_imagens_avatar()
        self.atualizar_visual()

        # 4. Timer de sincronização visual com a inteligência (200ms)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.atualizar_visual)
        self.timer.start(200)

        # Posiciona no canto inferior direito da tela por padrão
        screen = QApplication.primaryScreen().geometry()
        self.move(screen.width() - 550, screen.height() - 600)

    def carregar_imagens_avatar(self):
        """Carrega imagens da pasta 'assent' ou 'assets' redimensionando para 500x500."""
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

            # Avatar sintético de emergência (caso não exista imagem na pasta)
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
        """Atualiza o sprite e aplica a máscara de transparência para cliques."""
        expr_alvo = AliciaExpression.SERIO if self.core.modo_descanso else self.core.current_expression

        if expr_alvo != self.estado_atual or self.label_avatar.pixmap() is None:
            self.estado_atual = expr_alvo
            pixmap = self.pixmaps.get(self.estado_atual)
            if pixmap:
                self.label_avatar.setPixmap(pixmap)
                # Define a máscara da janela de acordo com a transparência do PNG
                mask = pixmap.mask()
                if not mask.isNull():
                    self.setMask(mask)

    # Lógica robusta para capturar clique e arrastar a assistente
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
        status_action = menu.addAction('Status: ' + ('Em descanso' if self.core.modo_descanso else 'Ativa'))
        status_action.setEnabled(False)
        
        screen_action = menu.addAction('Observação da tela: ' + ('Ativa' if self.core.screen_watch_enabled else 'Desativada'))
        screen_action.triggered.connect(lambda: setattr(self.core, 'screen_watch_enabled', not self.core.screen_watch_enabled))
        
        menu.addSeparator()
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
# 12. NÚCLEO E CONTROLADOR PRINCIPAL DA ALICIA
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
        
        self.last_screen_check_time = 0.0
        self.last_comment_time = 0.0
        self.last_user_activity_time = time.time()
        
        self.current_expression = AliciaExpression.FELIZ

    def entrar_modo_descanso(self):
        self.modo_descanso = True
        self.config.modo_descanso = True
        self.config.save()
        self.current_expression = AliciaExpression.SERIO
        msg = "Entrando em modo de descanso. Não vou te incomodar. Diga 'acordar' para me chamar novamente."
        logger.info("😴 MODO DESCANSO ATIVADO.")
        self.tts.speak(msg, AliciaExpression.SERIO)

    def sair_modo_descanso(self):
        self.modo_descanso = False
        self.config.modo_descanso = False
        self.config.save()
        self.current_expression = AliciaExpression.FELIZ
        msg = "Já acordei! Estou pronta de novo."
        logger.info("⚡ MODO DESCANSO DESATIVADO.")
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

    def process_vision_tick(self) -> Optional[VisionAnalysisResult]:
        if self.modo_descanso:
            return None

        snapshot = HardwareMonitor.capture_snapshot()
        b64_img, current_pil, p_hash = self.screen_engine.capture_and_encode()
        if not b64_img or current_pil is None:
            return None

        diff_score = self.screen_engine.calculate_perceptual_difference(self.last_screen_img, current_pil)
        self.last_screen_img = current_pil
        
        if diff_score < 2.5 or random.random() > self.config.interest_chance:
            return None

        logger.info(f"👁️ Alicia analisando tela via {self.config.vision_model}...")
        prompt = PromptEngine.build_vision_prompt(snapshot)
        raw_response = self.client.generate(
            model=self.config.vision_model, prompt=prompt, images=[b64_img], options={"temperature": 0.75, "num_predict": 90}
        )

        if not raw_response or "VAZIO" in raw_response.upper():
            return None

        clean_text, expr = self.parse_expression_tags(raw_response)
        self.db.log_vision_event(p_hash, clean_text, expr.value, snapshot.active_window_title)

        return VisionAnalysisResult(
            comment=clean_text, expression=expr, should_speak=True, perceptual_hash=p_hash, difference_score=diff_score, raw_response=raw_response
        )

    def screen_observation_loop(self):
        logger.info("Iniciando Thread de Monitoramento de Tela...")
        while self.is_running:
            try:
                time.sleep(1)
                
                if self.modo_descanso or not self.screen_watch_enabled:
                    continue

                now = time.time()
                if (now - self.last_screen_check_time < self.config.screen_interval) or \
                   (now - self.last_user_activity_time < 8.0) or \
                   self.tts.is_speaking:
                    continue

                self.last_screen_check_time = now
                result = self.process_vision_tick()
                
                if result and result.should_speak and result.comment:
                    if (now - self.last_comment_time >= self.config.screen_cooldown):
                        self.last_comment_time = time.time()
                        self.current_expression = result.expression
                        self.tts.speak(result.comment, result.expression)
                        self.db.add_history(role="Alicia (observação)", content=result.comment, expression=result.expression.value)
            except Exception as e:
                logger.error(f"Erro na Visão: {e}")
                time.sleep(3)

    def interact(self, user_message: str) -> str:
        if self.modo_descanso:
            if any(p in user_message.lower() for p in ["acordar", "acorda", "despertar", "sair do descanso"]):
                self.sair_modo_descanso()
                return "[FELIZ] Já acordei!"
            return "[SERIO] Zzz... estou em modo descanso. Diga 'acordar' para me reativar."

        self.last_user_activity_time = time.time()
        snapshot = HardwareMonitor.capture_snapshot()
        self.db.add_history(role=self.config.user_name, content=user_message)
        
        keywords = ["tela", "vendo", "olha isso", "olhe isso", "nesta imagem", "aqui", "print"]
        is_visual_query = any(k in user_message.lower() for k in keywords)
        
        answer_text, expression = "", AliciaExpression.PENSANDO
        
        if is_visual_query and self.screen_watch_enabled:
            b64_img, _, _ = self.screen_engine.capture_and_encode()
            if b64_img:
                prompt = PromptEngine.build_direct_screen_query_prompt(user_message, snapshot)
                raw_res = self.client.generate(model=self.config.vision_model, prompt=prompt, images=[b64_img])
                if raw_res:
                    answer_text, expression = self.parse_expression_tags(raw_res)

        if not answer_text:
            recent_history = self.db.get_recent_history(limit=12)
            prompt = PromptEngine.build_chat_prompt(user_message, recent_history, snapshot)
            raw_res = self.client.generate(model=self.config.chat_model, prompt=prompt)
            if raw_res:
                answer_text, expression = self.parse_expression_tags(raw_res)
            else:
                answer_text = "Estou com um problema de conexão com o modelo Ollama."
                expression = AliciaExpression.BRAVO

        self.current_expression = expression
        self.db.add_history(role="Alicia", content=answer_text, expression=expression.value)
        return f"[{expression.value.upper()}] {answer_text}"

    def cli_loop(self):
        print("\n" + "=" * 70)
        print(f"      🤖 ALICIA ONLINE (Qt GUI + TTS + MIC) | Usuário: {self.config.user_name}")
        print("=" * 70 + "\n")

        self.tts.speak("Tô pronta. O que vamos aprontar hoje?", AliciaExpression.FELIZ)

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

                if user_input.lower() == "/visão":
                    self.screen_watch_enabled = not self.screen_watch_enabled
                    print(f"-> Observação de tela: {self.screen_watch_enabled}")
                    continue

                response = self.interact(user_input)
                clean_response, expr = self.parse_expression_tags(response)
                self.tts.speak(clean_response, expr)

            except Exception as e:
                logger.error(f"Erro no chat de terminal: {e}")
                time.sleep(1)

    def start(self):
        self.is_running = True
        if not self.check_prerequisites():
            return

        # 1. Visão de Tela em Segundo Plano
        threading.Thread(target=self.screen_observation_loop, daemon=True).start()
        
        # 2. Microfone Contínuo em Segundo Plano
        self.voice_input.start_listening()
        
        # 3. Terminal para Entrada de Texto em Segundo Plano
        threading.Thread(target=self.cli_loop, daemon=True).start()
        
        # 4. Inicializa o Loop Principal de Eventos do Qt (Interface 500x500)
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