"""
Modelo de locutor: transforma um trecho de voz num vetor (embedding) que representa QUEM fala.

Isto NÃO é reconhecimento de fala (STT): o texto continua vindo do SpeechRecognition. Aqui roda, localmente e em CPU,
um modelo ONNX do WeSpeaker (ResNet34, VoxCeleb, 256 dimensões, Apache-2.0). Nada do áudio sai da máquina.

Sem numpy/onnxruntime ou sem o arquivo do modelo, `SpeakerModel.available` é False e o resto da Alicia segue normal.
"""

from __future__ import annotations

import hashlib
import os
import pathlib
import threading
from typing import Optional

from .deps import HAS_ONNX, np
from .logging_setup import logger

MODEL_NAME = "wespeaker_en_voxceleb_resnet34_LM.onnx"
MODEL_SHA256 = "e9848563da86f263117134dfd7ad63c92355b37de492b55e325400c9d9c39012"
MODEL_URL = "https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/" + MODEL_NAME
MODEL_DIR = pathlib.Path("data") / "models"
EMBEDDING_VERSION = 1            # muda se o modelo/pré-processamento mudar: perfis antigos deixam de ser comparáveis

SAMPLE_RATE = 16000
N_MELS = 80
FRAME_LEN = 400                  # 25 ms
FRAME_SHIFT = 160                # 10 ms
N_FFT = 512
MIN_SPEECH_SECONDS = 1.0         # menos fala útil que isto não gera embedding confiável


# ----------------------------------------------------------------------------------------------------------------
# Áudio -> 16 kHz mono float (escala int16)
# ----------------------------------------------------------------------------------------------------------------
def pcm16_to_float(pcm: bytes):
    return np.frombuffer(pcm, dtype="<i2").astype(np.float32)


def resample(wave, src_rate: int, dst_rate: int = SAMPLE_RATE):
    """Reamostragem com filtro passa-baixa (sinc com janela) + interpolação. Suficiente para voz."""
    if src_rate == dst_rate or len(wave) == 0:
        return wave
    if src_rate > dst_rate:                       # evita aliasing antes de reduzir
        cutoff = 0.46 * dst_rate / src_rate       # fração de Nyquist da taxa de origem
        half = 32
        n = np.arange(-half, half + 1)
        h = np.sinc(2 * cutoff * n) * np.hamming(2 * half + 1)
        h /= h.sum()
        wave = np.convolve(wave, h, mode="same")
    n_out = int(len(wave) * dst_rate / src_rate)
    t = np.arange(n_out) * (src_rate / dst_rate)
    return np.interp(t, np.arange(len(wave)), wave).astype(np.float32)


def speech_only(wave, sr: int = SAMPLE_RATE):
    """Mantém só os quadros com voz (energia). O segmentador entrega pré-rolagem e silêncio no fim, que sujam o embedding."""
    frame = int(0.02 * sr)
    n = len(wave) // frame
    if n < 5:
        return wave
    frames = wave[: n * frame].reshape(n, frame)
    rms = np.sqrt((frames ** 2).mean(axis=1))
    ref = float(np.percentile(rms, 90))
    if ref < 3:                                   # praticamente silêncio digital
        return wave[:0]
    wave = wave * (2000.0 / ref)                  # normaliza o ganho (microfone baixo / Discord com volume diferente)
    frames = wave[: n * frame].reshape(n, frame)
    rms = rms * (2000.0 / ref)
    keep = rms > 240.0                            # 12% do nível de fala
    # suaviza: um quadro isolado entre dois de voz também fica (consoantes surdas)
    keep = keep | (np.roll(keep, 1) & np.roll(keep, -1))
    return frames[keep].reshape(-1).astype(np.float32)


# ----------------------------------------------------------------------------------------------------------------
# fbank estilo Kaldi/WeSpeaker (80 bandas, 25/10 ms, janela Hamming, pré-ênfase 0,97, sem dither)
# ----------------------------------------------------------------------------------------------------------------
_MEL_BANK = None


def _mel_bank():
    global _MEL_BANK
    if _MEL_BANK is None:
        mel = lambda f: 1127.0 * np.log1p(f / 700.0)
        low, high = 20.0, SAMPLE_RATE / 2.0
        bins = N_FFT // 2
        width = SAMPLE_RATE / N_FFT
        m_low, m_high = mel(low), mel(high)
        delta = (m_high - m_low) / (N_MELS + 1)
        mel_i = mel(width * np.arange(bins))
        bank = np.zeros((N_MELS, bins + 1), dtype=np.float32)       # +1: coluna de Nyquist, sempre zero (como no Kaldi)
        for b in range(N_MELS):
            left = m_low + b * delta
            center = left + delta
            right = center + delta
            up = (mel_i - left) / (center - left)
            down = (right - mel_i) / (right - center)
            bank[b, :bins] = np.maximum(0.0, np.minimum(up, down))
        _MEL_BANK = bank
    return _MEL_BANK


def fbank(wave):
    """wave: float32 mono 16 kHz em escala int16. Devolve (T, 80) com média removida por faixa (CMN) ou None se curto."""
    if len(wave) < FRAME_LEN:
        return None
    n = 1 + (len(wave) - FRAME_LEN) // FRAME_SHIFT
    idx = np.arange(FRAME_LEN)[None, :] + FRAME_SHIFT * np.arange(n)[:, None]
    frames = wave[idx].astype(np.float32)
    frames -= frames.mean(axis=1, keepdims=True)                              # remove DC
    prev = np.concatenate([frames[:, :1], frames[:, :-1]], axis=1)
    frames = frames - 0.97 * prev                                             # pré-ênfase
    frames *= np.hamming(FRAME_LEN).astype(np.float32)
    spec = np.abs(np.fft.rfft(frames, n=N_FFT, axis=1)) ** 2
    # einsum (laço em C, 1 thread) em vez de @/BLAS: o BLAS acorda todos os núcleos e eles ficam girando à toa
    # enquanto o ONNX roda, o que custava ~1 s de CPU por trecho de fala.
    mel_e = np.einsum("tf,mf->tm", spec.astype(np.float32), _mel_bank())
    feats = np.log(np.maximum(mel_e, np.finfo(np.float32).eps))
    return (feats - feats.mean(axis=0, keepdims=True)).astype(np.float32)


def cosine(a, b) -> float:
    a, b = np.asarray(a, dtype=np.float32), np.asarray(b, dtype=np.float32)
    d = float(np.linalg.norm(a) * np.linalg.norm(b))
    return float(a @ b / d) if d > 0 else 0.0


# ----------------------------------------------------------------------------------------------------------------
# Download verificado do modelo
# ----------------------------------------------------------------------------------------------------------------
def model_path(base: Optional[pathlib.Path] = None) -> pathlib.Path:
    return (base or MODEL_DIR) / MODEL_NAME


def _sha256(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def ensure_model(base: Optional[pathlib.Path] = None, download: bool = True) -> Optional[pathlib.Path]:
    """Caminho do modelo verificado (SHA-256). Baixa uma única vez do GitHub (~26 MB) se faltar e `download` for True."""
    path = model_path(base)
    if path.exists():
        if _sha256(path) == MODEL_SHA256:
            return path
        logger.warning("Modelo de locutor corrompido (checksum diferente); removendo.")
        path.unlink(missing_ok=True)
    if not download:
        return None
    try:
        import requests
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".part")
        logger.info("Baixando o modelo de reconhecimento de voz (~26 MB, uma vez)...")
        with requests.get(MODEL_URL, stream=True, timeout=60) as r:
            r.raise_for_status()
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(1 << 20):
                    f.write(chunk)
        if _sha256(tmp) != MODEL_SHA256:
            tmp.unlink(missing_ok=True)
            logger.error("Download do modelo de voz recusado: checksum não confere.")
            return None
        os.replace(tmp, path)
        return path
    except Exception as e:
        logger.error(f"Não consegui baixar o modelo de voz: {type(e).__name__}: {e}")
        return None


class SpeakerModel:
    """Embeddings de locutor. Carregamento preguiçoso; thread-safe (uma inferência por vez)."""

    def __init__(self, base_dir: Optional[pathlib.Path] = None):
        self.base_dir = base_dir
        self._session = None
        self._lock = threading.Lock()
        self._failed = False

    @property
    def available(self) -> bool:
        return HAS_ONNX and not self._failed and ensure_model(self.base_dir, download=False) is not None

    def _load(self):
        if self._session is None and not self._failed:
            path = ensure_model(self.base_dir, download=False)
            if path is None or not HAS_ONNX:
                self._failed = True
                return None
            try:
                import onnxruntime as ort              # importado só aqui: poupa RAM de quem não usa o reconhecimento
                opts = ort.SessionOptions()
                opts.intra_op_num_threads = 2          # não disputa CPU com a Alicia / o jogo
                opts.inter_op_num_threads = 1
                opts.add_session_config_entry("session.intra_op.allow_spinning", "0")   # sem threads girando à toa
                self._session = ort.InferenceSession(str(path), sess_options=opts, providers=["CPUExecutionProvider"])
                logger.info("Modelo de locutor carregado.")
            except Exception as e:
                logger.error(f"Modelo de locutor não carregou: {type(e).__name__}: {e}")
                self._failed = True
        return self._session

    def embed(self, pcm16: bytes, rate: int):
        """PCM int16 mono -> embedding L2-normalizado (256,) ou None (modelo ausente, fala curta/silenciosa demais)."""
        if not HAS_ONNX:
            return None
        wave = resample(pcm16_to_float(pcm16), rate)
        wave = speech_only(wave)
        if len(wave) < MIN_SPEECH_SECONDS * SAMPLE_RATE:
            return None
        feats = fbank(wave)
        if feats is None:
            return None
        with self._lock:
            sess = self._load()
            if sess is None:
                return None
            out = sess.run(None, {"feats": feats[None, :, :]})[0][0]
        norm = float(np.linalg.norm(out))
        return (out / norm).astype(np.float32) if norm > 0 else None
