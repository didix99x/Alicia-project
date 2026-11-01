#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Teste COMPLETO da Alicia no seu computador (roda dentro da .venv).

Exercita de verdade, um por um: ambiente, módulos, banco, API do modelo, conversa de ponta a ponta,
voz (gerar + tocar), microfone (gravar + reconhecer), captura de tela, visão, comandos
"Alicia, olha/ver/olhar" e interface. Grava tudo em  relatorio_alicia.txt  (a chave NUNCA aparece;
o conteúdo da sua tela e das respostas também não — só tamanhos, tempos e erros).

  python teste_completo.py                 tudo
  python teste_completo.py --sem-audio     não toca som nem grava microfone
  python teste_completo.py --rapido        pula voz, microfone e visão
  python teste_completo.py --foco          mede o microfone com a janela em foco x em segundo plano
"""

import importlib
import importlib.metadata as md
import os
import pathlib
import platform
import shutil
import sys
import tempfile
import threading
import time
import traceback

APP_DIR = pathlib.Path(__file__).parent.resolve()
os.chdir(APP_DIR)
sys.path.insert(0, str(APP_DIR))
ARGS = set(sys.argv[1:])
NO_AUDIO = "--sem-audio" in ARGS or "--rapido" in ARGS
FAST = "--rapido" in ARGS

from alicia_core.env import load_env_file  # noqa: E402
from alicia_core.logging_setup import redact  # noqa: E402

load_env_file(".env")
REPORT = []     # linhas do relatório
RESULTS = []    # (status, nome)


def log(line=""):
    print(line, flush=True)
    REPORT.append(redact(line))


def step(name):
    def deco(fn):
        def run():
            log(f"\n--- {name}")
            t0 = time.time()
            try:
                status, detail = fn()
            except Exception as e:
                status, detail = "FALHA", f"{type(e).__name__}: {e}\n{traceback.format_exc(limit=6)}"
            RESULTS.append((status, name))
            log(f"[{status}] {name} ({time.time() - t0:.1f}s)")
            for ln in str(detail).splitlines():
                log(f"        {ln}")
        run.__name__ = fn.__name__
        return run
    return deco


# ---------------------------------------------------------------------- 1. ambiente
@step("1. Ambiente")
def t_env():
    pkgs = ["requests", "Pillow", "PyQt6", "edge-tts", "SpeechRecognition", "sounddevice", "mss", "psutil", "pygame"]
    versions = []
    for p in pkgs:
        try:
            versions.append(f"{p}=={md.version(p)}")
        except md.PackageNotFoundError:
            versions.append(f"{p}: ausente")
    warn = []
    if any(ord(c) > 127 for c in str(APP_DIR)):
        warn.append("A pasta do projeto tem caracteres com acento; se algo falhar, mova para C:\\Alicia.")
    return ("AVISO" if warn else "OK"), "\n".join([
        f"Windows/SO: {platform.platform()}", f"Python: {sys.version.split()[0]} ({platform.architecture()[0]})",
        f"Pasta: {APP_DIR}", "Pacotes: " + ", ".join(versions)] + warn)


# ---------------------------------------------------------------------- 2. módulos
@step("2. Importar cada módulo da Alicia")
def t_modules():
    bad = []
    for f in sorted((APP_DIR / "alicia_core").rglob("*.py")):
        mod = ".".join(f.relative_to(APP_DIR).with_suffix("").parts).replace(".__init__", "")
        try:
            importlib.import_module(mod)
        except BaseException as e:
            bad.append(f"{mod}: {type(e).__name__}: {e}")
    return ("FALHA", "\n".join(bad)) if bad else ("OK", "todos importam")


# ---------------------------------------------------------------------- 3. banco
@step("3. Banco de dados (cópia temporária; o seu histórico não é tocado)")
def t_db():
    from alicia_core.database import MemoryDatabase
    tmp = tempfile.mkdtemp()
    db = MemoryDatabase(os.path.join(tmp, "t.db"))
    db.set_user_name("Teste")
    ok = db.add_exchange("pergunta", "resposta", "feliz", "t1") and not db.add_exchange("pergunta", "resposta", "feliz", "t1")
    ok = ok and MemoryDatabase(os.path.join(tmp, "t.db")).get_user_name() == "Teste" and db.check_integrity()
    shutil.rmtree(tmp, ignore_errors=True)
    real = APP_DIR / "alicia_memory.db"
    extra = ""
    if real.exists():
        r = MemoryDatabase(str(real))
        extra = f"Seu banco real: íntegro={r.check_integrity()}, mensagens={r.count_chat_messages()}"
    return ("OK" if ok else "FALHA"), "gravar, ler, anti-duplicação e persistência " + ("ok" if ok else "falharam") + ("\n" + extra if extra else "")


# ---------------------------------------------------------------------- 4. API
@step("4. API da NVIDIA (chave, lista de modelos e teste real de chat)")
def t_api():
    from alicia_core.env import is_placeholder
    from alicia_core.nvidia_client import DEFAULT_BASE_URL, discover_model
    key = os.environ.get("NVIDIA_API_KEY", "")
    if is_placeholder(key):
        return "FALHA", "NVIDIA_API_KEY não configurada no .env"
    t0 = time.time()
    res = discover_model(key, os.environ.get("NVIDIA_BASE_URL") or DEFAULT_BASE_URL, os.environ.get("NVIDIA_MODEL", ""))
    if not res["ok"]:
        return "FALHA", f"{res['kind']}: {res.get('detail', '')}"
    if not res["chosen"]:
        return "FALHA", "nenhum modelo respondeu:\n" + "\n".join(f"{m}: {k} - {d}" for m, k, d in res["tried"])
    note = f"modelo configurado '{res['configured']}' NÃO respondeu; rode Iniciar_Alicia.bat para trocar" if res["changed"] else "modelo configurado respondeu"
    return ("AVISO" if res["changed"] else "OK"), f"modelo que respondeu: {res['chosen']} ({time.time() - t0:.1f}s)\n{note}"


# ---------------------------------------------------------------------- 5. conversa ponta a ponta
CORE = {"obj": None, "tmp": None}


def make_core():
    if CORE["obj"] is not None:
        return CORE["obj"]
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    CORE["tmp"] = tempfile.mkdtemp(prefix="alicia_teste_")
    shutil.copytree(APP_DIR / "assets", pathlib.Path(CORE["tmp"]) / "assets", dirs_exist_ok=True) if (APP_DIR / "assets").exists() else None
    os.chdir(CORE["tmp"])
    from alicia_core.core import AliciaAssistantCore
    core = AliciaAssistantCore(config_path="config.json")
    core.db.set_user_name("Teste")
    core.db.set_profile("personality", "normal")
    core.chat_ready.set()
    core.is_running = True
    CORE["obj"] = core
    return core


@step("5. Conversa de ponta a ponta (histórico e banco temporários)")
def t_chat():
    core = make_core()
    t0 = time.time()
    r1 = core.interact("Bom dia! Responda em uma frase curta.")
    t1 = time.time() - t0
    r2 = core.interact("Agora me diga quanto é 7 vezes 8, só o número.")
    dup = core.interact("Agora me diga quanto é 7 vezes 8, só o número.")
    rows = core.db.get_chat_messages(20)
    problems = []
    if not r1 or "NVIDIA" in r1 or "chave" in r1:
        problems.append(f"1ª resposta ruim: {r1[:120]!r}")
    if not r2 or "56" not in r2:
        problems.append(f"2ª resposta não trouxe 56: {r2[:120]!r}")
    if dup:
        problems.append("mensagem duplicada NÃO foi descartada")
    if len(rows) != 4:
        problems.append(f"esperava 4 linhas no histórico, há {len(rows)}")
    return ("FALHA", "\n".join(problems)) if problems else ("OK", f"respondeu em {t1:.1f}s; histórico com 4 mensagens; duplicata descartada")


# ---------------------------------------------------------------------- 6. comandos de voz
@step("6. Reconhecimento dos comandos 'Alicia, olha / ver / olhar'")
def t_commands():
    core = make_core()
    yes = ["Alicia, olha", "Alícia, ver", "alicia olhar", "Alícia olha a tela", "Alicia, olhe"]
    no = ["Bom dia, Alicia.", "como faço um bolo?"]
    bad = [p for p in yes if not core._is_direct_visual_command(p)] + [p for p in no if core._is_direct_visual_command(p)]
    return ("FALHA", "erros em: " + ", ".join(bad)) if bad else ("OK", f"{len(yes)} comandos reconhecidos; {len(no)} frases normais ignoradas")


# ---------------------------------------------------------------------- 7. captura de tela
@step("7. Captura de tela")
def t_screen():
    core = make_core()
    b64, img, h = core.screen_engine.capture_and_encode()
    if not b64 or img is None:
        return "FALHA", "a captura voltou vazia (sessão sem tela? tela bloqueada?)"
    return "OK", f"capturou {img.size[0]}x{img.size[1]} ({len(b64) // 1024} KB em base64)"


# ---------------------------------------------------------------------- 8. visão
@step("8. Visão: 'Alicia, olha' de ponta a ponta (o conteúdo não vai para o relatório)")
def t_vision():
    if FAST:
        return "PULADO", "--rapido"
    core = make_core()
    t0 = time.time()
    out = core.interact("Alicia, olha")
    backend = "visão local"
    if not out or "não está disponível" in out or "não consegui" in out.lower() or "greve" in out:
        return "FALHA", f"resposta: {out[:140]!r}\nNVIDIA visão: {core.vision.remote.model if core.vision.remote else None}"
    return "OK", f"respondeu via {backend} em {time.time() - t0:.1f}s ({len(out)} caracteres)"


# ---------------------------------------------------------------------- 9. voz (saída)
@step("9. Voz: gerar com Edge TTS e tocar (você deve OUVIR 'Teste de voz da Alicia')")
def t_tts():
    if FAST:
        return "PULADO", "--rapido"
    from alicia_core import deps
    if not deps.HAS_AUDIO:
        return "FALHA", f"voz indisponível: {deps.AUDIO_ERROR}"
    import asyncio
    import edge_tts
    path = os.path.join(tempfile.gettempdir(), "alicia_teste_voz.mp3")
    voice = "pt-BR-FranciscaNeural"
    try:
        voice = getattr(make_core().config, "tts_voice", voice) or voice
    except Exception:
        pass
    asyncio.run(edge_tts.Communicate("Teste de voz da Alicia.", voice).save(path))
    size = os.path.getsize(path)
    if size < 1000:
        return "FALHA", f"áudio gerado muito pequeno ({size} bytes) — sem internet?"
    if NO_AUDIO:
        return "OK", f"gerou {size // 1024} KB (reprodução pulada por --sem-audio)"
    from alicia_core.audio_output import play_mp3
    t0 = time.time()
    play_mp3(path)
    dur = time.time() - t0
    os.remove(path)
    return ("OK" if dur > 0.8 else "AVISO"), f"gerou {size // 1024} KB e tocou por {dur:.1f}s via {deps.AUDIO_BACKEND}" + ("" if dur > 0.8 else " (curto demais: confira o volume)")


# ---------------------------------------------------------------------- 10. microfone
@step("10. Microfone: gravar 4 s e reconhecer (FALE ALGO quando pedir)")
def t_mic():
    if FAST or NO_AUDIO:
        return "PULADO", "--sem-audio/--rapido"
    from alicia_core import deps
    from alicia_core.audio_input import list_microphones, open_microphone
    if not deps.HAS_MIC:
        return "FALHA", f"microfone indisponível: {deps.MIC_ERROR}"
    mics = list_microphones()
    if not mics:
        return "FALHA", "nenhum dispositivo de entrada. Confira Configurações do Windows > Privacidade > Microfone"
    import array
    import math
    log(f"        dispositivos: {len(mics)}  (usando o padrão do Windows)")
    log("        >>> FALE AGORA por 4 segundos...")
    src = open_microphone(None)
    chunks = []
    with src as s:
        end = time.time() + 4
        while time.time() < end:
            chunks.append(s.stream.read(s.CHUNK))
    data = b"".join(chunks)
    samples = array.array("h", data[: len(data) // 2 * 2])
    peak = max((abs(x) for x in samples), default=0)
    rms = math.sqrt(sum(x * x for x in samples) / max(len(samples), 1))
    detail = f"{len(data) // 1024} KB a {src.SAMPLE_RATE} Hz; nível: pico={peak}, rms={rms:.0f}"
    if peak < 300:
        return "FALHA", detail + "\nSEM SINAL: microfone mudo, bloqueado pelo Windows ou dispositivo errado."
    try:
        import speech_recognition as sr
        audio = sr.AudioData(data, src.SAMPLE_RATE, 2)
        text = sr.Recognizer().recognize_google(audio, language="pt-BR")
        return "OK", detail + f"\nreconheceu {len(text.split())} palavra(s)"
    except Exception as e:
        return "AVISO", detail + f"\ncaptou áudio, mas o reconhecimento falhou: {type(e).__name__} (internet? fale mais alto?)"


@step("10b. Microfone em SEGUNDO PLANO (--foco): a Alicia ainda ouve com outra janela em foco?")
def t_focus():
    if "--foco" not in ARGS:
        return "PULADO", "rode com --foco para medir"
    from alicia_core import deps
    from alicia_core.audio_input import measure_capture, open_microphone
    from alicia_core.winperf import harden_process
    if not deps.HAS_MIC:
        return "FALHA", f"microfone indisponível: {deps.MIC_ERROR}"
    applied = {} if "--sem-ajustes" in ARGS else harden_process()
    log(f"        ajustes do Windows aplicados: {applied or 'NENHUM (--sem-ajustes)'}")
    out = {}
    with open_microphone(None) as src:
        log("        >>> 1/2: deixe ESTA janela em foco e FALE por 5 s...")
        out["foco"] = measure_capture(src, 5)
        log("        >>> 2/2: em 4 s clique em OUTRA janela (navegador, Bloco de Notas...) e continue FALANDO por 10 s")
        time.sleep(4)
        out["fundo"] = measure_capture(src, 10)
    for k, v in out.items():
        log(f"        {k}: {v}")
    f, b = out["foco"], out["fundo"]
    detail = f"em foco: pausa máx {f['max_gap']}s, estouros {f['overflows']} | em segundo plano: pausa máx {b['max_gap']}s, estouros {b['overflows']}"
    if b["peak"] < 300:
        return "AVISO", detail + "\nnão ouvi voz em segundo plano (microfone mudo ou você não falou?)"
    if b["max_gap"] > 0.5 or b["overflows"] > f["overflows"] + 2 or b["chunks"] < b["expected"] * 0.8:
        return "FALHA", detail + "\nCONFIRMADO: o Windows atrasa a leitura do microfone quando a janela perde o foco. Desative o modo de eficiência (Gerenciador de Tarefas) do Python e do Windows Terminal."
    return "OK", detail + "\nem segundo plano a captura continua regular"


# ---------------------------------------------------------------------- 11. interface
@step("11. Interface (avatar) — inicialização sem abrir janela")
def t_ui():
    if not importlib.util.find_spec("PyQt6"):
        return "FALHA", "PyQt6 ausente"
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtWidgets import QApplication
    from alicia_core.enums import AliciaExpression
    from alicia_core.ui.desktop_pet import AliciaDesktopPetUI
    app = QApplication.instance() or QApplication(sys.argv[:1])
    core = make_core()
    ui = AliciaDesktopPetUI(core)
    n = len(ui.pixmaps)
    for expr in AliciaExpression:
        core.current_expression = expr
        ui.atualizar_visual()
    ui.close()
    ok = n == len(list(AliciaExpression))
    return ("OK" if ok else "AVISO"), f"{n} imagens de expressão carregadas" + ("" if ok else " (faltam algumas; usando círculos)")


@step("11b. Pôquer: motor (100 mãos) e mesa na interface REAL jogando uma mão pelos botões (sem mostrar janela)")
def t_poker():
    import random
    from alicia_core.poker.ai import AliciaAI
    from alicia_core.poker.cards import Deck
    from alicia_core.poker.engine import ALICIA, HUMAN, Config, Hand, Phase
    rng = random.Random(7)
    ai = AliciaAI(rng=random.Random(3), samples=40)
    for _ in range(100):                                    # 1) motor + IA, sem janela
        h = Hand([1000, 1000], rng.randrange(2), Config(), Deck(rng))
        while not h.is_over:
            if h.phase == Phase.DRAW:
                p = h.draw_turn()
                h.draw(p, ai.choose_discards(h.observation_for(p))[0] if p == ALICIA else [])
                continue
            p = h.to_act
            if p == ALICIA:
                d = ai.decide(h.observation_for(ALICIA)); h.act(ALICIA, d.kind, d.amount)
            else:
                legal = h.legal_actions(HUMAN); h.act(HUMAN, "check" if "check" in legal else "call")
        assert sum(h.final_stacks) == 2000, "fichas não conservadas"
    if FAST:
        return "OK", "motor e IA: 100 mãos com fichas conservadas (interface pulada por --rapido)"
    if not importlib.util.find_spec("PyQt6"):
        return "FALHA", "PyQt6 ausente"
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtWidgets import QApplication
    from alicia_core.ui import poker_window
    from alicia_core.ui.desktop_pet import AliciaDesktopPetUI
    app = QApplication.instance() or QApplication(sys.argv[:1])
    core = make_core()
    core.config.poker_voice = False                         # sem som durante o teste
    ui = AliciaDesktopPetUI(core)
    poker_window.ERRORS.clear()
    win = poker_window.PokerWindow(core, ui)
    win.show()

    def pump(sec):
        end = time.time() + sec
        while time.time() < end:
            app.processEvents()
            time.sleep(0.01)

    done = False
    for _ in range(300):                                    # até ~45 s (a Alicia "pensa" ~1 s por jogada)
        pump(0.15)
        if win.btn_next.isVisible() or win._last_snap.get("match_over"):
            done = True
            break
        if win.btn_draw.isEnabled():
            win.my_cards[0].click()
            win.btn_draw.click()
        elif win.btn_call.isEnabled():
            win.btn_call.click()
    snap = win.controller.snapshot()
    errors = list(poker_window.ERRORS)
    win.close()
    ui.close()
    if errors:
        return "FALHA", "erros de interface capturados:\n" + "\n".join(errors[:8])
    if not done:
        return "FALHA", "a mão não terminou jogando pelos botões da interface"
    return "OK", f"mão concluída pela interface: {snap['summary']['text']} (fichas {snap['stacks']})"


# ---------------------------------------------------------------------- 12. encerramento
@step("12. Encerramento limpo")
def t_shutdown():
    core = CORE["obj"]
    if core is not None:
        core.is_running = False
    time.sleep(0.5)
    alive = [t.name for t in threading.enumerate() if t is not threading.main_thread() and not t.daemon]
    os.chdir(APP_DIR)
    if CORE["tmp"]:
        shutil.rmtree(CORE["tmp"], ignore_errors=True)
    return ("OK" if not alive else "AVISO"), "sem threads pendentes" if not alive else f"threads não-daemon ainda vivas: {alive}"


def tail_log(n=40):
    p = APP_DIR / "logs" / "alicia_system.log"
    if not p.exists():
        return ["(sem log ainda)"]
    return [redact(x) for x in p.read_text(encoding="utf-8", errors="ignore").splitlines()[-n:]]


def main():
    log("=== TESTE COMPLETO DA ALICIA ===")
    log(time.strftime("%Y-%m-%d %H:%M:%S"))
    for fn in (t_env, t_modules, t_db, t_api, t_chat, t_commands, t_screen, t_vision, t_tts, t_mic, t_focus, t_ui, t_poker, t_shutdown):
        fn()
    counts = {s: sum(1 for r in RESULTS if r[0] == s) for s in ("OK", "AVISO", "FALHA", "PULADO")}
    log("\n=== RESUMO ===")
    for status, name in RESULTS:
        log(f"[{status:6}] {name}")
    log(f"\nOK={counts['OK']}  AVISO={counts['AVISO']}  FALHA={counts['FALHA']}  PULADO={counts['PULADO']}")
    log("\n--- últimas linhas do log (chaves mascaradas) ---")
    for line in tail_log():
        log("  " + line)
    out = APP_DIR / "relatorio_alicia.txt"
    out.write_text("\n".join(REPORT) + "\n", encoding="utf-8")
    log(f"\nRelatório salvo em: {out}")
    log("Se algo falhou, envie esse arquivo (ele não contém a sua chave nem o conteúdo da sua tela).")
    sys.exit(1 if counts["FALHA"] else 0)


if __name__ == "__main__":
    main()
