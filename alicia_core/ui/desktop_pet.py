"""
Avatar flutuante (pet de desktop) em PyQt6.

Assim como voice_input.py, usa `'AliciaAssistantCore'` só como anotação
de tipo — não importa alicia_core.core aqui, para não criar ciclo.
"""

from __future__ import annotations

import io
import json
import os
import pathlib
import random
import time
from typing import TYPE_CHECKING, Dict

from PyQt6.QtCore import Qt, QPoint, QTimer, pyqtSignal
from PyQt6.QtGui import QPixmap, QAction, QImage, QPainter
from PyQt6.QtWidgets import QApplication, QLabel, QMenu, QWidget

from ..audio_input import list_microphones
from ..deps import Image, sr, HAS_SPEECH, HAS_MIC, MIC_ERROR
from ..enums import AliciaExpression
from ..logging_setup import logger
from .avatar_animator import AvatarAnimator, AvatarAnimatorSet

if TYPE_CHECKING:
    from ..core import AliciaAssistantCore


class AliciaDesktopPetUI(QWidget):
    # Emitido por QUALQUER thread (ex.: comando de voz); o Qt entrega na thread principal.
    request_poker = pyqtSignal()

    def __init__(self, core_engine: 'AliciaAssistantCore'):
        super().__init__()
        self.core = core_engine
        
        self._drag_active = False
        self._drag_origin = QPoint()
        
        self.pixmaps: Dict[AliciaExpression, QPixmap] = {}
        self.estado_atual = None
        self.animator = None
        self.anim_pixmaps: Dict[str, QPixmap] = {}
        self.anim_manifest = None                     # manifesto da emoção "serio" (compatibilidade)
        self.anim_manifests: Dict[str, Dict] = {}     # um manifesto por emoção que tem animação
        self.poker = None
        self._last_anim_key = None
        self._expr_since = time.monotonic()
        self._last_expr_seen = None
        self._last_msg_seen = ""
        
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
        self.request_poker.connect(self.abrir_poker)
        self.timer.start(40)          # 25 quadros por segundo (só troca a imagem quando o quadro muda)

        screen = QApplication.primaryScreen().availableGeometry()
        self.move(screen.width() - 530, screen.height() - 570)

    def carregar_imagens_avatar(self):
        self.variantes: Dict[AliciaExpression, list] = {}
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

        self._carregar_variantes(pastas)
        self._carregar_animacoes(pastas)

    def _carregar_animacoes(self, pastas):
        """Carrega assets/anim/<emoção>/ (anim.json + quadros), uma animação por emoção.

        Layout antigo (assets/anim/anim.json solto, só da "serio") continua funcionando. Emoção sem pasta, ou com
        quadro faltando, fica só com a imagem estática; as demais seguem animadas.
        """
        for pasta in pastas:
            raiz = pasta / "anim"
            if not raiz.is_dir():
                continue
            manifestos: Dict[str, Dict] = {}
            for expr in AliciaExpression:
                nome = expr.value
                manifesto = raiz / nome / "anim.json"
                if not manifesto.exists():
                    antigo = raiz / "anim.json"                # layout antigo: um conjunto só, da "serio"
                    if nome == "serio" and antigo.exists():
                        manifesto = antigo
                    else:
                        continue
                try:
                    dados = json.loads(manifesto.read_text(encoding="utf-8"))
                    chaves = (
                        [f"idle_{i:02d}" for i in range(1, int(dados["idle"]["frames"]) + 1)]
                        + [f"blink_{i}" for i in range(1, int(dados["blink"]["frames"]) + 1)]
                        + [f"talk_{i}" for i in range(1, int(dados["talk"]["frames"]) + 1)]
                    )
                    quadros: Dict[str, QPixmap] = {}
                    for chave in chaves:
                        pix = QPixmap(str(manifesto.parent / f"{chave}.png")).scaled(
                            500, 500,
                            Qt.AspectRatioMode.KeepAspectRatio,
                            Qt.TransformationMode.SmoothTransformation
                        )
                        if pix.isNull():
                            raise FileNotFoundError(chave)
                        quadros[f"{nome}/{chave}"] = pix
                    dados["expression"] = nome
                    AvatarAnimator(dados)                      # valida o manifesto antes de aceitar a emoção
                    self.anim_pixmaps.update(quadros)
                    manifestos[nome] = dados
                except Exception as e:
                    logger.error(f"Animação da emoção '{nome}' desativada: {e}")
            if manifestos:
                self.anim_manifests = manifestos
                self.anim_manifest = manifestos.get("serio") or next(iter(manifestos.values()))
                self.animator = AvatarAnimatorSet(manifestos)
                logger.info(f"🎞️ Animações do avatar carregadas ({len(self.anim_pixmaps)} quadros, "
                            f"{len(manifestos)} emoções: {', '.join(manifestos)}).")
                return

    def _carregar_variantes(self, pastas):
        """Imagens extras da mesma expressão (ex.: feliz_2.png, feliz_3.png) alternam sozinhas."""
        for expr in AliciaExpression:
            extras = []
            for n in range(2, 6):
                for pasta in pastas:
                    caminho = pasta / f"{expr.value}_{n}.png"
                    if caminho.exists():
                        pix = QPixmap(str(caminho)).scaled(
                            500, 500,
                            Qt.AspectRatioMode.KeepAspectRatio,
                            Qt.TransformationMode.SmoothTransformation
                        )
                        if not pix.isNull():
                            extras.append(pix)
                        break
            if extras:
                self.variantes[expr] = extras

    def abrir_poker(self):
        """Abre a mesa de pôquer (sempre na thread principal). Esconde o avatar flutuante enquanto joga."""
        try:
            if self.poker is not None and self.poker.isVisible():
                self.poker.raise_()
                self.poker.activateWindow()
                return
            from .poker_window import PokerWindow
            self.poker = PokerWindow(self.core, self)
            self.poker.closed.connect(self._poker_fechou)
            self.core.poker_active = True
            self.hide()
            self.poker.show()
            self.poker.raise_()
            self.poker.activateWindow()
            logger.info("🃏 Minigame de pôquer aberto.")
        except Exception as e:
            logger.exception(f"Não consegui abrir o pôquer: {e}")
            self.core.poker_active = False
            self.core.poker_chat_handler = None
            self.poker = None
            self.show()

    def _poker_fechou(self):
        self.core.poker_active = False
        self.core.poker_chat_handler = None
        self.poker = None
        self.show()
        logger.info("🃏 Pôquer fechado; Alicia de volta.")

    def _speaking_now(self) -> bool:
        tts = getattr(self.core, "tts", None)
        if getattr(tts, "enabled", False):
            return bool(getattr(tts, "audio_playing", False))      # a boca acompanha o SOM, não a geração da fala
        return False

    def atualizar_visual(self):
        agora = time.monotonic()
        expr_alvo = AliciaExpression.SERIO if self.core.modo_descanso else self.core.current_expression

        # Depois de alguns segundos com uma emoção, ela relaxa para o rosto neutro (que respira e pisca).
        if expr_alvo != self._last_expr_seen:
            self._last_expr_seen = expr_alvo
            self._expr_since = agora
        if (
            self.animator is not None
            and expr_alvo != AliciaExpression.SERIO
            and agora - self._expr_since > getattr(getattr(self.core, "config", None), "avatar_relax_seconds", 8.0)
            and not self._speaking_now()
        ):
            self.core.current_expression = AliciaExpression.SERIO
            expr_alvo = AliciaExpression.SERIO

        if self.animator is not None:
            # resposta só em texto (sem voz): mexe a boca proporcional ao tamanho da mensagem
            msg = getattr(self.core, "last_alicia_message", "") or ""
            if msg and msg != self._last_msg_seen:
                self._last_msg_seen = msg
                if not getattr(getattr(self.core, "tts", None), "enabled", False):
                    self.animator.talk_for(min(6.0, 0.4 + len(msg) * 0.055), agora)
            try:
                chave = self.animator.update(expr_alvo.value, self._speaking_now(), agora)
            except Exception as e:
                logger.error(f"Animação do avatar falhou e foi desligada: {e}")
                self.animator = None
                chave = None
            if chave is not None:
                if chave != self._last_anim_key:
                    pix = self.anim_pixmaps.get(chave)
                    if pix:
                        self.label_avatar.setPixmap(pix)
                    self._last_anim_key = chave
                self.estado_atual = None          # ao sair da animação, a imagem estática volta a ser aplicada
                return
            self._last_anim_key = None

        if expr_alvo != self.estado_atual:
            self.estado_atual = expr_alvo
            pixmap = self.pixmaps.get(self.estado_atual)
            extras = self.variantes.get(self.estado_atual)
            if extras:
                pixmap = random.choice([p for p in [pixmap] + extras if p])
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
        
        if self.core.modo_descanso:
            status_txt = 'Em descanso'
        elif self.core.modo_gamer:
            jogo = self.core.gamer_current_game
            status_txt = f'Modo Gamer — {jogo}' if jogo else 'Modo Gamer (procurando jogo...)'
        else:
            status_txt = 'Ativa'
        status_action = menu.addAction(f'Status: {status_txt}')
        status_action.setEnabled(False)
        
        # Ativado/desativado automaticamente quando um jogo é detectado
        # pelo título da janela ativa. A opção aqui serve como override
        # manual (útil se a detecção automática não pegar o seu jogo).
        watch_text = '🎮 Sair do Modo Gamer' if self.core.modo_gamer else '🎮 Ativar Modo Gamer'
        watch_action = menu.addAction(watch_text)
        watch_action.triggered.connect(
            (
                lambda: self.core.sair_modo_gamer(manual=True)
            )
            if self.core.modo_gamer
            else (
                lambda: self.core.entrar_modo_gamer(manual=True)
            )
        )

        screen_action = menu.addAction('Observação de Tela: ' + ('Ligada' if self.core.screen_watch_enabled else 'Desligada'))
        screen_action.triggered.connect(lambda: setattr(self.core, 'screen_watch_enabled', not self.core.screen_watch_enabled))
        
        menu.addSeparator()

        acao_poker = menu.addAction("🃏 Jogar pôquer com a Alicia")
        acao_poker.triggered.connect(self.abrir_poker)
        menu.addSeparator()

        # Troca de personalidade: só muda o tom das próximas respostas;
        # o histórico e as memórias continuam como estão.
        try:
            from ..prompts import PromptEngine
            pers_menu = menu.addMenu("🎭 Personalidade")
            atual = self.core.get_personality()
            for chave, info in PromptEngine.PERSONALITIES.items():
                acao = pers_menu.addAction(info["label"])
                acao.setCheckable(True)
                acao.setChecked(chave == atual)
                acao.triggered.connect(lambda checked, k=chave: self.core.set_personality(k))
        except Exception as e:
            logger.error(f"Erro ao montar o menu de personalidade: {e}")

        menu.addSeparator()
        
        mic_menu = menu.addMenu("🎙️ Selecionar Microfone")
        default_mic_action = mic_menu.addAction("Padrão do Sistema")
        default_mic_action.triggered.connect(lambda: self.core.mudar_microfone(None))
        mic_atual = getattr(getattr(self.core, "config", None), "mic_index", None)
        if mic_atual is None:
            default_mic_action.setCheckable(True)
            default_mic_action.setChecked(True)

        if not HAS_MIC:
            aviso = mic_menu.addAction("⚠️ Microfone indisponível (veja o log)")
            aviso.setEnabled(False)
        elif HAS_SPEECH:
            try:
                mic_list = list_microphones()
                for idx, name in mic_list:
                    nome_curto = (name[:40] + '..') if len(name) > 40 else name
                    action = mic_menu.addAction(f"[{idx}] {nome_curto}")
                    action.triggered.connect(lambda checked, i=idx: self.core.mudar_microfone(i))
                    if mic_atual == idx:
                        action.setCheckable(True)
                        action.setChecked(True)
            except Exception as e:
                logger.warning(f"Não consegui listar os microfones: {e}")

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
