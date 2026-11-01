#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Iniciador da Alicia (Windows 10/11 64 bits; também roda em Linux/macOS para testes).

Usado por Iniciar_Alicia.bat. Só usa a biblioteca padrão do Python.

COMO FUNCIONA
  1. Verifica o computador SEM alterar nada: Python (versão e 64 bits), .venv (existe? está íntegra?),
     bibliotecas (tenta IMPORTAR de verdade, dentro da .venv), arquivos essenciais e config.json.
  2. Tudo certo  -> segue direto para a Alicia.
  3. Algo faltando -> explica em linguagem simples e PERGUNTA o que fazer (instalar tudo, só o obrigatório,
     ver detalhes, continuar sem instalar ou cancelar). Nada é baixado ou instalado sem a sua resposta.
  4. Instala só o que falta (sempre dentro da .venv), mostra o progresso, confere de novo ao terminar
     e registra tudo (sem dados pessoais) em logs/instalacao_alicia.log.
  Nunca mexe em config.json (a não ser com seu OK, com cópia de segurança), alicia_memory.db, .env,
  gamer_profiles.json, assets nem logs. Reconstruir a .venv NÃO apaga nenhum dado seu.

OPÇÕES
  --check       diagnóstico completo (com testes reais de tela, áudio, OCR e rede); oferece corrigir; não abre a Alicia
  --repair      verificação completa + reparo do ambiente; não abre a Alicia
  --set-key     pede uma nova chave de API e grava no .env
  --reinstall   (pede confirmação) reinstala todos os componentes, inclusive os que já funcionam
  --yes         autoriza a instalação de TODOS os componentes sem perguntar (para uso avançado/automação)
  --vision-local  instala o modelo de visão LOCAL (opcional, 100% no seu PC; explica e pede autorização)
  --env-only    só verifica (rápido) e, com a sua autorização, instala/repara o ambiente; depois sai (scripts e testes)
  --no-launch   prepara tudo mas não abre a Alicia
  --teste       roda o TESTE COMPLETO (modelo, conversa, voz, microfone, tela, visão) e gera relatorio_alicia.txt
"""

import getpass
import hashlib
import json
import os
import pathlib
import platform
import shutil
import struct
import subprocess
import sys
import threading
import time
import venv
import webbrowser

APP_DIR = pathlib.Path(__file__).parent.resolve()
VENV_DIR = APP_DIR / ".venv"
SETUP_OK = APP_DIR / ".alicia_setup_ok"
STATE_FILE = APP_DIR / ".alicia_setup_state.json"      # só lembra "usuário escolheu continuar sem estes opcionais"
IS_WIN = os.name == "nt"
MIN_PY = (3, 10)

sys.path.insert(0, str(APP_DIR))
from alicia_core import envcheck  # noqa: E402  (só biblioteca padrão)
from alicia_core.env import is_placeholder, load_env_file, set_env_value  # noqa: E402

LOG = None          # envcheck.InstallLog (criado em main)


def say(msg=""):
    print(msg, flush=True)


def die(msg, code=1):
    say(f"\n[ERRO] {msg}")
    sys.exit(code)


def venv_python() -> pathlib.Path:
    return VENV_DIR / ("Scripts/python.exe" if IS_WIN else "bin/python")


def run(cmd, **kw):
    return subprocess.run([str(c) for c in cmd], cwd=APP_DIR, **kw)


# ------------------------------------------------------------------ utilidades de conversa
def ask(prompt: str, valid=None):
    """Pergunta no terminal. None = sem resposta possível (entrada encerrada)."""
    while True:
        try:
            ans = input(prompt).strip().lower()
        except (EOFError, KeyboardInterrupt):
            return None
        if valid is None or ans in valid:
            return ans
        say("  Opção inválida. Digite um dos números/letras indicados.")


def ask_yes_no(question: str, default_yes=False) -> bool:
    ans = ask(question + (" (S/n): " if default_yes else " (s/N): "))
    if ans is None or ans == "":
        return default_yes if ans == "" else False
    return ans in ("s", "sim", "y", "yes")


def log(msg: str):
    if LOG:
        LOG.write(msg)


# ------------------------------------------------------------------ 1. Python
def check_python():
    """Confere o Python que está rodando este iniciador. Encerra com explicação se não servir."""
    v = sys.version_info
    bits = struct.calcsize("P") * 8
    machine = platform.machine() or ""
    item = envcheck.python_item(f"{v.major}.{v.minor}.{v.micro}", bits, machine)
    log(f"Python {v.major}.{v.minor}.{v.micro} {bits} bits; máquina {machine}; {platform.system()} {platform.release()}")
    if item.state != envcheck.OK:
        if (v.major, v.minor) < MIN_PY:
            die(f"Você está com o Python {v.major}.{v.minor}. A Alicia precisa do 3.10 ou mais novo.\n"
                "Instale o Python 3.12 de 64 bits em https://www.python.org/downloads/windows/ e rode de novo.")
        die(f"Este Python é de {bits} bits. A Alicia precisa do Python de 64 bits (a interface gráfica não funciona em 32 bits).\n"
            "Instale o Python 3.12 de 64 bits em https://www.python.org/downloads/windows/ e rode de novo.")
    return item


# ------------------------------------------------------------------ 2. venv
def venv_state() -> str:
    """'ok' | 'missing' | 'broken'. Só olha; não altera nada."""
    if not VENV_DIR.exists():
        return "missing"
    py = venv_python()
    if not py.exists():
        return "broken"
    try:
        if run([py, "-c", "import sys"], capture_output=True, timeout=60).returncode != 0:
            return "broken"
        if run([py, "-m", "pip", "--version"], capture_output=True, timeout=60).returncode != 0:
            return "broken"
    except (OSError, subprocess.TimeoutExpired):
        return "broken"
    return "ok"


def venv_is_healthy() -> bool:
    return venv_state() == "ok"


def retire_broken_venv():
    """Tira a .venv quebrada do caminho. SÓ a pasta .venv: nenhum dado da Alicia fica lá dentro."""
    if not VENV_DIR.exists():
        return
    if VENV_DIR.name != ".venv" or VENV_DIR.resolve().parent != APP_DIR or VENV_DIR.is_symlink():
        die("Por segurança não vou mexer nessa pasta .venv (ela não está onde deveria). Apague-a manualmente e tente de novo.")
    parked = APP_DIR / f".venv.quebrada-{time.strftime('%Y%m%d-%H%M%S')}"
    try:
        VENV_DIR.rename(parked)
    except OSError as e:
        log(f"não consegui renomear a .venv: {e}")
        die("Não consegui mexer na pasta .venv (algum programa a está usando). Feche a Alicia e outras janelas dela e tente de novo.")
    shutil.rmtree(parked, ignore_errors=True)           # melhor esforço; se sobrar algo, a pasta '.venv.quebrada-*' pode ser apagada à mão
    log("venv quebrada removida (somente a pasta .venv)")


def create_venv(rebuild: bool):
    if rebuild:
        say("Recriando o ambiente virtual (seus dados, memórias e configurações não são tocados)...")
        retire_broken_venv()
    say("Criando o ambiente virtual da Alicia (.venv)...")
    try:
        venv.EnvBuilder(with_pip=True, clear=False).create(VENV_DIR)
    except Exception as e:
        log(f"falha ao criar venv: {type(e).__name__}: {e}")
        die(f"Não consegui criar o ambiente virtual: {e}\n"
            "Reinstale o Python marcando a opção 'pip' no instalador (https://www.python.org/downloads/windows/).")
    if not venv_is_healthy():
        die("O ambiente virtual foi criado mas não funciona. Apague a pasta .venv e tente de novo.")
    say("Atualizando a ferramenta de instalação (pip) do ambiente novo...")
    if pip("install", "--upgrade", "pip")[0] != 0:
        say("[AVISO] Não consegui atualizar o pip; sigo com a versão que veio com o Python.")
    log("venv criada")


# ------------------------------------------------------------------ 3. dependências: verificar, perguntar, instalar
def pip(*args, timeout=1800, live_label=None):
    """Roda o pip da .venv. Devolve (código, saída). Mostra um contador de tempo se live_label."""
    cmd = [str(venv_python()), "-m", "pip", "--disable-pip-version-check", *args]
    proc = subprocess.Popen(cmd, cwd=APP_DIR, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            encoding="utf-8", errors="replace")
    chunks = []
    live = bool(live_label) and sys.stdout.isatty()
    if live_label and not live:
        print(f"  {live_label} ...", flush=True)
    reader = threading.Thread(target=lambda: chunks.extend(iter(proc.stdout.readline, "")), daemon=True)
    reader.start()
    t0 = time.time()
    try:
        while proc.poll() is None:
            if live:
                print(f"\r  {live_label} ... {int(time.time() - t0)} s", end="", flush=True)
            if time.time() - t0 > timeout:
                proc.kill()
                chunks.append("timed out")
                break
            time.sleep(1.0)
    except KeyboardInterrupt:
        proc.kill()
        proc.wait()
        if live:
            print()
        raise
    reader.join(timeout=5)
    if live:
        print("\r" + " " * 70 + "\r", end="", flush=True)
    return (proc.returncode if proc.returncode is not None else 1), "".join(chunks)


def gather(deep: bool):
    """Verifica tudo e devolve (lista de itens, resultado da sonda). Não altera nada."""
    items = []
    py_item = envcheck.python_item(".".join(map(str, sys.version_info[:3])), struct.calcsize("P") * 8, platform.machine() or "")
    items.append(py_item)
    state = venv_state()
    items.append(envcheck.venv_item(state))
    specs = envcheck.load_specs(APP_DIR)
    probe = envcheck.run_probe_in(venv_python(), APP_DIR, deep) if state == "ok" else None
    if state == "ok" and probe is None:
        items[-1] = envcheck.venv_item("broken")        # a venv não consegue nem rodar a sonda
    groups = envcheck.evaluate(probe, specs)
    items += groups
    ocr = next((g for g in groups if g.key == "ocr"), None)
    items.append(envcheck.tesseract_item(probe, ocr))
    items.append(envcheck.vlm_item(probe))
    items += envcheck.file_items(APP_DIR)
    items.append(envcheck.config_item(APP_DIR))
    return items, probe


def _problems(items):
    return [i for i in items if i.state != envcheck.OK and i.kind != "external"]


def _problem_fingerprint(problems) -> str:
    raw = "|".join(sorted(f"{i.key}:{i.state}" for i in problems))
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _declined() -> str:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8")).get("declined", "")
    except Exception:
        return ""


def _remember_declined(fp: str):
    try:
        STATE_FILE.write_text(json.dumps({"declined": fp}), encoding="utf-8")
    except OSError:
        pass


def show_details(problems):
    say("\nDetalhes do que está faltando:\n")
    for i in problems:
        tag = {"error": "[ERRO]", "warn": "[AVISO]"}[i.level]
        kind = "obrigatório" if i.required else "opcional"
        say(f"{tag} {i.label}  ({kind})")
        if i.purpose:
            say(f"        Para que serve: {i.purpose}.")
        if i.detail:
            say(f"        Situação: {i.detail}")
        if i.effect:
            say(f"        Sem isso: {i.effect}.")
        if i.packages:
            say("        Será instalado: " + ", ".join(p[0] for p in i.packages))
    say("\nFonte dos downloads: PyPI (pypi.org), o repositório oficial de bibliotecas Python. Tudo é instalado só dentro da pasta .venv da Alicia.\n")


MENU = """
Alicia identificou alguns componentes necessários que ainda não estão instalados no seu computador.

Posso baixar e configurar esses componentes automaticamente para você.

Deseja continuar?

  1. Instalar todos os componentes necessários.
  2. Instalar somente os componentes obrigatórios.
  3. Visualizar detalhes dos componentes ausentes.
  4. Continuar sem instalar, quando possível.
  5. Cancelar inicialização.
"""


def choose_action(problems, assume_yes=False):
    """Devolve 'all' | 'required' | 'skip' | 'cancel'. Nenhuma alteração acontece antes disto."""
    blocking = [i for i in problems if i.blocking]
    say("\nO que a verificação encontrou:")
    for i in problems:
        say(f"  - {i.label}" + ("" if i.required else "  (opcional)"))
    if assume_yes:
        say("\n(--yes: instalação de todos os componentes autorizada pela linha de comando)")
        return "all"
    say(MENU)
    while True:
        ans = ask("Escolha 1, 2, 3, 4 ou 5: ", valid={"1", "2", "3", "4", "5"})
        if ans is None:
            say("\nSem resposta; nada foi alterado.")
            return "cancel"
        if ans == "1":
            return "all"
        if ans == "2":
            if not any(i.required for i in problems):
                say("  Nenhum componente obrigatório está faltando; só há opcionais (opção 1 instala esses).")
                continue
            return "required"
        if ans == "3":
            show_details(problems)
            continue
        if ans == "4":
            if blocking:
                say("  Não dá para continuar sem instalar: faltam componentes obrigatórios (" + ", ".join(i.label for i in blocking) + ").")
                continue
            return "skip"
        return "cancel"


def _apply_repairs(items, scope):
    """venv e config.json: reparos que não são pacotes."""
    for it in items:
        if it.state == envcheck.OK:
            continue
        if it.repair in ("venv_create", "venv_rebuild"):
            create_venv(rebuild=it.repair == "venv_rebuild")
        elif it.repair.startswith("config_") and scope == "all":
            try:
                say(envcheck.repair_config(APP_DIR, it.repair))
                log(f"config.json reparado: {it.repair}")
            except Exception as e:
                say(f"[AVISO] Não consegui reparar o config.json ({e}).")
                log(f"falha ao reparar config.json: {type(e).__name__}: {e}")


def install_plan(plan):
    """Instala um por um e devolve [(linha, sucesso, mensagem simples)]."""
    results = []
    total = len(plan)
    for n, (line, force, required) in enumerate(plan, 1):
        name = line.split(">")[0].split("=")[0].split("[")[0]
        args = ["install", "--prefer-binary", "--progress-bar", "off"] + (["--force-reinstall"] if force else []) + [line]
        label = f"[{n}/{total}] Instalando {name}" + ("" if required else " (opcional)")
        t0 = time.time()
        try:
            rc, out = pip(*args, live_label=label)
        except KeyboardInterrupt:
            say(f"\n[AVISO] Instalação interrompida em {name}. Rode Iniciar_Alicia.bat de novo: ele continua de onde parou (só instala o que faltar).")
            log(f"instalação interrompida pelo usuário em {name}")
            raise SystemExit(130)
        dt = int(time.time() - t0)
        if rc == 0:
            say(f"[OK] {name} instalado ({dt} s)")
            log(f"instalado: {line} ({dt}s)")
            results.append((line, True, ""))
        else:
            kind, msg = envcheck.classify_pip_error(out)
            say(f"[ERRO] {name}: {msg}")
            log(f"FALHA ao instalar {line}: tipo={kind}; " + " ".join((out or "").strip().splitlines()[-4:]))
            results.append((line, False, msg))
    return results


def ensure_environment(args, assume_yes=False):
    """Verifica -> pergunta -> instala/repara -> verifica de novo. Devolve a lista final de itens."""
    deep = "--check" in args or "--repair" in args
    forced = "--reinstall" in args or "--check" in args or "--repair" in args
    say("Verificando o seu computador...")
    items, probe = gather(deep)
    log("Verificação inicial:")
    LOG.items(items, "estado")

    if "--reinstall" in args:
        if venv_state() == "ok" and ask_yes_no("Reinstalar TODOS os componentes (inclusive os que já funcionam)?"):
            specs = envcheck.load_specs(APP_DIR)
            say("Reinstalando todos os componentes (pode levar alguns minutos)...")
            install_plan([(s.line, True, s.required) for s in specs])
            items, probe = gather(deep)

    rounds = 0
    while True:
        problems = _problems(items)
        if not problems:
            break
        fp = _problem_fingerprint(problems)
        if not forced and not any(i.blocking for i in problems) and fp == _declined():
            log("opcionais ausentes já recusados antes; seguindo sem perguntar")
            break
        action = choose_action(problems, assume_yes=assume_yes)
        log(f"escolha do usuário: {action}")
        if action == "cancel":
            say("\nInicialização cancelada. Nada foi alterado.")
            sys.exit(0)
        if action == "skip":
            _remember_declined(fp)
            say("\nSeguindo sem instalar. Para instalar depois: Iniciar_Alicia.bat --repair")
            break

        if any(i.kind == "file" and i.blocking for i in problems):
            die("Arquivos essenciais da Alicia estão faltando; não dá para reparar sozinho.\n"
                "Baixe a Alicia de novo (seus dados e configurações não são afetados).\n" +
                "\n".join(i.detail for i in problems if i.kind == "file"))

        scope = "all" if action == "all" else "required"
        wanted = [i for i in problems if scope == "all" or i.required]
        say("")
        # internet só é exigida quando há o que baixar
        plan_items = items if venv_state() == "ok" else []
        needs_download = bool(envcheck.plan_install(plan_items, only_required=scope == "required")) or any(
            i.repair in ("venv_create", "venv_rebuild") for i in wanted) or venv_state() != "ok"
        if needs_download and not envcheck.internet_available():
            pkgs = [p[0].split(">")[0] for p in envcheck.plan_install(items, only_required=scope == "required")]
            names = ", ".join(pkgs) or ", ".join(i.label for i in wanted if i.kind in ("group", "venv"))
            say("[ERRO] Sem conexão com a internet neste momento. Não consegui baixar: " + (names or "os componentes") + ".")
            log("sem internet; instalação adiada")
            if ask_yes_no("Conectar à internet e tentar de novo agora?"):
                continue
            if any(i.blocking for i in problems):
                die("Faltam componentes obrigatórios e não há internet. Conecte-se e rode Iniciar_Alicia.bat de novo.")
            say("Seguindo sem instalar. Quando houver internet: Iniciar_Alicia.bat --repair")
            break

        _apply_repairs([i for i in wanted if i.kind in ("venv", "config")], scope)
        if venv_state() == "ok":
            # depois de criar/recriar a venv, todas as bibliotecas precisam ser reavaliadas
            items, probe = gather(deep)
            plan = envcheck.plan_install(items, only_required=scope == "required")
            if plan:
                say(f"Instalando {len(plan)} componente(s). Pode levar alguns minutos; não feche esta janela.\n")
                results = install_plan(plan)
                failed = [r for r in results if not r[1]]
            else:
                failed = []
        else:
            failed = [("ambiente virtual", False, "")]

        say("\nConferindo tudo de novo...")
        items, probe = gather(deep)
        LOG.items(items, "estado após instalação")
        rounds += 1
        remaining = _problems(items)
        if not remaining:
            say("[OK] Tudo instalado e funcionando.\n")
            break
        say("\nAinda há pendências:")
        for i in remaining:
            say(f"  - {i.label}: {i.detail}")
        if rounds >= 3:
            break
        if failed and ask_yes_no("Tentar instalar de novo os itens que falharam?"):
            continue
        # recusou novas tentativas
        if not any(i.blocking for i in remaining):
            _remember_declined(_problem_fingerprint(remaining))
            say("Os itens restantes são opcionais; a Alicia inicia sem eles.")
            break
        break

    if _problems(items):
        # tentativa de oferecer o Tesseract como alternativa, somente se o OCR não ficou disponível
        ocr = next((i for i in items if i.key == "ocr"), None)
        if ocr is not None and ocr.state != envcheck.OK and deep:
            offer_tesseract()
    log("Resultado final: " + ", ".join(f"{k}={len(v)}" for k, v in envcheck.summarize(items).items()))
    return items


# ------------------------------------------------------------------ 3a. modelo de visão LOCAL (opcional)
def _progress_bar(label):
    state = {"t": 0.0}

    def cb(done, total):
        if time.time() - state["t"] < 1.0 and done < total:
            return
        state["t"] = time.time()
        pct = f"{100 * done / total:.0f}%" if total else ""
        print(f"\r  {label}: {done / 1e6:.0f} MB de {total / 1e6:.0f} MB {pct}   ", end="", flush=True)
    return cb


def setup_local_vision(assume_yes=False):
    """--vision-local: instala llama-cpp-python na .venv e baixa o modelo. Nada acontece sem autorização."""
    from alicia_core import vlm_setup
    if venv_state() != "ok":
        die("Primeiro prepare o ambiente: rode Iniciar_Alicia.bat normalmente e depois use --vision-local.")
    py = subprocess.run([str(venv_python()), "-c", "import sys; print(sys.version_info[0]*100+sys.version_info[1])"],
                        capture_output=True, text=True).stdout.strip()
    if py.isdigit() and not (310 <= int(py) <= 312):
        say("[AVISO] O pacote llama-cpp-python só tem versão pronta para Python 3.10 a 3.12. Neste Python ele pode não instalar.")
        say("        Use o Python 3.12 de 64 bits (https://www.python.org/downloads/windows/).")
    say("\nModelo de visão LOCAL da Alicia")
    say("  O que é: um modelo de inteligência artificial que roda DENTRO do seu computador e descreve imagens")
    say("           (por exemplo, o que acontece no jogo). Nada da sua tela é enviado para a internet.")
    say("  Custo:   download de cerca de 3 GB (uma vez), uso de 4 a 6 GB de memória enquanto analisa,")
    say("           e o processador trabalha mais (sem placa de vídeo cada análise leva alguns segundos a dezenas de segundos).")
    say("  Fontes:  llama-cpp-python (PyPI/abetlen, versão pronta para CPU) e o modelo Qwen2.5-VL-3B (Hugging Face, arquivo conferido por SHA-256).")
    say("  É opcional: sem ele o Modo Gamer segue com a leitura local (OCR + movimento).\n")
    if not (assume_yes or ask_yes_no("Posso instalar o componente e baixar o modelo agora?")):
        say("Nada foi baixado nem alterado.")
        return
    if not envcheck.internet_available():
        die("Sem conexão com a internet neste momento. Conecte-se e rode Iniciar_Alicia.bat --vision-local de novo.")
    log("vision-local: autorizado pelo usuário")
    have = subprocess.run([str(venv_python()), "-c", "import llama_cpp"], capture_output=True).returncode == 0
    if not have:
        say("Instalando llama-cpp-python (versão pronta, sem compilar)...")
        rc, out = pip("install", "--prefer-binary", "--progress-bar", "off", *vlm_setup.PIP_ARGS, vlm_setup.PIP_PACKAGE,
                      live_label="Instalando llama-cpp-python")
        if rc != 0:
            kind, msg = envcheck.classify_pip_error(out)
            log(f"vision-local: FALHA ao instalar llama-cpp-python: tipo={kind}; " + " ".join((out or "").strip().splitlines()[-4:]))
            die(f"Não consegui instalar o llama-cpp-python. {msg}\nA Alicia continua funcionando sem o modelo local.")
        say("[OK] llama-cpp-python instalado.")
    try:
        main, mmproj = vlm_setup.describe_repo()
    except Exception as e:
        log(f"vision-local: falha ao consultar o repositório: {type(e).__name__}: {e}")
        die("Não consegui consultar o repositório do modelo (Hugging Face). Confira a internet e tente de novo.")
    dest = APP_DIR / "models" / "vision"
    say(f"Baixando o modelo: {main['name']} ({vlm_setup.human(main['size'])}) e {mmproj['name']} ({vlm_setup.human(mmproj['size'])})")
    for f in (main, mmproj):
        try:
            vlm_setup.download(f, dest, progress=_progress_bar(f["name"][:28]))
            print()
            say(f"[OK] {f['name']} baixado e conferido.")
        except KeyboardInterrupt:
            say("\n[AVISO] Download interrompido. Rode --vision-local de novo: ele continua de onde parou.")
            raise SystemExit(130)
        except Exception as e:
            print()
            log(f"vision-local: FALHA no download de {f['name']}: {e}")
            die(f"O download falhou: {e}")
    cfg_path = APP_DIR / "config.json"
    try:
        data = json.loads(cfg_path.read_text(encoding="utf-8")) if cfg_path.exists() else {}
        data["local_vlm_enabled"] = True
        cfg_path.write_text(json.dumps(data, indent=4, ensure_ascii=False), encoding="utf-8")
    except (OSError, ValueError) as e:
        say(f"[AVISO] Não consegui ligar a opção no config.json ({e}). Ligue manualmente: local_vlm_enabled = true.")
    log("vision-local: instalado e ligado")
    say("\n[OK] Modelo de visão local instalado e ligado (local_vlm_enabled). Teste com: Diagnostico_Alicia.bat")


# ------------------------------------------------------------------ 3b. programa externo opcional: Tesseract
TESSERACT_PAGE = "https://github.com/UB-Mannheim/tesseract/wiki"


def offer_tesseract():
    """Alternativa ao RapidOCR. Só é oferecida quando nenhum OCR funciona; nunca silenciosa."""
    if not IS_WIN:
        return
    say("\nO leitor de texto da tela (OCR) não está funcionando com o RapidOCR neste computador.")
    say("Alternativa: o programa Tesseract (externo, ~50 MB). Serve para a Alicia ler o texto que aparece na sua tela.")
    say("Ele NÃO é obrigatório: sem OCR a Alicia continua funcionando e observa só a janela ativa.")
    if not ask_yes_no("Quer instalar o Tesseract agora (o instalador abre uma janela para você acompanhar)?"):
        return
    ok = False
    if shutil.which("winget"):
        for pkg_id in ("tesseract-ocr.tesseract", "UB-Mannheim.TesseractOCR"):
            say(f"Pedindo ao winget (gerenciador oficial da Microsoft): {pkg_id}")
            r = subprocess.run(["winget", "install", "-e", "--id", pkg_id, "--interactive", "--accept-source-agreements"])
            if r.returncode == 0:
                ok = True
                break
    if not ok:
        say(f"Vou abrir a página oficial de downloads do Tesseract: {TESSERACT_PAGE}")
        try:
            webbrowser.open(TESSERACT_PAGE)
        except Exception:
            pass
        say("Instale o 'Tesseract OCR' (64 bits) e depois rode Iniciar_Alicia.bat --repair.")
        log("Tesseract: página oficial aberta para instalação manual")
        return
    say("Instalando o conector Python (pytesseract)...")
    rc, out = pip("install", "--prefer-binary", "pytesseract>=0.3.10", live_label="Instalando pytesseract")
    say("[OK] pytesseract instalado." if rc == 0 else "[ERRO] " + envcheck.classify_pip_error(out)[1])
    log(f"Tesseract: instalado via winget={ok}; pytesseract rc={rc}")


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
    """Parte do diagnóstico que não é de bibliotecas (essas o iniciador já verificou com os MESMOS critérios)."""
    cmd = [venv_python(), APP_DIR / "diagnostico.py", "--skip-libs"] + (["--quick"] if quick else [])
    return run(cmd).returncode


def main():
    global LOG
    args = set(sys.argv[1:])
    os.chdir(APP_DIR)
    say("\n=== Iniciador da Alicia ===\n")
    LOG = envcheck.InstallLog(APP_DIR)
    LOG.write("argumentos: " + (" ".join(sorted(args)) or "(nenhum)"))
    if "--vision" in args:
        say("[AVISO] A opção --vision não existe mais; ela foi removida junto com a integração antiga de visão.")

    check_python()
    if "--vision-local" in args:
        setup_local_vision(assume_yes="--yes" in args)
        return
    items = ensure_environment(args, assume_yes="--yes" in args)

    blocking = [i for i in items if i.blocking]
    if blocking:
        LOG.write("encerrado: faltam componentes obrigatórios: " + ", ".join(i.label for i in blocking))
        die("Ainda faltam componentes obrigatórios: " + ", ".join(i.label for i in blocking) + ".\n"
            "Rode Iniciar_Alicia.bat de novo para tentar outra vez (ou Iniciar_Alicia.bat --repair).\n"
            "Os detalhes técnicos estão em logs\\instalacao_alicia.log.")

    if "--check" in args or "--repair" in args:
        say("\n=== Resultado da verificação ===\n")
        say(envcheck.render_report(items))
    elif not _problems(items):
        say("Tudo certo no seu computador.")

    if ("--repair" in args or "--env-only" in args) and "--check" not in args:
        say("\nVerificação do ambiente concluída.")
        return

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

    if "--no-launch" in args:
        say("\nPreparação concluída (--no-launch).")
        return
    say("\nAbrindo a Alicia... (o nome e a conversa por teclado ficam NESTA janela; não a feche)\n")
    LOG.write("iniciando a Alicia")
    code = run([venv_python(), APP_DIR / "main.py"]).returncode
    sys.exit(code)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        say("\nCancelado.")
        sys.exit(130)
