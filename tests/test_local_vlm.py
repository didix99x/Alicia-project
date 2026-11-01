# -*- coding: utf-8 -*-
"""
Modelo de visão LOCAL: integração, privacidade e instalador de download.

Usa uma biblioteca `llama_cpp` FALSA (não há modelo real aqui). Prova: escolha do manipulador, carregamento preguiçoso,
descarga por inatividade, tempo limite, ocupado, local-primeiro, nada de rede, Modo Gamer usando o modelo e o
download com conferência de SHA-256. NÃO prova a qualidade das respostas de um modelo real nem o llama-cpp-python no Windows.
"""

import hashlib
import http.server
import pathlib
import shutil
import socketserver
import sys
import tempfile
import threading
import time
import types
import unittest

APP_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(APP_DIR / "tests"))

from alicia_core import local_vlm, vlm_setup  # noqa: E402
from alicia_core.local_vlm import LocalVlm  # noqa: E402

from test_observer import ObserverCase  # noqa: E402


def fake_llama_module(answer="Você está no meio de uma luta. [SERIO]", delay=0.0, fail=False, calls=None, loads=None):
    calls = calls if calls is not None else []
    loads = loads if loads is not None else []
    mod = types.ModuleType("llama_cpp")
    fmt = types.ModuleType("llama_cpp.llama_chat_format")

    class Qwen25VLChatHandler:
        def __init__(self, clip_model_path=None, verbose=False):
            self.clip = clip_model_path

    class Llava15ChatHandler(Qwen25VLChatHandler):
        pass

    class Llama:
        def __init__(self, model_path, chat_handler=None, n_ctx=0, verbose=False, **kw):
            loads.append((model_path, type(chat_handler).__name__, n_ctx))

        def create_chat_completion(self, messages, **kw):
            calls.append((messages, kw))
            time.sleep(delay)
            if fail:
                raise RuntimeError("falha nativa simulada")
            return {"choices": [{"message": {"content": answer}}]}

    fmt.Qwen25VLChatHandler, fmt.Llava15ChatHandler = Qwen25VLChatHandler, Llava15ChatHandler
    mod.Llama, mod.llama_chat_format = Llama, fmt
    return mod, fmt, calls, loads


class VlmCase(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.cfg = types.SimpleNamespace(local_vlm_enabled=True, local_vlm_dir=str(self.tmp), local_vlm_n_ctx=2048,
                                         local_vlm_threads=0, local_vlm_idle_unload_s=300.0)
        (self.tmp / "Qwen2.5-VL-3B-Instruct-Q4_K_M.gguf").write_bytes(b"x")
        (self.tmp / "mmproj-F16.gguf").write_bytes(b"y")
        self.mod, self.fmt, self.calls, self.loads = fake_llama_module()
        self._saved = {k: sys.modules.get(k) for k in ("llama_cpp", "llama_cpp.llama_chat_format")}
        sys.modules["llama_cpp"], sys.modules["llama_cpp.llama_chat_format"] = self.mod, self.fmt
        self.vlm = LocalVlm(self.cfg)

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestLocalVlm(VlmCase):
    def test_availability_requires_enabled_library_and_files(self):
        self.assertTrue(self.vlm.available)
        self.assertEqual(self.vlm.status(), "")
        self.cfg.local_vlm_enabled = False
        self.assertFalse(self.vlm.available)
        self.assertEqual(self.vlm.status(), "desligado")
        self.cfg.local_vlm_enabled = True
        (self.tmp / "mmproj-F16.gguf").unlink()
        self.assertFalse(self.vlm.available)
        self.assertIn("faltam os arquivos", self.vlm.status())

    def test_part_files_are_ignored(self):
        (self.tmp / "mmproj-F16.gguf").unlink()
        (self.tmp / "mmproj-F16.gguf.part").write_bytes(b"incompleto")
        self.assertIsNone(self.vlm.model_files())

    def test_generate_loads_lazily_once_and_sends_image_as_data_url(self):
        self.assertEqual(self.loads, [])
        out = self.vlm.generate("descreva", "iVBORw0KGgo=", {"max_tokens": 50, "temperature": 0.5})
        self.assertIn("luta", out)
        self.assertEqual(len(self.loads), 1)
        self.assertEqual(self.loads[0][1], "Qwen25VLChatHandler")          # escolhido pelo nome do arquivo
        self.assertEqual(self.loads[0][2], 2048)
        content = self.calls[0][0][0]["content"]
        self.assertTrue(content[0]["image_url"]["url"].startswith("data:image/png;base64,"))
        self.assertEqual(self.calls[0][1]["max_tokens"], 50)
        self.vlm.generate("de novo", "iVBORw0KGgo=")
        self.assertEqual(len(self.loads), 1)                               # não recarrega

    def test_handler_choice_by_filename(self):
        for f in self.tmp.glob("*.gguf"):
            f.unlink()
        (self.tmp / "llava-v1.5-7b-q4.gguf").write_bytes(b"x")
        (self.tmp / "mmproj-model-f16.gguf").write_bytes(b"y")
        self.vlm.generate("x", "abc")
        self.assertEqual(self.loads[0][1], "Llava15ChatHandler")

    def test_failure_returns_none_without_raising(self):
        mod, fmt, _, _ = fake_llama_module(fail=True)
        sys.modules["llama_cpp"], sys.modules["llama_cpp.llama_chat_format"] = mod, fmt
        self.assertIsNone(LocalVlm(self.cfg).generate("x", "abc"))

    def test_timeout_returns_none_and_busy_model_is_skipped(self):
        mod, fmt, _, _ = fake_llama_module(delay=1.2)
        sys.modules["llama_cpp"], sys.modules["llama_cpp.llama_chat_format"] = mod, fmt
        vlm = LocalVlm(self.cfg)
        t0 = time.time()
        self.assertIsNone(vlm.generate("x", "abc", timeout=0.3))
        self.assertLess(time.time() - t0, 1.0)
        self.assertIsNone(vlm.generate("x", "abc", timeout=0.3))           # ainda ocupado: não empilha
        time.sleep(1.4)
        self.assertIsNotNone(vlm.generate("x", "abc", timeout=5))

    def test_idle_unload_frees_the_model(self):
        self.vlm.generate("x", "abc")
        self.assertIsNotNone(self.vlm._llm)
        self.vlm.maybe_unload_idle(now=time.time() + 10)
        self.assertIsNotNone(self.vlm._llm)                                 # ainda dentro do prazo
        self.vlm.maybe_unload_idle(now=time.time() + 400)
        self.assertIsNone(self.vlm._llm)
        self.vlm.generate("x", "abc")                                       # volta a carregar quando precisa
        self.assertEqual(len(self.loads), 2)

    def test_selftest_reads_synthetic_image(self):
        mod, fmt, _, _ = fake_llama_module(answer="ALICIA 2024")
        sys.modules["llama_cpp"], sys.modules["llama_cpp.llama_chat_format"] = mod, fmt
        ok, detail = LocalVlm(self.cfg).selftest()
        self.assertTrue(ok, detail)
        mod, fmt, _, _ = fake_llama_module(answer="não sei")
        sys.modules["llama_cpp"], sys.modules["llama_cpp.llama_chat_format"] = mod, fmt
        self.assertFalse(LocalVlm(self.cfg).selftest()[0])

    def test_module_has_no_network_code(self):
        src = (APP_DIR / "alicia_core" / "local_vlm.py").read_text(encoding="utf-8")
        for bad in ("requests", "urllib", "socket", "http.client", "import http"):
            self.assertNotIn(bad, src)


class TestVisionBackendLocalFirst(VlmCase):
    def make_backend(self, allow_remote=False):
        from alicia_core.vision import VisionBackend
        cfg = types.SimpleNamespace(obs_allow_remote_vision=allow_remote, **vars(self.cfg))
        nv = types.SimpleNamespace(has_key=True, _api_key="k", base_url="http://x", list_models=lambda: self.fail("rede!"))
        return VisionBackend(cfg, nv)

    def test_local_is_the_backend_and_remote_is_never_touched(self):
        vb = self.make_backend(allow_remote=False)
        self.assertEqual(vb.backend, "local")
        self.assertTrue(vb.available)
        self.assertIn("luta", vb.generate("p", "abc"))

    def test_local_failure_does_not_leak_to_remote_without_authorization(self):
        mod, fmt, _, _ = fake_llama_module(fail=True)
        sys.modules["llama_cpp"], sys.modules["llama_cpp.llama_chat_format"] = mod, fmt
        vb = self.make_backend(allow_remote=False)
        self.assertIsNone(vb.generate("p", "abc"))                          # sem autorização: não cai para a NVIDIA

    def test_disabled_local_means_no_backend(self):
        self.cfg.local_vlm_enabled = False
        self.assertIsNone(self.make_backend().backend)


class TestGamerUsesLocalVlm(ObserverCase):
    def setUp(self):
        super().setUp()
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        (self.tmp / "m-Q4_K_M.gguf").write_bytes(b"x")
        (self.tmp / "mmproj-F16.gguf").write_bytes(b"y")
        self.mod, self.fmt, self.calls, _ = fake_llama_module(answer="Boa, essa foi por pouco! [FELIZ]")
        self._saved = {k: sys.modules.get(k) for k in ("llama_cpp", "llama_cpp.llama_chat_format")}
        sys.modules["llama_cpp"], sys.modules["llama_cpp.llama_chat_format"] = self.mod, self.fmt
        c = self.core
        c.config.local_vlm_enabled, c.config.local_vlm_dir = True, str(self.tmp)
        self.addCleanup(self.restore)

    def restore(self):
        for k, v in self._saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_gamer_cycle_comments_with_the_local_model_and_sends_nothing(self):
        c = self.core
        c.modo_gamer, c.gamer_current_game, c.screen_watch_enabled = True, "Elden Ring", True
        c.gamer_last_game_change_time = time.time() - 60
        c.screen_engine.capture_and_encode = lambda: ("x" * 100, self.image, "hash-1")
        c.last_screen_img = None
        c._run_gamer_analysis()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(len(self.said), 1)
        self.assertIn("essa foi por pouco", self.said[0][0])
        self.assertEqual(self.srv.requests, [])

    def test_gamer_falls_back_to_ocr_reading_when_model_gives_nothing(self):
        mod, fmt, _, _ = fake_llama_module(answer="VAZIO")
        sys.modules["llama_cpp"], sys.modules["llama_cpp.llama_chat_format"] = mod, fmt
        c = self.core
        ocr = self.obs.vision.ocr
        ocr._probed, ocr.backend = True, "fake"
        ocr.read = lambda img, timeout=15.0: ("YOU DIED", "fake")
        c.modo_gamer, c.gamer_current_game, c.screen_watch_enabled = True, "Elden Ring", True
        c.gamer_last_game_change_time = time.time() - 60
        c.screen_engine.capture_and_encode = lambda: ("x" * 100, self.image, "hash-2")
        c.last_screen_img = None
        c._run_gamer_analysis()
        self.assertEqual(len(self.said), 1)


# ======================================================================================
# Instalador de download
# ======================================================================================
class TestVlmSetup(unittest.TestCase):
    def test_pick_files_prefers_q4_k_m_and_f16_projector(self):
        sib = [{"rfilename": n} for n in ("README.md", "m-Q8_0.gguf", "m-Q4_K_M.gguf", "m-Q2_K.gguf",
                                          "mmproj-BF16.gguf", "mmproj-F16.gguf", "mmproj-F32.gguf")]
        main, mm = vlm_setup.pick_files(sib)
        self.assertEqual(main["rfilename"], "m-Q4_K_M.gguf")
        self.assertEqual(mm["rfilename"], "mmproj-F16.gguf")
        self.assertIsNone(vlm_setup.pick_files([{"rfilename": "x.gguf"}]))

    def test_describe_repo_reads_official_hash_and_size(self):
        info = {"siblings": [
            {"rfilename": "sub/m-Q4_K_M.gguf", "lfs": {"sha256": "aa", "size": 10}},
            {"rfilename": "mmproj-F16.gguf", "lfs": {"sha256": "bb", "size": 5}}]}
        orig = vlm_setup._get_json
        vlm_setup._get_json = lambda url, timeout=30.0: info
        try:
            main, mm = vlm_setup.describe_repo("a/b")
        finally:
            vlm_setup._get_json = orig
        self.assertEqual((main["name"], main["sha256"], main["size"]), ("m-Q4_K_M.gguf", "aa", 10))
        self.assertTrue(main["url"].endswith("/a/b/resolve/main/sub/m-Q4_K_M.gguf"))
        self.assertEqual(mm["sha256"], "bb")


class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


class TestDownload(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = pathlib.Path(tempfile.mkdtemp())
        cls.data = bytes(range(256)) * 4096                         # 1 MiB
        (cls.src / "model.gguf").write_bytes(cls.data)

        class H(_Quiet):
            def __init__(self, *a, **k):
                super().__init__(*a, directory=str(cls.src), **k)
        cls.httpd = socketserver.TCPServer(("127.0.0.1", 0), H)
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        shutil.rmtree(cls.src, ignore_errors=True)

    def setUp(self):
        self.dest = pathlib.Path(tempfile.mkdtemp())
        self.file = {"name": "model.gguf", "size": len(self.data), "sha256": hashlib.sha256(self.data).hexdigest(),
                     "url": f"http://127.0.0.1:{self.port}/model.gguf"}

    def tearDown(self):
        shutil.rmtree(self.dest, ignore_errors=True)

    def test_download_verifies_hash_and_reports_progress(self):
        seen = []
        path = vlm_setup.download(self.file, self.dest, progress=lambda d, t: seen.append((d, t)), chunk=65536)
        self.assertEqual(path.read_bytes(), self.data)
        self.assertEqual(seen[-1], (len(self.data), len(self.data)))
        self.assertFalse((self.dest / "model.gguf.part").exists())

    def test_existing_valid_file_is_not_downloaded_again(self):
        vlm_setup.download(self.file, self.dest)
        self.file["url"] = "http://127.0.0.1:1/nao-existe"            # se tentasse baixar, falharia
        self.assertTrue(vlm_setup.download(self.file, self.dest).exists())

    def test_wrong_hash_is_discarded(self):
        self.file["sha256"] = "0" * 64
        with self.assertRaises(RuntimeError) as cm:
            vlm_setup.download(self.file, self.dest)
        self.assertIn("SHA-256", str(cm.exception))
        self.assertFalse((self.dest / "model.gguf").exists())
        self.assertFalse((self.dest / "model.gguf.part").exists())

    def test_truncated_download_is_rejected_and_part_kept_for_resume(self):
        self.file["size"] = len(self.data) + 100                     # servidor entrega menos do que o prometido
        with self.assertRaises(RuntimeError):
            vlm_setup.download(self.file, self.dest)
        self.assertTrue((self.dest / "model.gguf.part").exists())
        self.assertFalse((self.dest / "model.gguf").exists())


if __name__ == "__main__":
    unittest.main()
