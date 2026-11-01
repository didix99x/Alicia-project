# -*- coding: utf-8 -*-
"""
Modelo de visão LOCAL (VLM) da Alicia — roda 100% no seu computador via llama-cpp-python (llama.cpp).

  * Nada é enviado para a internet: a imagem entra no modelo, na memória, e sai um texto.
  * Opcional e desligado por padrão. Ative com `Iniciar_Alicia.bat --vision-local` (explica, pede autorização,
    instala llama-cpp-python na .venv e baixa o modelo para models/vision/).
  * O modelo só é carregado na primeira análise e é descarregado depois de um tempo parado (local_vlm_idle_unload_s).
  * Funciona com modelos GGUF de visão + o arquivo "mmproj" ao lado (Qwen2.5-VL, MiniCPM-V, Moondream, LLaVA).

Este módulo só usa a biblioteca padrão no import; llama_cpp e PIL são importados na hora de usar.
"""

from __future__ import annotations

import base64
import importlib.util
import io
import pathlib
import sys
import threading
import time
from typing import Dict, Optional, Tuple

from .logging_setup import logger

APP_DIR = pathlib.Path(sys.modules[__name__].__file__).resolve().parent.parent

# nome do arquivo (minúsculo) -> classe de chat do llama-cpp-python que sabe falar com aquele modelo
HANDLER_HINTS = (
    (("qwen2.5-vl", "qwen2_5-vl", "qwen25vl", "qwen2.5vl"), "Qwen25VLChatHandler"),
    (("minicpm",), "MiniCPMv26ChatHandler"),
    (("moondream",), "MoondreamChatHandler"),
    (("llava-v1.6", "llava-1.6", "llava16"), "Llava16ChatHandler"),
    (("llava",), "Llava15ChatHandler"),
)


def _data_url(b64: str) -> str:
    mime = "image/png" if b64.startswith("iVBOR") else "image/webp" if b64.startswith("UklG") else "image/jpeg"
    return f"data:{mime};base64,{b64}"


class LocalVlm:
    def __init__(self, config):
        self.config = config
        self._llm = None
        self._lock = threading.Lock()
        self._last_used = 0.0
        self.error = ""
        self.handler_name = ""

    # ------------------------------------------------------------------ arquivos e estado
    @property
    def model_dir(self) -> pathlib.Path:
        d = pathlib.Path(str(getattr(self.config, "local_vlm_dir", "models/vision")))
        return d if d.is_absolute() else APP_DIR / d

    def model_files(self) -> Optional[Tuple[pathlib.Path, pathlib.Path]]:
        """(modelo .gguf, projetor mmproj .gguf) encontrados na pasta do modelo, ou None."""
        d = self.model_dir
        if not d.is_dir():
            return None
        ggufs = sorted(p for p in d.glob("*.gguf") if not p.name.endswith(".part"))
        mm = [p for p in ggufs if "mmproj" in p.name.lower()]
        main = [p for p in ggufs if "mmproj" not in p.name.lower()]
        if not main or not mm:
            return None
        return main[0], mm[0]

    @staticmethod
    def library_present() -> bool:
        if "llama_cpp" in sys.modules:
            return True
        try:
            return importlib.util.find_spec("llama_cpp") is not None
        except Exception:
            return False

    @property
    def enabled(self) -> bool:
        return bool(getattr(self.config, "local_vlm_enabled", False))

    @property
    def available(self) -> bool:
        return self.enabled and self.library_present() and self.model_files() is not None

    def status(self) -> str:
        """Frase simples do que falta (vazio = pronto)."""
        if not self.enabled:
            return "desligado"
        if not self.library_present():
            return "falta a biblioteca llama-cpp-python (rode Iniciar_Alicia.bat --vision-local)"
        if self.model_files() is None:
            return f"faltam os arquivos do modelo em {self.model_dir} (rode Iniciar_Alicia.bat --vision-local)"
        return ""

    # ------------------------------------------------------------------ carregar / descarregar
    def _pick_handler(self, llama_chat_format, main: pathlib.Path):
        name = main.name.lower()
        for hints, cls_name in HANDLER_HINTS:
            if any(h in name for h in hints) and hasattr(llama_chat_format, cls_name):
                return cls_name, getattr(llama_chat_format, cls_name)
        for cls_name in ("Qwen25VLChatHandler", "Llava16ChatHandler", "Llava15ChatHandler"):
            if hasattr(llama_chat_format, cls_name):
                return cls_name, getattr(llama_chat_format, cls_name)
        raise RuntimeError("esta versão do llama-cpp-python não tem um manipulador de visão")

    def _load(self):
        if self._llm is not None:
            return self._llm
        files = self.model_files()
        if files is None:
            raise RuntimeError("arquivos do modelo não encontrados")
        main, mmproj = files
        import llama_cpp
        from llama_cpp import llama_chat_format
        self.handler_name, cls = self._pick_handler(llama_chat_format, main)
        try:
            handler = cls(clip_model_path=str(mmproj), verbose=False)
        except TypeError:                                    # builds mais novas chamam o argumento de mmproj_path
            handler = cls(mmproj_path=str(mmproj), verbose=False)
        threads = int(getattr(self.config, "local_vlm_threads", 0) or 0)
        kwargs = dict(model_path=str(main), chat_handler=handler, n_ctx=int(getattr(self.config, "local_vlm_n_ctx", 4096)),
                      verbose=False)
        if threads > 0:
            kwargs["n_threads"] = threads
        t0 = time.time()
        self._llm = llama_cpp.Llama(**kwargs)
        logger.info(f"👁️ Modelo de visão local carregado ({main.name}, {self.handler_name}) em {time.time() - t0:.1f}s.")
        return self._llm

    def unload(self):
        with self._lock:
            self._llm = None
            self.error = ""

    def maybe_unload_idle(self, now: Optional[float] = None):
        """Libera a memória (alguns GB) quando o modelo está parado há muito tempo."""
        now = now if now is not None else time.time()
        idle = float(getattr(self.config, "local_vlm_idle_unload_s", 300.0))
        if self._llm is not None and self._last_used and now - self._last_used > idle and not self._lock.locked():
            logger.info("👁️ Modelo de visão local descarregado da memória (parado).")
            self.unload()

    # ------------------------------------------------------------------ uso
    def generate(self, prompt: str, image_b64: str, options: Optional[Dict] = None, timeout: int = 75) -> Optional[str]:
        """Descreve a imagem. None se não há modelo, se ele está ocupado ou se passou do tempo. Nunca levanta exceção."""
        if not self.available:
            return None
        options = options or {}
        if not self._lock.acquire(blocking=False):
            return None                                       # já está analisando outra imagem

        box: Dict[str, object] = {}

        def work():
            try:
                llm = self._load()
                out = llm.create_chat_completion(
                    messages=[{"role": "user", "content": [
                        {"type": "image_url", "image_url": {"url": _data_url(image_b64)}},
                        {"type": "text", "text": prompt},
                    ]}],
                    temperature=float(options.get("temperature", 0.7)),
                    top_p=float(options.get("top_p", 0.92)),
                    max_tokens=int(options.get("max_tokens", 100)),
                )
                box["text"] = (out["choices"][0]["message"]["content"] or "").strip()
            except BaseException as e:                        # inclui falhas nativas reportadas como exceção
                box["error"] = f"{type(e).__name__}: {e}"
            finally:
                self._last_used = time.time()
                self._lock.release()

        t = threading.Thread(target=work, daemon=True, name="AliciaLocalVLM")
        t.start()
        t.join(timeout=float(timeout))
        if t.is_alive():
            logger.warning("Visão local: passou do tempo; a resposta será descartada.")
            return None
        if "error" in box:
            self.error = str(box["error"])[:300]
            logger.error(f"Visão local falhou: {self.error}")
            return None
        self.error = ""
        return box.get("text") or None

    def selftest(self) -> Tuple[bool, str]:
        """Teste real e curto: mostra uma imagem desenhada na memória e confere se o modelo lê o texto dela."""
        if not self.available:
            return False, self.status() or "indisponível"
        from PIL import Image, ImageDraw, ImageFont
        img = Image.new("RGB", (448, 160), (255, 255, 255))
        try:
            font = ImageFont.load_default(size=56)
        except TypeError:
            font = ImageFont.load_default()
        ImageDraw.Draw(img).text((14, 40), "ALICIA 2024", fill=(0, 0, 0), font=font)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        t0 = time.time()
        text = self.generate("Que texto está escrito na imagem? Responda só com o texto.",
                             base64.b64encode(buf.getvalue()).decode(), {"max_tokens": 24, "temperature": 0.0}, timeout=240)
        if text is None:
            return False, self.error or "o modelo não respondeu a tempo"
        ok = "ALICIA" in text.upper().replace(" ", "") or "2024" in text
        return ok, f"{self.handler_name}: respondeu em {time.time() - t0:.0f}s ({'leu o texto de teste' if ok else 'não leu o texto de teste'})"
