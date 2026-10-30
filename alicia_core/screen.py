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
