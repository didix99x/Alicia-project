#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Iniciador da Alicia (Windows 10/11; também roda em Linux/macOS para testes).

Usado por Iniciar_Alicia.bat. Só usa a biblioteca padrão do Python.

PRIMEIRA EXECUÇÃO
  verifica Python -> cria .venv -> atualiza pip -> instala dependências essenciais
  -> tenta as opcionais (uma a uma) -> cria logs/ -> cria .env a partir do
  .env.example e pede a chave (oculta) -> valida a API e o modelo -> diagnóstico
  -> abre a Alicia.

EXECUÇÕES SEGUINTES
  reutiliza a .venv, só instala o que faltar (compara o conteúdo dos arquivos de
  requisitos), checa o essencial localmente (sem rede) e abre a Alicia.
  Nunca mexe em config.json, alicia_memory.db, .env, gamer_profiles.json nem logs.

OPÇÕES
  --check       só roda o diagnóstico completo (com rede) e sai
  --set-key     pede uma nova chave de API e grava no .env
  --reinstall   força reinstalar as dependências
  --no-launch   prepara tudo mas não abre a Alicia
  --teste       roda o TESTE COMPLETO (modelo, conversa, voz, microfone, tela, visão) e gera relatorio_alicia.txt
  --vision      oferece instalar o Ollama + modelo de visão (para ela olhar a tela)
"""

import getpass
import hashlib
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import venv

APP_DIR = pathlib.Path(__file__).parent.resolve()
VENV_DIR = APP_DIR / ".venv"
MARKER_HASH = VENV_DIR / ".alicia_requirements.hash"
SETUP_OK = APP_DIR / ".alicia_setup_ok"
IS_WIN = os.name == "nt"
MIN_PY = (3, 10)

sys.path.insert(0, str(APP_DIR))
from alicia_core.env import is_placeholder, load_env_file, set_env_value  # noqa: E402  (só biblioteca padrão)


def say(msg=""):
    print(msg, flush=True)


def die(msg, code=1):
    say(f"\n[ERRO] {msg}")
    sys.exit(code)


def venv_python() -> pathlib.Path:
    return VENV_DIR / ("Scripts/python.exe" if IS_WIN else "bin/python")


def run(cmd, **kw):
    return subprocess.run([str(c) for c in cmd], cwd=APP_DIR, **kw)


# ------------------------------------------------------------------ 1. Python
def check_python():
    v = sys.version_info
    if (v.major, v.minor) < MIN_PY:
        die(f"Você está com o Python {v.major}.{v.minor}. A Alicia precisa do 3.10 ou mais novo.\n"
            "Instale o Python 3.11 ou 3.12 em https://www.python.org/downloads/windows/ "
            "(marque 'Add python.exe to PATH') e rode de novo.")
    say(f"Python {v.major}.{v.minor}.{v.micro} OK.")


# ------------------------------------------------------------------ 2. venv
def venv_is_healthy() -> bool:
    py = venv_python()
    if not py.exists():
        return False
    return run([py, "-c", "import sys"], capture_output=True).returncode == 0


def ensure_venv() -> bool:
    """Devolve True se o ambiente acabou de ser criado."""
    if venv_is_healthy():
        return False
    if VENV_DIR.exists():
        say("O ambiente virtual está quebrado; recriando (seus dados não são afetados)...")
        import shutil
        shutil.rmtree(VENV_DIR, ignore_errors=True)
        MARKER_HASH.unlink(missing_ok=True)
    say("Criando o ambiente virtual da Alicia (.venv)...")
    try:
        venv.EnvBuilder(with_pip=True, clear=False).create(VENV_DIR)
    except Exception as e:
        die(f"Não consegui criar o ambiente virtual: {e}\n"
            "Reinstale o Python marcando a opção 'pip' e 'py launcher' no instalador.")
    if not venv_is_healthy():
        die("O ambiente virtual foi criado mas não funciona. Apague a pasta .venv e tente de novo.")
    return True


# ------------------------------------------------------------------ 3. dependências
def parse_requirements(path: pathlib.Path):
    specs = []
    if not path.exists():
        return specs
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            specs.append(line)
    return specs


def requirements_fingerprint() -> str:
    h = hashlib.sha256()
    for name in ("requirements.txt", "requirements-optional.txt"):
        p = APP_DIR / name
        h.update(p.read_bytes() if p.exists() else b"")
    return h.hexdigest()


def import_name(spec: str) -> str:
    pkg = spec.split(">")[0].split("=")[0].split("<")[0].split("[")[0].strip().lower()
    return {"pillow": "PIL", "pyqt6": "PyQt6", "speechrecognition": "speech_recognition",
            "edge-tts": "edge_tts", "sounddevice": "sounddevice"}.get(pkg, pkg.replace("-", "_"))


def missing_modules(specs):
    py = venv_python()
    missing = []
    for spec in specs:
        r = run([py, "-c", f"import importlib.util,sys; sys.exit(0 if importlib.util.find_spec('{import_name(spec)}') else 1)"],
                capture_output=True)
        if r.returncode != 0:
            missing.append(spec)
    return missing


def pip(*args, quiet=True):
    cmd = [venv_python(), "-m", "pip", "--disable-pip-version-check", *args]
    if quiet:
        cmd.insert(5, "-q")
    return run(cmd)


def install_dependencies(fresh_venv: bool, force: bool):
    essential = parse_requirements(APP_DIR / "requirements.txt")
    optional = parse_requirements(APP_DIR / "requirements-optional.txt")
    fingerprint = requirements_fingerprint()
    up_to_date = MARKER_HASH.exists() and MARKER_HASH.read_text().strip() == fingerprint

    miss_ess = missing_modules(essential)
    miss_opt = missing_modules(optional) if (force or not up_to_date) else []
    if up_to_date and not miss_ess and not force:
        say("Dependências já instaladas.")
        return

    if fresh_venv or force:
        say("Atualizando as ferramentas de instalação (pip)...")
        if pip("install", "--upgrade", "pip").returncode != 0:
            say("[AVISO] Não consegui atualizar o pip; seguindo com o que existe.")

    if miss_ess or force:
        say("Instalando as bibliotecas essenciais (pode levar alguns minutos)...")
        r = pip("install", "-r", str(APP_DIR / "requirements.txt"))
        if r.returncode != 0:
            die("Falha ao instalar as bibliotecas essenciais.\n"
                "  - Confira sua conexão com a internet.\n"
                "  - Se o erro citar PyQt6, use o Python 3.11 ou 3.12 (64 bits).\n"
                "  - Depois rode Iniciar_Alicia.bat de novo.")
        miss_after = missing_modules(essential)
        if miss_after:
            die("Instalei, mas estas bibliotecas continuam indisponíveis: " + ", ".join(miss_after))

    failed_optional = []
    for spec in (optional if (force or not up_to_date) else miss_opt):
        if not force and spec not in missing_modules([spec]):
            continue
        say(f"Instalando recurso opcional: {spec}")
        if pip("install", spec).returncode != 0:
            failed_optional.append(spec)
    if failed_optional:
        say("\n[AVISO] Não consegui instalar: " + ", ".join(failed_optional))
        say("        A Alicia funciona sem isso; só os recursos ligados a essas bibliotecas ficam desligados")
        say("        (pygame/edge-tts = voz; SpeechRecognition/sounddevice = microfone; mss = captura rápida; psutil = modo gamer).")
        say("        Para tentar de novo mais tarde: Iniciar_Alicia.bat --reinstall\n")

    MARKER_HASH.write_text(fingerprint)


# ------------------------------------------------------------------ 4. pastas e .env
def ensure_dirs_and_config():
    (APP_DIR / "logs").mkdir(exist_ok=True)
    env_path = APP_DIR / ".env"
    example = APP_DIR / ".env.example"
    if not env_path.exists():
        if not example.exists():
            die("Faltou o arquivo .env.example na pasta da Alicia.")
        env_path.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
        say("Criei o arquivo .env (fica só no seu computador).")


def current_key() -> str:
    load_env_file(str(APP_DIR / ".env"))
    return os.environ.get("NVIDIA_API_KEY", "")


def prompt_for_key(reason=""):
    if reason:
        say(reason)
    say("\nCrie uma chave NOVA em https://build.nvidia.com (Settings > API Keys).")
    say("A chave começa com 'nvapi-'. Ao colar, nada aparece na tela (é normal).")
    for _ in range(3):
        try:
            key = getpass.getpass("Cole sua NVIDIA_API_KEY e tecle Enter: ").strip()
        except (EOFError, KeyboardInterrupt):
            die("Configuração cancelada.")
        if is_placeholder(key):
            say("Não recebi uma chave. Tente de novo.")
            continue
        if not key.startswith("nvapi-"):
            say("[AVISO] Essa chave não começa com 'nvapi-'. Se tiver certeza, tecle Enter de novo para usar assim mesmo.")
            confirm = getpass.getpass("Cole novamente para confirmar (ou Enter para cancelar): ").strip()
            if confirm != key:
                continue
        set_env_value(str(APP_DIR / ".env"), "NVIDIA_API_KEY", key)
        os.environ["NVIDIA_API_KEY"] = key
        say("Chave gravada no .env.")
        return
    die("Sem chave de API não dá para iniciar. Rode Iniciar_Alicia.bat --set-key quando tiver a chave.")


def ensure_key(force_prompt=False):
    key = current_key()
    if force_prompt or is_placeholder(key):
        prompt_for_key("" if force_prompt else "A chave da NVIDIA ainda não foi configurada.")


# ------------------------------------------------------------------ 4b. visão (Ollama) — opcional, só com permissão
VISION_CHOICE = APP_DIR / ".alicia_vision_choice"
OLLAMA_URL = "https://ollama.com/download/OllamaSetup.exe"


def ollama_exe():
    found = shutil.which("ollama")
    if found:
        return found
    local = os.environ.get("LOCALAPPDATA", "")
    cand = pathlib.Path(local) / "Programs" / "Ollama" / "ollama.exe"
    return str(cand) if local and cand.exists() else None


def vision_model_name() -> str:
    try:
        cfg = json.loads((APP_DIR / "config.json").read_text(encoding="utf-8"))
        return cfg.get("vision_model") or "qwen2.5vl:7b"
    except Exception:
        return "qwen2.5vl:7b"


def ask_yes_no(question: str) -> bool:
    try:
        return input(question + " (s/N): ").strip().lower() in ("s", "sim", "y", "yes")
    except (EOFError, KeyboardInterrupt):
        return False


def install_ollama() -> bool:
    say("Baixando e instalando o Ollama...")
    if shutil.which("winget"):
        r = subprocess.run(["winget", "install", "-e", "--id", "Ollama.Ollama", "--silent",
                            "--accept-package-agreements", "--accept-source-agreements"])
        if r.returncode == 0 and ollama_exe():
            return True
    try:
        target = pathlib.Path(tempfile.gettempdir()) / "OllamaSetup.exe"
        urllib.request.urlretrieve(OLLAMA_URL, target)
        subprocess.run([str(target), "/SILENT"])
    except Exception as e:
        say(f"[AVISO] Não consegui baixar o Ollama automaticamente ({e}). Instale manualmente em https://ollama.com/download")
        return False
    return ollama_exe() is not None


def offer_vision_setup(force=False):
    """Visão é opcional e pesada (~7 GB). Pergunta UMA vez; --vision pergunta de novo."""
    if not IS_WIN:
        return
    if VISION_CHOICE.exists() and not force:
        return
    exe = ollama_exe()
    model = vision_model_name()
    have_model = False
    if exe:
        r = subprocess.run([exe, "list"], capture_output=True, text=True)
        have_model = r.returncode == 0 and model.split(":")[0] in r.stdout
        if have_model:
            VISION_CHOICE.write_text("ok")
            return
    say("\nVISÃO (opcional): para a Alicia olhar a sua tela e usar o modo gamer é preciso o Ollama "
        f"e o modelo '{model}' (download de alguns GB).")
    if not ask_yes_no("Instalar e baixar agora?"):
        say("Tudo bem. Para instalar depois: Iniciar_Alicia.bat --vision")
        VISION_CHOICE.write_text("recusado")
        return
    if not exe:
        if not install_ollama():
            return
        exe = ollama_exe()
    say(f"Baixando o modelo {model} (pode demorar bastante)...")
    r = subprocess.run([exe, "pull", model])
    if r.returncode == 0:
        say("Visão pronta.")
        VISION_CHOICE.write_text("ok")
    else:
        say("[AVISO] O download do modelo falhou; tente de novo com Iniciar_Alicia.bat --vision")


# ------------------------------------------------------------------ 5. validar API (dentro da venv)
VALIDATE_CODE = r"""
import os, sys, json
sys.path.insert(0, os.getcwd())
from alicia_core.env import load_env_file
from alicia_core.nvidia_client import discover_model, DEFAULT_BASE_URL
load_env_file(".env")
print(json.dumps(discover_model(os.environ.get("NVIDIA_API_KEY", ""),
                                os.environ.get("NVIDIA_BASE_URL") or DEFAULT_BASE_URL,
                                os.environ.get("NVIDIA_MODEL", ""))))
"""


def validate_api():
    import json
    for attempt in range(3):
        load_env_file(str(APP_DIR / ".env"))
        say("Testando a chave e o modelo com uma mensagem curta (pode levar alguns segundos)...")
        r = run([venv_python(), "-c", VALIDATE_CODE], capture_output=True, text=True)
        try:
            data = json.loads(r.stdout.strip().splitlines()[-1])
        except Exception:
            say("[AVISO] Não consegui validar a API agora (veja logs\\alicia_system.log).")
            return True
        if not data["ok"]:
            kind = data["kind"]
            if kind == "auth":
                prompt_for_key("A NVIDIA recusou essa chave (401/403).")
                continue
            if kind in ("network", "timeout", "server", "rate_limit"):
                say(f"\n[AVISO] Não consegui falar com a NVIDIA agora ({kind}: {data.get('detail', '')}).")
                ans = input("Abrir a Alicia mesmo assim? (s/N): ").strip().lower()
                return ans in ("s", "sim", "y")
            say(f"\n[AVISO] A API respondeu com problema ({kind}).")
            return False
        chosen = data.get("chosen")
        if chosen:
            if data["changed"]:
                say(f"O modelo '{data['configured']}' não respondeu ao teste.")
                for m, kind, detail in data.get("tried", []):
                    say(f"   - {m}: {kind}")
                say(f"Escolhi automaticamente o melhor disponível: {chosen}")
                set_env_value(str(APP_DIR / ".env"), "NVIDIA_MODEL", chosen)
                os.environ["NVIDIA_MODEL"] = chosen
            else:
                say(f"Chave e modelo validados ({chosen}).")
            return True
        say("\nNenhum modelo respondeu ao teste. Resultado de cada tentativa:")
        for m, kind, detail in data.get("tried", []):
            say(f"   {m}: {kind} - {detail}")
        say("\nModelos listados na sua conta (a lista da NVIDIA pode estar desatualizada):")
        for m in data["models"]:
            say(f"   {m}")
        choice = input("\nDigite o nome exato de um modelo da lista (ou Enter para cancelar): ").strip()
        if not choice:
            return False
        set_env_value(str(APP_DIR / ".env"), "NVIDIA_MODEL", choice)
        os.environ["NVIDIA_MODEL"] = choice
    return False


# ------------------------------------------------------------------ 6. diagnóstico e execução
def diagnostico(quick: bool) -> int:
    cmd = [venv_python(), APP_DIR / "diagnostico.py"] + (["--quick"] if quick else [])
    return run(cmd).returncode


def main():
    args = set(sys.argv[1:])
    os.chdir(APP_DIR)
    say("\n=== Iniciador da Alicia ===\n")

    check_python()
    fresh = ensure_venv()
    install_dependencies(fresh, force="--reinstall" in args)
    ensure_dirs_and_config()

    if "--set-key" in args:
        ensure_key(force_prompt=True)
    else:
        ensure_key()

    first_run = not SETUP_OK.exists()
    if first_run or "--check" in args or "--set-key" in args:
        if not validate_api():
            die("A API da NVIDIA não foi validada. Corrija o que foi indicado e rode de novo.\n"
                "Para ver o diagnóstico completo: Diagnostico_Alicia.bat")
        code = diagnostico(quick=False)
        if "--check" in args:
            sys.exit(code)
        if code != 0:
            die("O diagnóstico encontrou falhas essenciais (veja a lista acima).")
        SETUP_OK.write_text("ok")
    else:
        code = diagnostico(quick=True)
        if code != 0:
            die("Há um problema essencial (veja acima). Para detalhes: Diagnostico_Alicia.bat")

    if "--teste" in args:
        extra = [a for a in sys.argv[1:] if a in ("--sem-audio", "--rapido", "--foco", "--sem-ajustes")]
        sys.exit(run([venv_python(), APP_DIR / "teste_completo.py", *extra]).returncode)

    offer_vision_setup(force="--vision" in args)
    if "--vision" in args and "--no-launch" not in args and len(args) == 1:
        return

    if "--no-launch" in args:
        say("\nPreparação concluída (--no-launch).")
        return
    say("\nAbrindo a Alicia... (o nome e a conversa por teclado ficam NESTA janela; não a feche)\n")
    code = run([venv_python(), APP_DIR / "main.py"]).returncode
    sys.exit(code)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        say("\nCancelado.")
        sys.exit(130)
