"""
Qt FALSO só para testes: simula o comportamento (texto, habilitado, cliques, sinais, temporizadores) dos widgets usados
na janela do pôquer, para testar a LÓGICA da janela sem PyQt6. Não valida aparência nem a API real do Qt.
"""

import importlib.util
import pathlib
import sys
import types


class _Signal:
    def __init__(self):
        self.slots = []

    def connect(self, fn):
        self.slots.append(fn)

    def emit(self, *a):
        for fn in list(self.slots):
            fn(*a)


class pyqtSignal:
    def __init__(self, *types_):
        self.name = None

    def __set_name__(self, owner, name):
        self.name = "_sig_" + name

    def __get__(self, obj, objtype=None):
        if obj is None:
            return self
        if self.name not in obj.__dict__:
            obj.__dict__[self.name] = _Signal()
        return obj.__dict__[self.name]


class _Enum:
    def __getattr__(self, name):
        return _Enum()


class Qt:
    AlignmentFlag = _Enum()
    FocusPolicy = _Enum()
    TransformationMode = _Enum()
    AspectRatioMode = _Enum()


class QTimer:
    queue = []                                    # singleShot pendentes (executados pelo teste)

    def __init__(self, parent=None):
        self.timeout = _Signal()
        self.active = False

    def start(self, ms=0):
        self.active = True

    def stop(self):
        self.active = False

    @staticmethod
    def singleShot(ms, fn):
        QTimer.queue.append((ms, fn))

    @classmethod
    def run_pending(cls, limit=100):
        n = 0
        while cls.queue and n < limit:
            _, fn = cls.queue.pop(0)
            fn()
            n += 1
        return n


class QWidget:
    def __init__(self, parent=None):
        self._visible = False
        self._enabled = True
        self._style = ""
        self.close_events = 0

    def setObjectName(self, n): self._name = n
    def setWindowTitle(self, t): self.title = t
    def resize(self, *a): pass
    def setMinimumSize(self, *a): pass
    def setFixedSize(self, *a): pass
    def setFixedHeight(self, *a): pass
    def setFixedWidth(self, *a): pass
    def setMinimumWidth(self, *a): pass
    def setMaximumWidth(self, *a): pass
    def setFocusPolicy(self, *a): pass
    def setStyleSheet(self, s): self._style = s
    def setAlignment(self, *a): pass
    def setWordWrap(self, *a): pass
    def setSizePolicy(self, *a): pass
    def show(self): self._visible = True
    def hide(self): self._visible = False
    def setVisible(self, v): self._visible = bool(v)
    def isVisible(self): return self._visible
    def raise_(self): pass
    def activateWindow(self): pass
    def setEnabled(self, v): self._enabled = bool(v)
    def isEnabled(self): return self._enabled

    def close(self):
        ev = types.SimpleNamespace(accepted=False)
        ev.accept = lambda: setattr(ev, "accepted", True)
        self.closeEvent(ev)
        self._visible = False
        return True

    def closeEvent(self, event):
        event.accept()


class QFrame(QWidget):
    pass


class QLabel(QWidget):
    def __init__(self, text="", parent=None):
        super().__init__(parent if not isinstance(parent, str) else None)
        self._text = text if isinstance(text, str) else ""
        self._visible = True
        self.pixmap = None

    def setText(self, t): self._text = t
    def text(self): return self._text
    def setPixmap(self, p): self.pixmap = p


class QPushButton(QWidget):
    def __init__(self, text="", parent=None):
        super().__init__(parent if not isinstance(parent, str) else None)
        self._text = text if isinstance(text, str) else ""
        self._visible = True
        self.clicked = _Signal()

    def setText(self, t): self._text = t
    def text(self): return self._text

    def click(self):
        if self._enabled and self._visible:
            self.clicked.emit(False)


class QSpinBox(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._lo, self._hi, self._v = 0, 99, 0
        self._visible = True

    def setRange(self, lo, hi):
        self._lo, self._hi = lo, hi
        self._v = max(lo, min(hi, self._v))

    def setSingleStep(self, s): pass
    def setValue(self, v): self._v = max(self._lo, min(self._hi, v))
    def value(self): return self._v
    def minimum(self): return self._lo
    def maximum(self): return self._hi


class QListWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.items = []
        self._visible = True

    def addItem(self, t): self.items.append(t)
    def clear(self): self.items = []
    def count(self): return len(self.items)
    def scrollToBottom(self): pass


class _Layout:
    def __init__(self, *a): pass
    def addWidget(self, *a, **k): pass
    def addLayout(self, *a, **k): pass
    def addStretch(self, *a, **k): pass
    def setContentsMargins(self, *a): pass
    def setSpacing(self, *a): pass


class QPixmap:
    def __init__(self, name="pix"):
        self.name = name

    def scaledToHeight(self, h, mode=None):
        return QPixmap(self.name)


class QSizePolicy:
    Policy = _Enum()


def load_poker_window_with_fake_qt():
    """Carrega alicia_core/ui/poker_window.py com o Qt falso (sem mexer no resto do sistema)."""
    import alicia_core.ui  # noqa: F401  (garante o pacote-pai)
    fake_core = types.ModuleType("PyQt6.QtCore")
    fake_core.Qt, fake_core.QTimer, fake_core.pyqtSignal = Qt, QTimer, pyqtSignal
    fake_widgets = types.ModuleType("PyQt6.QtWidgets")
    for cls in (QFrame, QLabel, QPushButton, QSpinBox, QListWidget, QWidget, QSizePolicy):
        setattr(fake_widgets, cls.__name__, cls)
    fake_widgets.QHBoxLayout = fake_widgets.QVBoxLayout = _Layout
    saved = {k: sys.modules.get(k) for k in ("PyQt6", "PyQt6.QtCore", "PyQt6.QtWidgets")}
    sys.modules["PyQt6"] = types.ModuleType("PyQt6")
    sys.modules["PyQt6.QtCore"], sys.modules["PyQt6.QtWidgets"] = fake_core, fake_widgets
    try:
        path = pathlib.Path(__file__).resolve().parent.parent / "alicia_core" / "ui" / "poker_window.py"
        spec = importlib.util.spec_from_file_location("alicia_core.ui.poker_window_fake", path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v
