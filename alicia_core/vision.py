"""
Visão da Alicia: um único ponto de entrada com DOIS caminhos.

  1. Ollama local (preferido: grátis, privado, sem internet).
  2. API da NVIDIA com um modelo que aceite imagens (usado quando o Ollama não está disponível).
     O modelo é descoberto testando de verdade uma imagem pequena; o resultado fica em
     NVIDIA_VISION_MODEL no .env.

`generate()` recebe as mesmas opções no estilo Ollama que o código antigo usava.
Nunca levanta exceção: devolve None se nenhum caminho responder (o chamador mostra a mensagem).
"""

from __future__ import annotations

import base64
import io
import os
import time
from typing import Any, Dict, List, Optional

from .logging_setup import logger
from .nvidia_client import (
    BrainError, GOOD_FAMILIES, NvidiaClient,
)

VISION_PREFERRED = [
    "nvidia/nemotron-3.5-super-vl-120b-a12b",
    "moonshotai/kimi-k3",
    "z-ai/glm-5.3",
    "google/gemma-4-31b-it",
]
VISION_HINTS = ("vl", "vision", "omni", "gemma-4", "gemma-3", "kimi", "pixtral", "llava", "paligemma")
NOT_VISION = ("embed", "rerank", "guard", "safety", "reward", "translate", "riva", "parse", "retriev")


def _probe_image_b64() -> str:
    """PNG 96x96 com um quadrado vermelho (para provar que o modelo realmente enxerga imagens)."""
    from PIL import Image
    img = Image.new("RGB", (96, 96), (255, 255, 255))
    for x in range(20, 76):
        for y in range(20, 76):
            img.putpixel((x, y), (220, 20, 20))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


def _data_url(b64: str) -> str:
    head = b64[:8]
    mime = "image/png" if head.startswith("iVBOR") else "image/webp" if head.startswith("UklG") else "image/jpeg"
    return f"data:{mime};base64,{b64}"


class VisionBackend:
    def __init__(self, ollama, config, nvidia: NvidiaClient):
        self.ollama = ollama
        self.config = config
        self.nvidia = nvidia
        self.use_ollama = False
        self.remote: Optional[NvidiaClient] = None     # cliente já validado com modelo de visão
        self._discovery_done_at = 0.0
        env_model = os.environ.get("NVIDIA_VISION_MODEL", "").strip()
        self._env_model = env_model
        self.on_model_change = None

    # ------------------------------------------------------------------
    @property
    def backend(self) -> Optional[str]:
        if self.use_ollama:
            return "ollama"
        if self.remote is not None:
            return "nvidia"
        return None

    @property
    def available(self) -> bool:
        return self.backend is not None

    def _new_remote(self, model: str, timeout: float = 90.0) -> NvidiaClient:
        return NvidiaClient(self.nvidia._api_key, model, self.nvidia.base_url, timeout=timeout, max_retries=1, backoff=1.0)

    # ------------------------------------------------------------------
    def _messages(self, prompt: str, image_b64: str) -> List[Dict[str, Any]]:
        return [{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": _data_url(image_b64)}},
        ]}]

    def candidates(self, available: List[str]) -> List[str]:
        out: List[str] = []
        def add(m):
            if m and m not in out:
                out.append(m)
        add(self._env_model)
        for m in VISION_PREFERRED:
            add(m)
        extra = [m for m in available
                 if any(h in m.lower() for h in VISION_HINTS) and not any(n in m.lower() for n in NOT_VISION)]
        extra.sort(key=lambda m: m.lower(), reverse=True)
        for m in extra[:10]:
            add(m)
        return out

    def discover_remote(self, time_budget: float = 90.0) -> bool:
        """Testa modelos de visão da NVIDIA com uma imagem real. Guarda o primeiro que responder."""
        if not self.nvidia.has_key:
            return False
        self._discovery_done_at = time.time()
        try:
            models = self.nvidia.list_models()
        except BrainError as e:
            logger.warning(f"Visão (NVIDIA): não consegui listar modelos ({e.kind}).")
            return False
        probe = _probe_image_b64()
        t0 = time.time()
        for m in self.candidates(models):
            if time.time() - t0 > time_budget:
                break
            client = self._new_remote(m, timeout=40.0)
            try:
                answer = client.chat(self._messages("Que cor é o quadrado? Responda em uma palavra.", probe), max_tokens=64)
            except BrainError as e:
                if e.kind in ("auth", "network"):
                    return False
                logger.info(f"Visão (NVIDIA): {m} -> {e.kind}")
                continue
            logger.info(f"Visão (NVIDIA): {m} respondeu ao teste: {answer[:40]!r}")
            self.remote = self._new_remote(m)
            os.environ["NVIDIA_VISION_MODEL"] = m
            if self.on_model_change:
                try:
                    self.on_model_change(m)
                except Exception as e:
                    logger.warning(f"Não consegui gravar NVIDIA_VISION_MODEL: {e}")
            return True
        return False

    # ------------------------------------------------------------------
    def generate(self, prompt: str, image_b64: str, options: Optional[Dict] = None, timeout: int = 75) -> Optional[str]:
        options = options or {}
        if self.use_ollama:
            text = self.ollama.generate(model=self.config.vision_model, prompt=prompt, images=[image_b64],
                                        options=options, timeout=timeout)
            if text:
                return text
            logger.warning("Visão: o Ollama não respondeu; tentando a API da NVIDIA.")
        if self.remote is None and self.nvidia.has_key and time.time() - self._discovery_done_at > 1800:
            self.discover_remote(time_budget=60.0)
        if self.remote is not None:
            self.remote.timeout = float(timeout)
            try:
                return self.remote.chat(
                    self._messages(prompt, image_b64),
                    temperature=float(options.get("temperature", 0.75)),
                    top_p=float(options.get("top_p", 0.92)),
                    max_tokens=max(int(options.get("num_predict", 100)) * 2, 160),
                )
            except BrainError as e:
                logger.error(f"Visão (NVIDIA) falhou: {e.kind}")
                if e.kind in ("not_found", "gone"):
                    self.remote = None
                return None
        return None
