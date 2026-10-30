"""
Máquina de estados da animação do avatar (SEM Qt: pura lógica, por isso é testável).

Conjuntos de quadros (assets/anim/<emoção>/anim.json, uma pasta por emoção):
  idle_NN   respiração + balanço do cabelo, em loop
  blink_N   piscar (quadro 1 = olhos abertos; usamos 2, 3, 4)
  talk_N    formas da boca (1 fechada, 2 entreaberta, 3 aberta, 4 arredondada)

Regras:
  * `AvatarAnimator` anima UMA expressão (a do manifesto); nas outras devolve None e a janela mostra a imagem
    estática. `AvatarAnimatorSet` junta um animador por emoção (feliz, serio, bravo, pensando, surpresa).
  * Falando: a boca alterna formas em ritmo irregular; ao terminar, fecha a boca e volta a respirar.
  * Parada: respira em loop e pisca em intervalos irregulares (2,5 a 6 s; às vezes pisca duas vezes).
"""

from __future__ import annotations

import random
import time
from typing import Dict, List, Optional, Tuple


class AvatarAnimator:
    BLINK_MIN, BLINK_MAX = 2.5, 6.0
    DOUBLE_BLINK_CHANCE = 0.15
    HOLD_MIN, HOLD_MAX = 0.09, 0.17          # quanto tempo cada forma de boca fica
    CLOSE_HOLD = 0.12                         # boca fechada ao terminar de falar

    def __init__(self, manifest: Dict, rng: Optional[random.Random] = None):
        self.expression: str = manifest.get("expression", "serio")
        self.idle_frames: int = int(manifest["idle"]["frames"])
        self.idle_fps: float = float(manifest["idle"].get("fps", 10))
        self.blink_seq: List[Tuple[int, float]] = [(int(i), float(d)) for i, d in manifest["blink"]["sequence"]]
        self.talk_frames: int = int(manifest["talk"]["frames"])
        self.rng = rng or random.Random()
        self.reset()

    # ------------------------------------------------------------------
    def reset(self):
        self.mode = "idle"
        self._idle_t0: Optional[float] = None
        self._next_blink: Optional[float] = None
        self._blink_i = 0
        self._blink_until = 0.0
        self._pending_second_blink = False
        self._mouth = 1
        self._mouth_until = 0.0
        self._close_until = 0.0
        self._talk_until = 0.0              # fala "de mentirinha" (resposta só em texto)

    def talk_for(self, seconds: float, now: Optional[float] = None):
        """Anima a boca por alguns segundos (usado quando a resposta é só texto, sem voz)."""
        now = time.monotonic() if now is None else now
        self._talk_until = max(self._talk_until, now + seconds)

    # ------------------------------------------------------------------
    def _pick_mouth(self) -> int:
        open_shapes = [i for i in range(2, self.talk_frames + 1)]
        if self._mouth != 1 and self.rng.random() < 0.35:
            return 1                                          # fecha entre as sílabas
        choices = [m for m in open_shapes if m != self._mouth] or open_shapes
        return self.rng.choice(choices)

    def _schedule_blink(self, now: float):
        self._next_blink = now + self.rng.uniform(self.BLINK_MIN, self.BLINK_MAX)

    def _enter_idle(self, now: float):
        self.mode = "idle"
        self._idle_t0 = now
        self._schedule_blink(now)

    # ------------------------------------------------------------------
    def update(self, expression: str, speaking: bool, now: Optional[float] = None) -> Optional[str]:
        """Devolve a chave do quadro ('idle_07', 'blink_2', 'talk_3') ou None se não há animação."""
        now = time.monotonic() if now is None else now
        if expression != self.expression:
            if self.mode != "idle" or self._idle_t0 is not None:
                self.reset()
            return None

        speaking = speaking or now < self._talk_until

        # ---- falando ----
        if speaking:
            if self.mode != "talk":
                self.mode = "talk"
                self._mouth = 1
                self._mouth_until = 0.0
            if now >= self._mouth_until:
                self._mouth = self._pick_mouth()
                self._mouth_until = now + self.rng.uniform(self.HOLD_MIN, self.HOLD_MAX)
            return f"talk_{self._mouth}"

        # ---- acabou de falar: fecha a boca um instante ----
        if self.mode == "talk":
            self.mode = "closing"
            self._close_until = now + self.CLOSE_HOLD
        if self.mode == "closing":
            if now < self._close_until:
                return "talk_1"
            self._enter_idle(now)

        # ---- piscando ----
        if self.mode == "blink":
            if now < self._blink_until:
                return f"blink_{self.blink_seq[self._blink_i][0]}"
            self._blink_i += 1
            if self._blink_i < len(self.blink_seq):
                self._blink_until = now + self.blink_seq[self._blink_i][1]
                return f"blink_{self.blink_seq[self._blink_i][0]}"
            self.mode = "idle"                                 # terminou a sequência de piscar
            if self._pending_second_blink:
                self._pending_second_blink = False
                self._next_blink = now + 0.12                  # pisca de novo logo em seguida
            else:
                self._schedule_blink(now)
            # cai para o idle abaixo

        # ---- respirando ----
        if self._idle_t0 is None:
            self._enter_idle(now)
        if self._next_blink is not None and now >= self._next_blink and self.mode == "idle":
            self._pending_second_blink = self.rng.random() < self.DOUBLE_BLINK_CHANCE
            self._start_blink(now)
            return f"blink_{self.blink_seq[0][0]}"
        idx = int((now - self._idle_t0) * self.idle_fps) % self.idle_frames
        return f"idle_{idx + 1:02d}"

    def _start_blink(self, now: float):
        self.mode = "blink"
        self._blink_i = 0
        self._blink_until = now + self.blink_seq[0][1]
        self._next_blink = None


class AvatarAnimatorSet:
    """Um AvatarAnimator por emoção, com a mesma interface (`update` / `talk_for`).

    As chaves devolvidas levam a emoção na frente ('feliz/idle_07', 'bravo/talk_3'), porque cada emoção tem os
    seus próprios quadros. Emoção sem animação -> None (a janela cai na imagem estática dela).
    """

    def __init__(self, manifests: Dict[str, Dict], rng: Optional[random.Random] = None):
        self.animators: Dict[str, AvatarAnimator] = {name: AvatarAnimator(m, rng) for name, m in manifests.items()}
        self._current: Optional[str] = None

    def talk_for(self, seconds: float, now: Optional[float] = None):
        for a in self.animators.values():
            a.talk_for(seconds, now)

    def update(self, expression: str, speaking: bool, now: Optional[float] = None) -> Optional[str]:
        now = time.monotonic() if now is None else now
        if expression != self._current:
            previous = self.animators.get(self._current) if self._current else None
            if previous is not None:
                previous.reset()                         # ao voltar a esta emoção, ela recomeça respirando
            self._current = expression
        animator = self.animators.get(expression)
        if animator is None:
            return None
        key = animator.update(expression, speaking, now)
        return f"{expression}/{key}" if key else None

