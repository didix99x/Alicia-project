"""
Captura de tela e cálculo de diferença perceptual entre frames.
"""

import base64
import io
import hashlib
from typing import Optional, Tuple

from .deps import Image, ImageChops, ImageGrab, ImageStat, HAS_MSS
from .logging_setup import logger

if HAS_MSS:
    from .deps import mss


class ScreenAnalyzerEngine:
    def __init__(self, max_dim: int = 1280, quality: int = 65):
        self.max_dim = max(320, int(max_dim))
        self.quality = max(40, min(90, int(quality)))
        self.last_capture_error = ""

    def _capture(self) -> Image.Image:
        errors = []
        if HAS_MSS:
            try:
                with mss.mss() as sct:
                    # monitors[0] representa toda a área virtual e funciona melhor em múltiplos monitores.
                    monitor = sct.monitors[0] if len(sct.monitors) > 1 else sct.monitors[1]
                    shot = sct.grab(monitor)
                    return Image.frombytes("RGB", shot.size, shot.rgb)
            except Exception as e:
                errors.append(f"mss: {e}")
        try:
            return ImageGrab.grab(all_screens=True).convert("RGB")
        except TypeError:
            try:
                return ImageGrab.grab().convert("RGB")
            except Exception as e:
                errors.append(f"ImageGrab: {e}")
        except Exception as e:
            errors.append(f"ImageGrab: {e}")
        raise RuntimeError(" | ".join(errors) or "Nenhum backend de captura disponível")

    def capture_and_encode(self) -> Tuple[Optional[str], Optional[Image.Image], str]:
        try:
            raw_img = self._capture()
            if raw_img is None or raw_img.width < 2 or raw_img.height < 2:
                raise RuntimeError("captura vazia ou inválida")
            w, h = raw_img.size
            if max(w, h) > self.max_dim:
                scale = self.max_dim / float(max(w, h))
                raw_img = raw_img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.Resampling.LANCZOS)
            processed_img = raw_img.convert("RGB")
            buffer = io.BytesIO()
            processed_img.save(buffer, format="JPEG", quality=self.quality, optimize=False)
            img_bytes = buffer.getvalue()
            if len(img_bytes) < 128:
                raise RuntimeError("JPEG gerado está vazio")
            self.last_capture_error = ""
            return base64.b64encode(img_bytes).decode("ascii"), processed_img, hashlib.sha256(img_bytes).hexdigest()
        except Exception as e:
            self.last_capture_error = str(e)
            logger.exception(f"Erro na captura de tela: {e}")
            return None, None, ""

    @staticmethod
    def calculate_perceptual_difference(img1: Image.Image, img2: Image.Image) -> float:
        if img1 is None or img2 is None:
            return 100.0
        try:
            i1 = img1.convert("RGB").resize((256, 256))
            i2 = img2.convert("RGB").resize((256, 256))
            stat = ImageStat.Stat(ImageChops.difference(i1, i2))
            return round((sum(stat.mean) / len(stat.mean) / 255.0) * 100.0, 2)
        except Exception:
            return 100.0

    # ------------------------------------------------------------------
    # CAPTURA PARA A OBSERVADORA (resolução total, monitor certo)
    # ------------------------------------------------------------------
    @staticmethod
    def pick_monitor(monitors: list, mode: str = "active", window_rect=None) -> dict:
        """Escolhe o monitor a capturar. `monitors` segue o formato do mss (índice 0 = área virtual de todos).

        "active"  -> o monitor onde está o centro da janela ativa (cai para o primário se não souber);
        "primary" -> o primeiro monitor físico;  "all" -> a área virtual inteira.
        Capturar tudo e reduzir para 1280 px (o que o laço antigo fazia) deixa o texto ilegível em PCs com 2+ monitores.
        """
        physical = monitors[1:] if len(monitors) > 1 else monitors[:1]
        if mode == "all" and len(monitors) > 1:
            return monitors[0]
        if mode == "active" and window_rect and physical:
            left, top, right, bottom = window_rect
            cx, cy = (left + right) // 2, (top + bottom) // 2
            for m in physical:
                if m["left"] <= cx < m["left"] + m["width"] and m["top"] <= cy < m["top"] + m["height"]:
                    return m
        return physical[0]

    def capture_raw(self, mode: str = "active", window_rect=None) -> Image.Image:
        """Captura SEM reduzir (o OCR precisa da resolução real). Levanta RuntimeError com a causa se falhar."""
        errors = []
        if HAS_MSS:
            try:
                with mss.mss() as sct:
                    monitor = self.pick_monitor(sct.monitors, mode, window_rect)
                    shot = sct.grab(monitor)
                    img = Image.frombytes("RGB", shot.size, shot.rgb)
                    self.last_capture_error = ""
                    return img
            except Exception as e:
                errors.append(f"mss: {e}")
        try:
            try:
                img = ImageGrab.grab(all_screens=(mode == "all")).convert("RGB")
            except TypeError:
                img = ImageGrab.grab().convert("RGB")
            if img.width < 2 or img.height < 2:
                raise RuntimeError("captura vazia")
            self.last_capture_error = ""
            return img
        except Exception as e:
            errors.append(f"ImageGrab: {e}")
        try:                                            # último recurso: o método de captura antigo
            img = self._capture()
            if img is not None and img.width >= 2 and img.height >= 2:
                self.last_capture_error = ""
                return img.convert("RGB")
        except Exception as e:
            errors.append(f"captura clássica: {e}")
        self.last_capture_error = " | ".join(errors) or "Nenhum backend de captura disponível"
        raise RuntimeError(self.last_capture_error)
