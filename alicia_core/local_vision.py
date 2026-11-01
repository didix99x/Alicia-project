"""
Visão LOCAL da Alicia — não usa internet e nenhuma imagem sai do computador.

Pipeline de UMA leitura de tela (ver LocalVision.read):

  janela/processo ativo -> filtro de privacidade (se for app/janela proibida, NEM captura) ->
  captura -> diferença para o quadro anterior (NumPy) -> OCR opcional (RapidOCR ou Tesseract) ->
  filtro de texto sensível -> classificação da atividade -> eventos (erro na tela, vitória, derrota...)

Reconhecer TEXTO não é entender a cena: por isso a classificação junta três fontes —
(1) processo e título da janela (a mais confiável), (2) o texto lido por OCR e (3) o "jeito" da imagem
(quanto muda, quanta área se mexe). Ela diz O QUE você está fazendo; não descreve fotos nem vídeos.

Tudo aqui é configurável em config.json (prefixo obs_). Ver README, seção "Modo Observadora".
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import sys
import time
import unicodedata
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from .deps import Image
from .logging_setup import logger

try:
    import numpy as np
except ImportError:  # numpy é opcional no projeto; sem ele a observadora não liga (a Alicia segue normal)
    np = None


# ======================================================================
# UTILITÁRIOS
# ======================================================================
def norm(text: str) -> str:
    """minúsculo, sem acento, espaços normalizados (para comparar títulos e textos)."""
    t = unicodedata.normalize("NFD", (text or "").lower())
    t = "".join(c for c in t if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", t).strip()


def _luhn_ok(digits: str) -> bool:
    total, alt = 0, False
    for ch in reversed(digits):
        d = ord(ch) - 48
        if alt:
            d *= 2
            if d > 9:
                d -= 9
        total += d
        alt = not alt
    return total % 10 == 0


# ======================================================================
# RESULTADO DE UMA LEITURA
# ======================================================================
@dataclass
class ScreenReading:
    ts: float = 0.0
    process: str = ""
    title: str = ""
    category: str = "unknown"      # game|coding|terminal|browsing|video|music|chat|social|documents|creative|ai_chat|shopping|reading|searching|launcher|system|unknown
    app_label: str = ""            # nome legível do app/site/jogo ("Visual Studio Code", "YouTube", "Elden Ring")
    activity: str = ""             # frase curta do que está acontecendo ("programando no Visual Studio Code")
    file_hint: str = ""            # arquivo aberto, quando o título mostra
    events: List[str] = field(default_factory=list)   # erro_visivel, testes_ok, testes_falharam, jogo_vitoria, jogo_derrota
    event_sig: str = ""            # assinatura do evento (para não comentar o MESMO erro duas vezes)
    change: float = 0.0            # % de mudança em relação ao quadro anterior
    motion: float = 0.0            # % da área da tela que se mexeu (alto = vídeo/jogo rodando)
    text_density: float = 0.0      # proxy de "tela cheia de texto/interface" (0-100)
    first_frame: bool = False
    ocr_text: str = ""             # texto lido (curto, sem dados sensíveis). Só existe em memória.
    ocr_fresh: bool = False        # este OCR foi feito AGORA (e não reaproveitado)
    ocr_backend: str = ""
    skipped_reason: str = ""       # preenchido quando NÃO observou (app ignorado, janela sensível...)
    sensitive: bool = False
    capture_ms: float = 0.0
    ocr_ms: float = 0.0
    total_ms: float = 0.0
    low_confidence: bool = False   # não deu para entender (janela desconhecida e sem texto)

    @property
    def activity_key(self) -> str:
        """Identifica a atividade para saber se 'continua a mesma' (ignora mudanças de título dentro do mesmo app)."""
        return f"{self.category}|{norm(self.app_label) or norm(self.process)}"

    def short(self) -> str:
        """Resumo SEM texto da tela — seguro para log."""
        return (f"[{self.category}/{self.app_label or self.process or '?'}] change={self.change:.1f}% "
                f"motion={self.motion:.0f}% ocr={'sim' if self.ocr_text else 'não'}({self.ocr_backend or '-'}) "
                f"eventos={self.events or '-'} {self.total_ms:.0f}ms")


# ======================================================================
# PRIVACIDADE
# ======================================================================
class PrivacyGuard:
    """Decide ANTES de capturar se a janela é proibida e, DEPOIS do OCR, se o texto é sensível."""

    SENSITIVE_TEXT = (
        "cvv", "codigo de seguranca", "senha:", "password:", "sua senha", "digite a senha", "chave pix", "token de acesso",
        "api_key", "apikey", "secret_key", "client_secret", "private key", "begin rsa", "nvapi-", "numero do cartao",
        "validade do cartao", "saldo disponivel", "extrato", "agencia e conta", "seed phrase", "frase de recuperacao",
    )
    _CPF = re.compile(r"\b\d{3}\.\d{3}\.\d{3}-\d{2}\b")
    _CARD = re.compile(r"\b(?:\d[ -]?){13,19}\b")
    _TOKENS = re.compile(r"\b(?:sk-[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{30,}|xox[abp]-[A-Za-z0-9-]{20,})\b")

    def __init__(self, config):
        self.config = config

    def _apps(self) -> List[str]:
        return [str(a).lower().strip() for a in (getattr(self.config, "obs_ignored_apps", []) or []) if str(a).strip()]

    def _titles(self) -> List[str]:
        return [norm(str(t)) for t in (getattr(self.config, "obs_ignored_titles", []) or []) if str(t).strip()]

    def check_window(self, process: str, title: str) -> Optional[str]:
        """Motivo (texto) se a janela NÃO pode ser observada; None se pode."""
        proc = (process or "").lower().strip()
        if proc and proc in self._apps():
            return f"app na lista de privacidade ({proc})"
        t = norm(title)
        for fragment in self._titles():
            # palavras curtas exigem fronteira de palavra (evita "pix" dentro de "pixel art")
            if re.search(r"(?<![a-z0-9])" + re.escape(fragment.strip()) + r"(?![a-z0-9])", t):
                return f"título de janela sensível (“{fragment.strip()}”)"
        return None

    def text_is_sensitive(self, text: str) -> bool:
        if not text:
            return False
        low = norm(text)
        if any(k in low for k in self.SENSITIVE_TEXT):
            return True
        if self._CPF.search(text) or self._TOKENS.search(text):
            return True
        for m in self._CARD.finditer(text):
            digits = re.sub(r"\D", "", m.group(0))
            if 13 <= len(digits) <= 19 and _luhn_ok(digits):
                return True
        return False


# ======================================================================
# OCR (opcional): RapidOCR (só pip) -> Tesseract
# ======================================================================
class OcrEngine:
    """Lê texto da imagem. Escolhe sozinho o motor instalado; sem nenhum, a observadora segue só com janela + imagem."""

    INSTALL_HINT = ("OCR indisponível. Para ler o texto da tela instale UM destes: "
                    "`pip install rapidocr-onnxruntime` (recomendado no Windows, só pip) ou o Tesseract "
                    "(https://github.com/UB-Mannheim/tesseract/wiki) + `pip install pytesseract`.")

    WINDOWS_TESSERACT_PATHS = (
        r"C:\Program Files\Tesseract-OCR\tesseract.exe",
        r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Programs\Tesseract-OCR\tesseract.exe"),
    )

    def __init__(self, config):
        self.config = config
        self._rapid = None
        self._rapid_failures = 0
        self._tess = None            # módulo pytesseract
        self._tess_lang = "eng"
        self._probed = False
        self.backend = ""            # "rapidocr" | "tesseract" | ""
        self.detail = ""
        self.error = ""

    # ---- descoberta preguiçosa (só quando a observadora pede OCR pela primeira vez) ----
    def probe(self) -> bool:
        if self._probed:
            return bool(self.backend)
        self._probed = True
        if np is None:
            self.error = "NumPy não instalado: " + self.INSTALL_HINT
            return False
        try:
            from rapidocr_onnxruntime import RapidOCR  # type: ignore
            self._rapid = RapidOCR()
            self.backend, self.detail = "rapidocr", "RapidOCR (onnxruntime, CPU)"
            return True
        except Exception as e:
            logger.debug(f"OCR: RapidOCR indisponível ({type(e).__name__}: {e}).")
            self._rapid = None
        try:
            import pytesseract  # type: ignore
            cmd = shutil.which("tesseract")
            if not cmd and sys.platform == "win32":
                cmd = next((p for p in self.WINDOWS_TESSERACT_PATHS if os.path.exists(p)), None)
            if cmd:
                pytesseract.pytesseract.tesseract_cmd = cmd
            version = pytesseract.get_tesseract_version()
            langs = set(pytesseract.get_languages(config=""))
            wanted = [l for l in str(getattr(self.config, "obs_ocr_langs", "por+eng")).split("+") if l]
            usable = [l for l in wanted if l in langs]
            self._tess_lang = "+".join(usable) if usable else ("eng" if "eng" in langs else (sorted(langs)[0] if langs else "eng"))
            self._tess = pytesseract
            self.backend, self.detail = "tesseract", f"Tesseract {version} ({self._tess_lang})"
            return True
        except Exception as e:
            self.error = f"{self.INSTALL_HINT} (detalhe: {type(e).__name__}: {e})"
            logger.info(f"OCR: nenhum motor disponível — {self.error}")
            return False

    @property
    def available(self) -> bool:
        return self.probe()

    def unload(self):
        """Libera o motor da memória (chamado ao desligar a observadora)."""
        self._rapid = None
        self._tess = None
        self._probed = False
        self.backend = ""

    # ---- leitura ----
    def _prepare(self, img: "Image.Image") -> "Image.Image":
        max_w = max(640, int(getattr(self.config, "obs_ocr_max_width", 1600)))
        work = img.convert("L")
        if work.width > max_w:
            work = work.resize((max_w, max(1, int(work.height * max_w / work.width))), Image.Resampling.LANCZOS)
        arr = np.asarray(work)
        if float(arr.mean()) < 110:                 # fundo escuro (tema dark): o OCR lê melhor texto escuro em fundo claro
            work = Image.fromarray(255 - arr)
        lo, hi = np.percentile(np.asarray(work), (2, 98))
        if hi - lo > 10:
            work = Image.fromarray(np.clip((np.asarray(work).astype("float32") - lo) * (255.0 / (hi - lo)), 0, 255).astype("uint8"))
        return work

    def read(self, img: "Image.Image", timeout: float = 15.0) -> Tuple[str, str]:
        """(texto, motor). Nunca levanta: falha vira ('', '')."""
        if not self.probe():
            return "", ""
        try:
            work = self._prepare(img)
        except Exception as e:
            logger.warning(f"OCR: falha ao preparar a imagem: {e}")
            return "", ""
        if self._rapid is not None and self._rapid_failures < 3:
            try:
                result, _ = self._rapid(np.asarray(work.convert("RGB")))
                text = "\n".join(str(r[1]) for r in (result or []))
                return text.strip(), "rapidocr"
            except Exception as e:
                self._rapid_failures += 1
                logger.warning(f"OCR: RapidOCR falhou ({self._rapid_failures}/3): {e}")
                if self._tess is None:
                    return "", ""
        if self._tess is not None:
            try:
                text = self._tess.image_to_string(work, lang=self._tess_lang, config="--psm 11", timeout=timeout)
                return (text or "").strip(), "tesseract"
            except Exception as e:
                logger.warning(f"OCR: Tesseract falhou ({type(e).__name__}): {e}")
        return "", ""


# ======================================================================
# DIFERENÇA ENTRE QUADROS
# ======================================================================
@dataclass
class FrameStats:
    change: float = 0.0
    motion: float = 0.0
    text_density: float = 0.0
    brightness: float = 0.0
    first: bool = False
    ahash: str = ""


class FrameAnalyzer:
    """Compara a tela atual com a anterior em miniatura (192x108, escala de cinza). Guarda só UMA miniatura."""

    SMALL = (192, 108)

    def __init__(self):
        self._prev = None

    def reset(self):
        self._prev = None

    def analyze(self, img: "Image.Image") -> FrameStats:
        small = np.asarray(img.convert("L").resize(self.SMALL, Image.Resampling.BILINEAR), dtype=np.int16)
        stats = FrameStats(brightness=float(small.mean()))
        gx = np.abs(np.diff(small, axis=1)).mean()
        gy = np.abs(np.diff(small, axis=0)).mean()
        stats.text_density = float(min(100.0, (gx + gy) * 2.0))
        tiny = np.asarray(img.convert("L").resize((8, 8), Image.Resampling.BILINEAR), dtype=np.int16)
        bits = (tiny > tiny.mean()).flatten()
        stats.ahash = hashlib.sha1(bits.tobytes()).hexdigest()[:12]
        if self._prev is None:
            stats.first = True
        else:
            diff = np.abs(small - self._prev)
            stats.change = float(diff.mean() / 255.0 * 100.0)
            stats.motion = float((diff > 24).mean() * 100.0)
        self._prev = small
        return stats


# ======================================================================
# CLASSIFICAÇÃO DA ATIVIDADE
# ======================================================================
PROCESS_MAP: Dict[str, Tuple[str, str]] = {
    # programação
    "code.exe": ("coding", "Visual Studio Code"), "code - insiders.exe": ("coding", "VS Code Insiders"),
    "cursor.exe": ("coding", "Cursor"), "pycharm64.exe": ("coding", "PyCharm"), "idea64.exe": ("coding", "IntelliJ"),
    "devenv.exe": ("coding", "Visual Studio"), "godot.exe": ("coding", "Godot"), "sublime_text.exe": ("coding", "Sublime Text"),
    "notepad++.exe": ("coding", "Notepad++"), "studio64.exe": ("coding", "Android Studio"), "renpy.exe": ("coding", "Ren'Py"),
    "unity.exe": ("coding", "Unity"), "arduino ide.exe": ("coding", "Arduino IDE"),
    # terminal
    "windowsterminal.exe": ("terminal", "Terminal"), "powershell.exe": ("terminal", "PowerShell"), "pwsh.exe": ("terminal", "PowerShell"),
    "cmd.exe": ("terminal", "Prompt de comando"), "conhost.exe": ("terminal", "Terminal"), "python.exe": ("terminal", "Python (terminal)"),
    "wsl.exe": ("terminal", "WSL"), "mintty.exe": ("terminal", "Git Bash"),
    # navegadores
    "chrome.exe": ("browsing", "Chrome"), "msedge.exe": ("browsing", "Edge"), "firefox.exe": ("browsing", "Firefox"),
    "brave.exe": ("browsing", "Brave"), "opera.exe": ("browsing", "Opera"), "vivaldi.exe": ("browsing", "Vivaldi"),
    # conversa
    "discord.exe": ("chat", "Discord"), "telegram.exe": ("chat", "Telegram"), "whatsapp.exe": ("chat", "WhatsApp"),
    "slack.exe": ("chat", "Slack"), "teams.exe": ("chat", "Teams"), "ms-teams.exe": ("chat", "Teams"),
    "zoom.exe": ("chat", "Zoom"), "skype.exe": ("chat", "Skype"),
    # mídia
    "spotify.exe": ("music", "Spotify"), "vlc.exe": ("video", "VLC"), "mpv.exe": ("video", "mpv"),
    "potplayermini64.exe": ("video", "PotPlayer"), "wmplayer.exe": ("video", "Media Player"),
    # documentos
    "winword.exe": ("documents", "Word"), "excel.exe": ("documents", "Excel"), "powerpnt.exe": ("documents", "PowerPoint"),
    "onenote.exe": ("documents", "OneNote"), "acrord32.exe": ("documents", "leitor de PDF"), "notepad.exe": ("documents", "Bloco de Notas"),
    "obsidian.exe": ("documents", "Obsidian"), "notion.exe": ("documents", "Notion"), "soffice.bin": ("documents", "LibreOffice"),
    # criação
    "photoshop.exe": ("creative", "Photoshop"), "blender.exe": ("creative", "Blender"), "krita.exe": ("creative", "Krita"),
    "gimp-2.10.exe": ("creative", "GIMP"), "aseprite.exe": ("creative", "Aseprite"), "obs64.exe": ("creative", "OBS"),
    "premiere pro.exe": ("creative", "Premiere"), "afterfx.exe": ("creative", "After Effects"), "davinciresolve.exe": ("creative", "DaVinci Resolve"),
    "comfyui.exe": ("creative", "ComfyUI"), "lmstudio.exe": ("ai_chat", "LM Studio"),
    # lançadores e sistema
    "steam.exe": ("launcher", "Steam"), "epicgameslauncher.exe": ("launcher", "Epic Games"), "battle.net.exe": ("launcher", "Battle.net"),
    "riotclientservices.exe": ("launcher", "Riot Client"), "explorer.exe": ("system", "Explorador de Arquivos"),
    "taskmgr.exe": ("system", "Gerenciador de Tarefas"), "systemsettings.exe": ("system", "Configurações"),
}

# (trecho do título SEM acento, categoria, nome legível)
SITE_HINTS: List[Tuple[str, str, str]] = [
    ("pesquisa google", "searching", "Google"), ("google search", "searching", "Google"),
    ("youtube", "video", "YouTube"), ("netflix", "video", "Netflix"), ("twitch", "video", "Twitch"),
    ("prime video", "video", "Prime Video"), ("disney+", "video", "Disney+"), ("crunchyroll", "video", "Crunchyroll"),
    ("globoplay", "video", "Globoplay"), ("max |", "video", "Max"),
    ("github", "coding", "GitHub"), ("stack overflow", "coding", "Stack Overflow"), ("stackoverflow", "coding", "Stack Overflow"),
    ("mdn web docs", "coding", "MDN"), ("docs.python", "coding", "documentação do Python"), ("pygame", "coding", "documentação do Pygame"),
    ("chatgpt", "ai_chat", "ChatGPT"), ("claude", "ai_chat", "Claude"), ("gemini", "ai_chat", "Gemini"),
    ("perplexity", "ai_chat", "Perplexity"), ("copilot", "ai_chat", "Copilot"),
    ("whatsapp", "chat", "WhatsApp Web"), ("gmail", "chat", "e-mail"), ("outlook", "chat", "e-mail"), ("discord", "chat", "Discord"),
    ("instagram", "social", "Instagram"), ("facebook", "social", "Facebook"), ("twitter", "social", "X/Twitter"),
    ("reddit", "social", "Reddit"), ("tiktok", "social", "TikTok"), ("linkedin", "social", "LinkedIn"),
    ("mercado livre", "shopping", "Mercado Livre"), ("amazon", "shopping", "Amazon"), ("shopee", "shopping", "Shopee"),
    ("aliexpress", "shopping", "AliExpress"), ("kabum", "shopping", "KaBuM"), ("magazine luiza", "shopping", "Magalu"),
    ("wikipedia", "reading", "Wikipédia"), ("google docs", "documents", "Google Docs"), ("google planilhas", "documents", "Google Planilhas"),
    ("google sheets", "documents", "Google Sheets"),
    ("spotify", "music", "Spotify"),
]

BROWSER_SUFFIXES = ("google chrome", "microsoft edge", "mozilla firefox", "brave", "opera", "vivaldi", "chromium")
FILE_RE = re.compile(r"([\w.\-]+\.(?:py|js|ts|tsx|jsx|gd|cs|cpp|c|h|hpp|java|kt|rs|go|html|css|json|md|txt|tscn|tres|bat|sh|yml|yaml|toml|ini|sql|lua|rb|php))", re.I)

ERROR_PATTERNS = (
    "traceback (most recent", "syntaxerror", "nameerror", "typeerror", "attributeerror", "indentationerror", "valueerror",
    "importerror", "modulenotfounderror", "keyerror", "runtimeerror", "exception", "segmentation fault", "stack trace",
    "error:", "erro:", "falha ao", "failed to", "cannot find", "nao foi possivel", "undefined", "fatal error", "compilation failed",
)
# eventos de jogo: só valem com categoria "game"
GAME_WIN = ("victory", "vitoria", "you win", "voce venceu", "mission accomplished", "level complete", "stage clear", "winner")
GAME_LOSE = ("game over", "you died", "voce morreu", "defeat", "derrota", "wasted", "mission failed", "you lose", "eliminated", "eliminado")


@dataclass
class Classification:
    category: str = "unknown"
    app_label: str = ""
    activity: str = ""
    file_hint: str = ""
    known: bool = False


class ActivityClassifier:
    """Regras simples e transparentes. Para ensinar um app novo, acrescente-o em PROCESS_MAP / SITE_HINTS."""

    def __init__(self, game_detector: Optional[Callable[[str, str], Optional[str]]] = None):
        self.game_detector = game_detector

    @staticmethod
    def _site_from_title(title_norm: str) -> Optional[Tuple[str, str]]:
        for fragment, cat, label in SITE_HINTS:
            if fragment in title_norm:
                return cat, label
        return None

    def classify(self, process: str, title: str, ocr_norm: str = "") -> Classification:
        proc = (process or "").lower().strip()
        t = norm(title)
        out = Classification()

        # 1) jogo (usa o MESMO detector do Modo Gamer, para os dois concordarem)
        game = None
        if self.game_detector:
            try:
                game = self.game_detector(title or "", process or "")
            except Exception as e:
                logger.debug(f"Classificador: detector de jogo falhou: {e}")
        if game:
            return Classification("game", game, f"jogando {game}", known=True)

        base = PROCESS_MAP.get(proc)
        if base:
            out.category, out.app_label, out.known = base[0], base[1], True

        # 2) navegador: o SITE manda mais que o programa
        if out.category == "browsing" or (not base and any(b in t for b in BROWSER_SUFFIXES)):
            out.category = out.category if out.category != "unknown" else "browsing"
            site = self._site_from_title(t)
            if site:
                out.category, out.app_label, out.known = site[0], site[1], True
            elif not out.app_label:
                out.app_label = "navegador"
            out.known = True
        elif not base:
            site = self._site_from_title(t)
            if site and site[0] in ("video", "music"):
                out.category, out.app_label, out.known = site[0], site[1], True

        # 3) arquivo aberto (programação/terminal/documentos)
        if out.category in ("coding", "documents", "terminal"):
            m = FILE_RE.search(title or "")
            if m:
                out.file_hint = m.group(1)

        # 4) sem processo conhecido: tenta pelo que está escrito na tela
        if not out.known and ocr_norm:
            if any(k in ocr_norm for k in ("traceback", "def ", "import ", "function ", "class ", "#include", "console.log", "extends ")):
                out.category, out.app_label, out.known = "coding", "um editor de código", True
            elif any(k in ocr_norm for k in ("c:\\", "ps c:", "$ ", "pip install", "python ")):
                out.category, out.app_label, out.known = "terminal", "um terminal", True

        out.activity = self._phrase(out)
        return out

    @staticmethod
    def _phrase(c: Classification) -> str:
        app = c.app_label or "um programa"
        return {
            "coding": f"programando no {app}" + (f" (arquivo {c.file_hint})" if c.file_hint else ""),
            "terminal": f"mexendo no terminal ({app})",
            "browsing": f"navegando no {app}",
            "searching": "pesquisando na internet",
            "video": f"assistindo a vídeo ({app})",
            "music": f"ouvindo música ({app})",
            "chat": f"conversando ({app})",
            "social": f"nas redes sociais ({app})",
            "documents": f"mexendo em documentos ({app})" + (f", arquivo {c.file_hint}" if c.file_hint else ""),
            "creative": f"criando algo no {app}",
            "ai_chat": f"conversando com uma IA ({app})",
            "shopping": f"olhando compras ({app})",
            "reading": f"lendo ({app})",
            "launcher": f"na biblioteca de jogos ({app})",
            "system": f"mexendo no sistema ({app})",
            "game": f"jogando {app}",
        }.get(c.category, "fazendo algo que não consegui identificar")


def detect_events(category: str, ocr_norm: str) -> Tuple[List[str], str]:
    """Eventos visíveis no texto da tela. Devolve (eventos, assinatura curta do evento)."""
    if not ocr_norm:
        return [], ""
    events: List[str] = []
    sig = ""
    if category in ("coding", "terminal", "unknown", "documents"):
        if re.search(r"ran \d+ tests?.{0,80}\bfailed\b|\bfailed\b.{0,20}\(failures=|tests? failed|\d+ failed", ocr_norm, re.S):
            events.append("testes_falharam")
        elif re.search(r"ran \d+ tests?.{0,120}\bok\b|all tests passed|\d+ passed", ocr_norm, re.S):
            events.append("testes_ok")
        for pat in ERROR_PATTERNS:
            idx = ocr_norm.find(pat)
            if idx != -1:
                if "erro_visivel" not in events and "testes_falharam" not in events:
                    events.append("erro_visivel")
                sig = hashlib.sha1(ocr_norm[idx:idx + 60].encode()).hexdigest()[:10]
                break
    if category == "game":
        if any(k in ocr_norm for k in GAME_LOSE):
            events.append("jogo_derrota")
            sig = "lose"
        elif any(k in ocr_norm for k in GAME_WIN):
            events.append("jogo_vitoria")
            sig = "win"
    return events, sig


# ======================================================================
# ORQUESTRADOR
# ======================================================================
class LocalVision:
    """Uma instância por observadora. `read()` faz o pipeline completo e devolve um ScreenReading."""

    def __init__(self, config, screen_engine, window_provider, game_detector=None, rect_provider=None):
        self.config = config
        self.screen = screen_engine
        self.window_provider = window_provider      # () -> (processo, título)
        self.rect_provider = rect_provider          # () -> (l,t,r,b) | None
        self.privacy = PrivacyGuard(config)
        self.ocr = OcrEngine(config)
        self.frames = FrameAnalyzer()
        self.classifier = ActivityClassifier(game_detector)
        self._last_ocr_ts = 0.0
        self._last_ocr_text = ""
        self._last_ocr_backend = ""

    def reset(self):
        """Esquece quadro anterior e texto lido (usado ao pausar/desligar)."""
        self.frames.reset()
        self._last_ocr_ts = 0.0
        self._last_ocr_text = ""
        self._last_ocr_backend = ""

    def release(self):
        self.reset()
        self.ocr.unload()

    # ------------------------------------------------------------------
    def inspect_window(self) -> Tuple[str, str, Optional[str]]:
        """(processo, título, motivo_para_não_observar). NÃO captura nada."""
        try:
            process, title = self.window_provider()
        except Exception as e:
            logger.debug(f"Leitura da janela ativa falhou: {e}")
            process, title = "", ""
        return process or "", title or "", self.privacy.check_window(process, title)

    def read(self, force_ocr: bool = False, now: Optional[float] = None, image=None) -> ScreenReading:
        """Pipeline completo. `image` permite injetar uma imagem (testes); sem ela captura a tela de verdade."""
        t0 = time.time()
        now = now if now is not None else t0
        process, title, blocked = self.inspect_window()
        reading = ScreenReading(ts=now, process=process, title=title)
        if blocked:
            reading.skipped_reason = blocked
            reading.sensitive = True
            self.frames.reset()                       # não deixa quadro antigo para comparar depois
            self._last_ocr_text = ""
            return reading

        # ---- captura ----
        c0 = time.time()
        if image is None:
            rect = None
            try:
                rect = self.rect_provider() if self.rect_provider else None
            except Exception:
                rect = None
            image = self.screen.capture_raw(str(getattr(self.config, "obs_monitor", "active")), rect)
        reading.capture_ms = (time.time() - c0) * 1000.0

        # ---- quadro ----
        stats = self.frames.analyze(image)
        sens = max(0.1, float(getattr(self.config, "obs_change_sensitivity", 1.0)))
        reading.change = min(100.0, stats.change * sens)
        reading.motion = min(100.0, stats.motion * sens)
        reading.text_density = stats.text_density
        reading.first_frame = stats.first

        # ---- OCR (caro: com limite de frequência) ----
        threshold = float(getattr(self.config, "obs_change_threshold", 4.0))
        want_ocr = bool(getattr(self.config, "obs_ocr", True))
        due = (now - self._last_ocr_ts) >= float(getattr(self.config, "obs_ocr_every", 20.0))
        big_change = reading.change >= threshold * 2.5
        if want_ocr and (force_ocr or stats.first or due or big_change) and self.ocr.available:
            o0 = time.time()
            text, backend = self.ocr.read(image)
            reading.ocr_ms = (time.time() - o0) * 1000.0
            self._last_ocr_ts = now
            if self.privacy.text_is_sensitive(text):
                reading.sensitive = True
                reading.skipped_reason = "texto sensível na tela (descartado, não analisado)"
                self._last_ocr_text, self._last_ocr_backend = "", backend
                text = ""
            else:
                self._last_ocr_text, self._last_ocr_backend = text, backend
                reading.ocr_fresh = bool(text)
        reading.ocr_text = self._last_ocr_text[:1500]
        reading.ocr_backend = self._last_ocr_backend
        ocr_norm = norm(reading.ocr_text)

        # ---- classificação e eventos ----
        c = self.classifier.classify(process, title, ocr_norm)
        reading.category, reading.app_label, reading.activity, reading.file_hint = c.category, c.app_label, c.activity, c.file_hint
        if reading.ocr_fresh:                         # evento só nasce de OCR NOVO (senão repetiria a cada captura)
            reading.events, reading.event_sig = detect_events(reading.category, ocr_norm)
        if not c.known and not reading.ocr_text:
            reading.low_confidence = True
        reading.total_ms = (time.time() - t0) * 1000.0
        return reading
