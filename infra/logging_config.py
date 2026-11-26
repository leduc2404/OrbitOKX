"""Central, idempotent logging configuration for the OKXSquare runtime pipeline."""
from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from threading import Lock

_CONFIG_LOCK = Lock()
_CONFIGURED = False
_HANDLER_MARKER = "okx_square_managed"


class _ComponentFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        if not hasattr(record, "component"):
            record.component = record.name.rsplit(".", 1)[-1].upper()
        return super().format(record)


class _WinSafeRotatingFileHandler(RotatingFileHandler):
    """Skip rotation on Windows when the log file is locked by another handle
    (WinError 32); rotation is retried on the next size-limit trigger."""

    def doRollover(self) -> None:
        if sys.platform != "win32":
            super().doRollover()
            return
        try:
            super().doRollover()
        except PermissionError:
            pass


def setup_logging(log_dir: str = "data/logs", console_level: int = logging.INFO) -> logging.Logger:
    """Configure console and rotating UTF-8 files exactly once (idempotent)."""
    global _CONFIGURED
    with _CONFIG_LOCK:
        root = logging.getLogger()
        managed = [h for h in root.handlers if getattr(h, _HANDLER_MARKER, False)]
        if _CONFIGURED and len(managed) == 3:
            return root

        for handler in managed:
            root.removeHandler(handler)
            handler.close()

        Path(log_dir).mkdir(parents=True, exist_ok=True)
        root.setLevel(logging.DEBUG)

        console = logging.StreamHandler()
        console.setLevel(console_level)
        console.setFormatter(_ComponentFormatter(
            "%(asctime)s %(levelname)s %(component)s %(message)s", datefmt="%H:%M:%S"
        ))

        detailed = _WinSafeRotatingFileHandler(
            os.path.join(log_dir, "okx_square.log"),
            maxBytes=10 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        detailed.setLevel(logging.DEBUG)
        detailed.setFormatter(_ComponentFormatter(
            "%(asctime)s %(levelname)s %(threadName)s %(name)s "
            "%(module)s:%(lineno)d %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        ))

        errors = _WinSafeRotatingFileHandler(
            os.path.join(log_dir, "error.log"),
            maxBytes=10 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        errors.setLevel(logging.ERROR)
        errors.setFormatter(detailed.formatter)

        for handler in (console, detailed, errors):
            setattr(handler, _HANDLER_MARKER, True)
            root.addHandler(handler)

        _CONFIGURED = True
        return root


def get_logger(name: str) -> logging.Logger:
    setup_logging()
    return logging.getLogger(name)


def make_log_print(logger: logging.Logger):
    """Bridge noisy legacy runtime print output into the logger with level inference."""
    def log_print(*values, **kwargs):
        message = " ".join(str(value) for value in values).strip()
        if not message:
            return
        lowered = message.lower()
        if any(token in lowered for token in ("\u2717", "\u274c", "error", "failed")):
            logger.error(message)
        elif any(token in lowered for token in ("\u26a0", "warning", "timeout")):
            logger.warning(message)
        elif any(token in lowered for token in ("summary", "starting", "initialized", "cycle")):
            logger.info(message)
        else:
            logger.debug(message)
    return log_print