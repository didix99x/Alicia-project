"""
Perfis de voz em JSON (um arquivo por pessoa em data/users/) e a decisão de "quem está falando".

  data/users/user_001.json   um perfil por pessoa (embedding de locutor + contexto individual)
  data/users/unknown.json    só contadores de vozes não reconhecidas (nunca guarda voz)

Privacidade: o JSON guarda vetores numéricos (embeddings), nunca áudio. Os arquivos ficam com permissão restrita ao
dono (0600/0700 onde o sistema respeita) e a pasta data/ está no .gitignore. A identificação por voz PODE ERRAR e não é
autenticação: nunca use o resultado para liberar algo sensível.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from .deps import np
from .logging_setup import logger
from .speaker_model import EMBEDDING_VERSION, MODEL_NAME, MODEL_SHA256, cosine

USERS_DIR = pathlib.Path("data") / "users"
DEFAULT_THRESHOLD = 0.55        # cosseno mínimo com o perfil (calibrado com gravações reais; ver tests/test_voice_id.py)
DEFAULT_MARGIN = 0.08           # vantagem mínima sobre o 2º colocado, senão são "vozes semelhantes" -> incerto
CONFIRM_BAND = 0.12             # entre (limiar - banda) e o limiar: provável, mas pede confirmação
MAX_SAMPLES = 8
MAX_HISTORY = 50
MAX_INTERACTIONS = 30
MAX_NOTES = 30

IDENTIFIED, UNCERTAIN, UNKNOWN, TOO_SHORT, DISABLED, NO_PROFILES = (
    "identified", "uncertain", "unknown", "too_short", "disabled", "no_profiles")


@dataclass
class Identity:
    status: str = UNKNOWN
    user_id: Optional[str] = None
    name: Optional[str] = None
    confidence: float = 0.0
    is_owner: bool = False
    candidates: List[Tuple[str, float]] = field(default_factory=list)   # (nome, cosseno) dos mais próximos

    @property
    def known(self) -> bool:
        return self.status == IDENTIFIED and self.user_id is not None


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class ProfileStore:
    def __init__(self, directory: pathlib.Path = USERS_DIR):
        self.dir = pathlib.Path(directory)
        self._lock = threading.RLock()
        self._cache: Dict[str, Dict[str, Any]] = {}
        self._loaded = False

    # ------------------------------------------------------------------ disco
    def _ensure_dir(self):
        self.dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.dir, 0o700)
        except OSError:
            pass

    def _write(self, path: pathlib.Path, data: Dict[str, Any]):
        self._ensure_dir()
        tmp = path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, path)               # escrita atômica: queda no meio não deixa JSON pela metade

    def _path(self, user_id: str) -> pathlib.Path:
        return self.dir / f"{user_id}.json"

    def load(self):
        with self._lock:
            self._cache.clear()
            if self.dir.is_dir():
                for p in sorted(self.dir.glob("user_*.json")):
                    try:
                        data = json.loads(p.read_text(encoding="utf-8"))
                        if isinstance(data, dict) and data.get("user_id") == p.stem:
                            self._cache[p.stem] = data
                        else:
                            logger.warning(f"Perfil de voz ignorado (formato inesperado): {p.name}")
                    except Exception as e:
                        logger.error(f"Perfil de voz ilegível, ignorado ({p.name}): {type(e).__name__}")
            self._loaded = True

    def _ready(self):
        if not self._loaded:
            self.load()

    # ------------------------------------------------------------------ consulta
    def list(self) -> List[Dict[str, Any]]:
        with self._lock:
            self._ready()
            return [self._cache[k] for k in sorted(self._cache)]

    def get(self, user_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            self._ready()
            return self._cache.get(user_id)

    @staticmethod
    def _norm(name: str) -> str:
        return re.sub(r"\s+", " ", (name or "").strip().lower())

    def find_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        key = self._norm(name)
        with self._lock:
            self._ready()
            for p in self._cache.values():
                if self._norm(p.get("name", "")) == key or key in [self._norm(a) for a in p.get("aliases", [])]:
                    return p
        return None

    def owner(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            self._ready()
            return next((p for p in self._cache.values() if p.get("is_owner")), None)

    def active_profiles(self) -> List[Dict[str, Any]]:
        return [p for p in self.list() if p.get("voice_profile", {}).get("enabled")
                and p["voice_profile"].get("embedding_version") == EMBEDDING_VERSION
                and p["voice_profile"].get("centroid")]

    # ------------------------------------------------------------------ cadastro
    def _next_id(self) -> str:
        nums = [int(k.split("_")[1]) for k in self._cache if re.fullmatch(r"user_\d+", k)]
        return f"user_{(max(nums) + 1 if nums else 1):03d}"

    @staticmethod
    def _centroid(embeddings) -> List[float]:
        c = np.mean(np.stack([np.asarray(e, dtype=np.float32) for e in embeddings]), axis=0)
        n = float(np.linalg.norm(c))
        return [round(float(x), 6) for x in (c / n if n > 0 else c)]

    def create(self, name: str, embeddings, *, is_owner: bool = False, consent_by: str = "self",
               threshold: float = DEFAULT_THRESHOLD) -> Dict[str, Any]:
        if not embeddings:
            raise ValueError("sem amostras de voz")
        with self._lock:
            self._ready()
            if self.find_by_name(name):
                raise ValueError(f"já existe um perfil chamado {name}")
            if is_owner and self.owner():
                raise ValueError("o dono já tem um perfil de voz")
            uid = self._next_id()
            samples = [[round(float(x), 6) for x in e] for e in embeddings][-MAX_SAMPLES:]
            profile = {
                "user_id": uid,
                "name": name,
                "aliases": [],
                "is_owner": bool(is_owner),
                "created_at": datetime.now().strftime("%Y-%m-%d"),
                "updated_at": _now(),
                "consent": {"given": True, "at": _now(), "by": consent_by,
                            "scope": "reconhecimento de voz local; sem gravação de áudio"},
                "voice_profile": {
                    "enabled": True,
                    "embedding_version": EMBEDDING_VERSION,
                    "model": MODEL_NAME,
                    "model_sha256": MODEL_SHA256,
                    "n_samples": len(samples),
                    "samples": samples,
                    "centroid": self._centroid(samples),
                },
                "recognition": {"total_detections": 0, "confidence_threshold": threshold,
                                "last_seen": None, "last_confidence": None},
                "identification_history": [],
                "preferences": {},
                "settings": {},
                "context": {"notes": [], "interactions": [], "stats": {"turns": 0, "first_turn": None, "last_turn": None}},
            }
            self._write(self._path(uid), profile)
            self._cache[uid] = profile
            logger.info(f"Perfil de voz criado: {uid} ({name}).")
            return profile

    def delete(self, user_id: str) -> bool:
        with self._lock:
            self._ready()
            if user_id not in self._cache:
                return False
            try:
                self._path(user_id).unlink(missing_ok=True)
            except OSError as e:
                logger.error(f"Não consegui apagar o perfil {user_id}: {e}")
                return False
            self._cache.pop(user_id, None)
            logger.info(f"Perfil de voz apagado: {user_id}.")
            return True

    def save(self, profile: Dict[str, Any]):
        with self._lock:
            profile["updated_at"] = _now()
            self._write(self._path(profile["user_id"]), profile)

    def set_voice_enabled(self, user_id: str, enabled: bool) -> bool:
        with self._lock:
            p = self.get(user_id)
            if not p:
                return False
            p["voice_profile"]["enabled"] = bool(enabled)
            self.save(p)
            return True

    # ------------------------------------------------------------------ uso
    def record_identification(self, user_id: str, confidence: float, source: str):
        with self._lock:
            p = self.get(user_id)
            if not p:
                return
            rec = p["recognition"]
            rec["total_detections"] += 1
            rec["last_seen"], rec["last_confidence"] = _now(), round(confidence, 3)
            p["identification_history"] = (p["identification_history"] + [
                {"at": rec["last_seen"], "confidence": round(confidence, 3), "source": source}])[-MAX_HISTORY:]
            self.save(p)

    def add_interaction(self, user_id: str, user_text: str, answer: str):
        with self._lock:
            p = self.get(user_id)
            if not p:
                return
            ctx = p["context"]
            ctx["interactions"] = (ctx["interactions"] + [{"at": _now(), "user": user_text[:400], "alicia": answer[:400]}])[-MAX_INTERACTIONS:]
            st = ctx["stats"]
            st["turns"] += 1
            st["first_turn"] = st["first_turn"] or _now()
            st["last_turn"] = _now()
            self.save(p)

    def add_note(self, user_id: str, text: str) -> bool:
        with self._lock:
            p = self.get(user_id)
            text = (text or "").strip()
            if not p or len(text) < 3 or text in p["context"]["notes"]:
                return False
            p["context"]["notes"] = (p["context"]["notes"] + [text[:300]])[-MAX_NOTES:]
            self.save(p)
            return True

    def forget_notes(self, user_id: str, query: str) -> int:
        with self._lock:
            p = self.get(user_id)
            if not p:
                return 0
            q = self._norm(query)
            keep = [n for n in p["context"]["notes"] if q not in self._norm(n)]
            removed = len(p["context"]["notes"]) - len(keep)
            if removed:
                p["context"]["notes"] = keep
                self.save(p)
            return removed

    def note_unknown(self, kind: str = "unknown"):
        """Só contadores: nada da voz desconhecida é guardado."""
        with self._lock:
            path = self.dir / "unknown.json"
            try:
                data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
            except Exception:
                data = {}
            data.setdefault("user_id", "unknown")
            data.setdefault("note", "Contadores de vozes não reconhecidas. Nenhum áudio nem embedding é guardado aqui.")
            data[f"total_{kind}"] = int(data.get(f"total_{kind}", 0)) + 1
            data["last_seen"] = _now()
            try:
                self._write(path, data)
            except OSError as e:
                logger.error(f"Não consegui atualizar unknown.json: {e}")

    # ------------------------------------------------------------------ decisão: quem está falando?
    def identify(self, embedding, margin: float = DEFAULT_MARGIN) -> Identity:
        profiles = self.active_profiles()
        if not profiles:
            return Identity(status=NO_PROFILES)
        scored = sorted(((cosine(embedding, p["voice_profile"]["centroid"]), p) for p in profiles), key=lambda x: -x[0])
        best_score, best = scored[0]
        second = scored[1][0] if len(scored) > 1 else -1.0
        thr = float(best["recognition"].get("confidence_threshold", DEFAULT_THRESHOLD))
        cands = [(p["name"], round(s, 3)) for s, p in scored[:3]]
        base = dict(confidence=round(best_score, 3), candidates=cands)
        if best_score >= thr and (best_score - second) >= margin:
            return Identity(IDENTIFIED, best["user_id"], best["name"], is_owner=bool(best.get("is_owner")), **base)
        if best_score >= thr - CONFIRM_BAND:
            # forte mas com outra voz parecida, ou quase no limiar: não afirma, deixa pedir confirmação
            return Identity(UNCERTAIN, best["user_id"], best["name"], is_owner=bool(best.get("is_owner")), **base)
        return Identity(UNKNOWN, **base)
