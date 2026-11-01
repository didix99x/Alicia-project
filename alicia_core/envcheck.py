# -*- coding: utf-8 -*-
"""
Verificação do ambiente da Alicia — UM só conjunto de critérios para Iniciar_Alicia.bat (launcher.py)
e Diagnostico_Alicia.bat (diagnostico.py).

Só usa a biblioteca padrão (roda no Python do sistema, antes de a .venv existir).

Como funciona
  1. `probe_local()` roda DENTRO do Python da .venv (`python -m alicia_core.envcheck --probe`) e tenta
     IMPORTAR de verdade cada biblioteca (listar no requirements não basta). Com deep=True também faz
     testes funcionais curtos (janela offscreen, dispositivos de áudio, captura de tela, OCR...).
     Nenhuma imagem, áudio ou texto do usuário é gravado ou enviado: tudo fica na memória e é descartado.
  2. `evaluate()` transforma o resultado em uma lista de `Item` (OK / ausente / com falha / opcional).
  3. `plan_install()` diz o que instalar; `render_report()` desenha o relatório.
"""

from __future__ import annotations

import ast
import datetime
import importlib
import importlib.util
import json
import os
import pathlib
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import urllib.request
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Tuple

MIN_PY = (3, 10)

# ----------------------------------------------------------------------------------------------
# Catálogo: o que cada biblioteca é, para que serve e o que acontece sem ela (linguagem simples)
# ----------------------------------------------------------------------------------------------
GROUPS: Dict[str, str] = {
    "net": "Comunicação com a internet (requests)",
    "gui": "Interface gráfica",
    "image": "Processamento de imagens",
    "audio": "Sistema de áudio",
    "stt": "Reconhecimento de voz",
    "tts": "Síntese de voz",
    "screen": "Captura de tela",
    "ocr": "OCR (leitura de texto da tela)",
    "proc": "Detecção de processos (Modo Gamer)",
}


@dataclass(frozen=True)
class LibInfo:
    module: str          # nome para achar o pacote
    group: str
    purpose: str         # para que serve
    effect: str          # o que acontece sem ele
    import_as: str = ""  # o que realmente importar no teste (padrão = module)


LIBS: Dict[str, LibInfo] = {
    "requests": LibInfo("requests", "net", "conversa com o cérebro da Alicia pela internet", "a Alicia não consegue conversar"),
    "pillow": LibInfo("PIL", "image", "abre, reduz e converte imagens (avatar e captura de tela)", "a Alicia não abre"),
    "pyqt6": LibInfo("PyQt6", "gui", "desenha a janela e o avatar da Alicia", "a Alicia não abre", import_as="PyQt6.QtWidgets"),
    "edge-tts": LibInfo("edge_tts", "tts", "gera a voz da Alicia", "ela fala somente por texto"),
    "speechrecognition": LibInfo("speech_recognition", "stt", "entende o que você fala no microfone", "o microfone fica desligado"),
    "sounddevice": LibInfo("sounddevice", "audio", "capta o áudio do microfone", "o microfone fica desligado"),
    "soundcard": LibInfo("soundcard", "audio", "capta o som que sai do PC (modo call)", "o modo call fica indisponível"),
    "mss": LibInfo("mss", "screen", "captura a tela de forma rápida", "a captura usa um método mais lento (Pillow)"),
    "psutil": LibInfo("psutil", "proc", "detecta jogos e programas abertos", "o Modo Gamer detecta menos processos"),
    "numpy": LibInfo("numpy", "image", "cálculo de imagens e áudio (OCR, modo call)", "o OCR e o modo call ficam indisponíveis"),
    "rapidocr-onnxruntime": LibInfo("rapidocr_onnxruntime", "ocr", "lê o texto da tela (OCR local)", "a Observadora segue só com janela ativa"),
    "pytesseract": LibInfo("pytesseract", "ocr", "ponte para o programa Tesseract (OCR alternativo)", "o OCR alternativo fica indisponível"),
}
EXTRA_PROBE_MODULES = ("onnxruntime",)          # dependência do RapidOCR, testada junto

TESSERACT_PATHS = (
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
    r"%LOCALAPPDATA%\Programs\Tesseract-OCR\tesseract.exe",
)

ESSENTIAL_FILES = (
    "main.py", "launcher.py", "diagnostico.py", "requirements.txt", ".env.example",
    "alicia_core/__init__.py", "alicia_core/core.py", "alicia_core/config.py", "alicia_core/deps.py",
    "alicia_core/database.py", "alicia_core/ui/desktop_pet.py",
)
ESSENTIAL_DIRS = ("alicia_core", "alicia_core/ui")


# ----------------------------------------------------------------------------------------------
# Requisitos
# ----------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Spec:
    name: str            # nome do pacote no pip
    line: str            # linha completa (ex.: "PyQt6>=6.5")
    required: bool
    min_version: str = ""

    @property
    def key(self) -> str:
        return self.name.lower().replace("_", "-")

    @property
    def info(self) -> Optional[LibInfo]:
        return LIBS.get(self.key)


def parse_requirements(path: pathlib.Path, required: bool) -> List[Spec]:
    specs: List[Spec] = []
    if not path.exists():
        return specs
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        name = re.split(r"[<>=!~\[; ]", line, maxsplit=1)[0].strip()
        m = re.search(r">=\s*([0-9][0-9A-Za-z.]*)", line)
        specs.append(Spec(name, line, required, m.group(1) if m else ""))
    return specs


def load_specs(app_dir: pathlib.Path) -> List[Spec]:
    return (parse_requirements(app_dir / "requirements.txt", True)
            + parse_requirements(app_dir / "requirements-optional.txt", False))


def _vtuple(v: str) -> Tuple[int, ...]:
    out = []
    for part in re.split(r"[.\-+]", v or ""):
        m = re.match(r"\d+", part)
        if not m:
            break
        out.append(int(m.group()))
    return tuple(out)


def version_satisfies(installed: str, minimum: str) -> bool:
    if not minimum or not installed:
        return True
    return _vtuple(installed) >= _vtuple(minimum)


# ----------------------------------------------------------------------------------------------
# 1) SONDA — roda dentro do Python alvo (a .venv). Nada do usuário é guardado.
# ----------------------------------------------------------------------------------------------
def _short(e: BaseException) -> str:
    text = f"{type(e).__name__}: {e}".replace("\n", " ")
    home = os.path.expanduser("~")
    if home and home != "~":
        text = text.replace(home, "~")
    return text[:220]


def _dist_version(key: str) -> str:
    from importlib import metadata
    for name in (key, key.replace("-", "_")):
        try:
            return metadata.version(name)
        except Exception:
            continue
    return ""


def _probe_import(info: LibInfo) -> dict:
    try:
        if importlib.util.find_spec(info.module) is None:
            return {"state": "missing"}
    except Exception:
        return {"state": "missing"}
    try:
        importlib.import_module(info.import_as or info.module)
    except BaseException as e:        # ImportError, OSError (DLL/PortAudio), SystemExit de módulos ruins
        return {"state": "broken", "error": _short(e)}
    return {"state": "ok"}


def _find_tesseract() -> str:
    cmd = shutil.which("tesseract")
    if cmd:
        return cmd
    if sys.platform == "win32":
        for p in TESSERACT_PATHS:
            p = os.path.expandvars(p)
            if os.path.exists(p):
                return p
    return ""


def _synthetic_text_image():
    """Imagem desenhada na memória (nunca vem da sua tela) para provar que o OCR lê texto."""
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("RGB", (460, 120), (255, 255, 255))
    try:
        font = ImageFont.load_default(size=52)
    except TypeError:
        font = ImageFont.load_default()
    ImageDraw.Draw(img).text((14, 24), "ALICIA 2024", fill=(0, 0, 0), font=font)
    return img


def _functional_tests(libs: Dict[str, dict]) -> Dict[str, dict]:
    """Testes curtos e privados. Cada um devolve {state: ok|warn|fail, detail}."""
    out: Dict[str, dict] = {}

    def ok(mod):
        return libs.get(mod, {}).get("state") == "ok"

    if ok("PyQt6"):
        try:
            os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
            from PyQt6.QtWidgets import QApplication, QLabel
            app = QApplication.instance() or QApplication([])
            label = QLabel("Alicia")
            pix = label.grab()
            out["gui"] = {"state": "ok" if not pix.isNull() else "fail", "detail": "janela criada e desenhada sem abrir na tela"}
            label.deleteLater()
            del app
        except BaseException as e:
            out["gui"] = {"state": "fail", "detail": _short(e)}

    if ok("sounddevice"):
        try:
            import sounddevice as sd
            inputs = [d for d in sd.query_devices() if d.get("max_input_channels", 0) > 0]
            out["audio"] = ({"state": "ok", "detail": f"{len(inputs)} microfone(s) encontrado(s)"} if inputs else
                            {"state": "warn", "detail": "nenhum microfone encontrado (confira Configurações do Windows > Privacidade > Microfone)"})
        except BaseException as e:
            out["audio"] = {"state": "fail", "detail": _short(e)}

    if ok("speech_recognition"):
        try:
            import speech_recognition as sr
            sr.Recognizer()
            out["stt"] = {"state": "ok", "detail": "reconhecedor criado"}
        except BaseException as e:
            out["stt"] = {"state": "fail", "detail": _short(e)}

    if ok("edge_tts"):
        try:
            import edge_tts
            getattr(edge_tts, "Communicate")
            if sys.platform == "win32":
                import ctypes
                getattr(ctypes.windll.winmm, "mciSendStringW")
                out["tts"] = {"state": "ok", "detail": "edge-tts + player do Windows (MCI)"}
            else:
                out["tts"] = {"state": "ok", "detail": "edge-tts carregado (o player de áudio só existe no Windows)"}
        except BaseException as e:
            out["tts"] = {"state": "fail", "detail": _short(e)}

    # captura de tela: grava só em memória e descarta; só o tamanho é relatado
    try:
        size = None
        if ok("mss"):
            import mss
            with mss.mss() as sct:
                mon = sct.monitors[1] if len(sct.monitors) > 1 else sct.monitors[0]
                box = {"left": mon["left"], "top": mon["top"], "width": min(64, mon["width"]), "height": min(64, mon["height"])}
                shot = sct.grab(box)
                size = f"{shot.size[0]}x{shot.size[1]} px de teste"
            via = "mss"
        else:
            from PIL import ImageGrab
            img = ImageGrab.grab()
            size, via = f"{img.size[0]}x{img.size[1]} px de teste", "Pillow"
        out["screen"] = {"state": "ok", "detail": f"captura via {via} ({size}; descartada)"}
    except BaseException as e:
        out["screen"] = {"state": "warn", "detail": f"não consegui capturar a tela ({_short(e)}); sessão sem tela ou bloqueada?"}

    if ok("PIL"):
        try:
            import io
            from PIL import Image
            img = Image.new("RGB", (64, 64), (10, 120, 200)).resize((32, 32), Image.Resampling.LANCZOS)
            buf = io.BytesIO()
            img.convert("RGB").save(buf, format="JPEG")
            if ok("numpy"):
                import numpy as np
                np.asarray(img).mean()
            out["image"] = {"state": "ok", "detail": "criar, reduzir e converter imagens funciona"}
        except BaseException as e:
            out["image"] = {"state": "fail", "detail": _short(e)}

    # OCR: usa o MESMO motor que a Observadora usa (RapidOCR -> Tesseract)
    try:
        import types
        try:
            from alicia_core.local_vision import OcrEngine
        except SystemExit as e:
            raise RuntimeError(str(e)) from None
        engine = OcrEngine(types.SimpleNamespace(obs_ocr_langs="por+eng", obs_ocr_max_width=1600))
        if not engine.probe():
            out["ocr"] = {"state": "fail", "detail": (engine.error or "nenhum motor de OCR respondeu")[:200]}
        else:
            text, used = engine.read(_synthetic_text_image())
            compact = re.sub(r"[^A-Za-z0-9]", "", text).upper()
            if "ALICIA" in compact or "2024" in compact:
                out["ocr"] = {"state": "ok", "detail": f"{engine.detail}; leu o texto de teste"}
            else:
                out["ocr"] = {"state": "warn", "detail": f"{engine.detail} inicializou, mas não leu o texto de teste direito"}
            out["ocr"]["engine"] = used or engine.backend
        engine.unload()
    except BaseException as e:
        out["ocr"] = {"state": "fail", "detail": _short(e)}
    return out


def _vlm_probe(deep: bool) -> dict:
    """Modelo de visão LOCAL (opcional): só é examinado se estiver ligado no config.json."""
    cfg_path = pathlib.Path(sys.modules[__name__].__file__).resolve().parent.parent / "config.json"
    try:
        data = json.loads(cfg_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict) or not data.get("local_vlm_enabled"):
        return {"enabled": False}
    out: dict = {"enabled": True}
    try:
        import types
        from .local_vlm import LocalVlm
        vlm = LocalVlm(types.SimpleNamespace(**{k: v for k, v in data.items() if k.startswith("local_vlm_")}))
        out["status"] = vlm.status()
        if deep and not out["status"]:
            ok, detail = vlm.selftest()
            out["selftest"] = {"ok": ok, "detail": detail}
    except BaseException as e:
        out["status"] = f"não consegui examinar: {_short(e)}"
    return out


def probe_local(deep: bool = False) -> dict:
    libs: Dict[str, dict] = {}
    for info in list(LIBS.values()) + [LibInfo(m, "ocr", "", "") for m in EXTRA_PROBE_MODULES]:
        if info.module in libs:
            continue
        res = _probe_import(info)
        if res["state"] == "ok":
            res["version"] = _dist_version(next((k for k, v in LIBS.items() if v.module == info.module), info.module))
        libs[info.module] = res
    if libs.get("onnxruntime", {}).get("state") == "ok":
        try:
            import onnxruntime
            libs["onnxruntime"]["providers"] = list(onnxruntime.get_available_providers())[:4]
        except BaseException as e:
            libs["onnxruntime"] = {"state": "broken", "error": _short(e)}
    result = {
        "python": ".".join(map(str, sys.version_info[:3])),
        "bits": struct.calcsize("P") * 8,
        "machine": os.environ.get("PROCESSOR_ARCHITEW6432") or os.environ.get("PROCESSOR_ARCHITECTURE") or "",
        "platform": sys.platform,
        "in_venv": sys.prefix != getattr(sys, "base_prefix", sys.prefix),
        "tesseract_exe": bool(_find_tesseract()),
        "libs": libs,
        "func": _functional_tests(libs) if deep else {},
        "vlm": _vlm_probe(deep),
        "deep": deep,
    }
    return result


def run_probe_in(python_exe: pathlib.Path, app_dir: pathlib.Path, deep: bool, timeout: int = 240) -> Optional[dict]:
    """Roda a sonda DENTRO da .venv. None = não conseguiu (venv quebrada, tempo esgotado...)."""
    cmd = [str(python_exe), "-m", "alicia_core.envcheck", "--probe"] + (["--deep"] if deep else [])
    env = dict(os.environ, PYTHONIOENCODING="utf-8", QT_QPA_PLATFORM=os.environ.get("QT_QPA_PLATFORM", "offscreen"))
    try:
        r = subprocess.run(cmd, cwd=app_dir, capture_output=True, text=True, timeout=timeout, env=env, encoding="utf-8", errors="replace")
    except (subprocess.TimeoutExpired, OSError):
        return None
    for line in reversed((r.stdout or "").strip().splitlines()):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                return json.loads(line)
            except ValueError:
                continue
    return None


# ----------------------------------------------------------------------------------------------
# 2) AVALIAÇÃO
# ----------------------------------------------------------------------------------------------
OK, MISSING, FAILED, OPTIONAL_MISSING = "ok", "missing", "failed", "optional_missing"


@dataclass
class Item:
    key: str
    label: str
    state: str = OK
    required: bool = False
    detail: str = ""
    purpose: str = ""
    effect: str = ""
    packages: List[Tuple[str, bool, bool]] = field(default_factory=list)   # (linha do pip, forçar reinstalação, obrigatório)
    kind: str = "group"                                              # group | python | venv | file | config | external
    repair: str = ""                                                 # ação de reparo própria (config_repair...)

    @property
    def blocking(self) -> bool:
        return self.required and self.state in (MISSING, FAILED)

    @property
    def level(self) -> str:
        return {OK: "ok", MISSING: "error", FAILED: "error", OPTIONAL_MISSING: "warn"}[self.state]


def _lib_state(spec: Spec, probe: dict) -> Tuple[str, str]:
    info = spec.info
    mod = info.module if info else spec.name.replace("-", "_")
    res = probe.get("libs", {}).get(mod)
    if res is None:                      # biblioteca fora do catálogo: tenta direto
        res = {"state": "missing"}
    if res["state"] == "missing":
        return "missing", ""
    if res["state"] == "broken":
        return "broken", res.get("error", "")
    if not version_satisfies(res.get("version", ""), spec.min_version):
        return "outdated", f"versão {res.get('version')} instalada; precisa da {spec.min_version} ou mais nova"
    return "ok", res.get("version", "")


def python_item(version: str, bits: int, machine: str = "") -> Item:
    vt = _vtuple(version)
    if vt[:2] < MIN_PY:
        return Item("python", f"Python {version}", FAILED, True, "versão antiga demais (precisa do 3.10 ou mais novo)",
                    purpose="é o programa que executa a Alicia", kind="python")
    if bits != 64:
        return Item("python", f"Python {version} ({bits} bits)", FAILED, True,
                    "Python de 32 bits: a interface (PyQt6) não instala. Instale o Python de 64 bits.", kind="python")
    note = ""
    if vt[:2] >= (3, 13):
        note = "Python 3.13 ou mais novo: algumas bibliotecas opcionais (como o RapidOCR) podem ainda não ter versão compatível; o Python 3.12 é o mais garantido."
    return Item("python", f"Python {version} (64 bits{', ' + machine if machine else ''})", OK, True, note, kind="python")


def venv_item(state: str) -> Item:
    if state == "ok":
        return Item("venv", "Ambiente virtual (.venv)", OK, True, kind="venv")
    if state == "missing":
        return Item("venv", "Ambiente virtual (.venv) ainda não existe", MISSING, True,
                    "é a pasta privada onde ficam as bibliotecas da Alicia; será criada (nada seu é tocado)", kind="venv", repair="venv_create")
    return Item("venv", "Ambiente virtual (.venv) corrompido", FAILED, True,
                "será recriado; seus dados (memória, config, histórico, imagens) NÃO são tocados", kind="venv", repair="venv_rebuild")


def evaluate(probe: Optional[dict], specs: List[Spec]) -> List[Item]:
    """Um Item por grupo (interface, áudio, voz...). probe=None => nada está instalado ainda."""
    probe = probe or {"libs": {}, "func": {}, "deep": False, "tesseract_exe": False}
    func = probe.get("func", {}) or {}
    items: List[Item] = []
    for gkey, glabel in GROUPS.items():
        gspecs = [s for s in specs if s.info and s.info.group == gkey]
        if not gspecs:
            continue
        if gkey == "ocr":
            items.append(_ocr_item(gspecs, probe, func))
            continue
        item = Item(gkey, glabel, OK)
        details: List[str] = []
        for s in gspecs:
            st, extra = _lib_state(s, probe)
            if st == "ok":
                details.append(f"{s.name} {extra}".strip())
                continue
            info = s.info
            lib_state = (MISSING if s.required else OPTIONAL_MISSING) if st == "missing" else FAILED
            force = st == "broken"
            if _worse(lib_state, item.state):
                item.state = lib_state
            item.required = item.required or s.required
            item.packages.append((s.line, force, s.required))
            what = {"missing": "não instalado", "broken": f"instalado, mas não funciona ({extra})", "outdated": extra}[st]
            item.purpose = item.purpose or (info.purpose if info else "")
            item.effect = item.effect or (info.effect if info else "")
            details.append(f"{s.name}: {what}")
        f = func.get(gkey)
        if f and item.state == OK:
            if f["state"] == "fail":
                item.state, item.required = FAILED, any(s.required for s in gspecs)
                details.append(f"teste falhou: {f['detail']}")
            else:
                details.append(f["detail"])
        elif f and f["state"] == "warn" and item.state == OK:
            details.append(f["detail"])
        item.detail = "; ".join(d for d in details if d)
        if not item.purpose and gspecs[0].info:
            item.purpose, item.effect = gspecs[0].info.purpose, gspecs[0].info.effect
        items.append(item)
    return items


def _worse(a: str, b: str) -> bool:
    order = {OK: 0, OPTIONAL_MISSING: 1, FAILED: 2, MISSING: 2}
    return order[a] > order[b]


def _ocr_item(gspecs: List[Spec], probe: dict, func: dict) -> Item:
    """OCR: serve RapidOCR OU Tesseract. Não pede os dois."""
    item = Item("ocr", GROUPS["ocr"], OK)
    rapid = next((s for s in gspecs if s.key == "rapidocr-onnxruntime"), None)
    libs = probe.get("libs", {})
    rapid_state, rapid_extra = _lib_state(rapid, probe) if rapid else ("missing", "")
    onnx = libs.get("onnxruntime", {"state": "missing"})
    tess_ok = libs.get("pytesseract", {}).get("state") == "ok" and probe.get("tesseract_exe")
    rapid_ok = rapid_state == "ok" and onnx.get("state") == "ok"
    f = func.get("ocr")
    item.purpose = LIBS["rapidocr-onnxruntime"].purpose
    item.effect = LIBS["rapidocr-onnxruntime"].effect

    if f is not None:                           # diagnóstico completo: vale o teste real
        if f["state"] in ("ok", "warn"):
            engine = f.get("engine") or ("rapidocr" if rapid_ok else "tesseract")
            item.detail = f"{'RapidOCR' if engine == 'rapidocr' else 'Tesseract'}: {f['detail']}"
            return item
    elif rapid_ok or tess_ok:                   # verificação rápida: importar basta
        item.detail = "RapidOCR (onnxruntime) instalado" if rapid_ok else "Tesseract + pytesseract instalados"
        if rapid_ok:
            item.detail += f"; ONNX Runtime {onnx.get('version', '')}".rstrip()
        return item

    # nenhum motor funcionando: descobrir por quê
    if rapid_state == "missing" and not tess_ok:
        item.state = OPTIONAL_MISSING
        item.detail = "RapidOCR não instalado"
        if rapid:
            item.packages.append((rapid.line, False, False))
    elif rapid_state in ("broken", "outdated") or (rapid_state == "ok" and onnx.get("state") != "ok") or f is not None:
        item.state = FAILED
        why = rapid_extra if rapid_state in ("broken", "outdated") else (onnx.get("error") or (f or {}).get("detail", "o motor não iniciou"))
        item.detail = f"RapidOCR com problema: {why}"
        if rapid:
            item.packages.append((rapid.line, True, False))
        if onnx.get("state") == "broken":
            item.packages.append(("onnxruntime", True, False))
    return item


def vlm_item(probe: Optional[dict]) -> Item:
    """Modelo de visão local: sempre opcional e informativo (nunca bloqueia nem entra no menu de instalação)."""
    label = "Modelo de visão local (opcional)"
    v = (probe or {}).get("vlm") or {"enabled": False}
    if not v.get("enabled"):
        return Item("vlm", label, OK, False, "desligado; para ligar (100% no seu PC): Iniciar_Alicia.bat --vision-local", kind="external")
    if v.get("status"):
        return Item("vlm", label, FAILED, False, v["status"], kind="external")
    st = v.get("selftest")
    if st is not None and not st.get("ok"):
        return Item("vlm", label, FAILED, False, f"teste falhou: {st.get('detail')}", kind="external")
    return Item("vlm", label, OK, False, (st or {}).get("detail") or "instalado e ligado", kind="external")


def file_items(app_dir: pathlib.Path) -> List[Item]:
    missing = [f for f in ESSENTIAL_FILES if not (app_dir / f).is_file()]
    missing += [d for d in ESSENTIAL_DIRS if not (app_dir / d).is_dir()]
    items: List[Item] = []
    if missing:
        items.append(Item("files", "Arquivos essenciais da Alicia", MISSING, True,
                          "faltando: " + ", ".join(missing) + ". Baixe a Alicia de novo (os arquivos de programa não podem ser recriados aqui; seus dados não são afetados).",
                          kind="file"))
    else:
        items.append(Item("files", "Arquivos e pastas essenciais", OK, True, kind="file"))
    assets = app_dir / "assets"
    if not assets.is_dir() or not any(assets.glob("*.png")):
        items.append(Item("assets", "Imagens do avatar (pasta assets)", OPTIONAL_MISSING, False,
                          "sem as imagens a Alicia usa círculos coloridos no lugar do avatar", kind="file"))
    return items


def _config_defaults(app_dir: pathlib.Path) -> Dict[str, object]:
    """Lê os valores padrão de alicia_core/config.py SEM importar nada (sem efeitos colaterais)."""
    defaults: Dict[str, object] = {}
    try:
        tree = ast.parse((app_dir / "alicia_core" / "config.py").read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return defaults
    for node in ast.walk(tree):
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Attribute) \
                and isinstance(node.target.value, ast.Name) and node.target.value.id == "self" and node.value is not None:
            try:
                defaults[node.target.attr] = ast.literal_eval(node.value)
            except Exception:
                pass
    return defaults


def _type_ok(value, default) -> bool:
    if default is None:
        return True
    if isinstance(default, bool):
        return isinstance(value, bool)
    if isinstance(default, int):
        return isinstance(value, int) and not isinstance(value, bool)
    if isinstance(default, float):
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if isinstance(default, str):
        return isinstance(value, str)
    if isinstance(default, list):
        return isinstance(value, list)
    return True


def config_item(app_dir: pathlib.Path) -> Item:
    path = app_dir / "config.json"
    if not path.exists():
        return Item("config", "config.json (será criado na primeira execução)", OK, False, kind="config")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return Item("config", "config.json inválido", FAILED, False,
                    f"não consigo ler ({type(e).__name__}); uma cópia de segurança será guardada e a Alicia recria o arquivo com os padrões",
                    kind="config", repair="config_reset")
    if not isinstance(data, dict):
        return Item("config", "config.json inválido", FAILED, False, "o conteúdo não é um objeto JSON",
                    kind="config", repair="config_reset")
    defaults = _config_defaults(app_dir)
    bad = [k for k, v in data.items() if k in defaults and not _type_ok(v, defaults[k])]
    obsolete = [k for k in data if defaults and k not in defaults]
    if bad:
        return Item("config", "config.json com valores inválidos", FAILED, False,
                    "valores fora do tipo esperado: " + ", ".join(sorted(bad)[:8]) + "; só eles serão restaurados ao padrão (com cópia de segurança)",
                    kind="config", repair="config_fix")
    if obsolete:
        return Item("config", "config.json com opções obsoletas", OPTIONAL_MISSING, False,
                    "opções que a Alicia não usa mais (são ignoradas): " + ", ".join(sorted(obsolete)[:8]),
                    kind="config", repair="config_clean")
    return Item("config", "config.json válido", OK, False, kind="config")


def repair_config(app_dir: pathlib.Path, mode: str) -> str:
    """Repara o config.json SEMPRE com cópia de segurança. Devolve uma frase para o usuário."""
    path = app_dir / "config.json"
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = path.with_name(f"config.json.bak-{stamp}")
    shutil.copy2(path, backup)
    if mode == "config_reset":
        path.unlink()
        return f"Guardei o arquivo antigo como {backup.name}; a Alicia cria um novo com os padrões."
    data = json.loads(path.read_text(encoding="utf-8"))
    defaults = _config_defaults(app_dir)
    for k in list(data):
        if mode == "config_fix" and k in defaults and not _type_ok(data[k], defaults[k]):
            data[k] = defaults[k]
        elif mode == "config_clean" and defaults and k not in defaults:
            del data[k]
    path.write_text(json.dumps(data, indent=4, ensure_ascii=False), encoding="utf-8")
    return f"Corrigi o config.json (cópia de segurança: {backup.name})."


def tesseract_item(probe: Optional[dict], ocr_item: Optional[Item]) -> Item:
    """Programa externo. Só entra na conta se o RapidOCR não estiver servindo."""
    if probe and probe.get("tesseract_exe"):
        return Item("tesseract", "Tesseract (programa externo de OCR)", OK, False, "instalado (OCR alternativo disponível)", kind="external")
    if ocr_item is not None and ocr_item.state == OK:
        return Item("tesseract", "Tesseract (programa externo de OCR)", OK, False, "não necessário: o RapidOCR está em uso", kind="external")
    return Item("tesseract", "Tesseract (programa externo de OCR)", OPTIONAL_MISSING, False,
                "alternativa ao RapidOCR; só vale instalar se o RapidOCR não funcionar neste computador",
                purpose="programa que lê texto em imagens", effect="sem nenhum OCR a Observadora usa só a janela ativa", kind="external")


# ----------------------------------------------------------------------------------------------
# 3) PLANO DE INSTALAÇÃO E RELATÓRIO
# ----------------------------------------------------------------------------------------------
def plan_install(items: Iterable[Item], only_required: bool = False) -> List[Tuple[str, bool, bool]]:
    """[(linha do pip, forçar, obrigatório)] sem repetição, obrigatórios primeiro."""
    seen, out = set(), []
    for pass_required in (True, False):
        if only_required and not pass_required:
            break
        for it in items:
            if it.kind != "group":
                continue
            for line, force, req in it.packages:
                if req == pass_required and line not in seen:
                    seen.add(line)
                    out.append((line, force, req))
    return out


def has_problems(items: Iterable[Item]) -> bool:
    return any(i.state != OK for i in items)


def summarize(items: Iterable[Item]) -> Dict[str, List[Item]]:
    s: Dict[str, List[Item]] = {"ok": [], "missing": [], "failed": [], "optional": []}
    for i in items:
        s[{OK: "ok", MISSING: "missing", FAILED: "failed", OPTIONAL_MISSING: "optional"}[i.state]].append(i)
    return s


_TAG = {"ok": "[OK]   ", "warn": "[AVISO]", "error": "[ERRO] "}


def render_report(items: Iterable[Item], verbose: bool = True) -> str:
    lines: List[str] = []
    for it in items:
        label = it.label
        if it.state == OPTIONAL_MISSING and it.kind == "group":
            label = f"Componente opcional ausente: {it.label}"
        lines.append(f"{_TAG[it.level]} {label}")
        if it.detail and (verbose or it.state != OK):
            lines.append(f"         {it.detail}")
        if it.state != OK and it.effect:
            lines.append(f"         Sem isso: {it.effect}.")
    s = summarize(items)
    lines += ["", "Resumo:",
              f"  - Componentes funcionando: {len(s['ok'])}",
              f"  - Componentes ausentes: {len(s['missing'])}",
              f"  - Componentes com falha: {len(s['failed'])}",
              f"  - Componentes opcionais indisponíveis: {len(s['optional'])}"]
    return "\n".join(lines)


# ----------------------------------------------------------------------------------------------
# 4) ERROS DE INSTALAÇÃO EM LINGUAGEM SIMPLES, INTERNET E LOG
# ----------------------------------------------------------------------------------------------
def classify_pip_error(output: str) -> Tuple[str, str]:
    t = (output or "").lower()
    rules = [
        ("network", ("connectionerror", "connection aborted", "max retries exceeded", "temporary failure in name resolution",
                     "getaddrinfo failed", "proxyerror", "read timed out", "timed out", "network is unreachable", "name or service not known")),
        ("ssl", ("ssl", "certificate_verify_failed")),
        ("space", ("no space left", "not enough space", "espaço insuficiente")),
        ("permission", ("permission denied", "access is denied", "winerror 5", "winerror 32")),
        ("compat", ("no matching distribution", "could not find a version that satisfies", "requires-python", "requires a different python", "not supported wheel")),
        ("build", ("microsoft visual c++", "failed building wheel", "error: subprocess-exited-with-error", "could not build wheels")),
    ]
    messages = {
        "network": "Não consegui baixar: parece que o computador está sem internet (ou ela está bloqueando o download). Confira a conexão e tente de novo.",
        "ssl": "A conexão segura com o servidor de downloads falhou (antivírus, proxy ou data/hora errada do PC podem causar isso).",
        "space": "Não há espaço livre suficiente no disco para instalar este componente.",
        "permission": "O Windows não deixou gravar os arquivos (algum programa pode estar usando a pasta). Feche outros programas da Alicia e tente de novo.",
        "compat": "Não existe uma versão deste componente compatível com o seu Python/Windows. A Alicia funciona sem ele (ou use o Python 3.12 de 64 bits).",
        "build": "Este componente precisou ser compilado e o seu computador não tem as ferramentas para isso. A Alicia funciona sem ele.",
    }
    for kind, needles in rules:
        if any(n in t for n in needles):
            return kind, messages[kind]
    return "unknown", "A instalação falhou por um motivo que não consegui identificar (os detalhes técnicos estão em logs\\instalacao_alicia.log)."


def internet_available(timeout: float = 5.0) -> bool:
    """Testa o acesso ao servidor de pacotes (respeita proxy do Windows/variáveis de ambiente).
    Com PIP_NO_INDEX definido (pasta local de pacotes, sem internet) não há o que testar."""
    if os.environ.get("PIP_NO_INDEX"):
        return True
    try:
        with urllib.request.urlopen("https://pypi.org/simple/pip/", timeout=timeout) as r:
            return 200 <= r.status < 400
    except Exception:
        return False


class InstallLog:
    """logs/instalacao_alicia.log — só dados técnicos do ambiente. Nada de conversa, tela, chave ou nome."""

    def __init__(self, app_dir: pathlib.Path):
        self.path = app_dir / "logs" / "instalacao_alicia.log"
        self._home = os.path.expanduser("~")
        self.write("=" * 60)
        self.write(f"Sessão do iniciador | Python do sistema {'.'.join(map(str, sys.version_info[:3]))} | {sys.platform}")

    def _clean(self, text: str) -> str:
        text = str(text).replace("\r", " ").replace("\n", " | ")
        if self._home and self._home != "~":
            text = text.replace(self._home, "~")
        text = re.sub(r"nvapi-[A-Za-z0-9_\-]+", "nvapi-***", text)
        return text[:600]

    def write(self, msg: str) -> None:
        try:
            self.path.parent.mkdir(exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S} {self._clean(msg)}\n")
        except OSError:
            pass

    def items(self, items: Iterable[Item], title: str) -> None:
        self.write(f"{title}:")
        for it in items:
            self.write(f"  [{it.level.upper()}] {it.label} -> {it.state}" + (f" ({it.detail})" if it.state != OK and it.detail else ""))


if __name__ == "__main__":
    if "--probe" in sys.argv:
        print(json.dumps(probe_local(deep="--deep" in sys.argv), ensure_ascii=False))
