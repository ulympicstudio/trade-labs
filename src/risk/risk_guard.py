from dataclasses import dataclass
from datetime import date
from typing import Optional
import json
import logging
from pathlib import Path

from config.risk_limits import (
    MAX_OPEN_RISK_PCT,
    DAILY_MAX_LOSS_PCT,
    MAX_TRADES_PER_DAY,
    MAX_RISK_PER_TRADE_PCT,
)
from src.risk.session_gate import check_session_gate, GateResult
from src.signals.regime import get_regime, RegimeState, PANIC

_log = logging.getLogger(__name__)

_TRADE_COUNT_FILE = Path("data/trade_count_state.json")


def _save_trade_count(count: int, session_date: str) -> None:
    """Persist today's trade count to disk."""
    _TRADE_COUNT_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = _TRADE_COUNT_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps({
        "trades_taken_today": count,
        "date": session_date,
    }, indent=2))
    tmp.replace(_TRADE_COUNT_FILE)


def _load_trade_count() -> int:
    """Load today's trade count from disk. Returns 0 if file missing or stale."""
    try:
        data = json.loads(_TRADE_COUNT_FILE.read_text())
        if data.get("date") == str(date.today()):
            return data.get("trades_taken_today", 0)
        return 0
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        return 0

@dataclass
class RiskStatus:
    allowed: bool
    reason: str = ""
    quality_score: float = 1.0

@dataclass
class RiskState:
    day: date
    trades_taken_today: int = 0
    trading_halted: bool = False
    halted_reason: str = ""


# Module-level persistent state — survives across calls within the process
# and loads from disk on import to survive restarts.
_risk_state = RiskState(day=date.today(), trades_taken_today=_load_trade_count())
if _risk_state.trades_taken_today > 0:
    _log.info("Restored trade count from disk: %d trades today", _risk_state.trades_taken_today)


def get_risk_state() -> RiskState:
    """Return the module-level RiskState, rolling over if the day changed."""
    global _risk_state
    if _risk_state.day != date.today():
        _risk_state = RiskState(day=date.today(), trades_taken_today=_load_trade_count())
    return _risk_state

def usd(x: float) -> float:
    return float(round(x, 2))

def calc_daily_max_loss_usd(equity_usd: float) -> float:
    return usd(equity_usd * DAILY_MAX_LOSS_PCT)

def calc_max_open_risk_usd(equity_usd: float) -> float:
    return usd(equity_usd * MAX_OPEN_RISK_PCT)

def calc_max_risk_per_trade_usd(equity_usd: float) -> float:
    return usd(equity_usd * MAX_RISK_PER_TRADE_PCT)

def should_halt_trading(
    state: RiskState,
    equity_usd: float,
    realized_pnl_usd: float,
    unrealized_pnl_usd: float,
) -> Optional[str]:
    """
    Halt if current-day PnL breaches the daily max loss.
    """
    daily_limit = calc_daily_max_loss_usd(equity_usd)
    total_pnl = realized_pnl_usd + unrealized_pnl_usd
    if total_pnl <= -daily_limit:
        return f"Daily loss limit hit ({usd(total_pnl)} <= -{daily_limit})"
    return None

def approve_new_trade(
    state: RiskState,
    equity_usd: float,
    open_risk_usd: float,
    proposed_trade_risk_usd: float,
) -> RiskStatus:
    """
    Portfolio-level gatekeeper.
    """
    max_open = calc_max_open_risk_usd(equity_usd)
    would_pass = (open_risk_usd + proposed_trade_risk_usd) <= max_open
    _log.debug(
        "approve_new_trade open_risk=$%.2f proposed=$%.2f cap=$%.2f pass=%s",
        open_risk_usd, proposed_trade_risk_usd, max_open, would_pass,
    )

    # Session gate check
    gate = check_session_gate()
    if not gate.allowed:
        return RiskStatus(False, f"Session gate blocked: {gate.reason}", quality_score=0.0)

    # Regime check — block new entries in RED regime
    # (spy_closes will be empty here so regime falls back to YELLOW safely)
    regime = get_regime()
    if regime.regime == PANIC:
        return RiskStatus(False, f"Regime PANIC — no new entries: {regime.reasons}", quality_score=0.0)

    if state.trading_halted:
        return RiskStatus(False, f"Trading halted: {state.halted_reason}")

    if state.trades_taken_today >= MAX_TRADES_PER_DAY:
        return RiskStatus(False, f"Max trades per day reached ({MAX_TRADES_PER_DAY})")

    max_open = calc_max_open_risk_usd(equity_usd)
    if open_risk_usd + proposed_trade_risk_usd > max_open:
        return RiskStatus(
            False,
            f"Open risk cap exceeded: {usd(open_risk_usd)} + {usd(proposed_trade_risk_usd)} > {max_open}",
        )

    max_trade = calc_max_risk_per_trade_usd(equity_usd)
    if proposed_trade_risk_usd > max_trade:
        return RiskStatus(
            False,
            f"Per-trade risk cap exceeded: {usd(proposed_trade_risk_usd)} > {max_trade}",
        )

    return RiskStatus(True, "Approved", quality_score=gate.quality_score)


def record_trade_taken() -> None:
    """Increment the daily trade counter and persist to disk."""
    state = get_risk_state()
    state.trades_taken_today += 1
    _save_trade_count(state.trades_taken_today, str(date.today()))
    _log.debug("trade_count incremented to %d", state.trades_taken_today)
