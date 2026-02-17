"""
Daily PnL Manager: Real kill switch based on session P&L.

Tracks:
- Session start equity (9:30 AM ET)
- Realized P&L (closed trades)
- Unrealized P&L (open positions)
- Total P&L for the day

Triggers kill switch if total PnL ≤ -1.5% equity.
Prevents death spirals on bad days.
"""

import logging
import json
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple, Dict, Any

from ib_insync import IB

log = logging.getLogger(__name__)

# Kill switch threshold: -1.5% equity
DAILY_LOSS_THRESHOLD = -0.015

# Session tracking file
SESSION_FILE = Path("data/trade_history/session.json")


def get_session_start_equity() -> Optional[float]:
    """Get the equity recorded at session start (9:30 AM)."""
    if SESSION_FILE.exists():
        with open(SESSION_FILE) as f:
            data = json.load(f)
            return data.get("start_equity")
    return None


def record_session_start_equity(equity: float):
    """Record session start equity at 9:30 AM market open."""
    SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
    
    data = {
        "session_date": datetime.utcnow().date().isoformat(),
        "start_equity": equity,
        "start_time": datetime.utcnow().isoformat(),
    }
    
    with open(SESSION_FILE, "w") as f:
        json.dump(data, f, indent=2, default=str)
    
    log.info(f"Recorded session start equity: ${equity:,.2f}")


def get_realized_pnl() -> float:
    """
    Calculate realized P&L from closed trades.
    Reads trades.json and sums up pnl for CLOSED status trades.
    """
    trades_file = Path("data/trade_history/trades.json")
    if not trades_file.exists():
        return 0.0
    
    with open(trades_file) as f:
        trades = json.load(f)
    
    realized = sum(
        (t.get("pnl") or 0.0)
        for t in trades
        if t.get("status") == "CLOSED"
    )
    return realized


def get_unrealized_pnl(ib: IB) -> float:
    """
    Calculate unrealized P&L from open positions.
    Uses current market prices via IB.
    """
    unrealized = 0.0
    
    try:
        for trade in ib.openTrades():
            contract = trade.contract
            position = trade.order.totalQuantity
            
            if position == 0:
                continue
            
            # Get market data
            try:
                market_data = ib.reqMktData(contract, "", True, False)
                ib.sleep(0.1)
            except Exception as e:
                log.warning(f"Failed to get market data for {contract.symbol}: {e}")
                continue
            
            # Get current price (bid/ask average or last)
            try:
                if market_data.last is not None and market_data.last > 0:
                    current_price = market_data.last
                elif market_data.bid is not None and market_data.ask is not None:
                    current_price = (market_data.bid + market_data.ask) / 2.0
                else:
                    log.warning(f"No price data for {contract.symbol}")
                    continue
            except Exception as e:
                log.warning(f"Error extracting price for {contract.symbol}: {e}")
                continue
            
            # Find the trade entry price from trades.json
            trades_file = Path("data/trade_history/trades.json")
            entry_price = None
            
            if trades_file.exists():
                with open(trades_file) as f:
                    trades = json.load(f)
                # Find the most recent OPEN trade for this symbol
                for t in reversed(trades):
                    if (t.get("symbol") == contract.symbol and 
                        t.get("status") == "OPEN"):
                        entry_price = t.get("entry_price")
                        break
            
            if entry_price is None:
                log.warning(f"No entry price found for {contract.symbol}")
                continue
            
            # Calculate unrealized for this position
            pnl = (current_price - entry_price) * position
            unrealized += pnl
            
            log.debug(f"{contract.symbol}: {position} @ ${entry_price:.2f} → ${current_price:.2f}, U/R: ${pnl:,.2f}")
    
    except Exception as e:
        log.error(f"Error calculating unrealized P&L: {e}")
    
    return unrealized


def get_daily_pnl(ib: IB) -> Tuple[float, float, float]:
    """
    Get total daily P&L breakdown.
    
    Returns:
        (realized_pnl, unrealized_pnl, total_pnl)
    """
    realized = get_realized_pnl()
    unrealized = get_unrealized_pnl(ib)
    total = realized + unrealized
    
    return realized, unrealized, total


def get_daily_pnl_percent(ib: IB) -> Optional[float]:
    """
    Calculate daily P&L as percentage of session start equity.
    
    Returns:
        P&L percentage (e.g., -0.015 for -1.5%) or None if session not started.
    """
    start_equity = get_session_start_equity()
    if start_equity is None or start_equity == 0:
        return None
    
    _, _, total_pnl = get_daily_pnl(ib)
    return total_pnl / start_equity


def is_kill_switch_active(ib: IB) -> bool:
    """
    Check if daily kill switch should be active.
    
    Returns:
        True if realized + unrealized PnL ≤ -1.5% of start equity.
    """
    pnl_pct = get_daily_pnl_percent(ib)
    
    if pnl_pct is None:
        # Session not yet started, no kill switch
        return False
    
    is_active = pnl_pct <= DAILY_LOSS_THRESHOLD
    
    if is_active:
        log.warning(f"KILL SWITCH ACTIVE: Daily P&L = {pnl_pct*100:.2f}% (threshold: {DAILY_LOSS_THRESHOLD*100:.2f}%)")
    
    return is_active


def get_kill_switch_status(ib: IB) -> Dict[str, Any]:
    """
    Get full kill switch status for reporting.
    
    Returns:
        Dict with session_date, start_equity, realized_pnl, unrealized_pnl, 
        total_pnl, pnl_percent, is_active
    """
    start_equity = get_session_start_equity()
    realized, unrealized, total = get_daily_pnl(ib)
    pnl_pct = get_daily_pnl_percent(ib)
    is_active = is_kill_switch_active(ib)
    
    return {
        "session_date": datetime.utcnow().date().isoformat(),
        "start_equity": start_equity,
        "realized_pnl": realized,
        "unrealized_pnl": unrealized,
        "total_pnl": total,
        "pnl_percent": pnl_pct * 100 if pnl_pct else None,
        "is_active": is_active,
        "threshold_percent": DAILY_LOSS_THRESHOLD * 100,
    }
