"""
Apoio aos testes: servidor HTTP LOCAL que imita o formato /v1/chat/completions
(serve apenas para testar a MECÂNICA do cliente e do fluxo de conversa — NÃO prova
que a API real da NVIDIA está funcionando; isso é verificado pelo diagnóstico com a
sua chave) e stubs de PyQt6 para quando a biblioteca não estiver instalada.
"""

import json
import os
import pathlib
import sys
import threading
import time
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

APP_DIR = pathlib.Path(__file__).resolve().parent.parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))
os.chdir(APP_DIR)  # logs/ do import inicial ficam na pasta do projeto

FAKE_KEY = "nvapi-FAKE-KEY-FOR-LOCAL-TESTS-0000000000"


def install_qt_stubs_if_needed():
    try:
        import PyQt6.QtWidgets  # noqa: F401
        return False
    except ImportError:
        pass

    class _Any:
        def __init__(self, *a, **k): pass
        def __getattr__(self, name): return _Any()
        def __call__(self, *a, **k): return _Any()

    class QWidget:
        def __init__(self, *a, **k): pass

    names = {
        "PyQt6": {},
        "PyQt6.QtCore": {"Qt": _Any(), "QPoint": _Any, "QTimer": _Any, "pyqtSignal": _Any},
        "PyQt6.QtGui": {"QPixmap": _Any, "QAction": _Any, "QImage": _Any, "QPainter": _Any},
        "PyQt6.QtWidgets": {"QApplication": _Any, "QLabel": _Any, "QMenu": _Any, "QWidget": QWidget, "QFrame": _Any, "QHBoxLayout": _Any, "QVBoxLayout": _Any,
                            "QListWidget": _Any, "QPushButton": _Any, "QSizePolicy": _Any, "QSpinBox": _Any},
    }
    for mod, attrs in names.items():
        m = types.ModuleType(mod)
        m.__dict__.update(attrs)
        sys.modules[mod] = m
    return True


install_qt_stubs_if_needed()


class MockNvidia:
    """Servidor local. `script` = fila de comportamentos; vazio -> resposta padrão."""

    def __init__(self):
        self.requests = []          # corpo JSON de cada POST /chat/completions
        self.headers_seen = []
        self.script = []
        self.reply_fn = None        # fn(messages) -> texto
        self.models = ["test-model", "outro/modelo-instruct"]
        self.bad_models = {}        # modelo -> (status, mensagem)
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):  # silêncio
                pass

            def _send(self, status, body, headers=None):
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                outer.headers_seen.append(dict(self.headers))
                if self.path.endswith("/models"):
                    self._send(200, {"data": [{"id": m} for m in outer.models]})
                else:
                    self._send(404, {"error": "nope"})

            def do_POST(self):
                outer.headers_seen.append(dict(self.headers))
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length) or b"{}")
                outer.requests.append(body)
                if body.get("model") in outer.bad_models:
                    st, msg = outer.bad_models[body["model"]]
                    self._send(st, {"error": {"message": msg}})
                    return
                step = outer.script.pop(0) if outer.script else None
                if step is None:
                    text = outer.reply_fn(body["messages"]) if outer.reply_fn else "[FELIZ] Resposta padrão do servidor de teste."
                    step = ("ok", text)
                kind = step[0]
                if kind == "ok":
                    self._send(200, {"choices": [{"message": {"role": "assistant", "content": step[1]}}]})
                elif kind == "empty":
                    self._send(200, {"choices": [{"message": {"role": "assistant", "content": ""}}]})
                elif kind == "badjson":
                    self._send(200, {"unexpected": True})
                elif kind == "status":
                    self._send(step[1], {"error": {"message": step[2] if len(step) > 2 else "erro"}}, step[3] if len(step) > 3 else None)
                elif kind == "sleep":
                    time.sleep(step[1])
                    try:
                        self._send(200, {"choices": [{"message": {"role": "assistant", "content": step[2]}}]})
                    except Exception:
                        pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self.port}/v1"

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()
