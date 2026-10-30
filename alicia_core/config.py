"""
Configuração persistente da Alicia (config.json).
"""

import json
import pathlib
from typing import Optional

from .logging_setup import logger


class SystemConfig:
    def __init__(self, config_path: str = "config.json"):
        self.config_path = pathlib.Path(config_path)
        
        # ------------------------------------------------------------------
        # VISÃO / MODO GAMER (Ollama local). O cérebro da conversa NÃO usa
        # mais o Ollama: ele usa a API da NVIDIA (ver .env e brain_* abaixo).
        # ------------------------------------------------------------------
        self.ollama_host: str = "http://localhost:11434"
        self.vision_model: str = "qwen2.5vl:7b"
        self.vision_enabled: bool = True

        # ------------------------------------------------------------------
        # CÉREBRO (API NVIDIA). A chave/modelo/endpoint ficam no .env
        # (NVIDIA_API_KEY, NVIDIA_MODEL, NVIDIA_BASE_URL) — nunca aqui.
        # ------------------------------------------------------------------
        self.brain_timeout: int = 60
        self.brain_max_retries: int = 2
        self.brain_history_messages: int = 20
        self.brain_temperature: float = 0.8
        self.brain_top_p: float = 0.9
        self.brain_max_tokens: int = 400
        self.brain_max_sentences: int = 6
        self.brain_max_chars: int = 800
        self.log_level: str = "INFO"
        self.poker_voice: bool = True            # a Alicia fala as reações do pôquer (se a voz estiver ligada)
        self.avatar_relax_seconds: float = 8.0   # após uma emoção, volta ao rosto neutro (que respira e pisca)
        
        self.screen_interval: int = 3
        self.screen_cooldown: int = 12
        self.interest_chance: float = 0.72
        self.image_quality: int = 65
        self.max_image_dimension: int = 1280
        
        # Identificação e Primeiro Acesso
        # O nome real fica no banco de dados (SQLite). Estes campos são só cache/legado.
        self.user_name: str = "Usuário"
        self.first_run: bool = True
        
        self.db_path: str = "alicia_memory.db"
        self.history_limit: int = 30  # legado (agora: brain_history_messages)
        
        self.modo_descanso: bool = False
        
        self.enable_audio: bool = True
        self.voice_speed: int = 100
        self.voice_name: str = "pt-BR-FranciscaNeural"   # outra voz: ex. "pt-BR-ThalitaMultilingualNeural" (se a sua conta do Edge tiver)
        self.voice_expressive: bool = True               # a voz muda ritmo/tom conforme a emoção (feliz, brava, surpresa...)
        self.debug_mode: bool = False
        self.mic_index: Optional[int] = None

        # ------------------------------------------------------------------
        # CAPTURA DE MICROFONE
        # ------------------------------------------------------------------
        # Piso mínimo de sensibilidade (quanto MENOR, mais sensível).
        # Evita que o limiar dinâmico "derreta" demais em silêncio total
        # e comece a disparar com qualquer ruidinho.
        self.mic_energy_floor: int = 250
        # A cada quantos segundos a Alicia recalibra o ruído ambiente
        # em condições normais.
        self.mic_recalibration_interval: float = 45.0
        # Recalibração mais frequente durante o Modo Gamer, já que o
        # áudio do jogo muda o "silêncio de fundo" que o microfone ouve.
        self.mic_recalibration_interval_gamer: float = 20.0
        # Quantos reconhecimentos de voz fracassados (fala captada mas
        # não entendida) em sequência forçam uma recalibração imediata.
        self.mic_fails_before_recalibration: int = 5
        # Captura contínua (ver voice_input.py)
        self.mic_preroll_seconds: float = 1.6      # áudio guardado ANTES de a fala ser detectada
        self.mic_end_silence: float = 1.0           # silêncio que encerra a frase
        self.mic_post_speech_cooldown: float = 1.0 # pausa após a Alicia falar (evita ouvir o eco dela)
        self.mic_start_multiplier: float = 2.5     # limiar = ruído de fundo x isto

        # Modo Gamer: tolerância e desempenho.
        self.gamer_auto_exit_grace: float = 25.0
        self.gamer_detection_interval: float = 1.5
        self.gamer_analysis_interval: float = 7.0
        self.gamer_event_diff_threshold: float = 6.0
        self.gamer_min_diff_threshold: float = 1.25
        self.gamer_vision_timeout: int = 55
        self.gamer_auto_detection: bool = True

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
