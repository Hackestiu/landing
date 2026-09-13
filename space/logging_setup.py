"""
Centralized logging configuration built on loguru.

Every module logs through the shared ``logger`` exported here:

    from logging_setup import logger
    logger.info("Camera opened on /dev/video{}", index)

``setup_logging()`` is called once from main.py before anything else runs. It
installs two sinks:

  * stderr, at the level given by ``CULTURA_LOG_LEVEL`` (default INFO), so the
    App Lab console stays readable during a visit;
  * a per-run file named ``data/logs/cultura_<YYYYMMDD>_<HHMMSS>.log``, always at
    DEBUG, so a failure in the field can be inspected after the fact.

The file sink is line-buffered: a power cut loses at most the record being
written, which matters on a battery-powered device. Old runs are pruned by
``retention`` so the SD card cannot fill up.

This module deliberately does NOT import config.py — config imports
hw.device_discovery, which logs, so importing config here would be circular.
The log directory is therefore resolved from __file__ instead.
"""

import os
import sys
from datetime import datetime
from pathlib import Path

try:
    from loguru import logger
except ModuleNotFoundError:  # pragma: no cover - device may lack the dependency
    # Degrade to stdlib logging rather than refusing to boot, mirroring how the
    # AI modules handle a missing optional dependency.
    import logging as _logging

    _logging.basicConfig(
        level=_logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(name)s - %(message)s",
        stream=sys.stderr,
    )

    class _FallbackLogger:
        """Minimal loguru-compatible shim (brace formatting, .success, .exception)."""

        def __init__(self):
            self._log = _logging.getLogger("cultura")

        def _emit(self, level, message, args, kwargs, exc_info=False):
            text = str(message).format(*args, **kwargs) if (args or kwargs) else str(message)
            self._log.log(level, text, exc_info=exc_info)

        def debug(self, message, *a, **k):
            self._emit(_logging.DEBUG, message, a, k)

        def info(self, message, *a, **k):
            self._emit(_logging.INFO, message, a, k)

        def success(self, message, *a, **k):
            self._emit(_logging.INFO, message, a, k)

        def warning(self, message, *a, **k):
            self._emit(_logging.WARNING, message, a, k)

        def error(self, message, *a, **k):
            self._emit(_logging.ERROR, message, a, k)

        def exception(self, message, *a, **k):
            self._emit(_logging.ERROR, message, a, k, exc_info=True)

        def critical(self, message, *a, **k):
            self._emit(_logging.CRITICAL, message, a, k)

    logger = _FallbackLogger()  # type: ignore[assignment]

LOG_DIR = Path(__file__).resolve().parent / "data" / "logs"

_CONSOLE_FORMAT = (
    "<green>{time:HH:mm:ss.SSS}</green> | "
    "<level>{level: <8}</level> | "
    "<cyan>{name}</cyan> - <level>{message}</level>"
)
_FILE_FORMAT = (
    "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | "
    "{name}:{function}:{line} - {message}"
)

_configured = False
_log_file: Path | None = None


def setup_logging(console_level: str | None = None) -> Path | None:
    """Installs the console and per-run file sinks. Safe to call more than once.

    Returns the path of the run's log file, or None when loguru is unavailable or
    the log directory cannot be created (the console sink still works in that case).
    """
    global _configured, _log_file
    if _configured:
        return _log_file

    _configured = True

    if not hasattr(logger, "remove"):  # fallback shim: stdlib basicConfig is enough
        return None

    level = (console_level or os.environ.get("CULTURA_LOG_LEVEL") or "INFO").upper()

    logger.remove()
    logger.add(
        sys.stderr,
        level=level,
        format=_CONSOLE_FORMAT,
        colorize=True,
        backtrace=True,
        diagnose=False,
    )

    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        _log_file = LOG_DIR / f"cultura_{datetime.now():%Y%m%d_%H%M%S}.log"
        logger.add(
            str(_log_file),
            level="DEBUG",
            format=_FILE_FORMAT,
            rotation="5 MB",
            retention=10,
            encoding="utf-8",
            buffering=1,  # line-buffered: survives an abrupt power loss
            backtrace=True,
            diagnose=False,
        )
    except OSError as exc:
        _log_file = None
        logger.warning("Could not open log file in {}: {}", LOG_DIR, exc)

    return _log_file


def get_log_file() -> Path | None:
    """Returns the path of the current run's log file, or None if there isn't one."""
    return _log_file


__all__ = ["logger", "setup_logging", "get_log_file", "LOG_DIR"]
