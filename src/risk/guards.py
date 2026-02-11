"""Risk guard helpers."""
from dataclasses import dataclass
from datetime import date
from typing import Optional


@dataclass
class RiskState:
    """Track daily risk state."""
    day: date


@dataclass
class TradeApprovalStatus:
    """Result of trade approval check."""
    allowed: bool
    reason: Optional[str] = None


def check_risk(position_size: float, max_size: float) -> bool:
    """Return True if position_size is within max_size."""
    return position_size <= max_size


def calc_max_open_risk_usd(equity_usd: float) -> float:
    """Calculate maximum open/concurrent risk at 2% of equity."""
    return equity_usd * 0.02


def calc_daily_max_loss_usd(equity_usd: float) -> float:
    """Calculate daily maximum loss limit at 1% of equity."""
    return equity_usd * 0.01


def approve_new_trade(
    state: RiskState,
    equity_usd: float,
    open_risk_usd: float,
    proposed_trade_risk_usd: float,
) -> TradeApprovalStatus:
    """
    Check if a new trade can be approved based on risk limits.
    
    Returns TradeApprovalStatus with allowed=True if trade fits within limits.
    """
    max_open_risk = calc_max_open_risk_usd(equity_usd)
    
    # Check if adding this trade would exceed open risk cap
    if open_risk_usd + proposed_trade_risk_usd > max_open_risk:
        return TradeApprovalStatus(
            allowed=False,
            reason=f"Trade risk ${proposed_trade_risk_usd} would exceed max open risk of ${max_open_risk}"
        )
    
    return TradeApprovalStatus(allowed=True)


def should_halt_trading(
    state: RiskState,
    equity_usd: float,
    realized_pnl_usd: float,
    unrealized_pnl_usd: float,
) -> Optional[str]:
    """
    Check if trading should be halted due to daily loss limit exceeded.
    
    Returns reason string if trading should halt, None if trading should continue.
    """
    daily_max_loss = calc_daily_max_loss_usd(equity_usd)
    total_loss = realized_pnl_usd + unrealized_pnl_usd
    
    if total_loss < -daily_max_loss:
        return f"Daily loss ${-total_loss} exceeds limit of ${daily_max_loss}"
    
    return None
