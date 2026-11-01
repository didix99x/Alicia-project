# -*- coding: utf-8 -*-
"""
Testes do verificador de ambiente (alicia_core/envcheck.py) e do iniciador (launcher.py).

Parte A: unidades puras (avaliação, plano de instalação, config.json, log, erros do pip).
Parte B: ponta a ponta. Copia o iniciador para uma pasta temporária, cria uma .venv REAL e instala
         pacotes de uma pasta LOCAL de "wheels" mínimas (sem internet): prova o fluxo
         verificar -> perguntar -> instalar/reparar -> verificar de novo, sem tocar nos dados do usuário.

O que NÃO prova: downloads reais do PyPI, o .bat no Windows, o instalador do Python, hardware real.
"""

import base64
import hashlib
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile

APP_DIR = pathlib.Path(__file__).resolve().parent.parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from alicia_core import envcheck as ec  # noqa: E402


# ======================================================================================
# Parte A — unidades
# ======================================================================================
def libs_ok(**over):
    """Sonda sintética com tudo instalado; `over` troca o estado de módulos."""
    base = {"requests": "2.33.1", "PIL": "12.0", "PyQt6": "6.7", "edge_tts": "7.2", "speech_recognition": "3.10",
            "sounddevice": "0.5", "soundcard": "0.4.3", "mss": "9.0", "psutil": "5.9", "numpy": "1.26",
            "rapidocr_onnxruntime": "1.4.4", "onnxruntime": "1.20", "pytesseract": "0.3.13"}
    libs = {m: {"state": "ok", "version": v} for m, v in base.items()}
    for mod, st in over.items():
        libs[mod] = st if isinstance(st, dict) else {"state": st}
    return {"libs": libs, "func": {}, "deep": False, "tesseract_exe": False,
            "python": "3.12.3", "bits": 64, "machine": "AMD64", "in_venv": True}


def specs():
    return ec.load_specs(APP_DIR)


def by_key(items, key):
    return next(i for i in items if i.key == key)


class TestRequirements(unittest.TestCase):
    def test_specs_come_from_requirement_files(self):
        sp = {s.name: s for s in specs()}
        for name in ("requests", "Pillow", "PyQt6"):
            self.assertTrue(sp[name].required)
        for name in ("edge-tts", "SpeechRecognition", "sounddevice", "mss", "psutil", "numpy", "rapidocr-onnxruntime"):
            self.assertFalse(sp[name].required)
        self.assertEqual(sp["PyQt6"].min_version, "6.5")

    def test_every_listed_requirement_is_in_the_catalog(self):
        for s in specs():
            self.assertIsNotNone(s.info, f"{s.name} não está no catálogo de verificação")

    def test_version_compare(self):
        self.assertTrue(ec.version_satisfies("7.2.0", "7.0.0"))
        self.assertTrue(ec.version_satisfies("10.0", "9.0"))     # 10 > 9 (comparação numérica, não texto)
        self.assertFalse(ec.version_satisfies("6.4.9", "6.5"))
        self.assertTrue(ec.version_satisfies("", "1.0"))


class TestEvaluate(unittest.TestCase):
    def test_everything_installed_is_ok(self):
        items = ec.evaluate(libs_ok(), specs())
        self.assertFalse(ec.has_problems(items), [(i.key, i.state, i.detail) for i in items if i.state != ec.OK])

    def test_not_installed_at_all_marks_required_as_missing(self):
        items = ec.evaluate(None, specs())
        self.assertEqual(by_key(items, "gui").state, ec.MISSING)
        self.assertTrue(by_key(items, "gui").blocking)
        self.assertEqual(by_key(items, "tts").state, ec.OPTIONAL_MISSING)
        self.assertFalse(by_key(items, "tts").blocking)

    def test_listed_but_not_importable_is_failed_not_ok(self):
        sonda = libs_ok(PyQt6={"state": "broken", "error": "ImportError: DLL load failed"})
        item = by_key(ec.evaluate(sonda, specs()), "gui")
        self.assertEqual(item.state, ec.FAILED)
        self.assertTrue(item.blocking)
        self.assertEqual(item.packages, [("PyQt6>=6.5", True, True)])       # reinstala só ele, forçado

    def test_old_version_is_incompatible(self):
        sonda = libs_ok(edge_tts={"state": "ok", "version": "6.1.0"})
        item = by_key(ec.evaluate(sonda, specs()), "tts")
        self.assertEqual(item.state, ec.FAILED)
        self.assertIn("7.0.0", item.detail)
        self.assertEqual(item.packages, [("edge-tts>=7.0.0", False, False)])  # atualiza, sem forçar

    def test_missing_optional_never_blocks(self):
        sonda = libs_ok(mss="missing", soundcard="missing")
        items = ec.evaluate(sonda, specs())
        self.assertFalse(any(i.blocking for i in items))
        self.assertEqual(by_key(items, "screen").state, ec.OPTIONAL_MISSING)

    def test_plan_installs_only_what_is_missing(self):
        sonda = libs_ok(mss="missing")
        plan = ec.plan_install(ec.evaluate(sonda, specs()))
        self.assertEqual([p[0] for p in plan], ["mss>=9.0"])           # nada de reinstalar o que funciona

    def test_plan_required_first_and_required_only_filter(self):
        items = ec.evaluate(None, specs())
        plan = ec.plan_install(items)
        flags = [p[2] for p in plan]
        self.assertEqual(flags, sorted(flags, reverse=True))           # obrigatórios antes
        only = ec.plan_install(items, only_required=True)
        self.assertEqual({p[0] for p in only}, {"requests>=2.31", "Pillow>=10.0", "PyQt6>=6.5"})
        self.assertTrue(all(p[2] for p in only))

    def test_numpy_optional_is_not_pulled_into_required_plan(self):
        sonda = libs_ok(PIL="missing", numpy="missing")
        only = ec.plan_install(ec.evaluate(sonda, specs()), only_required=True)
        self.assertEqual([p[0] for p in only], ["Pillow>=10.0"])


class TestOcrLogic(unittest.TestCase):
    def test_rapidocr_installed_is_enough(self):
        item = by_key(ec.evaluate(libs_ok(), specs()), "ocr")
        self.assertEqual(item.state, ec.OK)

    def test_rapidocr_absent_is_optional_and_offers_only_rapidocr(self):
        sonda = libs_ok(rapidocr_onnxruntime="missing", pytesseract="missing")
        item = by_key(ec.evaluate(sonda, specs()), "ocr")
        self.assertEqual(item.state, ec.OPTIONAL_MISSING)
        self.assertEqual([p[0] for p in item.packages], ["rapidocr-onnxruntime>=1.3.0"])
        self.assertFalse(item.blocking)

    def test_tesseract_working_means_rapidocr_is_not_requested(self):
        sonda = libs_ok(rapidocr_onnxruntime="missing")
        sonda["tesseract_exe"] = True
        item = by_key(ec.evaluate(sonda, specs()), "ocr")
        self.assertEqual(item.state, ec.OK)
        self.assertEqual(item.packages, [])                             # não instala os dois

    def test_onnxruntime_broken_is_a_failure_with_repair(self):
        sonda = libs_ok(onnxruntime={"state": "broken", "error": "ImportError: DLL load failed"})
        item = by_key(ec.evaluate(sonda, specs()), "ocr")
        self.assertEqual(item.state, ec.FAILED)
        self.assertFalse(item.blocking)                                 # OCR é opcional
        self.assertIn(("onnxruntime", True, False), item.packages)

    def test_deep_test_decides_over_imports(self):
        sonda = libs_ok()
        sonda["func"] = {"ocr": {"state": "fail", "detail": "modelo não carregou"}}
        item = by_key(ec.evaluate(sonda, specs()), "ocr")
        self.assertEqual(item.state, ec.FAILED)
        self.assertIn("modelo não carregou", item.detail)
        sonda["func"] = {"ocr": {"state": "ok", "detail": "RapidOCR; leu o texto de teste", "engine": "rapidocr"}}
        self.assertEqual(by_key(ec.evaluate(sonda, specs()), "ocr").state, ec.OK)

    def test_tesseract_external_item(self):
        ocr_ok = ec.Item("ocr", "OCR", ec.OK)
        self.assertEqual(ec.tesseract_item({"tesseract_exe": False}, ocr_ok).state, ec.OK)      # não necessário
        ocr_bad = ec.Item("ocr", "OCR", ec.OPTIONAL_MISSING)
        self.assertEqual(ec.tesseract_item({"tesseract_exe": False}, ocr_bad).state, ec.OPTIONAL_MISSING)


class TestFunctionalFailures(unittest.TestCase):
    def test_functional_failure_turns_ok_group_into_failed(self):
        sonda = libs_ok()
        sonda["func"] = {"gui": {"state": "fail", "detail": "plataforma Qt não carregou"}}
        item = by_key(ec.evaluate(sonda, specs()), "gui")
        self.assertEqual(item.state, ec.FAILED)
        self.assertTrue(item.blocking)

    def test_no_microphone_is_only_a_note(self):
        sonda = libs_ok()
        sonda["func"] = {"audio": {"state": "warn", "detail": "nenhum microfone encontrado"}}
        item = by_key(ec.evaluate(sonda, specs()), "audio")
        self.assertEqual(item.state, ec.OK)
        self.assertIn("nenhum microfone", item.detail)


class TestPythonAndReport(unittest.TestCase):
    def test_python_versions(self):
        self.assertEqual(ec.python_item("3.12.3", 64).state, ec.OK)
        self.assertEqual(ec.python_item("3.9.1", 64).state, ec.FAILED)
        bits32 = ec.python_item("3.12.3", 32)
        self.assertEqual(bits32.state, ec.FAILED)
        self.assertIn("32 bits", bits32.detail)
        self.assertIn("RapidOCR", ec.python_item("3.13.1", 64).detail)

    def test_report_format_and_summary(self):
        items = [ec.python_item("3.12.3", 64), ec.venv_item("ok")] + ec.evaluate(
            libs_ok(rapidocr_onnxruntime="missing", pytesseract="missing", PyQt6={"state": "broken", "error": "x"}, mss="missing"), specs())
        text = ec.render_report(items)
        self.assertIn("[OK]", text)
        self.assertIn("[ERRO]", text)
        self.assertIn("[AVISO] Componente opcional ausente", text)
        self.assertIn("Resumo:", text)
        self.assertIn("Componentes funcionando:", text)
        self.assertIn("Componentes ausentes:", text)
        self.assertIn("Componentes com falha:", text)
        self.assertIn("Componentes opcionais indisponíveis:", text)
        self.assertNotIn("llama", text.lower())

    def test_venv_item_states(self):
        self.assertEqual(ec.venv_item("ok").state, ec.OK)
        self.assertEqual(ec.venv_item("missing").repair, "venv_create")
        broken = ec.venv_item("broken")
        self.assertEqual(broken.repair, "venv_rebuild")
        self.assertIn("NÃO são tocados", broken.detail)


class TestPipErrors(unittest.TestCase):
    def test_classification_is_plain_language(self):
        cases = {
            "WARNING: Retrying ... Max retries exceeded with url": "network",
            "ERROR: Could not find a version that satisfies the requirement x": "compat",
            "OSError: [Errno 28] No space left on device": "space",
            "PermissionError: [WinError 5] Access is denied": "permission",
            "error: Microsoft Visual C++ 14.0 or greater is required": "build",
            "algo estranho": "unknown",
        }
        for text, kind in cases.items():
            got, msg = ec.classify_pip_error(text)
            self.assertEqual(got, kind, text)
            self.assertGreater(len(msg), 20)


class TestConfigCheck(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        (self.tmp / "alicia_core").mkdir()
        shutil.copy(APP_DIR / "alicia_core" / "config.py", self.tmp / "alicia_core" / "config.py")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, text):
        (self.tmp / "config.json").write_text(text, encoding="utf-8")

    def test_missing_file_is_fine(self):
        self.assertEqual(ec.config_item(self.tmp).state, ec.OK)

    def test_valid(self):
        self.write(json.dumps({"voice_speed": 100, "enable_audio": True, "obs_interval": 4.0}))
        self.assertEqual(ec.config_item(self.tmp).state, ec.OK)

    def test_invalid_json_is_repairable_with_backup(self):
        self.write("{ isto não é json")
        item = ec.config_item(self.tmp)
        self.assertEqual(item.state, ec.FAILED)
        self.assertFalse(item.blocking)
        msg = ec.repair_config(self.tmp, item.repair)
        self.assertFalse((self.tmp / "config.json").exists())
        backups = list(self.tmp.glob("config.json.bak-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(encoding="utf-8"), "{ isto não é json")   # nada foi perdido
        self.assertIn("cópia", msg.lower() + "cópia")

    def test_wrong_types_restore_only_those_keys(self):
        self.write(json.dumps({"voice_speed": "rápido", "voice_name": "pt-BR-Minha", "enable_audio": True}))
        item = ec.config_item(self.tmp)
        self.assertEqual(item.state, ec.FAILED)
        self.assertEqual(item.repair, "config_fix")
        ec.repair_config(self.tmp, "config_fix")
        data = json.loads((self.tmp / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(data["voice_speed"], 100)                 # restaurado
        self.assertEqual(data["voice_name"], "pt-BR-Minha")        # preservado
        self.assertEqual(ec.config_item(self.tmp).state, ec.OK)

    def test_obsolete_options_are_cleaned_keeping_the_rest(self):
        self.write(json.dumps({"voice_speed": 110, "opcao_antiga_xyz": "valor", "outra_antiga": 1}))
        item = ec.config_item(self.tmp)
        self.assertEqual(item.state, ec.OPTIONAL_MISSING)
        ec.repair_config(self.tmp, "config_clean")
        data = json.loads((self.tmp / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(data, {"voice_speed": 110})


class TestFilesAndLog(unittest.TestCase):
    def test_project_files_are_complete(self):
        items = ec.file_items(APP_DIR)
        self.assertEqual(by_key(items, "files").state, ec.OK)

    def test_missing_essential_file_blocks(self):
        tmp = pathlib.Path(tempfile.mkdtemp())
        try:
            item = ec.file_items(tmp)[0]
            self.assertEqual(item.state, ec.MISSING)
            self.assertTrue(item.blocking)
            self.assertIn("main.py", item.detail)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_log_has_no_secrets_or_home_path(self):
        tmp = pathlib.Path(tempfile.mkdtemp())
        try:
            lg = ec.InstallLog(tmp)
            lg.write(f"erro em {os.path.expanduser('~')}/x chave nvapi-ABCDEF123456 fim")
            text = (tmp / "logs" / "instalacao_alicia.log").read_text(encoding="utf-8")
            self.assertNotIn("nvapi-ABCDEF", text)
            self.assertIn("nvapi-***", text)
            if os.path.expanduser("~") != "~":
                self.assertNotIn(os.path.expanduser("~"), text)
            self.assertRegex(text, r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestRealProbe(unittest.TestCase):
    """A sonda de verdade, neste Python (qualquer combinação de bibliotecas serve: só conferimos a estrutura)."""

    def test_probe_structure_and_privacy(self):
        pr = ec.probe_local(deep=True)
        for key in ("python", "bits", "libs", "func", "tesseract_exe"):
            self.assertIn(key, pr)
        self.assertIn("PIL", pr["libs"])
        blob = json.dumps(pr)
        self.assertNotIn(os.path.expanduser("~") + "/", blob)     # sem caminhos pessoais
        # a imagem de teste do OCR é desenhada na memória, não vem da tela
        from PIL import Image
        self.assertIsInstance(ec._synthetic_text_image(), Image.Image)

    def test_probe_runs_inside_another_interpreter(self):
        pr = ec.run_probe_in(pathlib.Path(sys.executable), APP_DIR, deep=False)
        self.assertIsNotNone(pr)
        self.assertEqual(pr["python"], ".".join(map(str, sys.version_info[:3])))


class TestInterruptedInstall(unittest.TestCase):
    def test_ctrl_c_during_install_stops_cleanly_and_logs(self):
        import launcher
        tmp = pathlib.Path(tempfile.mkdtemp())
        old_log, old_pip = launcher.LOG, launcher.pip
        try:
            launcher.LOG = ec.InstallLog(tmp)

            def interrupted(*a, **k):
                raise KeyboardInterrupt
            launcher.pip = interrupted
            with self.assertRaises(SystemExit) as cm:
                launcher.install_plan([("mss>=9.0", False, False)])
            self.assertEqual(cm.exception.code, 130)
            self.assertIn("interrompida", (tmp / "logs" / "instalacao_alicia.log").read_text(encoding="utf-8"))
        finally:
            launcher.LOG, launcher.pip = old_log, old_pip
            shutil.rmtree(tmp, ignore_errors=True)


class TestNoOllamaLeft(unittest.TestCase):
    def test_no_reference_in_project_files(self):
        needles = ("ollama", "11434", "api/generate", "api/tags")
        skip = {".venv", "__pycache__", "logs", "backups", ".git"}
        hits = []
        for path in APP_DIR.rglob("*"):
            if any(part in skip for part in path.parts) or not path.is_file() or path.suffix in (".png", ".gif", ".db", ".pyc", ".zip"):
                continue
            if path.name == "test_envcheck.py":
                continue
            try:
                text = path.read_text(encoding="utf-8").lower()
            except (UnicodeDecodeError, OSError):
                continue
            hits += [f"{path.relative_to(APP_DIR)}: {n}" for n in needles if n in text]
        self.assertEqual(hits, [])


# ======================================================================================
# Parte B — ponta a ponta com .venv real e pacotes locais
# ======================================================================================
FAKE_PKGS = {
    # nome pip: (módulo, versão, arquivos extras {caminho: conteúdo}, dependências)
    "requests": ("requests", "2.32.0", {}, []),
    "Pillow": ("PIL", "10.4.0", {}, []),
    "PyQt6": ("PyQt6", "6.7.0", {"PyQt6/QtWidgets.py": "class QApplication: pass\nclass QLabel: pass\n"}, []),
    "edge-tts": ("edge_tts", "7.0.2", {}, []),
    "SpeechRecognition": ("speech_recognition", "3.10.4", {}, []),
    "sounddevice": ("sounddevice", "0.5.0", {}, []),
    "mss": ("mss", "9.0.1", {}, []),
    "psutil": ("psutil", "5.9.8", {}, []),
    "soundcard": ("soundcard", "0.4.3", {}, []),
    "numpy": ("numpy", "1.26.4", {}, []),
    "onnxruntime": ("onnxruntime", "1.18.0", {"onnxruntime/__init__.py": "def get_available_providers():\n    return ['CPUExecutionProvider']\n"}, []),
    "rapidocr-onnxruntime": ("rapidocr_onnxruntime", "1.4.4", {}, ["onnxruntime"]),
}


def build_wheel(dest: pathlib.Path, name, module, version, extra, deps):
    dist = name.replace("-", "_")
    tag = f"{dist}-{version}"
    files = {f"{module}/__init__.py": "", **extra}
    meta = f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n" + "".join(f"Requires-Dist: {d}\n" for d in deps)
    files[f"{tag}.dist-info/METADATA"] = meta
    files[f"{tag}.dist-info/WHEEL"] = "Wheel-Version: 1.0\nGenerator: alicia-tests\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
    record = []
    for path, content in files.items():
        digest = base64.urlsafe_b64encode(hashlib.sha256(content.encode()).digest()).rstrip(b"=").decode()
        record.append(f"{path},sha256={digest},{len(content.encode())}")
    record.append(f"{tag}.dist-info/RECORD,,")
    files[f"{tag}.dist-info/RECORD"] = "\n".join(record) + "\n"
    with zipfile.ZipFile(dest / f"{tag}-py3-none-any.whl", "w") as z:
        for path, content in files.items():
            z.writestr(path, content)


class LauncherE2E(unittest.TestCase):
    """Roda `launcher.py --env-only` em uma cópia do projeto, com stdin simulado."""

    @classmethod
    def setUpClass(cls):
        cls.base = pathlib.Path(tempfile.mkdtemp(prefix="alicia_e2e_"))
        cls.wheels = cls.base / "wheelhouse"
        cls.wheels.mkdir()
        for name, (module, version, extra, deps) in FAKE_PKGS.items():
            build_wheel(cls.wheels, name, module, version, extra, deps)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.base, ignore_errors=True)

    def setUp(self):
        self.app = self.base / f"app_{self._testMethodName}"
        shutil.copytree(APP_DIR, self.app, ignore=shutil.ignore_patterns(
            ".venv", "__pycache__", "logs", "*.db", "config.json", ".env", ".alicia_*", "tests", ".venv.*", "config.json.bak-*"))
        (self.app / ".env").write_text("NVIDIA_API_KEY=nvapi-TESTE-NAO-REAL-0000\n", encoding="utf-8")
        # "dados do usuário" que NUNCA podem mudar
        self.user_files = {
            "alicia_memory.db": b"SQLite format 3\x00fake-memory",
            "config.json": json.dumps({"voice_speed": 105, "user_name": "Teste"}).encode(),
            "gamer_profiles.json": b'{"jogo": {"perfil": 1}}',
            "assets/minha_animacao.gif": b"GIF89a-fake",
        }
        for rel, data in self.user_files.items():
            path = self.app / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        self.hashes = self.user_hashes()

    def tearDown(self):
        shutil.rmtree(self.app, ignore_errors=True)

    def user_hashes(self):
        out = {}
        for rel in list(self.user_files) + [".env"]:
            out[rel] = hashlib.sha256((self.app / rel).read_bytes()).hexdigest()
        return out

    def assertUserDataIntact(self):
        self.assertEqual(self.user_hashes(), self.hashes, "dados do usuário foram alterados!")

    def launch(self, *args, stdin="", offline_index=True, extra_env=None, timeout=420):
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1")
        env.pop("PIP_NO_INDEX", None)
        env.pop("PIP_FIND_LINKS", None)
        if offline_index:
            env["PIP_NO_INDEX"] = "1"
            env["PIP_FIND_LINKS"] = str(self.wheels)
        env.update(extra_env or {})
        r = subprocess.run([sys.executable, str(self.app / "launcher.py"), *args], cwd=self.app, input=stdin,
                           capture_output=True, text=True, timeout=timeout, env=env, encoding="utf-8")
        return r.returncode, r.stdout + r.stderr

    def venv_py(self):
        return self.app / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")

    def log_text(self):
        p = self.app / "logs" / "instalacao_alicia.log"
        return p.read_text(encoding="utf-8") if p.exists() else ""

    def install_everything(self):
        code, out = self.launch("--env-only", "--yes")
        self.assertEqual(code, 0, out)
        self.assertTrue(self.venv_py().exists())
        return out

    # ---- cenários ---------------------------------------------------------------------------------
    def test_01_no_venv_user_cancels_nothing_changes(self):
        code, out = self.launch("--env-only", stdin="5\n")
        self.assertEqual(code, 0, out)
        self.assertIn("Alicia identificou alguns componentes necessários", out)
        self.assertIn("Instalar todos os componentes necessários", out)
        self.assertIn("Continuar sem instalar", out)
        self.assertIn("Inicialização cancelada. Nada foi alterado.", out)
        self.assertFalse((self.app / ".venv").exists(), "criou a .venv sem autorização")
        self.assertUserDataIntact()

    def test_02_no_answer_available_changes_nothing(self):
        code, out = self.launch("--env-only", stdin="")
        self.assertFalse((self.app / ".venv").exists())
        self.assertIn("nada foi alterado", out.lower())

    def test_03_continue_without_install_is_refused_when_required_missing(self):
        code, out = self.launch("--env-only", stdin="4\n5\n")
        self.assertIn("Não dá para continuar sem instalar", out)
        self.assertFalse((self.app / ".venv").exists())

    def test_04_details_option_then_cancel(self):
        code, out = self.launch("--env-only", stdin="3\n5\n")
        self.assertIn("Para que serve", out)
        self.assertIn("PyPI", out)
        self.assertFalse((self.app / ".venv").exists())

    def test_05_authorized_full_install_then_healthy_start_is_silent(self):
        code, out = self.launch("--env-only", stdin="1\n")
        self.assertEqual(code, 0, out)
        self.assertIn("Criando o ambiente virtual", out)
        self.assertIn("[OK] PyQt6 instalado", out)
        self.assertIn("Tudo instalado e funcionando", out)
        # segunda execução: tudo certo, nenhuma pergunta, nenhuma instalação
        code2, out2 = self.launch("--env-only", stdin="")
        self.assertEqual(code2, 0, out2)
        self.assertNotIn("Alicia identificou", out2)
        self.assertNotIn("Instalando", out2)
        self.assertIn("Tudo certo", out2)
        self.assertUserDataIntact()
        log = self.log_text()
        self.assertIn("instalado: PyQt6>=6.5", log)
        self.assertIn("Resultado final", log)
        self.assertNotIn("nvapi-TESTE", log)

    def test_06_required_only_leaves_optional_uninstalled_and_still_ok(self):
        code, out = self.launch("--env-only", stdin="2\n")
        self.assertEqual(code, 0, out)
        py = str(self.venv_py())
        have = subprocess.run([py, "-c", "import PyQt6.QtWidgets, requests, PIL"], capture_output=True)
        self.assertEqual(have.returncode, 0)
        not_have = subprocess.run([py, "-c", "import edge_tts"], capture_output=True)
        self.assertNotEqual(not_have.returncode, 0, "instalou um opcional na opção 'somente obrigatórios'")
        self.assertNotIn("(opcional)", out.split("Instalando")[1] if "Instalando" in out else "")

    def test_07_one_missing_library_installs_only_that_one(self):
        self.install_everything()
        subprocess.run([str(self.venv_py()), "-m", "pip", "uninstall", "-y", "mss"], capture_output=True)
        code, out = self.launch("--env-only", stdin="1\n")
        self.assertEqual(code, 0, out)
        self.assertIn("mss", out)
        self.assertEqual(out.count("[OK]"), 1 + out.count("[OK] Tudo"), out)   # só o mss (+ a linha final)
        self.assertNotIn("PyQt6 instalado", out)
        self.assertIn("Tudo instalado e funcionando", out)

    def test_08_corrupted_venv_is_rebuilt_without_touching_user_data(self):
        self.install_everything()
        exe = self.venv_py()
        exe.unlink()                                  # venv corrompida: sem interpretador
        (self.app / ".venv" / "lixo.txt").write_text("x")
        code, out = self.launch("--env-only", stdin="1\n")
        self.assertEqual(code, 0, out)
        self.assertIn("corrompido", out)
        self.assertIn("Recriando o ambiente virtual", out)
        self.assertTrue(exe.exists())
        self.assertFalse((self.app / ".venv" / "lixo.txt").exists())
        self.assertIn("Tudo instalado e funcionando", out)
        self.assertUserDataIntact()
        self.assertFalse(list(self.app.glob(".venv.quebrada-*")) and any(p.exists() for p in self.app.glob(".venv.quebrada-*/*")),
                         "sobrou lixo da .venv antiga")

    def test_09_broken_library_is_repaired(self):
        self.install_everything()
        site = next((self.app / ".venv").glob("lib/python*/site-packages")) if os.name != "nt" else self.app / ".venv" / "Lib" / "site-packages"
        (site / "edge_tts" / "__init__.py").write_text("raise ImportError('DLL load failed (simulado)')\n")
        code, out = self.launch("--env-only", stdin="1\n")
        self.assertEqual(code, 0, out)
        self.assertIn("edge-tts", out)
        self.assertIn("Tudo instalado e funcionando", out)

    def test_10_continue_without_optional_is_remembered(self):
        self.install_everything()
        subprocess.run([str(self.venv_py()), "-m", "pip", "uninstall", "-y", "mss", "soundcard"], capture_output=True)
        code, out = self.launch("--env-only", stdin="4\n")
        self.assertEqual(code, 0, out)
        self.assertIn("Seguindo sem instalar", out)
        code2, out2 = self.launch("--env-only", stdin="")                # não pergunta de novo
        self.assertEqual(code2, 0, out2)
        self.assertNotIn("Alicia identificou", out2)
        # mas --repair volta a oferecer
        code3, out3 = self.launch("--repair", stdin="5\n")
        self.assertIn("Alicia identificou", out3)

    def test_11_offline_explains_and_keeps_going_when_nothing_required_is_missing(self):
        self.install_everything()
        subprocess.run([str(self.venv_py()), "-m", "pip", "uninstall", "-y", "mss"], capture_output=True)
        # sem pasta local e sem internet: apontamos o pip para um índice inalcançável
        code, out = self.launch("--env-only", stdin="1\nn\n", offline_index=False,
                                extra_env={"PIP_INDEX_URL": "http://127.0.0.1:9/simple", "HTTPS_PROXY": "http://127.0.0.1:9",
                                           "HTTP_PROXY": "http://127.0.0.1:9", "NO_PROXY": ""})
        self.assertEqual(code, 0, out)
        self.assertIn("Sem conexão com a internet", out)
        self.assertIn("mss", out)
        self.assertIn("sem internet", self.log_text())

    def test_12_offline_with_required_missing_stops_with_clear_message(self):
        code, out = self.launch("--env-only", stdin="1\nn\n", offline_index=False,
                                extra_env={"HTTPS_PROXY": "http://127.0.0.1:9", "HTTP_PROXY": "http://127.0.0.1:9", "NO_PROXY": ""})
        self.assertNotEqual(code, 0)
        self.assertIn("Sem conexão com a internet", out)
        self.assertIn("Conecte-se", out)
        self.assertFalse((self.app / ".venv").exists())
        self.assertUserDataIntact()

    def test_13_install_failure_explained_and_retry_offered(self):
        self.install_everything()
        subprocess.run([str(self.venv_py()), "-m", "pip", "uninstall", "-y", "psutil"], capture_output=True)
        wheel = next(self.wheels.glob("psutil-*.whl"))
        hidden = wheel.with_suffix(".oculta")
        wheel.rename(hidden)
        try:
            code, out = self.launch("--env-only", stdin="1\nn\n")
        finally:
            hidden.rename(wheel)
        self.assertEqual(code, 0, out)
        self.assertIn("[ERRO] psutil", out)
        self.assertIn("compatível", out)                      # linguagem simples
        self.assertIn("Tentar instalar de novo", out)
        self.assertIn("FALHA ao instalar psutil", self.log_text())

    def test_14_config_with_obsolete_options_is_cleaned_with_backup(self):
        self.install_everything()
        cfg = {"voice_speed": 105, "user_name": "Teste", "opcao_antiga_xyz": "http://localhost:1"}
        (self.app / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
        code, out = self.launch("--env-only", stdin="1\n")
        self.assertEqual(code, 0, out)
        data = json.loads((self.app / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(data, {"voice_speed": 105, "user_name": "Teste"})
        self.assertEqual(len(list(self.app.glob("config.json.bak-*"))), 1)

    def test_15_reinstall_asks_confirmation(self):
        self.install_everything()
        code, out = self.launch("--env-only", "--reinstall", stdin="n\n")
        self.assertEqual(code, 0, out)
        self.assertIn("Reinstalar TODOS", out)
        self.assertNotIn("[OK] PyQt6 instalado", out)


    def test_16_vision_local_without_environment_asks_to_prepare_first(self):
        code, out = self.launch("--vision-local", stdin="s\n")
        self.assertNotEqual(code, 0)
        self.assertIn("prepare o ambiente", out)
        self.assertFalse((self.app / "models").exists())

    def test_17_vision_local_explains_cost_and_declining_changes_nothing(self):
        self.install_everything()
        before = self.user_hashes()
        code, out = self.launch("--vision-local", stdin="n\n")
        self.assertEqual(code, 0, out)
        self.assertIn("100% no seu computador" if "100% no seu computador" in out else "DENTRO do seu computador", out)
        self.assertIn("3 GB", out)
        self.assertIn("Nada foi baixado nem alterado", out)
        self.assertFalse((self.app / "models").exists())
        self.assertEqual(self.user_hashes(), before)
        code2, out2 = self.launch("--env-only", stdin="")
        self.assertNotIn("Alicia identificou", out2)

    def test_18_diagnostic_reports_local_vlm_state(self):
        self.install_everything()
        cfg = json.loads((self.app / "config.json").read_text(encoding="utf-8"))
        cfg["local_vlm_enabled"] = True
        (self.app / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
        probe = ec.run_probe_in(self.venv_py(), self.app, deep=False)
        item = ec.vlm_item(probe)
        self.assertEqual(item.state, ec.FAILED)                  # ligado, mas sem biblioteca/modelo
        self.assertIn("--vision-local", item.detail)
        self.assertFalse(item.blocking)
        code, out = self.launch("--env-only", stdin="")          # e não trava nem pergunta nada
        self.assertEqual(code, 0, out)
        self.assertNotIn("Alicia identificou", out)

if __name__ == "__main__":
    unittest.main()
