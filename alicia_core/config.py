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

        # ------------------------------------------------------------------
        # MODO CALL (ouvir o áudio do PC numa call do Discord; Linux/PipeWire)
        # ------------------------------------------------------------------
        self.call_virtual_audio: bool = True       # cria os canais virtuais (o amigo ouve a Alicia; só o Discord é escutado)
        self.call_source: Optional[str] = None     # fonte a escutar; None = automática (canal do Discord ou áudio geral)
        self.call_mic_source: Optional[str] = None # seu microfone real para o mix; None = o padrão do sistema
        self.call_discord_apps: list = ["discord", "vesktop", "webcord", "armcord", "legcord", "equibop"]
        self.call_context_seconds: float = 600.0   # quanto tempo de conversa ela lembra
        self.call_context_max_items: int = 60
        self.call_reply_cooldown: float = 2.0      # intervalo mínimo entre chamadas (evita responder 2x à mesma frase)
        self.call_end_silence: float = 0.9         # silêncio que encerra a fala do amigo
        self.call_energy_floor: int = 150

        # Modo Gamer: tolerância e desempenho.
        self.gamer_auto_exit_grace: float = 25.0
        self.gamer_detection_interval: float = 1.5
        self.gamer_analysis_interval: float = 7.0
        self.gamer_event_diff_threshold: float = 6.0
        self.gamer_min_diff_threshold: float = 1.25
        self.gamer_vision_timeout: int = 55          # só vale para o modelo de visão remoto (reserva, se autorizado)
        # Modelo de visão LOCAL (opcional; instale com Iniciar_Alicia.bat --vision-local). Nada sai do computador.
        self.local_vlm_enabled: bool = False
        self.local_vlm_dir: str = "models/vision"
        self.local_vlm_n_ctx: int = 4096
        self.local_vlm_threads: int = 0              # 0 = automático
        self.local_vlm_idle_unload_s: float = 300.0  # descarrega da memória depois de tanto tempo parado
        self.gamer_local_analysis: bool = True       # Modo Gamer analisa o jogo LOCALMENTE (OCR + quadros): vitória/derrota e marcos de tempo
        self.gamer_auto_detection: bool = True


        # ------------------------------------------------------------------
        # MODO OBSERVADORA (observação da tela, 100% local)
        # TODAS as opções da observadora ficam aqui (config.json). Feche a Alicia antes de editar.
        # ------------------------------------------------------------------
        self.obs_interval: float = 4.0               # segundos entre capturas da tela
        self.obs_comment_cooldown: float = 90.0      # intervalo mínimo entre comentários espontâneos
        self.obs_event_cooldown: float = 40.0        # intervalo mínimo entre comentários sobre EVENTOS (erro na tela, vitória, derrota)
        self.obs_max_comments_per_hour: int = 12     # teto de comentários por hora
        self.obs_talkativeness: float = 0.7          # 0 = quase muda ... 1 = comenta sempre que há motivo
        self.obs_settle_seconds: float = 20.0        # a atividade precisa durar isto para virar assunto (ignora alt-tab)
        self.obs_change_threshold: float = 4.0       # % de mudança da tela considerada relevante
        self.obs_change_sensitivity: float = 1.0     # multiplicador: 2.0 = mais sensível, 0.5 = menos
        self.obs_long_activity_minutes: list = [20, 45, 90]   # marcos de "você está nisso há muito tempo"
        self.obs_quiet_after_user_seconds: float = 20.0       # depois que você fala/digita, ela fica quieta por este tempo
        self.obs_context_minutes: float = 30.0       # quanto tempo o contexto visual temporário dura
        self.obs_context_max_items: int = 60         # máximo de leituras guardadas (só em memória, nunca em disco)
        self.obs_monitor: str = "active"             # "active" (monitor da janela ativa), "primary" ou "all"
        self.obs_ocr: bool = True                    # ler texto da tela (OCR) se houver motor instalado
        self.obs_ocr_every: float = 20.0             # OCR é caro: no máximo a cada N s (ou quando a tela muda muito)
        self.obs_ocr_max_width: int = 1600           # a imagem é reduzida para esta largura antes do OCR
        self.obs_ocr_langs: str = "por+eng"          # idiomas do Tesseract (cai para "eng" se "por" não existir)
        self.obs_use_brain_for_comments: bool = True # usa o cérebro (NVIDIA) para escrever o comentário; SÓ envia fatos abstratos
                                                     # (categoria, nome do app, minutos, tipo de evento), nunca texto da tela nem imagem
        self.obs_allow_remote_vision: bool = False   # False = NENHUMA imagem de tela vai para serviço externo (NVIDIA)
        self.obs_ignored_apps: list = [              # executáveis que ela NUNCA observa (nem captura a tela)
            "keepass.exe", "keepassxc.exe", "bitwarden.exe", "1password.exe", "lastpass.exe",
            "dashlane.exe", "nordpass.exe", "authy.exe", "veracrypt.exe",
        ]
        self.obs_ignored_titles: list = [            # trechos de título de janela que ela NUNCA observa (sem acento, minúsculo)
            "senha", "password", "login", "entrar na conta", "banco", "internet banking", "nubank", "itau", "bradesco",
            "santander", "caixa economica", "banco do brasil", "inter ", "c6 bank", "mercado pago", "paypal", "pix",
            "carteira", "wallet", "cartao de credito", "fatura", "gerenciador de senhas", "autenticacao", "2fa", "token",
            "incognito", "navegacao anonima", "inprivate", "private browsing",
        ]
        self.legacy_screen_comments: bool = False    # laço antigo de comentários espontâneos. A Observadora o substitui.

        # ------------------------------------------------------------------
        # MODO SEM FILTRO (camada extra de personalidade; a personalidade original nunca é alterada)
        # O texto da camada fica em alicia_core/unfiltered.py (UNFILTERED_CORE / LEVEL_*).
        # ------------------------------------------------------------------
        self.unf_sarcasm: int = 4                    # 1 (leve) a 5 (ácido total)
        self.unf_profanity: int = 3                  # 0 (sem palavrão) a 5 (palavrão em quase toda fala)
        self.unf_fire_back: bool = True              # devolve provocação com provocação
        self.unf_remember_state: bool = False        # True = lembra o modo ligado ao reiniciar (padrão: sempre começa desligado)
        self.unf_custom_instructions: str = ""       # texto livre acrescentado à camada (ex.: apelidos, manias, assuntos preferidos)

        self.load()

    def load(self):
        if self.config_path.exists():
            try:
                with open(self.config_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    obsoletas = []
                    for key, val in data.items():
                        if hasattr(self, key):
                            if key == 'voice_speed' and (val < 70 or val > 130):
                                val = 100
                            setattr(self, key, val)
                        else:
                            obsoletas.append(key)
                    if obsoletas:
                        # Opções de versões antigas: ignoradas (e removidas na próxima gravação normal do arquivo).
                        logger.info(f"Opções obsoletas ignoradas no config.json: {', '.join(obsoletas)}")
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
