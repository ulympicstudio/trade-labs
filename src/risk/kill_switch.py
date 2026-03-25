"""
Kill Switch & Circuit Breakers — hard safety limits for the risk arm.

Provides a single ``check_circuit_breakers()`` function that returns
PASS / BLOCK with reasons.  Integrates into risk_main.py before any
order approval.

Breakers
--------
1. Daily loss limit (trailing from session high-water mark)
2. Max trades per hour (global and per-symbol)
3. Max symbol exposure (% of equity in one name)
4. Max correlated exposure (mega-cap cluster: SPY/QQQ overlap)
5. Volatility halt (if wide spreads / ATR spike → reduce or block)

All thresholds are env-overridable.  PAPER mode uses relaxed defaults.
"""

from __future__ import annotations

import logging
import os
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

from src.config.settings import settings
from src.risk.kill_switch_state import load_state, save_state

_log = logging.getLogger(__name__)


# ── Tunables ─────────────────────────────────────────────────────────

_PAPER = settings.trade_mode.value == "PAPER"

# 1. Daily loss limit
_DAILY_LOSS_MAX_PCT = float(os.environ.get(
    "TL_KS_DAILY_LOSS_PCT",
    "0.03" if _PAPER else "0.02",  # 3% paper, 2% live
))

# 2. Trades per hour
_MAX_TRADES_PER_HOUR = int(os.environ.get(
    "TL_KS_MAX_TRADES_HOUR",
    "120" if _PAPER else "60",
))
_MAX_TRADES_PER_SYMBOL_HOUR = int(os.environ.get(
    "TL_KS_MAX_PER_SYMBOL_HOUR",
    "8" if _PAPER else "5",
))

# 3. Max symbol exposure (% of equity in one name)
_MAX_SYMBOL_EXPOSURE_PCT = float(os.environ.get(
    "TL_KS_MAX_SYMBOL_PCT",
    "0.10",  # 10% of equity in one name
))

# 4. Correlated exposure (mega-cap basket)
_CORRELATED_CLUSTER: Set[str] = set(os.environ.get(
    "TL_KS_CORRELATED_SYMBOLS",
    "SPY,QQQ,AAPL,MSFT,GOOG,GOOGL,AMZN,NVDA,META,TSLA",
).split(","))
_MAX_CLUSTER_EXPOSURE_PCT = float(os.environ.get(
    "TL_KS_MAX_CLUSTER_PCT",
    "0.25",  # 25% of equity in the cluster
))

# 5. Volatility halt (spread threshold)
_VOL_HALT_SPREAD_PCT = float(os.environ.get(
    "TL_KS_VOL_HALT_SPREAD",
    "0.005",  # 0.5% spread → halt new orders
))

# 6. Max failed orders per hour
_MAX_FAILED_ORDERS_HOUR = int(os.environ.get(
    "TL_KS_MAX_FAILED_HOUR",
    "10",
))

# 7. Loss streak (consecutive losses)
_MAX_LOSS_STREAK = int(os.environ.get(
    "TL_KS_MAX_LOSS_STREAK",
    "7",
))
# Loss streak pause duration
_LOSS_STREAK_PAUSE_S = int(os.environ.get(
    "TL_KILL_LOSS_STREAK_PAUSE_S",
    "900",
))

# 8. ATR spike breaker (SPY ATR % >> baseline)
_ATR_SPIKE_MULT = float(os.environ.get("TL_KILL_ATR_SPIKE_MULT", "2.5"))
_ATR_SPIKE_ACTION = os.environ.get("TL_KILL_ATR_SPIKE_ACTION", "REDUCE").upper()  # REDUCE | BLOCK

# Master kill switch (emergency off)
_KILL_SWITCH_ACTIVE = os.environ.get(
    "TL_KILL_SWITCH", "false"
).lower() in ("1", "true", "yes")


# ── Result ───────────────────────────────────────────────────────────

PASS = "PASS"
BLOCK = "BLOCK"
REDUCE = "REDUCE"


@dataclass(frozen=True)
class BreakerResult:
    """Result of circuit breaker checks."""

    action: str = PASS                  # PASS / BLOCK / REDUCE
    reasons: List[str] = field(default_factory=list)
    size_mult: float = 1.0             # multiplicative size adjustment

    @property
    def ok(self) -> bool:
        return self.action == PASS

    @property
    def blocked(self) -> bool:
        return self.action == BLOCK


# ── State tracking ───────────────────────────────────────────────────

_trade_timestamps: List[float] = []                # global trade times
_symbol_trade_ts: Dict[str, List[float]] = defaultdict(list)
_session_hwm: float = 0.0                         # session high-water mark (PnL)
_session_pnl: float = 0.0                         # current session PnL
_symbol_exposure: Dict[str, float] = {}            # symbol → USD exposure
_equity: float = float(os.environ.get("TL_ACCOUNT_EQUITY", "100000"))
_failed_order_ts: List[float] = []                 # timestamps of failed orders
_loss_streak: int = 0                              # consecutive losing fills
_loss_streak_pause_until: float = 0.0              # epoch when pause expires
_atr_spike_active: bool = False                    # latest ATR spike state


def reset_session() -> None:
    """Reset daily state (call at session open).

    After clearing, attempts to restore state from disk if a same-day
    snapshot exists (survives restarts within the same trading day).
    """
    global _session_hwm, _session_pnl, _loss_streak, _loss_streak_pause_until, _atr_spike_active
    _trade_timestamps.clear()
    _symbol_trade_ts.clear()
    _symbol_exposure.clear()
    _failed_order_ts.clear()
    _session_hwm = 0.0
    _session_pnl = 0.0
    _loss_streak = 0
    _loss_streak_pause_until = 0.0
    _atr_spike_active = False

    # Restore from disk if same-day state exists
    saved = load_state()
    if saved is not None:
        restore_state(saved)
        _log.info("Kill switch state restored from disk")
    else:
        _log.info("Kill switch starting fresh (no saved state for today)")


def record_trade(symbol: str, risk_usd: float) -> None:
    """Record a trade execution for circuit breaker tracking."""
    now = time.time()
    _trade_timestamps.append(now)
    _symbol_trade_ts[symbol].append(now)
    _symbol_exposure[symbol] = _symbol_exposure.get(symbol, 0.0) + risk_usd


def update_pnl(pnl: float) -> None:
    """Update session PnL (call periodically)."""
    global _session_pnl, _session_hwm
    _session_pnl = pnl
    _session_hwm = max(_session_hwm, pnl)


def update_exposure(symbol: str, usd: float) -> None:
    """Set absolute exposure for a symbol."""
    _symbol_exposure[symbol] = usd


def record_failed_order() -> None:
    """Record a failed/rejected order for circuit breaker tracking."""
    _failed_order_ts.append(time.time())


def record_fill(symbol: str, qty: int, price: float, pnl: float = 0.0) -> None:
    """Record a fill outcome for loss-streak tracking.

    Parameters
    ----------
    pnl : float
        Realized PnL of this fill.  Negative → loss.
    """
    global _loss_streak, _session_pnl, _session_hwm, _loss_streak_pause_until
    if pnl < 0:
        _loss_streak += 1
        if _loss_streak >= _MAX_LOSS_STREAK:
            _loss_streak_pause_until = time.time() + _LOSS_STREAK_PAUSE_S
    else:
        _loss_streak = 0
    _session_pnl += pnl
    _session_hwm = max(_session_hwm, _session_pnl)


def update_atr_spike(atr_pct: float, baseline_pct: float) -> None:
    """Update the ATR spike state from regime data.

    If ``atr_pct / baseline_pct >= _ATR_SPIKE_MULT`` the spike flag is set,
    otherwise it is cleared.  Called by risk_main on every intent.
    """
    global _atr_spike_active
    if baseline_pct > 0 and atr_pct / baseline_pct >= _ATR_SPIKE_MULT:
        if not _atr_spike_active:
            _atr_spike_active = True
            import logging
            logging.getLogger("kill_switch").warning(
                "atr_spike_ON  atr_pct=%.4f baseline=%.4f ratio=%.2f mult=%.1f",
                atr_pct, baseline_pct, atr_pct / baseline_pct, _ATR_SPIKE_MULT,
            )
    else:
        _atr_spike_active = False


# ── Core check ───────────────────────────────────────────────────────

def check_circuit_breakers(
    symbol: str,
    risk_usd: float = 0.0,
    spread_pct: float = 0.0,
) -> BreakerResult:
    """Run all circuit breakers.  Returns PASS, REDUCE, or BLOCK.

    Parameters
    ----------
    symbol:
        The symbol being traded.
    risk_usd:
        Dollar risk of the proposed trade.
    spread_pct:
        Current bid-ask spread as fraction of price.
    """
    reasons: List[str] = []
    size_mult = 1.0

    # ── 0. Master kill switch ────────────────────────────────────────
    if _KILL_SWITCH_ACTIVE:
        return BreakerResult(action=BLOCK, reasons=["KILL_SWITCH_ACTIVE"])

    # ── 1. Daily loss limit ──────────────────────────────────────────
    drawdown = _session_hwm - _session_pnl
    max_drawdown = _equity * _DAILY_LOSS_MAX_PCT
    if drawdown > max_drawdown:
        return BreakerResult(
            action=BLOCK,
            reasons=[f"daily_loss_limit: drawdown=${drawdown:.0f} > max=${max_drawdown:.0f}"],
        )
    # Approaching limit → reduce sizing
    if drawdown > max_drawdown * 0.7:
        remaining_frac = max(0.2, 1.0 - (drawdown / max_drawdown))
        size_mult = min(size_mult, remaining_frac)
        reasons.append(f"approaching_daily_limit: mult={remaining_frac:.2f}")

    # ── 2. Trades per hour ───────────────────────────────────────────
    now = time.time()
    hour_ago = now - 3600

    global_recent = [t for t in _trade_timestamps if t > hour_ago]
    if len(global_recent) >= _MAX_TRADES_PER_HOUR:
        return BreakerResult(
            action=BLOCK,
            reasons=[f"max_trades_hour: {len(global_recent)} >= {_MAX_TRADES_PER_HOUR}"],
        )

    sym_recent = [t for t in _symbol_trade_ts.get(symbol, []) if t > hour_ago]
    if len(sym_recent) >= _MAX_TRADES_PER_SYMBOL_HOUR:
        return BreakerResult(
            action=BLOCK,
            reasons=[f"max_per_symbol_hour: {symbol} {len(sym_recent)} >= {_MAX_TRADES_PER_SYMBOL_HOUR}"],
        )

    # ── 3. Symbol exposure ───────────────────────────────────────────
    current_exposure = _symbol_exposure.get(symbol, 0.0) + risk_usd
    max_exposure = _equity * _MAX_SYMBOL_EXPOSURE_PCT
    if current_exposure > max_exposure:
        return BreakerResult(
            action=BLOCK,
            reasons=[f"symbol_exposure: {symbol}=${current_exposure:.0f} > max=${max_exposure:.0f}"],
        )

    # ── 4. Correlated cluster exposure ───────────────────────────────
    if symbol in _CORRELATED_CLUSTER:
        cluster_total = sum(
            _symbol_exposure.get(s, 0.0) for s in _CORRELATED_CLUSTER
        ) + risk_usd
        max_cluster = _equity * _MAX_CLUSTER_EXPOSURE_PCT
        if cluster_total > max_cluster:
            return BreakerResult(
                action=BLOCK,
                reasons=[f"cluster_exposure: ${cluster_total:.0f} > max=${max_cluster:.0f}"],
            )

    # ── 5. Volatility halt ───────────────────────────────────────────
    if spread_pct > _VOL_HALT_SPREAD_PCT:
        # Wide spread → reduce, not block (let high-conviction through)
        size_mult = min(size_mult, 0.5)
        reasons.append(f"wide_spread={spread_pct:.4f}→size×0.5")
    # ── 6. Failed orders per hour ───────────────────────────────
    failed_recent = [t for t in _failed_order_ts if t > hour_ago]
    if len(failed_recent) >= _MAX_FAILED_ORDERS_HOUR:
        return BreakerResult(
            action=BLOCK,
            reasons=[f"max_failed_orders: {len(failed_recent)} >= {_MAX_FAILED_ORDERS_HOUR}"],
        )

    # ── 7. Loss streak ─────────────────────────────────────────
    if _loss_streak >= _MAX_LOSS_STREAK:
        now_t = time.time()
        if now_t < _loss_streak_pause_until:
            remaining = int(_loss_streak_pause_until - now_t)
            return BreakerResult(
                action=BLOCK,
                reasons=[f"loss_streak_pause: {_loss_streak}>={_MAX_LOSS_STREAK} resume_in={remaining}s"],
            )
        # Pause expired — reset streak and allow
        _loss_streak = 0
        _log.info("loss_streak_pause_expired reset streak→0")
        reasons.append("loss_streak_pause_expired_reset")

    # ── 8. ATR spike ───────────────────────────────────────────────
    if _atr_spike_active:
        if _ATR_SPIKE_ACTION == "BLOCK":
            return BreakerResult(
                action=BLOCK,
                reasons=["atr_spike_block"],
            )
        else:
            size_mult = min(size_mult, 0.5)
            reasons.append("atr_spike_reduce")

    # ── All clear ────────────────────────────────────────────────────
    # Persist state on every breaker check so it survives restarts
    save_state(snapshot_state())

    if reasons and size_mult < 1.0:
        return BreakerResult(action=REDUCE, reasons=reasons, size_mult=size_mult)

    return BreakerResult(action=PASS, reasons=reasons, size_mult=1.0)


# ── Snapshot / Restore ────────────────────────────────────────────

def snapshot_state() -> dict:
    """Capture current kill switch state into a serialisable dict."""
    now = time.time()
    hour_ago = now - 3600

    # Count trades per symbol from timestamps in the last hour
    trade_count_by_symbol: Dict[str, int] = {}
    for sym, ts_list in _symbol_trade_ts.items():
        count = len([t for t in ts_list if t > hour_ago])
        if count:
            trade_count_by_symbol[sym] = count

    return {
        "session_pnl": round(_session_pnl, 2),
        "high_water": round(_session_hwm, 2),
        "consecutive_losers": _loss_streak,
        "trades_this_session": len(_trade_timestamps),
        "failed_orders": len([t for t in _failed_order_ts if t > hour_ago]),
        "trade_count_by_symbol": trade_count_by_symbol,
    }


def restore_state(state: dict) -> None:
    """Restore kill switch state from a previously saved dict.

    Only restores aggregate counters — individual timestamps are NOT
    restored because they would be stale.
    """
    global _session_pnl, _session_hwm, _loss_streak

    if "session_pnl" in state:
        _session_pnl = float(state["session_pnl"])
        _log.info("  restored session_pnl=%.2f", _session_pnl)
    else:
        _log.warning("  session_pnl missing from saved state — skipped")

    if "high_water" in state:
        _session_hwm = float(state["high_water"])
        _log.info("  restored high_water=%.2f", _session_hwm)
    else:
        _log.warning("  high_water missing from saved state — skipped")

    if "consecutive_losers" in state:
        _loss_streak = int(state["consecutive_losers"])
        _log.info("  restored consecutive_losers=%d", _loss_streak)
    else:
        _log.warning("  consecutive_losers missing from saved state — skipped")

    if "failed_orders" in state:
        # Recreate stub timestamps so the hourly count is approximately right
        count = int(state["failed_orders"])
        now = time.time()
        _failed_order_ts.clear()
        _failed_order_ts.extend([now] * count)
        _log.info("  restored failed_orders=%d (stub timestamps)", count)

    if "trades_this_session" in state:
        count = int(state["trades_this_session"])
        now = time.time()
        _trade_timestamps.clear()
        _trade_timestamps.extend([now] * count)
        _log.info("  restored trades_this_session=%d (stub timestamps)", count)

    if "trade_count_by_symbol" in state:
        now = time.time()
        _symbol_trade_ts.clear()
        for sym, cnt in state["trade_count_by_symbol"].items():
            _symbol_trade_ts[sym] = [now] * int(cnt)
        _log.info("  restored trade_count_by_symbol: %s", state["trade_count_by_symbol"])


def status_summary() -> Dict[str, object]:
    """Return a dict summarizing current breaker state (for snapshots)."""
    now = time.time()
    hour_ago = now - 3600
    return {
        "kill_switch": _KILL_SWITCH_ACTIVE,
        "session_pnl": round(_session_pnl, 2),
        "session_hwm": round(_session_hwm, 2),
        "drawdown": round(_session_hwm - _session_pnl, 2),
        "max_drawdown": round(_equity * _DAILY_LOSS_MAX_PCT, 2),
        "trades_this_hour": len([t for t in _trade_timestamps if t > hour_ago]),
        "max_trades_hour": _MAX_TRADES_PER_HOUR,
        "symbols_exposed": len(_symbol_exposure),
        "total_exposure": round(sum(_symbol_exposure.values()), 2),
        "failed_orders_1h": len([t for t in _failed_order_ts if t > hour_ago]),
        "loss_streak": _loss_streak,
        "atr_spike": _atr_spike_active,
    }
