"""
Núcleo cognitivo da Alicia — orquestra conversa, memória, visão,
modo gamer, TTS, microfone e a interface.
"""

from __future__ import annotations

import json
import os
import random
import re
import sys
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from PyQt6.QtWidgets import QApplication

from .config import SystemConfig
from .conversation import ConversationMixin
from .database import MemoryDatabase, DatabaseUnavailable
from .nvidia_client import NvidiaClient, BrainError, DEFAULT_BASE_URL
from .env import load_env_file, set_env_value
from .vision import VisionBackend
from .hardware import HardwareMonitor
from .screen import ScreenAnalyzerEngine
from .ollama_client import OllamaClient
from .tts import TextToSpeechEngine
from .prompts import PromptEngine
from .voice_input import VoiceInputEngine
from .call_context import CallContext
from .call_listener import CallListener
from .voice_id import VoiceID
from .enums import AliciaExpression
from .models import HardwareSnapshot
from .logging_setup import logger
from .ui.desktop_pet import AliciaDesktopPetUI
from .audio_output import stop_playback
from .deps import psutil, HAS_PSUTIL, Image


class AliciaAssistantCore(ConversationMixin):

    """
    Núcleo cognitivo da Alicia.

    Responsabilidades:

    - Conversação
    - Orquestração da conversa (ver conversation.py) e do cérebro (brain.py)
    - Visão computacional
    - Modo gamer
    - Modo descanso
    - TTS
    - Microfone
    - Controle da interface
    """

    # ==========================================================================
    # CONFIGURAÇÕES COGNITIVAS
    # ==========================================================================

    MAX_VISION_MEMORY = 6


    # Palavras que normalmente indicam mudança explícita de assunto.

    # Referências que normalmente significam continuidade.

    # ==========================================================================
    # INICIALIZAÇÃO
    # ==========================================================================

    def __init__(self, config_path: str = "config.json"):
        load_env_file(".env")
        self.config = SystemConfig(config_path)
        self.db = MemoryDatabase(self.config.db_path)
        # Ollama: SOMENTE visão / modo gamer. A conversa usa a API NVIDIA.
        self.client = OllamaClient(self.config.ollama_host)
        self.poker_active = False           # mesa de pôquer aberta: pausa comentários espontâneos, tela e modo gamer
        self.poker_chat_handler = None      # função(texto) -> (fala, expressão): conversa exclusiva da mesa (registrada pela janela)
        self.ui = None
        self.vision_available = False       # pedidos diretos ("Alicia, olha")
        self.vision_spontaneous = False     # comentários espontâneos / modo gamer (só com Ollama local)
        self._last_vision_recheck = 0.0
        self.nvidia = NvidiaClient(
            api_key=os.environ.get('NVIDIA_API_KEY', ''),
            model=os.environ.get('NVIDIA_MODEL', ''),
            base_url=os.environ.get('NVIDIA_BASE_URL', '') or DEFAULT_BASE_URL,
            timeout=float(os.environ.get('NVIDIA_TIMEOUT', self.config.brain_timeout)),
            max_retries=int(os.environ.get('NVIDIA_MAX_RETRIES', self.config.brain_max_retries)),
        )

        self.screen_engine = ScreenAnalyzerEngine(
            self.config.max_image_dimension,
            self.config.image_quality
        )
        self.tts = TextToSpeechEngine(
            self.config.enable_audio,
            self.config.voice_speed,
            getattr(self.config, "voice_name", "pt-BR-FranciscaNeural") or "pt-BR-FranciscaNeural",
            expressive=bool(getattr(self.config, "voice_expressive", True)),
        )
        self.voice_input = VoiceInputEngine(self)
        self.call_context = CallContext(
            max_age=float(getattr(self.config, "call_context_seconds", 600.0)),
            max_items=int(getattr(self.config, "call_context_max_items", 60)),
        )
        self.call_listener = CallListener(self)
        # Quem está falando (locutor). Local; só atua depois de alguém ser cadastrado com consentimento.
        self.voice_id = VoiceID(self.config, owner_name_getter=lambda: self.db.get_user_name() or "")

        self.is_running = False
        self.screen_watch_enabled = True
        self.last_screen_img: Optional[Image.Image] = None

        self.modo_descanso = self.config.modo_descanso

        # ------------------------- Gamer state -------------------------
        self.modo_gamer = False
        self.gamer_manual_override = False
        self.gamer_current_game: Optional[str] = None
        self.gamer_current_profile: Dict[str, Any] = {}
        self.gamer_recent_analysis: List[str] = []
        self.gamer_recent_events: List[str] = []
        self.gamer_last_screen_hash: Optional[str] = None
        self.gamer_last_analysis_time = 0.0
        self.gamer_last_game_change_time = 0.0
        self.gamer_last_detect_check_time = 0.0
        self.gamer_last_known_process: Optional[str] = None
        self.gamer_last_process_seen_time = 0.0
        self.gamer_last_game_window_seen_time = 0.0
        self.gamer_last_foreground_game_title = ""
        self.gamer_last_window_title = ""
        self.gamer_analysis_count = 0
        self.gamer_comment_cooldown = 18.0
        self.gamer_event_cooldown = 5.0
        self.gamer_profiles_path = "gamer_profiles.json"
        self.gamer_idle_counter = 0
        self.gamer_analysis_lock = threading.Lock()
        self.gamer_analysis_running = False
        self.gamer_profile_lock = threading.Lock()
        self.gamer_profile_loading: set[str] = set()

        self.last_screen_check_time = 0.0
        self.last_comment_time = 0.0
        self.last_user_activity_time = time.time()
        self.current_expression = AliciaExpression.SERIO   # rosto neutro: é o que respira e pisca

        # ------------------------- Memória / visão -----------------------
        self.recent_vision_comments: List[str] = []
        self.last_visual_context = ""
        self._init_conversation()
        self.brain.on_model_change = self._persist_model
        self.vision = VisionBackend(self.client, self.config, self.nvidia)
        self.vision.on_model_change = lambda m: set_env_value(".env", "NVIDIA_VISION_MODEL", m)

        logger.info("🧠 Cérebro cognitivo da Alicia inicializado.")


    # ==========================================================================
    # UTILITÁRIOS
    # ==========================================================================

    @staticmethod
    def _normalize_text(text: str) -> str:

        if not text:
            return ""

        text = str(text)

        text = re.sub(
            r"\s+",
            " ",
            text
        ).strip()

        return text

    @staticmethod
    def _fingerprint(text: str) -> str:

        if not text:
            return ""

        normalized = re.sub(
            r"[^a-zA-ZÀ-ÿ0-9\s]",
            "",
            text.lower()
        )

        normalized = re.sub(
            r"\s+",
            " ",
            normalized
        ).strip()

        return normalized

    # ==========================================================================
    # LIMITADOR DE RESPOSTA
    # ==========================================================================

    @staticmethod
    def _limit_response_length(
        text: str,
        max_sentences: int = 3,
        max_chars: int = 320
    ) -> str:

        if not text:
            return ""

        text = str(text)

        text = re.sub(
            r"\s+",
            " ",
            text
        ).strip()

        # ----------------------------------------------------------------------
        # Remove "Alicia:"
        # ----------------------------------------------------------------------

        text = re.sub(
            r"^\s*(?:\*\*)?Alicia(?:\*\*)?\s*:\s*",
            "",
            text,
            flags=re.IGNORECASE
        )

        # ----------------------------------------------------------------------
        # Remove listas
        # ----------------------------------------------------------------------

        text = re.sub(
            r"^\s*(?:[-*•]|\d+[.)])\s*",
            "",
            text
        )

        # ----------------------------------------------------------------------
        # Divide frases
        # ----------------------------------------------------------------------

        sentences = re.split(
            r"(?<=[.!?])\s+",
            text
        )

        sentences = [
            s.strip()
            for s in sentences
            if s.strip()
        ]

        text = " ".join(
            sentences[:max_sentences]
        )

        # ----------------------------------------------------------------------
        # Limite de caracteres
        # ----------------------------------------------------------------------

        if len(text) > max_chars:

            shortened = (
                text[:max_chars]
                .rsplit(" ", 1)[0]
                .rstrip(" ,;:-")
            )

            text = shortened + "..."

        return text.strip()

    # ==========================================================================
    # MEMÓRIA VISUAL
    # ==========================================================================

    def _remember_vision_comment(
        self,
        comment: str
    ):

        if not comment:
            return

        comment = self._normalize_text(
            comment
        )

        if not comment:
            return

        # Evita duplicação imediata.
        fingerprint = self._fingerprint(
            comment
        )

        for previous in self.recent_vision_comments:

            if (
                self._fingerprint(previous)
                == fingerprint
            ):
                return

        self.recent_vision_comments.append(
            comment
        )

        self.recent_vision_comments = (
            self.recent_vision_comments[
                -self.MAX_VISION_MEMORY:
            ]
        )

    # ==========================================================================
    # MEMÓRIA CONVERSACIONAL
    # ==========================================================================


    # ==========================================================================
    # HISTÓRICO PARA O MODELO
    # ==========================================================================


    # ==========================================================================
    # DETECTOR DE MUDANÇA DE ASSUNTO
    # ==========================================================================


    # ==========================================================================
    # SIMILARIDADE LEXICAL SIMPLES
    # ==========================================================================


    # ==========================================================================
    # ATUALIZAÇÃO DO ASSUNTO
    # ==========================================================================


    # ==========================================================================
    # PROMPT COGNITIVO
    # ==========================================================================


    # ==========================================================================
    # DETECTOR DE RESPOSTA ERRADA
    # ==========================================================================


    # ==========================================================================
    # REPARO COGNITIVO
    # ==========================================================================


    # ==========================================================================
    # COMANDOS SOCIAIS
    # ==========================================================================

    def _detect_alicia_greeting_command(
        self,
        user_message: str
    ) -> Tuple[bool, Optional[str], Optional[str]]:

        if not user_message:
            return False, None, None

        text = self._normalize_text(
            user_message
        )

        lower = text.lower()

        if not re.search(
            r"\balicia\b",
            lower
        ):
            return False, None, None

        command = re.sub(
            r"^\s*alicia\b[\s,:-]*",
            "",
            text,
            flags=re.IGNORECASE
        ).strip()

        command_lower = command.lower()

        # ----------------------------------------------------------------------
        # APRESENTAÇÃO
        # ----------------------------------------------------------------------

        presentation_patterns = [
            r"^apresente[- ]?se$",
            r"^se apresente$",
            r"^apresenta[- ]?se$",
            r"^se apresenta$",
            r"^pode se apresentar$",
            r"^fale sobre você$",
            r"^fala sobre você$",
            r"^diga quem você é$",
            r"^quem é você$",
            r"^fale quem você é$"
        ]

        for pattern in presentation_patterns:

            if re.search(
                pattern,
                command_lower,
                re.IGNORECASE
            ):
                return True, "apresentacao", None

        # ----------------------------------------------------------------------
        # CUMPRIMENTOS
        # ----------------------------------------------------------------------

        greeting_patterns = [

            r"^(?:fale|fala|diga|diz)\s+(?:um\s+)?(?:oi|olá|ola)\s+(?:para|pra)\s+(.+)$",

            r"^(?:dê|de)\s+(?:um\s+)?(?:oi|olá|ola)\s+(?:para|pra)\s+(.+)$",

            r"^(?:cumprimente|cumprimenta)\s+(.+)$",

            r"^(?:mande|manda)\s+(?:um\s+)?(?:oi|olá|ola)\s+(?:para|pra)\s+(.+)$",

            r"^(?:oi|olá|ola)\s+(?:para|pra)\s+(.+)$"
        ]

        for pattern in greeting_patterns:

            match = re.search(
                pattern,
                command,
                re.IGNORECASE
            )

            if match:

                name = match.group(1).strip()

                name = re.sub(
                    r"[.!?,]+$",
                    "",
                    name
                ).strip()

                if name:

                    return (
                        True,
                        "cumprimento",
                        name
                    )

        return False, None, None

    # ==========================================================================
    # APRESENTAÇÃO
    # ==========================================================================

    def _alicia_apresentar(self) -> str:

        apresentacoes = [
            "[FELIZ] Oi! Eu sou a Alicia. Moro aqui no seu computador, tá? Prazer!",
            "[FELIZ] Eu sou a Alicia. Fico por aqui, escuto, observo... e de vez em quando dou meu pitaco.",
            "[SURPRESA] Ah, apresentação oficial? Tá bom. Alicia, muito prazer. Moro atrás desse vidro.",
            "[FELIZ] Meu nome é Alicia! Sou curiosa e meio enxerida, aviso logo.",
            "[PENSANDO] Eu sou a Alicia. Não sou exatamente gente, mas opinião eu tenho de sobra.",
        ]

        return random.choice(
            apresentacoes
        )

    # ==========================================================================
    # CUMPRIMENTO
    # ==========================================================================

    def _alicia_cumprimentar(
        self,
        nome: str
    ) -> str:

        if not nome:

            return (
                "[FELIZ] Oi! Prazer em te conhecer."
            )

        estilos = [
            "[FELIZ] Oi, {nome}! Prazer! Prometo tentar me comportar... sem garantias.",
            "[FELIZ] Oi, {nome}! Eu sou a Alicia. Pode ficar tranquilo, eu só julgo baixinho.",
            "[SURPRESA] Opa, {nome}! Então é você que veio parar aqui. Que legal!",
            "[FELIZ] Oi, {nome}! Seja bem-vindo. Espero que tenha trazido boas histórias.",
            "[PENSANDO] Oi, {nome}. Prazer! Vou fingir que não tô analisando você.",
            "[FELIZ] Prazer, {nome}! Agora você oficialmente conhece a Alicia.",
            "[SURPRESA] Oi, {nome}! Finalmente alguém novo por aqui, eu já tava falando com a parede.",
        ]

        return random.choice(
            estilos
        ).format(
            nome=nome
        )

    # ==========================================================================
    # COMANDOS SOCIAIS
    # ==========================================================================


    # ==========================================================================
    # COMANDO VISUAL
    # ==========================================================================

    _WAKE = r"(?:alicia|alicea|alissa|alisia|alicha|elisia|alicya)"
    _VISUAL_PATTERNS = [
        # "Alicia, olha / olhar / olhe / ver / veja / analisa / observa ..." (com ou sem "por favor")
        r"\b" + _WAKE + r"\s+(?:por favor\s+)?(?:olha|olhar|olhe|ver|veja|ve|analisa|analise|analisar|observa|observe|observar)\b",
        r"\b" + _WAKE + r"\s+(?:o que|oque)\s+(?:esta|ta|tem|voce esta)\s+(?:na tela|vendo)\b",
        r"\b" + _WAKE + r"\s+(?:o que|oque)\s+acha\s+(?:disso|da tela)\b",
        r"\b(?:olha|olhe|olhar|veja|ver)\s+(?:isso|aqui|a tela)\s+" + _WAKE + r"\b",
    ]

    def _is_direct_visual_command(self, user_message: str) -> bool:
        """Reconhece 'Alicia, olha/ver/olhar...' ignorando acentos e pontuação (o reconhecimento
        de voz costuma devolver 'Alícia')."""
        if not user_message:
            return False
        import unicodedata
        text = unicodedata.normalize("NFD", user_message.lower())
        text = "".join(c for c in text if unicodedata.category(c) != "Mn")
        text = re.sub(r"[^a-z0-9\s]+", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        return any(re.search(p, text) for p in self._VISUAL_PATTERNS)

    # ==========================================================================
    # ANÁLISE VISUAL DIRETA
    # ==========================================================================

    def analyze_screen_now(
        self,
        user_message: str
    ) -> Tuple[str, AliciaExpression, bool]:
        if not self.vision_available and not self._prepare_vision_now():
            return (
                "Meu módulo de visão não está disponível: nem o Ollama local nem um modelo de visão da NVIDIA responderam. Rode Diagnostico_Alicia.bat.",
                AliciaExpression.SERIO,
                False
            )
        try:

            logger.info(
                "👁️ Comando visual direto."
            )

            snapshot = (
                HardwareMonitor.capture_snapshot()
            )

            (
                b64_img,
                current_pil,
                p_hash
            ) = (
                self.screen_engine.capture_and_encode()
            )

            if (
                not b64_img
                or current_pil is None
            ):

                return (
                    "Não consegui enxergar a tela agora.", AliciaExpression.BRAVO, False
                )

            prompt = (
                PromptEngine
                .build_direct_screen_query_prompt(
                    user_message,
                    snapshot,
                    user_name=self.config.user_name,
                    personality=self.get_personality()
                )
            )

            raw_res = self.vision.generate(
                prompt, b64_img,
                options={
                    "temperature": 0.75,
                    "top_p": 0.92,
                    "num_predict": 100,
                    "repeat_penalty": 1.2,
                    "repeat_last_n": 192
                },
                timeout=90
            )

            if not raw_res:

                return (
                    "Eu olhei, mas meus olhos resolveram entrar em greve.", AliciaExpression.BRAVO, False
                )

            answer_text, expression = (
                self.parse_expression_tags(
                    raw_res
                )
            )

            answer_text = (
                self._limit_response_length(
                    answer_text
                )
            )

            if (
                not answer_text
                or answer_text.upper() == "VAZIO"
            ):

                return (
                    "", AliciaExpression.PENSANDO, False )

            self.last_visual_context = (
                answer_text
            )

            self._remember_vision_comment(
                answer_text
            )

            self.current_expression = expression

            try:

                self.db.log_vision_event(
                    p_hash,
                    answer_text,
                    expression.value,
                    snapshot.active_window_title
                )

            except Exception as e:

                logger.warning(
                    f"Falha ao salvar evento visual: {e}"
                )

            return (
                answer_text, expression, True )

        except Exception as e:

            logger.exception(
                "❌ Erro na análise visual."
            )

            return (
                "Minha visão tropeçou em alguma coisa. Tenta de novo.", AliciaExpression.BRAVO, False
            )

    # ==========================================================================
    # MICROFONE
    # ==========================================================================

    def _speak_async(
        self,
        text: str,
        expression: AliciaExpression
    ):
        if not text or not self.config.enable_audio:
            return

        threading.Thread(
            target=self.tts.speak,
            args=(text, expression),
            daemon=True,
            name="AliciaTTS"
        ).start()

    # ==========================================================================
    # MODO CALL (ouvir o áudio do PC numa call do Discord)
    # ==========================================================================

    def entrar_modo_call(self) -> str:
        ok, msg = self.call_listener.start()
        if ok:
            self.call_context.clear()
        return msg

    def sair_modo_call(self) -> str:
        if not self.call_listener.active:
            return "O modo call não estava ligado."
        self.call_listener.stop()
        return "Modo call desligado. Áudio virtual removido."

    def say_voice_reply(self, reply):
        """Fala (e mostra) uma resposta do cadastro de voz. `reply` = (texto, expressão). Não passa pela conversa nem grava no histórico."""
        text, expr = reply
        try:
            print(f"🤖 Alicia [{expr.upper()}]: {text}")
            self.tts.speak(text, AliciaExpression.from_str(expr))
        except Exception as e:
            logger.error(f"Erro ao falar a resposta do cadastro de voz: {e}")

    def responder_na_call(self, texto: str, quem: str = "Amigo", identity=None):
        """Alguém da call chamou a Alicia: responde (por voz, que sai também para a call)."""
        if self.modo_descanso or not self.is_running:
            return
        try:
            response = self.interact(f"({quem} diz na call): {texto}", source="call_friend",
                                     **({"speaker": identity} if identity is not None else {}))
            if not response:
                return
            clean, expr = self.parse_expression_tags(response)
            print(f"🤖 Alicia [{expr.value.upper()}]: {clean}")
            self.call_context.add("Alicia", clean)
            self.tts.speak(clean, expr)
        except Exception as e:
            logger.error(f"Erro ao responder na call: {type(e).__name__}: {e}")

    def resumir_call(self) -> str:
        ctx = self.call_context.format_for_prompt(6000)
        if not ctx:
            return "Ainda não ouvi nada nessa call."
        prompt = ("Resuma em 3 a 5 frases curtas, em português, o que foi conversado nesta call. "
                  "Diga quem falou o quê quando for importante. Só o resumo, sem introdução.\n\n" + ctx)
        try:
            return self.brain.complete_text(prompt, temperature=0.3, max_tokens=300).strip() or "Não consegui resumir agora."
        except Exception as e:
            logger.error(f"Resumo da call falhou: {e}")
            return "Não consegui resumir agora (erro no cérebro)."

    def mudar_microfone(
        self,
        index: Optional[int]
    ):

        try:

            self.config.mic_index = index

            self.config.save()

            logger.info(
                f"🎙️ Microfone alterado: {index}"
            )

            msg = (
                "Microfone alterado com sucesso!"
            )

            self._speak_async(
                msg,
                AliciaExpression.FELIZ
            )

            self.voice_input.stop_listening()

            time.sleep(0.3)

            self.voice_input.start_listening()

        except Exception as e:

            logger.exception(
                f"Erro ao mudar microfone: {e}"
            )

    # ==========================================================================
    # MODO GAMER (substitui o antigo "Modo Assistindo")
    # ==========================================================================
    # Detecta automaticamente quando você abre um jogo (pelo título da
    # janela ativa), monta/reaproveita um "perfil" do jogo gerado pela
    # própria IA (gênero, objetivo, mecânicas, erros comuns, oportu-
    # nidades de humor etc.) e usa esse perfil para comentar a partida
    # de tempos em tempos — podendo ser dica, brincadeira, piada ou
    # reação a algo que aconteceu na tela.
    # ==========================================================================

    def _load_gamer_profiles(self) -> Dict[str, Any]:
        """
        Carrega os perfis de jogos salvos.

        Caso o arquivo não exista ou esteja corrompido,
        cria uma estrutura vazia em vez de derrubar a Alicia.
        """

        try:

            if not os.path.exists(self.gamer_profiles_path):
                return {}

            with open(
                self.gamer_profiles_path,
                "r",
                encoding="utf-8"
            ) as f:

                data = json.load(f)

            if isinstance(data, dict):
                return data

            return {}

        except Exception as e:

            logger.warning(
                f"⚠️ Não foi possível carregar perfis gamer: {e}"
            )

            return {}

    def _save_gamer_profiles(
        self,
        profiles: Dict[str, Any]
    ):

        try:

            temp_path = self.gamer_profiles_path + ".tmp"

            with open(
                temp_path,
                "w",
                encoding="utf-8"
            ) as f:

                json.dump(
                    profiles,
                    f,
                    ensure_ascii=False,
                    indent=2
                )

            os.replace(
                temp_path,
                self.gamer_profiles_path
            )

        except Exception as e:

            logger.warning(
                f"⚠️ Não foi possível salvar perfil gamer: {e}"
            )

    def _normalize_game_name(
        self,
        name: str
    ) -> str:

        if not name:
            return ""

        name = re.sub(
            r"\s+",
            " ",
            name
        ).strip()

        name = name.replace(
            ".exe",
            "",
        )

        return name[:120]

    def _classify_window(
        self,
        window_title: str,
        process_name: str = ""
    ) -> Tuple[str, Optional[str]]:
        """
        Retorna:
          game    -> há evidência positiva de jogo
          ignored -> janela conhecida que não é jogo
          unknown -> não é possível afirmar

        'ignored' nunca é motivo suficiente para desligar o Gamer.
        """
        window_title = self._normalize_text(window_title)
        process_name = self._normalize_text(process_name)

        title_lower = window_title.lower()
        process_lower = process_name.lower()

        if process_lower in HardwareMonitor.GAMING_PROCESSES:
            return ("game", self._normalize_game_name(
                window_title or process_name
            ))

        ignored_windows = (
            "desktop", "área de trabalho", "area de trabalho",
            "explorer", "gerenciador de tarefas", "task manager",
            "configurações", "settings", "discord", "spotify",
            "youtube", "chrome", "google chrome", "microsoft edge",
            "firefox", "visual studio code", "pycharm", "terminal",
            "powershell", "cmd", "prompt de comando", "steam",
            "epic games", "battle.net", "riot client", "ollama", "comfyui"
        )

        if any(item in title_lower for item in ignored_windows):
            return ("ignored", None)

        game_indicators = (
            "valorant", "minecraft", "fortnite", "counter-strike",
            "counter strike", "cs2", "league of legends", "dota",
            "apex legends", "overwatch", "elden ring", "dark souls",
            "monster hunter", "red dead", "grand theft auto", "gta",
            "cyberpunk", "resident evil", "the witcher", "baldur",
            "terraria", "stardew", "palworld", "warframe", "fallout",
            "skyrim", "forza", "need for speed", "assassin",
            "god of war", "hades", "dead cells", "hollow knight",
            "genshin", "wuthering waves", "zenless", "pokemon",
            "roblox", "among us"
        )

        if any(item in title_lower for item in game_indicators):
            return ("game", self._normalize_game_name(window_title))

        if any(
            re.search(pattern, title_lower)
            for pattern in (r"\bgame\b", r"\bgameplay\b", r"\blauncher\b")
        ):
            return ("game", self._normalize_game_name(window_title))

        return ("unknown", None)

    def _detect_current_game(self, snapshot=None) -> Optional[str]:
        try:
            snapshot = snapshot or HardwareMonitor.capture_snapshot()
            title = getattr(snapshot, "active_window_title", "") or ""
            process = getattr(snapshot, "active_process_name", "") or ""

            category, name = self._classify_window(title, process)

            if category == "game" and name:
                return name

            if process.lower() in HardwareMonitor.GAMING_PROCESSES:
                return self._normalize_game_name(title or process)

            return None
        except Exception as e:
            logger.debug(f"Erro detectando jogo: {e}")
            return None

    def _build_gamer_profile_prompt(
        self,
        game_name: str
    ) -> str:
        """
        O modelo cria um conhecimento operacional sobre o jogo.

        Não estamos pedindo um tratado.
        Estamos criando um perfil que será reutilizado nas análises.
        """

        return f"""
Você é o cérebro estratégico da Alicia.

Crie um perfil compacto e útil para o jogo:

JOGO: {game_name}

O perfil será utilizado por uma IA que observa partidas
através de screenshots.

NÃO invente informações específicas se não tiver certeza.

Retorne SOMENTE JSON válido neste formato:

{{
    "game": "{game_name}",
    "genre": "",
    "objective": "",
    "core_gameplay": [],
    "important_mechanics": [],
    "common_mistakes": [],
    "strategy_principles": [],
    "things_to_watch_for": [],
    "possible_events": [],
    "humor_opportunities": [],
    "advice_style": ""
}}

Regras:

- Seja objetivo.
- Priorize informações úteis durante uma partida.
- Não escreva explicações fora do JSON.
- Não invente personagens, mapas ou mecânicas desconhecidas.
- Se não souber algo, deixe vazio.
- O perfil deve ajudar outra IA a interpretar screenshots.
"""

    def _create_gamer_profile(
        self,
        game_name: str
    ) -> Dict[str, Any]:

        game_name = self._normalize_game_name(
            game_name
        )

        if not game_name:
            return {}

        logger.info(
            f"🎮 Criando perfil gamer para: {game_name}"
        )

        prompt = self._build_gamer_profile_prompt(
            game_name
        )

        try:

            try:
                response = self.brain.complete_text(prompt, temperature=0.2, max_tokens=500)
            except BrainError as e:
                logger.warning(f"⚠️ Perfil gamer não gerado: {e.kind}")
                return {}

            if not response:

                logger.warning(
                    "⚠️ A API não retornou perfil gamer."
                )

                return {}

            # --------------------------------------------------------------
            # LIMPA POSSÍVEIS MARKDOWNS
            # --------------------------------------------------------------

            clean = response.strip()

            clean = re.sub(
                r"^```json",
                "",
                clean,
                flags=re.IGNORECASE
            )

            clean = re.sub(
                r"^```",
                "",
                clean
            )

            clean = re.sub(
                r"```$",
                "",
                clean
            )

            clean = clean.strip()

            # --------------------------------------------------------------
            # EXTRAI JSON MESMO SE O MODELO FALAR ALGO ANTES
            # --------------------------------------------------------------

            start = clean.find("{")
            end = clean.rfind("}")

            if start == -1 or end == -1:

                logger.warning(
                    "⚠️ Perfil gamer não contém JSON válido."
                )

                return {}

            json_text = clean[
                start:end + 1
            ]

            profile = json.loads(
                json_text
            )

            if not isinstance(
                profile,
                dict
            ):

                return {}

            profile["game"] = game_name

            logger.info(
                f"✅ Perfil gamer criado: {game_name}"
            )

            return profile

        except json.JSONDecodeError as e:

            logger.warning(
                f"⚠️ JSON inválido no perfil gamer: {e}"
            )

            return {}

        except Exception as e:

            logger.error(
                f"❌ Erro criando perfil gamer: {e}"
            )

            return {}

    def _get_gamer_profile(
        self,
        game_name: str
    ) -> Dict[str, Any]:

        profiles = self._load_gamer_profiles()

        key = self._normalize_game_name(
            game_name
        ).lower()

        # --------------------------------------------------------------
        # PERFIL JÁ EXISTE
        # --------------------------------------------------------------

        if key in profiles:

            logger.info(
                f"🎮 Perfil encontrado em cache: {game_name}"
            )

            return profiles[key]

        # --------------------------------------------------------------
        # CRIA NOVO PERFIL
        # --------------------------------------------------------------

        profile = self._create_gamer_profile(
            game_name
        )

        if not profile:

            # Mesmo sem perfil, Alicia consegue jogar
            # utilizando apenas o nome do jogo.

            return {
                "game": game_name,
                "genre": "",
                "objective": "",
                "core_gameplay": [],
                "important_mechanics": [],
                "common_mistakes": [],
                "strategy_principles": [],
                "things_to_watch_for": [],
                "possible_events": [],
                "humor_opportunities": [],
                "advice_style": ""
            }

        profiles[key] = profile

        self._save_gamer_profiles(
            profiles
        )

        return profile

    @staticmethod
    def _empty_gamer_profile(game_name: str) -> Dict[str, Any]:
        return {
            "game": game_name,
            "genre": "",
            "objective": "",
            "core_gameplay": [],
            "important_mechanics": [],
            "common_mistakes": [],
            "strategy_principles": [],
            "things_to_watch_for": [],
            "possible_events": [],
            "humor_opportunities": [],
            "advice_style": ""
        }

    def _load_gamer_profile_background(self, game_name: str):
        key = self._normalize_game_name(game_name).lower()
        
        with self.gamer_profile_lock:
            if key in self.gamer_profile_loading:
                return
            self.gamer_profile_loading.add(key)
            
        try:
            profile = self._get_gamer_profile(game_name)
            self.gamer_current_profile = profile
            logger.info(f"🎮 Perfil gamer carregado em background para: {game_name}")
        except Exception as e:
            logger.error(f"Erro ao carregar perfil em background: {e}")
        finally:
            with self.gamer_profile_lock:
                self.gamer_profile_loading.discard(key)
    def _on_game_detected(self, game_name: str):
        if not self.modo_gamer or not game_name:
            return

        normalized = self._normalize_game_name(game_name)
        if not normalized:
            return

        now = time.time()
        if (
            self.gamer_current_game
            and self.gamer_current_game.lower() == normalized.lower()
        ):
            self.gamer_last_game_window_seen_time = now
            return

        old_game = self.gamer_current_game
        self.gamer_current_game = normalized
        self.gamer_last_game_change_time = now
        self.gamer_last_game_window_seen_time = now
        self.gamer_last_screen_hash = None
        self.gamer_idle_counter = 0
        self.gamer_recent_analysis.clear()
        self.gamer_recent_events.clear()

        if self.voice_input.enabled:
            self.voice_input.request_recalibration()

        logger.info(f"🎮 Jogo detectado: {normalized}")
        if old_game:
            logger.info(f"🔄 Mudança de jogo: {old_game} -> {normalized}")

        # Perfil já salvo é carregado instantaneamente.
        profiles = self._load_gamer_profiles()
        key = normalized.lower()
        cached = profiles.get(key)

        if cached:
            self.gamer_current_profile = cached
        else:
            # Não bloqueie a entrada no Gamer esperando o Ollama.
            self.gamer_current_profile = self._empty_gamer_profile(normalized)

            with self.gamer_profile_lock:
                if key not in self.gamer_profile_loading:
                    self.gamer_profile_loading.add(key)
                    threading.Thread(
                        target=self._load_gamer_profile_background,
                        args=(normalized,),
                        daemon=True,
                        name="AliciaGamerProfile"
                    ).start()

        msg = (
            f"[SURPRESA] Mudamos para {normalized}. "
            "Quero ver qual vai ser a próxima decisão questionável."
            if old_game
            else
            f"[FELIZ] Então é {normalized}. "
            "Agora estou prestando atenção de verdade."
        )

        clean, expr = self.parse_expression_tags(msg)
        self.current_expression = expr
        logger.info(
            f"🤖 Alicia [{expr.value.upper()}] (Gamer): {clean}"
        )
        self._speak_async(clean, expr)

    def entrar_modo_gamer(self, manual: bool = False):
        if self.poker_active:
            logger.info("🎮 Modo Gamer ignorado: a mesa de pôquer está aberta.")
            return
        if self.modo_gamer:
            if manual:
                self.gamer_manual_override = True
                logger.info("🎮 Modo Gamer fixado manualmente.")
            return

        self.modo_gamer = True
        self.gamer_manual_override = bool(manual)
        now = time.time()
        self.gamer_last_game_change_time = now
        self.gamer_last_process_seen_time = now
        self.gamer_last_game_window_seen_time = now

        self.gamer_current_game = None
        self.gamer_current_profile = {}
        self.gamer_last_screen_hash = None
        self.gamer_last_analysis_time = 0.0
        self.gamer_recent_analysis.clear()
        self.gamer_recent_events.clear()
        self.current_expression = AliciaExpression.FELIZ

        logger.info(
            "🎮 Modo Gamer ATIVADO (%s).",
            "manual" if manual else "automático"
        )

        threading.Thread(
            target=self._entrar_modo_gamer_impl,
            daemon=True,
            name="AliciaGamerEnter"
        ).start()

    def _entrar_modo_gamer_impl(self):
        if not self.is_running:
            return

        try:
            snapshot = HardwareMonitor.capture_snapshot()
            self.gamer_last_foreground_game_title = (
                snapshot.active_window_title or ""
            )

            game = self._detect_current_game(snapshot)

            if game:
                self._on_game_detected(game)
            else:
                msg = "[PENSANDO] Modo Gamer ativado. Ainda estou procurando o jogo."
                clean, expr = self.parse_expression_tags(msg)
                self.current_expression = expr
                self._speak_async(clean, expr)

        except Exception as e:
            # Uma falha durante a entrada não mata o sistema.
            logger.exception(f"❌ Falha entrando no Modo Gamer: {e}")

    def sair_modo_gamer(self, manual: bool = True):
        if not self.modo_gamer:
            return

        # O detector automático nunca pode cancelar um modo gamer
        # explicitamente fixado pelo usuário.
        if not manual and self.gamer_manual_override:
            return

        old_game = self.gamer_current_game

        self.modo_gamer = False
        self.gamer_manual_override = False
        self.gamer_current_game = None
        self.gamer_current_profile = {}
        self.gamer_last_screen_hash = None
        self.gamer_last_known_process = None
        self.gamer_last_game_window_seen_time = 0.0
        self.gamer_last_process_seen_time = 0.0
        self.gamer_recent_analysis.clear()
        self.gamer_recent_events.clear()
        self.current_expression = AliciaExpression.FELIZ

        if self.voice_input.enabled:
            self.voice_input.request_recalibration()

        if manual:
            msg = (
                "[FELIZ] Modo Gamer desativado. "
                "Pode continuar sem uma entidade digital julgando suas jogadas."
            )
        else:
            msg = (
                "[PENSANDO] O jogo parece ter fechado. "
                "Vou parar de narrar antes que a máquina ache que estou exagerando."
            )

        logger.info(
            "🎮 Modo Gamer desativado%s.",
            f" — jogo: {old_game}" if old_game else ""
        )
        clean, expr = self.parse_expression_tags(msg)
        self.current_expression = expr
        self._speak_async(clean, expr)

    def _remember_gamer_analysis(
        self,
        text: str
    ):

        if not text:
            return

        text = text.strip()

        if not text:
            return

        self.gamer_recent_analysis.append(
            text
        )

        # Mantém somente as últimas análises
        self.gamer_recent_analysis = (
            self.gamer_recent_analysis[-10:]
        )

    def _is_repetitive_gamer_comment(self, text: str) -> bool:
        if not text:
            return True

        current = set(re.findall(r"[a-zA-ZÀ-ÿ0-9]{4,}", text.lower()))
        current -= {
            "isso", "essa", "esse", "aquela", "aquele",
            "você", "agora", "aqui", "mais", "muito",
            "jogo", "partida", "parece"
        }

        if not current:
            return False

        for anterior in self.gamer_recent_analysis[-8:]:
            previous = set(
                re.findall(r"[a-zA-ZÀ-ÿ0-9]{4,}", anterior.lower())
            )
            previous -= {
                "isso", "essa", "esse", "aquela", "aquele",
                "você", "agora", "aqui", "mais", "muito",
                "jogo", "partida", "parece"
            }

            if self._fingerprint(text) == self._fingerprint(anterior):
                return True

            union = len(current | previous)
            overlap = len(current & previous)

            if union and overlap / union >= 0.68:
                return True

        return False

    def _build_gamer_analysis_prompt(
        self,
        snapshot: HardwareSnapshot,
        evento_importante: bool = False
    ) -> str:
        perfil = self.gamer_current_profile or {}
        game = self.gamer_current_game or "jogo desconhecido"
        user_name = self.config.user_name or "Usuário"

        def fmt(key):
            values = perfil.get(key) or []
            return ", ".join(str(v) for v in values[:8]) if values else "(nenhum)"

        recent = (
            "\n".join(f"- {x}" for x in self.gamer_recent_analysis[-8:])
            if self.gamer_recent_analysis else "(nenhum)"
        )

        priority = (
            "ALTA: houve grande mudança visual. Procure primeiro uma ação "
            "recente e concreta: combate, morte, vitória, perigo, objetivo, "
            "item, erro ou decisão."
            if evento_importante else
            "NORMAL: a cena está relativamente estável. Só fale se houver "
            "algo realmente específico e interessante."
        )

        return f"""{PromptEngine.personality_for(user_name)}

[MODO GAMER — AO VIVO]

Você está acompanhando {user_name} jogando "{game}".
Imagine que está sentado ao lado dele, observando a partida pela tela.

Você NÃO é um narrador esportivo e NÃO precisa comentar tudo.
Você só fala quando encontra algo que merece reação.

[PRIORIDADE VISUAL]
{priority}

Procure:
- decisões do jogador;
- posicionamento e risco;
- oportunidade tática;
- objetivo em andamento;
- recursos/HUD;
- inimigos e aliados;
- erros e consequências;
- progresso;
- mudança de estado;
- momentos estranhos ou engraçados.

[CONHECIMENTO DO JOGO]
Gênero: {perfil.get("genre") or "desconhecido"}
Objetivo: {perfil.get("objective") or "não especificado"}
Mecânicas: {fmt("core_gameplay")}
Mecânicas importantes: {fmt("important_mechanics")}
Erros comuns: {fmt("common_mistakes")}
Estratégia: {fmt("strategy_principles")}
Observe: {fmt("things_to_watch_for")}
Eventos: {fmt("possible_events")}
Humor: {fmt("humor_opportunities")}

Use isso apenas como contexto. NUNCA invente algo que não possa ser apoiado
pela imagem.

[ANTI-REPETIÇÃO]
Últimos comentários:
{recent}

Não repita ideia, piada, dica, metáfora ou estrutura.
Uma paráfrase ainda é repetição.

[ESTILO]
Alterne entre reação genuína, provocação, observação tática, humor e tensão.
Uma dica só deve aparecer quando houver algo concreto na tela.
Não dê tutorial espontâneo.
{PromptEngine.style_line(self.get_personality()) if self.get_personality() != PromptEngine.DEFAULT_PERSONALITY else ""}

Se nada valer a pena:
VAZIO

[FORMATO]
Comece com exatamente uma destas tags:
[FELIZ] [SERIO] [SURPRESA] [PENSANDO] [BRAVO]

Depois escreva no máximo 2 frases curtas, naturais e faláveis.
"""

    def _maybe_run_gamer_analysis(self):
        if (
            not self.modo_gamer
            or self.poker_active
            or self.modo_descanso
            or not self.screen_watch_enabled
            or not self.gamer_current_game
            or self.tts.is_speaking
        ):
            return

        with self.gamer_analysis_lock:
            if self.gamer_analysis_running:
                return
            self.gamer_analysis_running = True

        threading.Thread(
            target=self._run_gamer_analysis_safe,
            daemon=True,
            name="AliciaGamerVision"
        ).start()

    def _run_gamer_analysis_safe(self):
        try:
            if self.modo_gamer:
                self._run_gamer_analysis()
        except Exception as e:
            logger.exception(f"❌ Erro na análise gamer: {e}")
        finally:
            with self.gamer_analysis_lock:
                self.gamer_analysis_running = False

    def _run_gamer_analysis(self):
        if not self.modo_gamer or not self.gamer_current_game:
            return

        b64_img, current_pil, p_hash = (
            self.screen_engine.capture_and_encode()
        )

        if not b64_img or current_pil is None:
            return

        diff_score = self.screen_engine.calculate_perceptual_difference(
            self.last_screen_img,
            current_pil
        )
        self.last_screen_img = current_pil

        tela_parada = p_hash == self.gamer_last_screen_hash
        self.gamer_idle_counter = (
            self.gamer_idle_counter + 1 if tela_parada else 0
        )
        self.gamer_last_screen_hash = p_hash

        if tela_parada and self.gamer_idle_counter < 3:
            return

        evento_importante = (
            diff_score >= self.config.gamer_event_diff_threshold
        )

        cooldown = (
            self.gamer_event_cooldown
            if evento_importante
            else max(
                float(self.config.gamer_analysis_interval),
                self.gamer_comment_cooldown
            )
        )

        now = time.time()
        if now - self.gamer_last_analysis_time < cooldown:
            return

        if (
            diff_score < self.config.gamer_min_diff_threshold
            and not evento_importante
        ):
            return

        if not self.modo_gamer:
            return

        snapshot = HardwareMonitor.capture_snapshot()

        # A janela pode perder foco sem que o jogo tenha fechado.
        # Atualizamos a última evidência, mas nunca desligamos aqui.
        active_proc = (snapshot.active_process_name or "").lower()
        if active_proc in HardwareMonitor.GAMING_PROCESSES:
            self.gamer_last_known_process = active_proc
            self.gamer_last_process_seen_time = now
            self.gamer_last_game_window_seen_time = now

        prompt = self._build_gamer_analysis_prompt(
            snapshot,
            evento_importante
        )

        raw_response = self.vision.generate(
                prompt, b64_img,
                options={
                "temperature": 0.82 if evento_importante else 0.72,
                "top_p": 0.92,
                "num_predict": 100,
                "repeat_penalty": 1.28,
                "repeat_last_n": 256
            },
            timeout=int(self.config.gamer_vision_timeout)
        )

        self.gamer_last_analysis_time = time.time()
        self.gamer_analysis_count += 1

        if not raw_response or raw_response.strip().upper() == "VAZIO":
            return

        clean_text, expr = self.parse_expression_tags(raw_response)
        clean_text = self._limit_response_length(
            clean_text,
            max_sentences=2,
            max_chars=260
        )

        if not clean_text or clean_text.upper() == "VAZIO":
            return

        if self.poker_active:        # a mesa abriu enquanto a análise rodava: não fala por cima da partida
            return

        if self._is_repetitive_gamer_comment(clean_text):
            logger.debug("🎮 Comentário gamer descartado por repetição.")
            return

        self._remember_gamer_analysis(clean_text)

        if evento_importante:
            self.gamer_recent_events.append(clean_text)
            self.gamer_recent_events = self.gamer_recent_events[-6:]

        self.last_visual_context = clean_text
        self.last_comment_time = time.time()
        self.current_expression = expr

        try:
            self.db.log_vision_event(
                p_hash,
                clean_text,
                expr.value,
                snapshot.active_window_title
            )
            self.db.add_history(
                role="Alicia (gamer)",
                content=clean_text,
                expression=expr.value
            )
        except Exception as e:
            logger.warning(f"Falha ao salvar análise gamer: {e}")

        print(
            f"\n🤖 Alicia [{expr.value.upper()}] "
            f"(Gamer - {self.gamer_current_game}): {clean_text}"
        )

        self._speak_async(clean_text, expr)

    # ==========================================================================
    # MODO DESCANSO
    # ==========================================================================

    def entrar_modo_descanso(self):

        self.modo_descanso = True

        self.config.modo_descanso = True

        self.config.save()

        self.current_expression = (
            AliciaExpression.SERIO
        )

        msg = (
            "Entrando em modo de descanso. "
            "Não vou te incomodar."
        )

        logger.info(
            "😴 Modo descanso ativado."
        )

        self._speak_async(
            msg,
            AliciaExpression.SERIO
        )

    def sair_modo_descanso(self):

        self.modo_descanso = False

        self.config.modo_descanso = False

        self.config.save()

        self.current_expression = (
            AliciaExpression.FELIZ
        )

        msg = (
            "Já acordei! Estou pronta de novo."
        )

        logger.info(
            "⚡ Modo descanso desativado."
        )

        self._speak_async(
            msg,
            AliciaExpression.FELIZ
        )

    # ==========================================================================
    # EXPRESSÕES
    # ==========================================================================

    def parse_expression_tags(
        self,
        text: str
    ) -> Tuple[str, AliciaExpression]:

        if not text:

            return (
                "",
                AliciaExpression.FELIZ
            )

        text = str(text).strip()

        pattern = (
            r"\[\s*(FELIZ|SERIO|SURPRESA|PENSANDO|BRAVO)\s*\]"
        )

        match = re.search(
            pattern,
            text,
            re.IGNORECASE
        )

        if match:

            tag_str = (
                match.group(1).upper()
            )

            expression = (
                AliciaExpression.from_str(
                    tag_str
                )
            )

            clean_text = re.sub(
                pattern,
                "",
                text,
                flags=re.IGNORECASE
            ).strip()

            return (
                clean_text,
                expression
            )

        # --------------------------------------------------------------
        # FALLBACK: o modelo às vezes "esquece" os colchetes e escreve
        # a etiqueta solta bem no início (ex.: "FELIZ: Oi..." ou
        # "*Feliz* - Oi..."). Sem isso, a etiqueta acaba sendo lida em
        # voz alta pelo TTS. Só removemos quando ela está isolada logo
        # no começo e seguida de um separador claro (":" ou "-"), para
        # não arrancar a palavra de uma frase normal como "Feliz
        # aniversário!".
        # --------------------------------------------------------------

        loose_pattern = (
            r"^[\*\(]{0,2}\s*"
            r"(FELIZ|SERIO|SURPRESA|PENSANDO|BRAVO)"
            r"\s*[\*\)]{0,2}\s*[:\-–—]\s*"
        )

        loose_match = re.match(
            loose_pattern,
            text,
            re.IGNORECASE
        )

        if loose_match:

            tag_str = (
                loose_match.group(1).upper()
            )

            expression = (
                AliciaExpression.from_str(
                    tag_str
                )
            )

            clean_text = (
                text[loose_match.end():].strip()
            )

            return (
                clean_text,
                expression
            )

        return (
            text,
            AliciaExpression.FELIZ
        )

    # ==========================================================================
    # OLLAMA
    # ==========================================================================



    # ==========================================================================
    # LOOP DE VISÃO
    # ==========================================================================

    def screen_observation_loop(self):
        logger.info("👁️ Thread de visão iniciada.")

        while self.is_running:
            try:
                now = time.time()

                # ==============================================================
                # 1. DETECÇÃO AUTOMÁTICA DE JOGO
                # ==============================================================
                if (
                    self.config.gamer_auto_detection
                    and not self.poker_active          # com a mesa aberta, nada de entrar/sair do modo gamer sozinha
                    and not self.modo_descanso
                    and now - self.gamer_last_detect_check_time
                    >= float(self.config.gamer_detection_interval)
                ):
                    self.gamer_last_detect_check_time = now

                    try:
                        title = HardwareMonitor.get_active_window_title()
                        process = HardwareMonitor.get_active_process_name()

                        category, game_name = self._classify_window(
                            title,
                            process
                        )

                        active_proc = (process or "").lower()

                        # Evidência forte e atual: processo de jogo em primeiro plano.
                        if active_proc in HardwareMonitor.GAMING_PROCESSES:
                            self.gamer_last_known_process = active_proc
                            self.gamer_last_process_seen_time = now
                            self.gamer_last_game_window_seen_time = now

                        if category == "game" and game_name:
                            self.gamer_last_game_window_seen_time = now

                            if active_proc and active_proc != "desconhecido":
                                self.gamer_last_known_process = active_proc
                                self.gamer_last_process_seen_time = now

                            if not self.modo_gamer:
                                self.entrar_modo_gamer(manual=False)

                            if self.modo_gamer:
                                self._on_game_detected(game_name)

                        elif (
                            self.modo_gamer
                            and not self.gamer_manual_override
                            and self.gamer_current_game
                        ):
                            # --------------------------------------------------
                            # NÃO DESLIGA POR ALT-TAB.
                            #
                            # O jogo pode estar minimizado, atrás do Discord,
                            # navegador, Steam, overlay etc. Só saímos depois
                            # de uma janela de tolerância sem nenhuma evidência.
                            # --------------------------------------------------
                            process_alive = False

                            if self.gamer_last_known_process and HAS_PSUTIL:
                                try:
                                    wanted = self.gamer_last_known_process.lower()
                                    for proc in psutil.process_iter(['name']):
                                        try:
                                            if (
                                                proc.info.get('name') or ''
                                            ).lower() == wanted:
                                                process_alive = True
                                                break
                                        except (psutil.NoSuchProcess, psutil.AccessDenied):
                                            continue
                                except Exception as e:
                                    logger.debug(
                                        f"Erro verificando processo gamer: {e}"
                                    )

                            recent_process = (
                                now - self.gamer_last_process_seen_time
                                < float(self.config.gamer_auto_exit_grace)
                            )
                            recent_window = (
                                now - self.gamer_last_game_window_seen_time
                                < float(self.config.gamer_auto_exit_grace)
                            )

                            # Se não temos um processo verificável, não
                            # arriscamos um desligamento falso.
                            can_confirm_exit = bool(
                                self.gamer_last_known_process
                            )

                            if (
                                can_confirm_exit
                                and not process_alive
                                and not recent_process
                                and not recent_window
                            ):
                                self.sair_modo_gamer(manual=False)

                    except Exception as e:
                        # Falha de detecção jamais desliga o modo.
                        logger.debug(
                            f"Falha na detecção automática de jogo: {e}"
                        )

                # ==============================================================
                # 2. PAUSA / DESCANSO
                # ==============================================================
                self._maybe_recheck_vision()
                if self.modo_descanso or self.poker_active or not self.screen_watch_enabled or not self.vision_spontaneous:
                    time.sleep(0.35)
                    continue

                # ==============================================================
                # 3. MODO GAMER
                # ==============================================================
                if self.modo_gamer:
                    self._maybe_run_gamer_analysis()
                    time.sleep(0.35)
                    continue

                # ==============================================================
                # 4. VISÃO GERAL
                # ==============================================================
                intervalo = max(1.0, float(self.config.screen_interval))

                if now - self.last_screen_check_time < intervalo:
                    time.sleep(0.2)
                    continue

                if now - self.last_comment_time < 25.0:
                    time.sleep(0.2)
                    continue

                if self.tts.is_speaking:
                    time.sleep(0.2)
                    continue

                self.last_screen_check_time = now

                snapshot = HardwareMonitor.capture_snapshot()
                self._loop_ok("loop visual")
                b64_img, current_pil, p_hash = (
                    self.screen_engine.capture_and_encode()
                )

                if not b64_img or current_pil is None:
                    time.sleep(0.5)
                    continue

                diff_score = self.screen_engine.calculate_perceptual_difference(
                    self.last_screen_img,
                    current_pil
                )
                self.last_screen_img = current_pil

                if diff_score < 3.0:
                    continue

                if random.random() > self.config.interest_chance:
                    continue

                prompt = PromptEngine.build_vision_prompt(
                    snapshot,
                    recent_comments=self.recent_vision_comments,
                    user_name=self.config.user_name,
                    personality=self.get_personality()
                )

                raw_response = self.vision.generate(
                prompt, b64_img,
                options={
                        "temperature": 0.78,
                        "top_p": 0.92,
                        "num_predict": 80,
                        "repeat_penalty": 1.22,
                        "repeat_last_n": 192
                    },
                    timeout=70
                )

                if not raw_response or raw_response.strip().upper() == "VAZIO":
                    continue

                clean_text, expr = self.parse_expression_tags(raw_response)
                clean_text = self._limit_response_length(
                    clean_text,
                    max_sentences=2,
                    max_chars=260
                )

                if not clean_text or clean_text.upper() == "VAZIO":
                    continue

                self._remember_vision_comment(clean_text)
                self.last_visual_context = clean_text
                self.last_comment_time = time.time()
                self.current_expression = expr

                try:
                    self.db.log_vision_event(
                        p_hash,
                        clean_text,
                        expr.value,
                        snapshot.active_window_title
                    )
                    self.db.add_history(
                        role="Alicia (observação)",
                        content=clean_text,
                        expression=expr.value
                    )
                except Exception as e:
                    logger.warning(
                        f"Falha ao salvar observação: {e}"
                    )

                print(
                    f"\n🤖 Alicia [{expr.value.upper()}] "
                    f"(Tela): {clean_text}"
                )

                # Não bloqueia a captura esperando a fala.
                self._speak_async(clean_text, expr)

            except Exception as e:
                time.sleep(self._loop_error("loop visual", e))

    def _loop_error(self, where: str, exc: Exception) -> float:
        """
        Registra um erro de laço em segundo plano SEM encher o log: o traceback completo sai na primeira
        vez (e quando o erro muda); repetições viram uma linha por minuto. Devolve quanto esperar
        (backoff: 2 s, 4 s, 8 s... até 30 s) antes de tentar de novo.
        """
        state = getattr(self, "_loop_err", None)
        if state is None:
            state = self._loop_err = {}
        sig = f"{where}:{type(exc).__name__}:{exc}"
        st = state.setdefault(where, {"sig": None, "t": 0.0, "n": 0})
        now = time.time()
        if st["sig"] != sig:
            logger.exception(f"❌ Erro no {where}: {exc}")
            st.update(sig=sig, t=now, n=1)
        else:
            st["n"] += 1
            if now - st["t"] > 60:
                logger.error(f"❌ Erro no {where} continua ({st['n']}x): {exc}")
                st["t"] = now
        return min(2.0 * (2 ** min(st["n"] - 1, 4)), 30.0)

    def _loop_ok(self, where: str):
        st = getattr(self, "_loop_err", {}).get(where)
        if st and st["sig"] is not None:
            logger.info(f"✅ {where} voltou a funcionar.")
            st.update(sig=None, n=0)

    # ==========================================================================
    # INTERAÇÃO PRINCIPAL — NOVO CÉREBRO
    # ==========================================================================



    # ==========================================================================
    # LOOP DO TERMINAL
    # ==========================================================================


    # ==========================================================================
    # START
    # ==========================================================================

    def open_poker(self) -> bool:
        """Pede à interface (thread principal) para abrir o minigame. Seguro de chamar de qualquer thread."""
        ui = getattr(self, "ui", None)
        if ui is None:
            return False
        try:
            ui.request_poker.emit()
            return True
        except Exception as e:
            logger.error(f"Não consegui abrir o pôquer: {e}")
            return False

    def _persist_model(self, model: str):
        os.environ["NVIDIA_MODEL"] = model
        set_env_value(".env", "NVIDIA_MODEL", model)

    # ==========================================================================
    # VISÃO (Ollama opcional)
    # ==========================================================================

    def _maybe_recheck_vision(self):
        """A cada 60 s reavalia o Ollama local (visão preferida). Sem spam de erros."""
        now = time.time()
        if now - self._last_vision_recheck < 60.0:
            return
        self._last_vision_recheck = now
        was = self.vision.backend
        ollama_ok = bool(self.config.vision_enabled) and self.client.check_health()
        if ollama_ok:
            ollama_ok = self._ensure_vision_model(self.client.list_models())
        self.vision.use_ollama = ollama_ok
        if not ollama_ok and self.vision.remote is None and os.environ.get("NVIDIA_VISION_MODEL"):
            # modelo de visão já descoberto antes: reaproveita sem testar de novo
            self.vision.remote = self.vision._new_remote(os.environ["NVIDIA_VISION_MODEL"])
        self.vision_available = self.vision.available
        self.vision_spontaneous = ollama_ok
        if self.vision.backend != was and self.vision.backend:
            logger.info(f"👁️ Visão disponível via {self.vision.backend}.")

    def _prepare_vision_now(self) -> bool:
        """Pedido direto sem visão pronta: descobre um modelo de visão da NVIDIA (uma tentativa por 30 min)."""
        if self.vision.remote is None and time.time() - self.vision._discovery_done_at > 1800:
            logger.info("👁️ Procurando um modelo de visão na API da NVIDIA...")
            self.vision.discover_remote(time_budget=60.0)
        self.vision_available = self.vision.available
        return self.vision_available

    def _ensure_vision_model(self, modelos_instalados: List[str]) -> bool:
        """Confere se o modelo de visão do Ollama está instalado; senão usa outro de visão que exista."""
        if not modelos_instalados:
            return False
        configurado = (self.config.vision_model or "").lower()
        if any(m == configurado or m.startswith(configurado) for m in modelos_instalados):
            return True
        palavras = ("vl", "vision", "llava", "moondream", "minicpm-v")
        preferidos = [m for m in modelos_instalados if any(p in m for p in palavras)]
        if not preferidos:
            logger.warning(f"⚠️ Modelo de visão '{configurado}' não instalado no Ollama. Rode: ollama pull {configurado}")
            return False
        logger.warning(f"➡️ Modelo de visão '{configurado}' não instalado; usando '{preferidos[0]}' nesta sessão.")
        self.config.vision_model = preferidos[0]
        return True

    # ==========================================================================
    # PRÉ-REQUISITOS
    # ==========================================================================

    def check_prerequisites(self) -> bool:
        """
        Essencial: chave/modelo da NVIDIA configurados e banco íntegro.
        Opcional: Ollama (só visão e modo gamer).
        """
        if not self.nvidia.has_key:
            logger.error("❌ NVIDIA_API_KEY não configurada. Preencha o arquivo .env (veja .env.example).")
            return False
        if not self.nvidia.model:
            logger.error("❌ NVIDIA_MODEL não configurado no .env.")
            return False
        if not self.db.check_integrity():
            logger.error("❌ O banco de memória falhou na verificação de integridade. Nada foi apagado.")
            return False
        try:
            if self.db.count_chat_messages() > 0:
                saved = self.db.backup(min_interval_hours=24)
                if saved:
                    logger.info(f"💾 Backup automático da memória: {saved}")
        except Exception as e:
            logger.warning(f"Backup automático falhou (a conversa segue normal): {e}")
        self._last_vision_recheck = 0.0
        self._maybe_recheck_vision()
        if not self.vision_available:
            logger.warning("⚠️ Ollama indisponível: comentários espontâneos e modo gamer ficam desligados; \"Alicia, olha\" usará um modelo de visão da NVIDIA, se houver.")
        return True

    # ==========================================================================
    # LOOP DO TERMINAL (entrada por teclado)
    # ==========================================================================

    def cli_loop(self):
        print("\n" + "=" * 70)
        print("        🤖 ALICIA ONLINE")
        print("=" * 70 + "\n")

        # Configuração inicial ANTES de qualquer conversa (teclado ou voz).
        if not self.ensure_user_name():
            logger.error("Não foi possível definir o nome do usuário. Reinicie a Alicia para tentar de novo.")
            self.is_running = False
            return
        if not self.ensure_personality():
            logger.warning("Personalidade não escolhida; usando o modo Normal.")
        self.chat_ready.set()

        try:
            self.voice_input.start_listening()
        except Exception as e:
            logger.warning(f"⚠️ Microfone não iniciou: {e}")

        name = self.db.get_user_name() or ""
        try:
            greeting, g_expr = self.parse_expression_tags(PromptEngine.random_greeting(name))
            self._say(greeting, g_expr)
        except Exception as e:
            logger.warning(f"Falha na saudação inicial: {e}")

        while self.is_running:
            try:
                label = self.db.get_user_name() or "Você"
                user_input = input(f"\n{label} (Teclado) > ").strip()
                if not user_input:
                    continue
                lower_input = user_input.lower()

                if lower_input in {"sair", "exit", "quit"}:
                    self.is_running = False
                    try:
                        self.voice_input.stop_listening()
                    except Exception:
                        pass
                    os._exit(0)
                if lower_input in {"/descanso", "/dormir"}:
                    self.entrar_modo_descanso()
                    continue
                if lower_input in {"/acordar", "/voltar"}:
                    self.sair_modo_descanso()
                    continue
                if lower_input in {"/call", "/discord"}:
                    print("📞 " + (self.sair_modo_call() if self.call_listener.active else self.entrar_modo_call()))
                    continue
                if lower_input in {"/resumo", "/resumir"}:
                    print("📞 " + self.resumir_call())
                    continue
                if lower_input in {"/gamer", "/jogo", "/assistindo"}:
                    if self.modo_gamer:
                        self.sair_modo_gamer(manual=True)
                    else:
                        self.entrar_modo_gamer(manual=True)
                    continue

                response = self.interact(user_input, source="keyboard")
                if not response:
                    continue
                clean_response, expr = self.parse_expression_tags(response)
                print(f"🤖 Alicia [{expr.value.upper()}]: {clean_response}")
                try:
                    self.tts.speak(clean_response, expr)
                except Exception as e:
                    logger.warning(f"Erro no TTS: {e}")
            except (EOFError, KeyboardInterrupt):
                logger.info("Encerramento solicitado.")
                self.is_running = False
                break
            except Exception as e:
                logger.exception(f"❌ Erro no chat: {type(e).__name__}")
                time.sleep(1)

    # ==========================================================================
    # START
    # ==========================================================================

    def start(self):
        self.is_running = True
        if not self.check_prerequisites():
            self.is_running = False
            return
        try:
            # Qt deve nascer e continuar na thread principal.
            app = QApplication(sys.argv)
            app.setQuitOnLastWindowClosed(False)     # fechar a mesa de pôquer não pode encerrar a Alicia
            ui = AliciaDesktopPetUI(self)
            self.ui = ui
            ui.show()
            threading.Thread(target=self.screen_observation_loop, daemon=True, name="AliciaVision").start()
            threading.Thread(target=self.cli_loop, daemon=True, name="AliciaCLI").start()
            logger.info("✅ Alicia iniciada.")
            app.exec()
        except Exception as e:
            logger.exception(f"❌ Erro na interface da Alicia: {type(e).__name__}")
        finally:
            self.is_running = False
            try:
                self.voice_input.stop_listening()
            except Exception:
                pass
            try:
                self.call_listener.stop()
            except Exception:
                pass
            try:
                stop_playback()
            except Exception:
                pass
            logger.info("🛑 Alicia encerrada.")
