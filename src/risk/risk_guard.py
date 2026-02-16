from dataclasses import dataclass
from datetime import date
from typing import Optional

from config.risk_limits import (
    MAX_OPEN_RISK_PCT,
    DAILY_MAX_LOSS_PCT,
    MAX_TRADES_PER_DAY,
    MAX_RISK_PER_TRADE_PCT,
)

@dataclass
class RiskStatus:
    allowed: bool
    reason: str = ""

@dataclass
class RiskState:
    day: date
    trades_taken_today: int = 0
    trading_halted: bool = False
    halted_reason: str = ""

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

    return RiskStatus(True, "Approved")
