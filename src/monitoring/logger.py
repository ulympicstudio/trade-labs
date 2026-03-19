"""
Structured logging utility for the arms architecture.

Provides a consistent logger factory that:
  - Emits human-readable logs by default.
  - Emits JSON lines when ``settings.log_json`` is True.
  - Attaches ``arm=`` and ``mode=`` context to every record.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from typing import Optional

from src.config.settings import settings


class _JsonFormatter(logging.Formatter):
    """Formats log records as single-line JSON objects."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "arm": getattr(record, "arm", "unknown"),
            "mode": getattr(record, "mode", settings.trade_mode.value),
            "msg": record.getMessage(),
        }
        if record.exc_info and record.exc_info[1]:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


class _HumanFormatter(logging.Formatter):
    """Readable format: ``[LEVEL] arm | message``."""

    fmt_str = "%(asctime)s [%(levelname)-5s] %(arm)s | %(message)s"

    def format(self, record: logging.LogRecord) -> str:
        if not hasattr(record, "arm"):
            record.arm = "unknown"  # type: ignore[attr-defined]
        if not hasattr(record, "mode"):
            record.mode = settings.trade_mode.value  # type: ignore[attr-defined]
        self._style._fmt = self.fmt_str
        return super().format(record)


class _SafeStreamHandler(logging.StreamHandler):
    """StreamHandler that silently ignores BrokenPipeError on flush/emit.

    This prevents noisy tracebacks when stdout is piped through a process
    that exits early (e.g. ``head``, ``timeout``, or a closed ``tee``).
    """

    def flush(self) -> None:
        try:
            super().flush()
        except BrokenPipeError:
            pass

    def emit(self, record: logging.LogRecord) -> None:
        try:
            super().emit(record)
        except BrokenPipeError:
            pass


def get_logger(arm_name: str, level: Optional[str] = None) -> logging.Logger:
    """Return a logger scoped to *arm_name*.

    Parameters
    ----------
    arm_name:
        Short identifier for the arm (e.g. ``"ingest"``, ``"risk"``).
    level:
        Override log level.  Falls back to ``settings.log_level``.
    """
    logger = logging.getLogger(f"trade_labs.{arm_name}")
    if logger.handlers:
        return logger  # already set up

    handler = _SafeStreamHandler(sys.stdout)
    formatter: logging.Formatter
    if settings.log_json:
        formatter = _JsonFormatter()
    else:
        formatter = _HumanFormatter()
    handler.setFormatter(formatter)

    logger.addHandler(handler)
    logger.setLevel(getattr(logging, (level or settings.log_level).upper(), logging.INFO))
    logger.propagate = False

    # Inject default extra fields so %(arm)s never errors out.
    old_factory = logger.makeRecord

    def _make_record(*args, **kwargs):  # type: ignore[override]
        record = old_factory(*args, **kwargs)
        if not hasattr(record, "arm"):
            record.arm = arm_name  # type: ignore[attr-defined]
        if not hasattr(record, "mode"):
            record.mode = settings.trade_mode.value  # type: ignore[attr-defined]
        return record

    logger.makeRecord = _make_record  # type: ignore[assignment]
    return logger
