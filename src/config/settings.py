"""
Configuration loader for the arms architecture.

Reads environment variables with sensible defaults.
Supports PAPER vs LIVE mode via TRADE_MODE env var.

If a ``.env`` file exists in the project root, values are loaded
automatically via *python-dotenv* (optional dependency).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

# ── Load .env if python-dotenv is installed ──────────────────────────
try:
    from dotenv import load_dotenv

    load_dotenv()  # reads .env from cwd / parents; existing env vars win
except ModuleNotFoundError:
    pass  # python-dotenv is optional


class TradeMode(str, Enum):
    """Trading mode — controls whether orders hit the real market."""

    PAPER = "PAPER"
    LIVE = "LIVE"


@dataclass(frozen=True)
class Settings:
    """Immutable application settings populated from environment variables."""

    # ── Mode ──────────────────────────────────────────────────────────
    trade_mode: TradeMode = TradeMode.PAPER

    # ── Broker connection ─────────────────────────────────────────────
    ib_host: str = "127.0.0.1"
    ib_port: int = 7497          # 7497 = TWS paper, 7496 = TWS live
    ib_client_id: int = 1

    # ── Heartbeat / timing ────────────────────────────────────────────
    heartbeat_interval_s: float = 10.0

    # ── Logging ───────────────────────────────────────────────────────
    log_level: str = "INFO"
    log_json: bool = False       # emit JSON lines when True

    # ── Data paths ────────────────────────────────────────────────────
    data_dir: str = "data"
    log_dir: str = "logs"

    @property
    def is_live(self) -> bool:
        return self.trade_mode is TradeMode.LIVE

    @property
    def is_paper(self) -> bool:
        return self.trade_mode is TradeMode.PAPER


def load_settings() -> Settings:
    """Build a *Settings* instance from ``os.environ``.

    Every env var is prefixed with ``TL_`` (trade-labs).
    Missing vars fall back to the dataclass defaults.
    """

    def _env(key: str, default: Optional[str] = None) -> Optional[str]:
        return os.environ.get(f"TL_{key}", default)

    mode_raw = _env("TRADE_MODE", TradeMode.PAPER.value).upper()
    try:
        trade_mode = TradeMode(mode_raw)
    except ValueError:
        trade_mode = TradeMode.PAPER

    return Settings(
        trade_mode=trade_mode,
        ib_host=_env("IB_HOST", "127.0.0.1"),
        ib_port=int(_env("IB_PORT", "7497")),
        ib_client_id=int(_env("IB_CLIENT_ID", "1")),
        heartbeat_interval_s=float(_env("HEARTBEAT_INTERVAL_S", "10.0")),
        log_level=_env("LOG_LEVEL", "INFO").upper(),
        log_json=_env("LOG_JSON", "false").lower() in ("1", "true", "yes"),
        data_dir=_env("DATA_DIR", "data"),
        log_dir=_env("LOG_DIR", "logs"),
    )


# Module-level singleton — import this from other modules.
settings = load_settings()

# ── Startup diagnostics (uses stdlib logging to avoid circular imports) ──
_log = logging.getLogger("trade_labs.config")

_finnhub_key = os.environ.get("FINNHUB_API_KEY", "")
if _finnhub_key:
    _masked = _finnhub_key[:4] + "****" + _finnhub_key[-2:] if len(_finnhub_key) > 6 else "****"
    _log.info("FINNHUB_API_KEY is set (%s)", _masked)
else:
    _log.warning("FINNHUB_API_KEY is NOT set — Finnhub news/catalyst features will be unavailable")

_log.info(
    "Settings loaded: mode=%s  ib=%s:%d  log_level=%s",
    settings.trade_mode.value,
    settings.ib_host,
    settings.ib_port,
    settings.log_level,
)
