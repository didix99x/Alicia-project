"""
Janela do minigame de pôquer (Five-Card Draw contra a Alicia).

Só desenha e repassa cliques: TODAS as regras, a IA e as falas estão em alicia_core/poker/ (testadas sem janela).
A Alicia aparece do outro lado da mesa usando as MESMAS imagens e a mesma animação do avatar principal
(respira, pisca e mexe a boca enquanto fala).
"""

from __future__ import annotations

import inspect
import threading
import time
from typing import Dict, List, Optional

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QListWidget, QPushButton, QSizePolicy, QSpinBox, QVBoxLayout, QWidget,
)

from ..enums import AliciaExpression
from ..logging_setup import logger
from ..poker.cards import Card
from ..poker.controller import DbStatsStore, PokerController
from ..poker.engine import ALICIA, HUMAN
from ..poker.reactions import Reaction
from ..poker.talk import EXPRESSION_TO_STATE, PokerTalk
from .avatar_animator import AvatarAnimator, AvatarAnimatorSet

AVATAR_HEIGHT = 300
ERRORS: List[str] = []        # erros de interface engolidos pelo protetor (o teste completo confere que está vazio)

STYLE = """
QWidget#pokerRoot { background: #0b0810; }
QLabel { color: #d9cbe9; font-family: Georgia, 'Times New Roman', serif; }
QFrame#table {
    border: 2px solid #4a2f66; border-radius: 28px;
    background: qradialgradient(cx:0.5, cy:0.55, radius:0.95, fx:0.5, fy:0.5, stop:0 #2b1e3a, stop:0.75 #15101d, stop:1 #0d0912);
}
QLabel#title { font-size: 20px; color: #e8d8ff; font-weight: bold; }
QLabel#phase { font-size: 17px; color: #ffd24a; font-weight: bold; }
QLabel#chips { font-size: 17px; color: #f1e4ff; font-weight: bold; padding: 4px 10px; border: 1px solid #5a3d7a; border-radius: 12px; background: #1b1326; }
QLabel#pot { font-size: 24px; color: #ffd24a; font-weight: bold; }
QLabel#potInfo { font-size: 13px; color: #bfa9d6; }
QLabel#bubble { font-size: 15px; color: #f4ecff; background: #231733; border: 2px solid #6d4a99; border-radius: 16px; padding: 12px 16px; }
QLabel#banner { font-size: 17px; font-weight: bold; color: #ffffff; background: #3a1d52; border: 2px solid #ffd24a; border-radius: 14px; padding: 8px 14px; }
QLabel#status { font-size: 13px; color: #a995c2; font-style: italic; }
QListWidget { background: #120c1a; color: #cdbfe0; border: 1px solid #3d2a55; border-radius: 10px; font-size: 12px; }
QPushButton { font-family: Georgia, serif; font-size: 15px; color: #f1e4ff; background: #2a1b3d; border: 2px solid #6d4a99; border-radius: 12px; padding: 8px 16px; }
QPushButton:hover { background: #3b2756; }
QPushButton:disabled { color: #6e6180; background: #17111f; border: 2px solid #2d2140; }
QPushButton#danger { border-color: #a33a52; }
QPushButton#primary { background: #5a2f86; border-color: #ffd24a; font-weight: bold; }
QSpinBox { font-size: 15px; color: #f1e4ff; background: #17111f; border: 2px solid #6d4a99; border-radius: 8px; padding: 4px; min-width: 80px; }
QSpinBox:disabled { color: #6e6180; border-color: #2d2140; }
"""


def card_style(face: bool, red: bool = False, selected: bool = False, win: bool = False, dim: bool = False) -> str:
    if not face:       # verso da carta
        return ("QPushButton { background: #2a1540; border: 2px solid #8a68c8; border-radius: 10px; color: #8a68c8; font-size: 34px; }")
    bg, fg, border, bw = "#f4ecff", ("#b3122a" if red else "#1a1024"), "#2b1b3d", 2
    if dim:
        bg, fg = "#cfc6dc", ("#8a5560" if red else "#5d5568")
    if selected:
        bg, border, bw = "#ffe3a3", "#ff9d00", 4
    if win:
        border, bw = "#ffd24a", 4
    return (f"QPushButton {{ background: {bg}; color: {fg}; border: {bw}px solid {border}; border-radius: 10px; "
            f"font-size: 24px; font-weight: bold; padding: 2px; }} QPushButton:disabled {{ color: {fg}; background: {bg}; border: {bw}px solid {border}; }}")


class CardButton(QPushButton):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(86, 124)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.card: Optional[Card] = None
        self.show_card(None)

    def show_card(self, card: Optional[Card], selected: bool = False, win: bool = False, dim: bool = False, back: bool = True):
        self.card = card
        if card is None:
            self.setText("♠" if back else "")
            self.setStyleSheet(card_style(False) if back else "QPushButton { background: transparent; border: none; }")
            return
        tag = "\nTROCAR" if selected else "\n "
        self.setText(f"{card.rank_char}\n{card.symbol}{tag}")
        self.setStyleSheet(card_style(True, card.is_red, selected, win, dim))


class PokerWindow(QWidget):
    closed = pyqtSignal()
    # Emitidos por QUALQUER thread (API / conversa); o Qt entrega na thread principal.
    narration_ready = pyqtSignal(int, str, str)       # (sequência, fala, rosto); fala vazia = usar a fala pronta
    chat_reply = pyqtSignal(str, str)                 # (fala, rosto) resposta à conversa do jogador

    def __init__(self, core, avatar_ui=None, parent=None):
        super().__init__(parent)
        self.core = core
        self.avatar_ui = avatar_ui
        self.setObjectName("pokerRoot")
        self.setWindowTitle("Pôquer com a Alicia — Five-Card Draw")
        self.resize(1060, 800)
        self.setMinimumSize(980, 760)
        self.setStyleSheet(STYLE)

        self.selected: set = set()
        self._last_history_len = -1
        self._last_snap: Dict = {}
        self._avatar_expr = "serio"
        self._expr_until = 0.0
        self._avatar_cache: Dict[str, object] = {}
        self._last_avatar_key = None
        self._bubble_until = 0.0
        self.animator = None
        manifests = getattr(avatar_ui, "anim_manifests", None) if avatar_ui else None
        manifest = getattr(avatar_ui, "anim_manifest", None) if avatar_ui else None
        if (manifests or manifest) and getattr(avatar_ui, "anim_pixmaps", None):
            try:
                self.animator = AvatarAnimatorSet(manifests) if manifests else AvatarAnimator(manifest)
            except Exception as e:
                logger.error(f"Pôquer: animação do avatar indisponível: {e}")

        self._build_ui()

        db = getattr(core, "db", None)
        self.controller = PokerController(
            scheduler=lambda ms, fn: QTimer.singleShot(ms, fn),
            personality_getter=lambda: core.get_personality() if hasattr(core, "get_personality") else "normal",
            name_getter=lambda: (db.get_user_name() if db else "") or "",
            stats_store=DbStatsStore(db) if db else None,
        )
        self.controller.ai.warm_up()
        self.controller.on_change = self._safe(self.refresh)
        self.controller.on_say = self._safe(self.say)
        self.controller.on_error = self._safe(self.show_error)

        # Conversa na mesa: falas geradas só com o contexto da partida. Sem chave/API, tudo segue com as falas prontas.
        self._narr_seq = 0
        self._narr_pending: Dict[int, Reaction] = {}
        self.talk: Optional[PokerTalk] = None
        client = getattr(core, "nvidia", None)
        if client is not None and getattr(client, "has_key", False):
            self.talk = PokerTalk(client, personality_getter=self.controller.personality, name_getter=self.controller.name)
            self.controller.narrator = self._narrate          # sem _safe: se falhar, o controlador usa a fala pronta
        self.narration_ready.connect(self._safe(self._on_narration))
        self.chat_reply.connect(self._safe(self.show_chat))
        try:
            core.poker_chat_handler = self.handle_user_chat      # a conversa (voz/teclado) vai para cá enquanto a mesa estiver aberta
        except Exception:
            pass

        self.anim_timer = QTimer(self)
        self.anim_timer.timeout.connect(self._safe(self._tick_avatar))
        self.anim_timer.start(40)
        self.controller.new_match()

    # ------------------------------------------------------------------ proteção: erro de interface nunca derruba a Alicia
    @staticmethod
    def _safe(fn):
        """Protege o callback e repassa só os argumentos que ele aceita (o sinal clicked envia `checked`)."""
        try:
            params = list(inspect.signature(fn).parameters.values())
            has_var = any(p.kind == p.VAR_POSITIONAL for p in params)
            max_args = len([p for p in params if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)])
        except (TypeError, ValueError):
            has_var, max_args = True, 0

        def wrapper(*a, **k):
            try:
                return fn(*(a if has_var else a[:max_args]), **k)
            except Exception as e:
                if len(ERRORS) < 50:
                    ERRORS.append(f"{type(e).__name__}: {e}")
                logger.exception("Erro na janela do pôquer (ignorado para não derrubar a Alicia)")
        return wrapper

    # ------------------------------------------------------------------ construção
    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(14, 10, 14, 12)
        root.setSpacing(8)

        top = QHBoxLayout()
        title = QLabel("♠  Pôquer com a Alicia  ♥  ·  Five-Card Draw")
        title.setObjectName("title")
        self.phase_label = QLabel("")
        self.phase_label.setObjectName("phase")
        self.btn_new_match = QPushButton("Nova partida")
        self.btn_exit = QPushButton("Sair do jogo")
        self.btn_exit.setObjectName("danger")
        top.addWidget(title)
        top.addStretch(1)
        top.addWidget(self.phase_label)
        top.addStretch(1)
        top.addWidget(self.btn_new_match)
        top.addWidget(self.btn_exit)
        root.addLayout(top)

        table = QFrame()
        table.setObjectName("table")
        t = QVBoxLayout(table)
        t.setContentsMargins(18, 12, 18, 12)
        t.setSpacing(6)

        # --- Alicia (topo): fichas à esquerda, avatar no centro, fala à direita
        arow = QHBoxLayout()
        left = QVBoxLayout()
        self.ai_name = QLabel("Alicia")
        self.ai_name.setObjectName("potInfo")
        self.ai_chips = QLabel("🪙 1000")
        self.ai_chips.setObjectName("chips")
        self.ai_status = QLabel("")
        self.ai_status.setObjectName("status")
        left.addWidget(self.ai_name)
        left.addWidget(self.ai_chips)
        left.addWidget(self.ai_status)
        left.addStretch(1)
        self.avatar = QLabel()
        self.avatar.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.avatar.setFixedHeight(AVATAR_HEIGHT)
        self.avatar.setMinimumWidth(340)
        self.bubble = QLabel("")
        self.bubble.setObjectName("bubble")
        self.bubble.setWordWrap(True)
        self.bubble.setMinimumWidth(260)
        self.bubble.setMaximumWidth(330)
        self.bubble.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.bubble.hide()
        right = QVBoxLayout()
        right.addStretch(1)
        right.addWidget(self.bubble)
        right.addStretch(2)
        arow.addLayout(left, 1)
        arow.addWidget(self.avatar, 2)
        arow.addLayout(right, 1)
        t.addLayout(arow)

        # --- cartas da Alicia
        self.ai_cards: List[CardButton] = [CardButton() for _ in range(5)]
        crow = QHBoxLayout()
        crow.addStretch(1)
        for c in self.ai_cards:
            c.setEnabled(False)
            crow.addWidget(c)
        crow.addStretch(1)
        t.addLayout(crow)

        # --- pote e histórico
        mid = QHBoxLayout()
        mid.addStretch(1)
        potbox = QVBoxLayout()
        self.pot_label = QLabel("Pote: 0")
        self.pot_label.setObjectName("pot")
        self.pot_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.pot_info = QLabel("")
        self.pot_info.setObjectName("potInfo")
        self.pot_info.setAlignment(Qt.AlignmentFlag.AlignCenter)
        potbox.addWidget(self.pot_label)
        potbox.addWidget(self.pot_info)
        mid.addLayout(potbox)
        mid.addStretch(1)
        self.history = QListWidget()
        self.history.setFixedWidth(260)
        self.history.setFixedHeight(92)
        mid.addWidget(self.history)
        t.addLayout(mid)

        self.banner = QLabel("")
        self.banner.setObjectName("banner")
        self.banner.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.banner.setWordWrap(True)
        self.banner.hide()
        t.addWidget(self.banner)

        # --- cartas do jogador
        self.my_cards: List[CardButton] = [CardButton() for _ in range(5)]
        prow = QHBoxLayout()
        prow.addStretch(1)
        for i, c in enumerate(self.my_cards):
            c.clicked.connect(self._safe(lambda checked=False, idx=i: self.toggle_card(idx)))
            prow.addWidget(c)
        prow.addStretch(1)
        t.addLayout(prow)

        info = QHBoxLayout()
        self.my_chips = QLabel("🪙 1000")
        self.my_chips.setObjectName("chips")
        self.my_name = QLabel("Você")
        self.my_name.setObjectName("potInfo")
        self.sel_label = QLabel("")
        self.sel_label.setObjectName("potInfo")
        info.addWidget(self.my_name)
        info.addWidget(self.my_chips)
        info.addStretch(1)
        info.addWidget(self.sel_label)
        t.addLayout(info)
        root.addWidget(table, 1)

        # --- ações
        bar = QHBoxLayout()
        bar.setSpacing(8)
        self.btn_fold = QPushButton("Desistir")
        self.btn_fold.setObjectName("danger")
        self.btn_call = QPushButton("Passar")
        self.spin = QSpinBox()
        self.spin.setRange(0, 0)
        self.spin.setSingleStep(10)
        self.btn_min = QPushButton("Mín")
        self.btn_half = QPushButton("½ pote")
        self.btn_pot = QPushButton("Pote")
        for b in (self.btn_min, self.btn_half, self.btn_pot):
            b.setStyleSheet("QPushButton { padding: 6px 10px; font-size: 13px; }")
        self.btn_bet = QPushButton("Apostar")
        self.btn_allin = QPushButton("All-in")
        self.btn_draw = QPushButton("Confirmar troca")
        self.btn_draw.setObjectName("primary")
        self.btn_next = QPushButton("Próxima mão")
        self.btn_next.setObjectName("primary")
        for w in (self.btn_fold, self.btn_call, self.spin, self.btn_min, self.btn_half, self.btn_pot, self.btn_bet,
                  self.btn_allin, self.btn_draw, self.btn_next):
            bar.addWidget(w)
        root.addLayout(bar)

        self.btn_exit.clicked.connect(self._safe(self.close))
        self.btn_new_match.clicked.connect(self._safe(self.on_new_match))
        self.btn_fold.clicked.connect(self._safe(lambda: self.controller.player_action("fold")))
        self.btn_call.clicked.connect(self._safe(self.on_call_check))
        self.btn_bet.clicked.connect(self._safe(self.on_bet_raise))
        self.btn_allin.clicked.connect(self._safe(lambda: self.controller.player_action("allin")))
        self.btn_draw.clicked.connect(self._safe(self.on_confirm_draw))
        self.btn_next.clicked.connect(self._safe(self.on_next_hand))
        self.btn_min.clicked.connect(self._safe(lambda: self._set_spin("min")))
        self.btn_half.clicked.connect(self._safe(lambda: self._set_spin("half")))
        self.btn_pot.clicked.connect(self._safe(lambda: self._set_spin("pot")))

    # ------------------------------------------------------------------ ações do jogador
    def on_new_match(self):
        self.selected.clear()
        self.banner.hide()
        self._narr_pending.clear()
        if self.talk is not None:
            self.talk.reset()
        self.controller.new_match()

    def on_next_hand(self):
        self.selected.clear()
        self.banner.hide()
        self.controller.start_hand()

    def on_call_check(self):
        legal = self._last_snap.get("legal", {})
        self.controller.player_action("check" if "check" in legal else "call")

    def on_bet_raise(self):
        legal = self._last_snap.get("legal", {})
        kind = "bet" if "bet" in legal else "raise"
        self.controller.player_action(kind, self.spin.value())

    def on_confirm_draw(self):
        if self.controller.player_draw(sorted(self.selected)):
            self.selected.clear()

    def toggle_card(self, idx: int):
        if not self._last_snap.get("player_draw_turn"):
            return
        if idx in self.selected:
            self.selected.discard(idx)
        else:
            self.selected.add(idx)
        self._refresh_selection()

    def _set_spin(self, which: str):
        legal = self._last_snap.get("legal", {})
        rng = legal.get("bet") or legal.get("raise")
        if not rng:
            return
        lo, hi = rng
        pot = self._last_snap.get("pot", 0)
        base = self._last_snap.get("current_bet", 0) if "raise" in legal else 0
        value = {"min": lo, "half": base + pot // 2, "pot": base + pot}[which]
        self.spin.setValue(max(lo, min(hi, int(value))))

    # ------------------------------------------------------------------ atualização da tela
    def refresh(self, snap: Dict):
        prev_cards = self._last_snap.get("my_cards")
        self._last_snap = snap
        if snap.get("my_cards") != prev_cards:
            self.selected.clear()
        stacks = snap.get("stacks", [0, 0])
        self.my_chips.setText(f"🪙 {stacks[HUMAN]}")
        self.ai_chips.setText(f"🪙 {stacks[ALICIA]}")
        n = snap.get("hands_played", 0)
        label = snap.get("phase_label", "")
        self.phase_label.setText(f"Mão {n + (0 if snap.get('summary') or snap.get('match_over') else 1)} · {label}")
        self.pot_label.setText(f"Pote: {snap.get('pot', 0)}")
        if snap.get("phase") in ("BET1", "BET2"):
            call = snap.get("to_call", 0)
            self.pot_info.setText(f"Aposta a pagar: {call}" if call else "Sem aposta aberta")
        elif snap.get("phase") == "DRAW":
            self.pot_info.setText("Troca de cartas (uma vez por mão)")
        else:
            self.pot_info.setText("")
        self.ai_status.setText("Alicia está pensando…" if snap.get("busy") else
                               ("Alicia desistiu" if snap.get("folded", [0, 0])[ALICIA] else ""))
        self._refresh_cards(snap)
        self._refresh_history(snap)
        self._refresh_buttons(snap)
        self._refresh_banner(snap)
        if snap.get("busy") and time.monotonic() > self._expr_until:
            self._avatar_expr = "pensando"

    def _refresh_cards(self, snap: Dict):
        summary = snap.get("summary") or {}
        win_idx = summary.get("win_idx") or [[], []]
        mine = snap.get("my_cards") or []
        draw_turn = bool(snap.get("player_draw_turn"))
        finished = bool(summary)
        for i, w in enumerate(self.my_cards):
            if i < len(mine):
                w.show_card(mine[i], selected=(i in self.selected and draw_turn), win=(i in win_idx[HUMAN]),
                            dim=(finished and bool(win_idx[HUMAN]) and i not in win_idx[HUMAN]))
            else:
                w.show_card(None, back=False)
            w.setEnabled(draw_turn)
        theirs = snap.get("alicia_cards")
        has_hand = snap.get("has_hand")
        for i, w in enumerate(self.ai_cards):
            if not has_hand:
                w.show_card(None, back=False)
            elif theirs:
                w.show_card(theirs[i], win=(i in win_idx[ALICIA]), dim=(bool(win_idx[ALICIA]) and i not in win_idx[ALICIA]))
            elif snap.get("folded", [0, 0])[ALICIA] and summary:
                w.show_card(None, back=True)
            else:
                w.show_card(None, back=True)
        self._refresh_selection()

    def _refresh_selection(self):
        snap = self._last_snap
        draw_turn = bool(snap.get("player_draw_turn"))
        mine = snap.get("my_cards") or []
        for i, w in enumerate(self.my_cards):
            if i < len(mine):
                summary = snap.get("summary") or {}
                win_idx = summary.get("win_idx") or [[], []]
                w.show_card(mine[i], selected=(i in self.selected and draw_turn), win=(i in win_idx[HUMAN]),
                            dim=(bool(summary) and bool(win_idx[HUMAN]) and i not in win_idx[HUMAN]))
        n = len(self.selected)
        if draw_turn:
            self.sel_label.setText(f"Selecionadas para trocar: {n}")
            self.btn_draw.setText(f"Confirmar troca ({n})" if n else "Ficar com as cinco")
        else:
            self.sel_label.setText("")
            self.btn_draw.setText("Confirmar troca")

    def _refresh_history(self, snap: Dict):
        hist = snap.get("history", [])
        if len(hist) == self._last_history_len and hist:
            return
        self._last_history_len = len(hist)
        self.history.clear()
        for line in hist:
            self.history.addItem(line)
        self.history.scrollToBottom()

    def _refresh_buttons(self, snap: Dict):
        legal = snap.get("legal", {}) if snap.get("player_turn") else {}
        self.btn_fold.setEnabled("fold" in legal)
        if "check" in legal:
            self.btn_call.setText("Passar")
        elif "call" in legal:
            allin_call = legal["call"] >= snap.get("stacks", [0, 0])[HUMAN]
            self.btn_call.setText(f"Pagar {legal['call']}" + (" (all-in)" if allin_call else ""))
        else:
            self.btn_call.setText("Passar")
        self.btn_call.setEnabled("check" in legal or "call" in legal)
        rng = legal.get("bet") or legal.get("raise")
        can_bet = bool(rng)
        self.spin.setEnabled(can_bet)
        for b in (self.btn_min, self.btn_half, self.btn_pot, self.btn_bet):
            b.setEnabled(can_bet)
        if can_bet:
            lo, hi = rng
            self.spin.setRange(lo, hi)
            self.btn_bet.setText("Apostar" if "bet" in legal else "Aumentar para")
            if not (lo <= self.spin.value() <= hi):
                self.spin.setValue(lo)
        else:
            self.spin.setRange(0, 0)
            self.btn_bet.setText("Apostar")
        self.btn_allin.setEnabled("allin" in legal)
        self.btn_draw.setEnabled(bool(snap.get("player_draw_turn")))
        self.btn_next.setVisible(bool(snap.get("can_next_hand")))
        self.btn_draw.setVisible(not snap.get("can_next_hand"))

    def _refresh_banner(self, snap: Dict):
        parts = []
        s = snap.get("summary")
        if s:
            parts.append(s["text"])
            if s.get("descriptions"):
                parts.append(f"Você: {s['descriptions'][HUMAN]}  ·  Alicia: {s['descriptions'][ALICIA]}")
            if s.get("refunded"):
                parts.append(f"({s['refunded']} fichas não igualadas voltaram ao dono)")
        info = snap.get("match_info")
        if snap.get("match_over") and info:
            won = info["winner"] == "player"
            parts.append("🏆 Você venceu a partida!" if won else "💀 A Alicia venceu a partida.")
            st = info.get("stats", {})
            parts.append(f"Placar de partidas — você {st.get('player_match_wins', 0)} x {st.get('alicia_match_wins', 0)} Alicia")
            parts.append("Clique em \"Nova partida\" para recomeçar com 1000 fichas para cada.")
        if parts:
            self.banner.setText("\n".join(parts))
            self.banner.show()
        elif not snap.get("has_hand"):
            self.banner.hide()
        else:
            self.banner.hide()

    def show_error(self, message: str):
        self.ai_status.setText(f"⚠ {message}")

    # ------------------------------------------------------------------ Alicia: fala, expressão e avatar
    # ---- reações a acontecimentos da partida (geradas em segundo plano; a fala pronta entra se a API demorar/falhar)
    NARRATION_TIMEOUT_MS = 5000

    def _narrate(self, event: str, facts: str, fallback: Reaction):
        if self.talk is None or not self.talk.available:
            self.say(fallback)
            return
        self._narr_seq += 1
        seq = self._narr_seq
        self._narr_pending = {seq: fallback}                  # uma reação nova torna a anterior obsoleta (não fala atrasada)
        context = self.controller.context_text
        threading.Thread(target=self._narrate_worker, args=(seq, facts, context, fallback.state), daemon=True, name="PokerNarrate").start()
        QTimer.singleShot(self.NARRATION_TIMEOUT_MS, self._safe(lambda: self._narration_timeout(seq)))

    def _narrate_worker(self, seq: int, facts: str, context: str, state: str):
        text, expr = "", ""
        try:
            out = self.talk.narrate(facts, context, state)
            if out:
                text, expr = out[0], out[1] or ""
        except Exception as e:
            logger.info(f"Pôquer: narração indisponível ({type(e).__name__}); usando a fala pronta.")
        try:
            self.narration_ready.emit(seq, text, expr)
        except RuntimeError:
            pass                                              # janela já fechada

    def _on_narration(self, seq: int, text: str, expr: str):
        fallback = self._narr_pending.pop(seq, None)
        if fallback is None:
            return                                            # expirou ou ficou obsoleta
        if text:
            state = EXPRESSION_TO_STATE.get(expr, fallback.state)
            self.say(Reaction(text, state))
        else:
            self.say(fallback)

    def _narration_timeout(self, seq: int):
        fallback = self._narr_pending.pop(seq, None)
        if fallback is not None:
            self.say(fallback)

    # ---- conversa do jogador com a Alicia durante a partida (chamado pela thread da conversa)
    def handle_user_chat(self, text: str):
        """Devolve (fala, AliciaExpression). Só usa o contexto da mesa; levanta BrainError se a API falhar."""
        if self.talk is None or not self.talk.available:
            return "Estou concentrada na partida. Sua vez de jogar.", AliciaExpression.SERIO
        reply, expr = self.talk.chat(text, self.controller.context_text)
        try:
            self.chat_reply.emit(reply, expr)
        except RuntimeError:
            pass
        try:
            face = AliciaExpression(expr)
        except ValueError:
            face = AliciaExpression.FELIZ
        return reply, face

    def show_chat(self, text: str, expr: str):
        """Mostra no balão uma resposta de conversa. A voz fica por conta de quem respondeu (voz/teclado)."""
        self._narr_pending.clear()                            # a conversa vale mais que uma reação pendente
        state = EXPRESSION_TO_STATE.get(expr, "neutral")
        self.say(Reaction(text, state), speak=False, remember=False)

    def say(self, reaction: Reaction, speak: bool = True, remember: bool = True):
        if remember and self.talk is not None:
            self.talk.remember_alicia(reaction.text)
        self.bubble.setText(reaction.text)
        self.bubble.show()
        self._bubble_until = time.monotonic() + max(4.0, min(9.0, 1.5 + len(reaction.text) * 0.06))
        self._avatar_expr = reaction.expression
        self._expr_until = time.monotonic() + 4.5
        if self.animator is not None:
            self.animator.talk_for(min(5.0, 0.4 + len(reaction.text) * 0.055))
        if speak:
            self._speak(reaction)

    def _speak(self, reaction: Reaction):
        tts = getattr(self.core, "tts", None)
        cfg = getattr(self.core, "config", None)
        if not tts or not getattr(tts, "enabled", False) or not getattr(cfg, "poker_voice", True):
            return
        if getattr(tts, "is_speaking", False):
            return                                              # não empilha falas: o balão já mostra o texto
        try:
            self.core._speak_async(reaction.text, AliciaExpression(reaction.expression))
        except Exception as e:
            logger.warning(f"Pôquer: voz indisponível ({e}); seguindo só com o balão de fala.")

    def _speaking_now(self) -> bool:
        tts = getattr(self.core, "tts", None)
        return bool(tts and getattr(tts, "enabled", False) and getattr(tts, "audio_playing", False))

    def _pixmap_for(self, key: str):
        if self.avatar_ui is None:
            return None
        if key in self._avatar_cache:
            return self._avatar_cache[key]
        pix = self.avatar_ui.anim_pixmaps.get(key) if ("/" in key or key[:4] in ("idle", "blin", "talk")) else None
        if pix is None:
            try:
                pix = self.avatar_ui.pixmaps.get(AliciaExpression(key))
            except ValueError:
                pix = None
        if pix is not None:
            pix = pix.scaledToHeight(AVATAR_HEIGHT, Qt.TransformationMode.SmoothTransformation)
        self._avatar_cache[key] = pix
        return pix

    def _tick_avatar(self):
        now = time.monotonic()
        if self.bubble.isVisible() and now > self._bubble_until:
            self.bubble.hide()
        if now > self._expr_until and not self._last_snap.get("busy"):
            self._avatar_expr = "serio"
        key = None
        if self.animator is not None:
            key = self.animator.update(self._avatar_expr, self._speaking_now(), now)
        if key is None:
            key = self._avatar_expr
        if key != self._last_avatar_key:
            pix = self._pixmap_for(key)
            if pix is not None:
                self.avatar.setPixmap(pix)
            self._last_avatar_key = key

    # ------------------------------------------------------------------ fechar
    def closeEvent(self, event):
        try:
            self.anim_timer.stop()
            self.controller.close()
            self._narr_pending.clear()
            if getattr(self.core, "poker_chat_handler", None) == self.handle_user_chat:
                self.core.poker_chat_handler = None
        finally:
            self.closed.emit()
            event.accept()
