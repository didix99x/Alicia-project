#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Diagnóstico da Alicia (roda dentro do ambiente virtual).

  python diagnostico.py            -> verificação completa (inclui rede e interface)
  python diagnostico.py --quick    -> só o que é local (sem rede), usado a cada inicialização

Cada linha mostra [OK], [AVISO] ou [FALHA] e, quando não está OK, o que fazer.
A chave da API NUNCA é exibida.
Código de saída: 0 = pode iniciar (avisos permitidos); 1 = há falha essencial.
"""

import importlib
import importlib.util
import json
import os
import pathlib
import sys

APP_DIR = pathlib.Path(__file__).parent.resolve()
os.chdir(APP_DIR)
sys.path.insert(0, str(APP_DIR))

QUICK = "--quick" in sys.argv
results = []  # (nivel, titulo, detalhe)


def add(level, title, hint=""):
    results.append((level, title, hint))
    tag = {"ok": "[ OK  ]", "warn": "[AVISO]", "fail": "[FALHA]"}[level]
    print(f"{tag} {title}")
    if hint and level != "ok":
        for line in hint.splitlines():
            print(f"         -> {line}")


def module_ok(name):
    return importlib.util.find_spec(name) is not None


def check_python():
    v = sys.version_info
    if v < (3, 10):
        add("fail", f"Python {v.major}.{v.minor} é antigo demais", "Instale o Python 3.11 ou 3.12 (python.org) e apague a pasta .venv.")
    else:
        add("ok", f"Python {v.major}.{v.minor}.{v.micro}")


def check_dependencies():
    essential = {"requests": "requests", "PIL": "Pillow", "PyQt6": "PyQt6"}
    for mod, pkg in essential.items():
        if module_ok(mod):
            add("ok", f"Biblioteca essencial: {pkg}")
        else:
            add("fail", f"Biblioteca essencial ausente: {pkg}", "Rode Iniciar_Alicia.bat de novo (ele instala) ou: .venv\\Scripts\\python -m pip install " + pkg)
    optional = {
        "edge_tts": ("edge-tts", "sem ela a Alicia não fala em voz alta"),
        "speech_recognition": ("SpeechRecognition", "sem ela o microfone fica desligado"),
        "sounddevice": ("sounddevice", "sem ela o microfone fica desligado"),
        "mss": ("mss", "a captura de tela usa o método mais lento do Pillow"),
        "psutil": ("psutil", "o modo gamer detecta menos processos"),
    }
    for mod, (pkg, consequence) in optional.items():
        if module_ok(mod):
            add("ok", f"Biblioteca opcional: {pkg}")
        else:
            add("warn", f"Biblioteca opcional ausente: {pkg}", consequence)


def check_package_imports():
    try:
        importlib.import_module("alicia_core.core")
        add("ok", "Todos os módulos da Alicia importam sem erro")
    except SystemExit as e:
        add("fail", "Falha ao importar os módulos da Alicia", str(e))
    except Exception as e:
        add("fail", f"Falha ao importar os módulos da Alicia: {type(e).__name__}: {e}", "Veja logs\\alicia_system.log e envie a mensagem acima para quem está te ajudando.")


def load_env():
    from alicia_core.env import load_env_file
    return load_env_file(".env")


def check_env():
    from alicia_core.env import is_placeholder
    if not load_env():
        add("fail", "Arquivo .env não encontrado", "Rode Iniciar_Alicia.bat: ele cria o .env e pede a sua chave.")
        return None
    key = os.environ.get("NVIDIA_API_KEY", "")
    if is_placeholder(key):
        add("fail", "NVIDIA_API_KEY não configurada no .env", "Crie uma chave em https://build.nvidia.com e cole em NVIDIA_API_KEY (ou rode Iniciar_Alicia.bat --set-key).")
        return None
    note = "" if key.startswith("nvapi-") else "A chave não começa com 'nvapi-'; confirme se copiou a chave certa."
    add("ok" if not note else "warn", f"Chave da API presente ({len(key)} caracteres; conteúdo oculto)", note)
    model = os.environ.get("NVIDIA_MODEL", "").strip()
    if not model:
        add("fail", "NVIDIA_MODEL não definido no .env", "Copie o valor de NVIDIA_MODEL do arquivo .env.example.")
        return None
    add("ok", f"Modelo configurado: {model}")
    return key


def check_config_and_db():
    cfg = pathlib.Path("config.json")
    if cfg.exists():
        try:
            json.loads(cfg.read_text(encoding="utf-8"))
            add("ok", "config.json válido")
        except Exception as e:
            add("fail", f"config.json inválido: {e}", "Renomeie config.json para config.json.bak; a Alicia recria um novo (seus dados ficam no banco).")
    else:
        add("ok", "config.json será criado na primeira execução")
    try:
        from alicia_core.database import MemoryDatabase, DatabaseUnavailable
        db_path = "alicia_memory.db"
        try:
            db_path = json.loads(cfg.read_text(encoding="utf-8")).get("db_path", db_path) if cfg.exists() else db_path
        except Exception:
            pass
        existed = pathlib.Path(db_path).exists()
        db = MemoryDatabase(db_path)
        if not db.check_integrity():
            add("fail", "Banco de memória falhou na verificação de integridade", "Faça uma CÓPIA de alicia_memory.db antes de qualquer coisa. Nada foi apagado.")
        else:
            n = db.count_chat_messages()
            name = db.get_user_name()
            add("ok", f"Banco de memória OK ({n} mensagens; nome {'salvo' if name else 'ainda não definido'}{'' if existed else '; banco novo criado'})")
    except DatabaseUnavailable as e:
        add("fail", "Banco de memória inacessível", str(e))
    except Exception as e:
        add("fail", f"Erro ao abrir o banco: {type(e).__name__}: {e}")


def check_api(key):
    from alicia_core.nvidia_client import NvidiaClient, BrainError, DEFAULT_BASE_URL
    client = NvidiaClient(key, os.environ.get("NVIDIA_MODEL", ""), os.environ.get("NVIDIA_BASE_URL") or DEFAULT_BASE_URL)
    try:
        models = client.list_models()
    except BrainError as e:
        add("fail", f"Conexão com a API NVIDIA falhou ({e.kind})", e.user_message)
        return client, None
    add("ok", f"API NVIDIA respondeu ({len(models)} modelos listados)")
    if client.model in models:
        add("ok", f"Modelo '{client.model}' está disponível")
    else:
        sugestoes = [m for m in models if "instruct" in m.lower() or "chat" in m.lower()][:12]
        add("fail", f"Modelo '{client.model}' não aparece na lista da sua conta",
            "Escolha um destes em NVIDIA_MODEL no .env:\n" + "\n".join(f"  {m}" for m in (sugestoes or models[:12])))
    return client, models


def check_ui():
    if not module_ok("PyQt6"):
        return
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")  # sem abrir janela
    try:
        from PyQt6.QtWidgets import QApplication
        from alicia_core.ui.desktop_pet import AliciaDesktopPetUI
        from alicia_core.enums import AliciaExpression
        app = QApplication.instance() or QApplication(sys.argv[:1])

        import types

        class _FakeTTS:
            enabled = False
            is_speaking = False
            audio_playing = False

        class _FakeCore:
            """Núcleo de mentira só para testar a interface: tem tudo que o avatar lê ao desenhar."""
            modo_descanso = False
            modo_gamer = False
            screen_watch_enabled = False
            gamer_current_game = None
            poker_active = False
            last_alicia_message = ""
            current_expression = AliciaExpression.FELIZ
            config = types.SimpleNamespace(mic_index=None, avatar_relax_seconds=8.0)
            tts = _FakeTTS()

        ui = AliciaDesktopPetUI(_FakeCore())
        ui.atualizar_visual()          # desenha um quadro de verdade
        ok = len(ui.pixmaps) == len(list(AliciaExpression))
        ui.close()
        add("ok" if ok else "warn", "Interface (avatar) inicializa", "" if ok else "Faltam imagens do avatar; usando círculos coloridos.")
    except Exception as e:
        add("fail", f"A interface não inicializou: {type(e).__name__}: {e}", "Reinstale o PyQt6: .venv\\Scripts\\python -m pip install --force-reinstall PyQt6")


def check_audio():
    from alicia_core import deps
    add("ok" if deps.HAS_AUDIO else "warn", "Saída de áudio (voz)" + (f" via {deps.AUDIO_BACKEND}" if deps.HAS_AUDIO else " indisponível"),
        "" if deps.HAS_AUDIO else f"Motivo: {deps.AUDIO_ERROR}. A Alicia segue por texto.")
    if not deps.HAS_MIC:
        add("warn", "Microfone indisponível", deps.MIC_ERROR)
    else:
        try:
            from alicia_core.audio_input import list_microphones
            mics = list_microphones()
            add("ok" if mics else "warn", f"Microfones encontrados: {len(mics)}",
                "" if mics else "Conecte um microfone e confira Configurações do Windows > Privacidade > Microfone. A Alicia segue por teclado.")
        except Exception as e:
            add("warn", f"Não consegui listar microfones: {e}", "A Alicia segue por teclado.")


def check_ollama():
    try:
        import requests
        cfg = json.loads(pathlib.Path("config.json").read_text(encoding="utf-8")) if pathlib.Path("config.json").exists() else {}
        host = cfg.get("ollama_host", "http://localhost:11434").rstrip("/")
        ok = requests.get(host + "/api/tags", timeout=3).status_code == 200
        add("ok" if ok else "warn", "Ollama (só para visão/modo gamer)" + (" disponível" if ok else " respondeu com erro"))
    except Exception:
        add("warn", "Ollama não está rodando", "É opcional: sem ele a Alicia conversa normalmente, mas não olha a tela nem usa o modo gamer.")


def main():
    print("\n=== Diagnóstico da Alicia ===\n")
    check_python()
    check_dependencies()
    check_package_imports()
    key = check_env()
    check_config_and_db()
    if not QUICK:
        if key:
            check_api(key)
        check_ui()
        check_audio()
        check_ollama()
    fails = [r for r in results if r[0] == "fail"]
    warns = [r for r in results if r[0] == "warn"]
    print(f"\nResumo: {len(results) - len(fails) - len(warns)} OK, {len(warns)} aviso(s), {len(fails)} falha(s).")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
