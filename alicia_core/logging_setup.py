"""
Sistema de logs da Alicia (console colorido + arquivo rotativo em logs/).

Todo registro passa por um filtro que mascara chaves de API (nvapi-...),
cabeçalhos Authorization e variáveis *_API_KEY, para que segredos nunca
cheguem ao console nem ao arquivo de log.
"""

import logging
import logging.handlers
import os
import pathlib
import re

_SECRET_PATTERNS = [
    (re.compile(r"nvapi-[A-Za-z0-9_\-]{6,}"), "nvapi-***"),
    (re.compile(r"(Bearer\s+)[A-Za-z0-9_\-\.=]{8,}", re.IGNORECASE), r"\1***"),
    (re.compile(r"(API_KEY\s*[=:]\s*)\S+", re.IGNORECASE), r"\1***"),
]


def redact(text: str) -> str:
    for pattern, repl in _SECRET_PATTERNS:
        text = pattern.sub(repl, text)
    return text


class SecretRedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
            record.msg = redact(msg)
            record.args = ()
            if record.exc_info:
                # Formata a exceção já mascarada.
                import traceback
                record.exc_text = redact("".join(traceback.format_exception(*record.exc_info)))
        except Exception:
            pass
        return True


class ColorFormatter(logging.Formatter):
    GREY = "\x1b[38;20m"
    CYAN = "\x1b[36;20m"
    YELLOW = "\x1b[33;20m"
    RED = "\x1b[31;20m"
    BOLD_RED = "\x1b[31;1m"
    RESET = "\x1b[0m"

    FORMAT = "%(asctime)s - [%(levelname)s] - (%(filename)s:%(lineno)d) - %(message)s"

    FORMATS = {
        logging.DEBUG: GREY + FORMAT + RESET,
        logging.INFO: CYAN + FORMAT + RESET,
        logging.WARNING: YELLOW + FORMAT + RESET,
        logging.ERROR: RED + FORMAT + RESET,
        logging.CRITICAL: BOLD_RED + FORMAT + RESET,
    }

    def format(self, record: logging.LogRecord) -> str:
        log_fmt = self.FORMATS.get(record.levelno, self.FORMAT)
        return logging.Formatter(log_fmt, datefmt="%H:%M:%S").format(record)


def _level_from_env(default: int = logging.INFO) -> int:
    name = os.environ.get("ALICIA_LOG_LEVEL", "").strip().upper()
    return getattr(logging, name, default) if name else default


def setup_logger(name: str = "AliciaCore", level: int = None) -> logging.Logger:
    level = level if level is not None else _level_from_env()
    logger = logging.getLogger(name)
    logger.setLevel(level)

    if not logger.handlers:
        redactor = SecretRedactingFilter()

        ch = logging.StreamHandler()
        ch.setLevel(level)
        ch.setFormatter(ColorFormatter())
        ch.addFilter(redactor)
        logger.addHandler(ch)

        try:
            log_dir = pathlib.Path("logs")
            log_dir.mkdir(exist_ok=True)
            fh = logging.handlers.RotatingFileHandler(
                log_dir / "alicia_system.log",
                maxBytes=2_000_000, backupCount=3, encoding="utf-8",
            )
            fh.setLevel(level)
            fh.setFormatter(logging.Formatter("%(asctime)s - [%(levelname)s] - %(message)s"))
            fh.addFilter(redactor)
            logger.addHandler(fh)
        except OSError:
            pass  # sem permissão de escrita: segue só com o console

    return logger


logger = setup_logger()
