"""PnL Attribution Engine — multi-dimensional trade outcome tracking.

Records opens, fills, closes, and marks for every position, then
aggregates realized / unrealized PnL across all U.T.S. dimensions:
playbook, engine_bucket, sector, industry, regime, market_mode,
volatility_state, scorecard_bias, and confluence_score.

All thresholds are overridable via ``TL_ATTRIB_*`` env vars.
**Stocks only — no options logic.**
"""

from __future__ import annotations

import os
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Deque, Dict, List, Optional, Tuple

from src.monitoring.logger import get_logger

log = get_logger("attribution")

# ── Tunables ────────────────────────────────────────────────────────

ATTRIB_ENABLED: bool = os.environ.get(
    "TL_ATTRIB_ENABLED", "true"
).lower() in ("1", "true", "yes")

MONITOR_ENABLED: bool = os.environ.get(
    "TL_ATTRIB_MONITOR_ENABLED", "true"
).lower() in ("1", "true", "yes")

_LOOKBACK_TRADES: int = int(os.environ.get("TL_ATTRIB_LOOKBACK_TRADES", "50"))
_MIN_TRADES: int = int(os.environ.get("TL_ATTRIB_MIN_TRADES", "5"))


# ── Data structures ─────────────────────────────────────────────────

@dataclass
class TradeAttribution:
    """Full attribution record for one trade (open → close)."""
    symbol: str = ""
    intent_id: str = ""
    playbook: str = ""              # news / rotation / volatility / meanrevert
    engine_bucket: str = ""         # same as playbook (canonical name)
    sector: str = ""
    industry: str = ""
    regime: str = ""                # TREND_UP / TREND_DOWN / CHOP / PANIC
    market_mode: str = ""           # TREND_EXPANSION / ROTATION_TAPE / ...
    volatility_state: str = ""      # QUIET / BUILDING / TRIGGERED
    scorecard_bias: float = 1.0
    confluence_score: float = 0.0
    allocation_bucket: str = ""
    side: str = "LONG"
    qty: int = 0
    entry_price: float = 0.0
    exit_price: float = 0.0
    current_price: float = 0.0     # latest mark
    entry_ts: float = 0.0
    exit_ts: float = 0.0
    hold_time_s: float = 0.0
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    mfe: float = 0.0               # max favorable excursion $
    mae: float = 0.0               # max adverse excursion $
    r_multiple: float = 0.0
    risk_usd: float = 0.0
    exit_action: str = ""           # HOLD / TRAIL / TRIM_25 / EXIT_FULL / ...
    exit_reason: str = ""           # time_stop / target / trail_hit / ...
    is_closed: bool = False


@dataclass
class AttributionSummary:
    """Aggregated summary across all tracked trades."""
    total_trades: int = 0
    open_trades: int = 0
    closed_trades: int = 0
    total_realized_pnl: float = 0.0
    total_unrealized_pnl: float = 0.0
    avg_r_multiple: float = 0.0
    win_count: int = 0
    loss_count: int = 0
    win_rate: float = 0.0
    avg_hold_time_s: float = 0.0
    best_bucket: str = ""
    best_bucket_pnl: float = 0.0
    worst_bucket: str = ""
    worst_bucket_pnl: float = 0.0
    best_mode: str = ""
    best_mode_pnl: float = 0.0
    worst_mode: str = ""
    worst_mode_pnl: float = 0.0


# ── Module state ────────────────────────────────────────────────────

_trades: Deque[TradeAttribution] = deque(maxlen=max(200, _LOOKBACK_TRADES * 4))
_open_index: Dict[str, TradeAttribution] = {}   # symbol → open trade
_closed_count: int = 0
_total_realized: float = 0.0


# ── Record lifecycle ────────────────────────────────────────────────

def record_open(
    symbol: str,
    side: str,
    entry_price: float,
    qty: int,
    risk_usd: float = 0.0,
    playbook: str = "",
    sector: str = "",
    industry: str = "",
    regime: str = "",
    market_mode: str = "",
    volatility_state: str = "",
    scorecard_bias: float = 1.0,
    confluence_score: float = 0.0,
    allocation_bucket: str = "",
    intent_id: str = "",
) -> None:
    """Register a new trade open for attribution tracking."""
    if not ATTRIB_ENABLED:
        return

    bucket = playbook.lower() or "unknown"
    rec = TradeAttribution(
        symbol=symbol,
        intent_id=intent_id,
        playbook=bucket,
        engine_bucket=bucket,
        sector=sector,
        industry=industry,
        regime=regime,
        market_mode=market_mode,
        volatility_state=volatility_state,
        scorecard_bias=scorecard_bias,
        confluence_score=confluence_score,
        allocation_bucket=allocation_bucket or bucket,
        side=side,
        qty=qty,
        entry_price=entry_price,
        current_price=entry_price,
        risk_usd=risk_usd,
        entry_ts=time.time(),
    )
    _open_index[symbol] = rec
    _trades.append(rec)
    log.info(
        "attrib_open symbol=%s side=%s entry=%.2f qty=%d playbook=%s "
        "sector=%s mode=%s regime=%s risk=$%.2f confluence=%.2f",
        symbol, side, entry_price, qty, bucket,
        sector, market_mode, regime, risk_usd, confluence_score,
    )


def record_fill(symbol: str, fill_price: float, filled_qty: int) -> None:
    """Update trade record with fill price (may differ from entry)."""
    if not ATTRIB_ENABLED:
        return
    rec = _open_index.get(symbol)
    if rec is None:
        return
    rec.entry_price = fill_price
    rec.current_price = fill_price
    rec.qty = filled_qty
    log.info(
        "attrib_fill symbol=%s fill=%.2f qty=%d",
        symbol, fill_price, filled_qty,
    )


def record_mark(
    symbol: str,
    current_price: float,
    mfe: float = 0.0,
    mae: float = 0.0,
    r_multiple: float = 0.0,
    exit_action: str = "",
) -> None:
    """Update current mark / MFE / MAE for an open position."""
    if not ATTRIB_ENABLED:
        return
    rec = _open_index.get(symbol)
    if rec is None:
        return
    rec.current_price = current_price
    pnl_mult = 1 if rec.side == "LONG" else -1
    rec.unrealized_pnl = round((current_price - rec.entry_price) * rec.qty * pnl_mult, 2)
    if mfe != 0:
        rec.mfe = max(rec.mfe, mfe)
    if mae != 0:
        rec.mae = min(rec.mae, mae)
    if r_multiple != 0:
        rec.r_multiple = r_multiple
    if exit_action:
        rec.exit_action = exit_action


def record_close(
    symbol: str,
    exit_price: float = 0.0,
    realized_pnl: float = 0.0,
    r_multiple: float = 0.0,
    exit_action: str = "",
    exit_reason: str = "",
    mfe: float = 0.0,
    mae: float = 0.0,
) -> None:
    """Close out an attributed trade."""
    global _closed_count, _total_realized
    if not ATTRIB_ENABLED:
        return
    rec = _open_index.pop(symbol, None)
    if rec is None:
        return

    now = time.time()
    rec.exit_price = exit_price or rec.current_price
    rec.exit_ts = now
    rec.hold_time_s = now - rec.entry_ts
    rec.is_closed = True

    if realized_pnl != 0:
        rec.realized_pnl = realized_pnl
    else:
        pnl_mult = 1 if rec.side == "LONG" else -1
        rec.realized_pnl = round(
            (rec.exit_price - rec.entry_price) * rec.qty * pnl_mult, 2
        )
    rec.unrealized_pnl = 0.0
    if r_multiple != 0:
        rec.r_multiple = r_multiple
    elif rec.risk_usd > 0:
        rec.r_multiple = round(rec.realized_pnl / rec.risk_usd, 2)
    if exit_action:
        rec.exit_action = exit_action
    if exit_reason:
        rec.exit_reason = exit_reason
    if mfe != 0:
        rec.mfe = max(rec.mfe, mfe)
    if mae != 0:
        rec.mae = min(rec.mae, mae)

    _closed_count += 1
    _total_realized += rec.realized_pnl

    log.info(
        "attrib_close symbol=%s pnl=%.2f R=%.2f hold=%ds action=%s "
        "reason=%s playbook=%s mode=%s",
        symbol, rec.realized_pnl, rec.r_multiple,
        int(rec.hold_time_s), rec.exit_action,
        rec.exit_reason, rec.playbook, rec.market_mode,
    )


# ── Summaries ───────────────────────────────────────────────────────

def _get_recent(closed_only: bool = False) -> List[TradeAttribution]:
    """Return recent trades (bounded by lookback)."""
    out = []
    for t in _trades:
        if closed_only and not t.is_closed:
            continue
        out.append(t)
    return out[-_LOOKBACK_TRADES:]


def _bucket_agg(key_fn, closed_only: bool = True) -> Dict[str, Dict]:
    """Aggregate PnL by an arbitrary key function."""
    agg: Dict[str, Dict] = defaultdict(
        lambda: {"pnl": 0.0, "count": 0, "wins": 0, "r_sum": 0.0}
    )
    for t in _get_recent(closed_only=closed_only):
        k = key_fn(t)
        if not k:
            continue
        pnl = t.realized_pnl if t.is_closed else t.unrealized_pnl
        agg[k]["pnl"] += pnl
        agg[k]["count"] += 1
        if pnl > 0:
            agg[k]["wins"] += 1
        agg[k]["r_sum"] += t.r_multiple
    return dict(agg)


def compute_attribution_summary() -> AttributionSummary:
    """Compute full attribution summary across all recent trades."""
    recent = _get_recent()
    if not recent:
        return AttributionSummary()

    closed = [t for t in recent if t.is_closed]
    opn = [t for t in recent if not t.is_closed]

    total_realized = sum(t.realized_pnl for t in closed)
    total_unrealized = sum(t.unrealized_pnl for t in opn)
    wins = [t for t in closed if t.realized_pnl > 0]
    losses = [t for t in closed if t.realized_pnl <= 0]
    avg_r = (sum(t.r_multiple for t in closed) / len(closed)) if closed else 0.0
    avg_hold = (sum(t.hold_time_s for t in closed) / len(closed)) if closed else 0.0

    # Bucket aggregation
    bucket_agg = _bucket_agg(lambda t: t.engine_bucket)
    best_b = max(bucket_agg.items(), key=lambda kv: kv[1]["pnl"], default=("", {"pnl": 0}))
    worst_b = min(bucket_agg.items(), key=lambda kv: kv[1]["pnl"], default=("", {"pnl": 0}))

    # Mode aggregation
    mode_agg = _bucket_agg(lambda t: t.market_mode)
    best_m = max(mode_agg.items(), key=lambda kv: kv[1]["pnl"], default=("", {"pnl": 0}))
    worst_m = min(mode_agg.items(), key=lambda kv: kv[1]["pnl"], default=("", {"pnl": 0}))

    return AttributionSummary(
        total_trades=len(recent),
        open_trades=len(opn),
        closed_trades=len(closed),
        total_realized_pnl=round(total_realized, 2),
        total_unrealized_pnl=round(total_unrealized, 2),
        avg_r_multiple=round(avg_r, 2),
        win_count=len(wins),
        loss_count=len(losses),
        win_rate=round(len(wins) / max(len(closed), 1), 3),
        avg_hold_time_s=round(avg_hold, 1),
        best_bucket=best_b[0],
        best_bucket_pnl=round(best_b[1]["pnl"], 2),
        worst_bucket=worst_b[0],
        worst_bucket_pnl=round(worst_b[1]["pnl"], 2),
        best_mode=best_m[0],
        best_mode_pnl=round(best_m[1]["pnl"], 2),
        worst_mode=worst_m[0],
        worst_mode_pnl=round(worst_m[1]["pnl"], 2),
    )


def get_top_winners(n: int = 5) -> List[TradeAttribution]:
    """Return top N winning closed trades by realized PnL."""
    closed = [t for t in _get_recent(closed_only=True) if t.realized_pnl > 0]
    closed.sort(key=lambda t: t.realized_pnl, reverse=True)
    return closed[:n]


def get_top_losers(n: int = 5) -> List[TradeAttribution]:
    """Return top N losing closed trades by realized PnL."""
    closed = [t for t in _get_recent(closed_only=True) if t.realized_pnl <= 0]
    closed.sort(key=lambda t: t.realized_pnl)
    return closed[:n]


def get_bucket_summary() -> Dict[str, Dict]:
    """Return PnL aggregated by engine_bucket (playbook)."""
    return _bucket_agg(lambda t: t.engine_bucket)


def get_mode_summary() -> Dict[str, Dict]:
    """Return PnL aggregated by market_mode."""
    return _bucket_agg(lambda t: t.market_mode)


def get_playbook_summary() -> Dict[str, Dict]:
    """Return PnL aggregated by playbook name."""
    return _bucket_agg(lambda t: t.playbook)


def get_regime_summary() -> Dict[str, Dict]:
    """Return PnL aggregated by regime."""
    return _bucket_agg(lambda t: t.regime)


def get_sector_summary() -> Dict[str, Dict]:
    """Return PnL aggregated by sector."""
    return _bucket_agg(lambda t: t.sector)


def get_recent_attribution_snapshot() -> Dict:
    """Return a JSON-serialisable snapshot of recent attributions."""
    summary = compute_attribution_summary()
    open_trades = [t for t in _trades if not t.is_closed]
    return {
        "ts": time.time(),
        "total_trades": summary.total_trades,
        "open_trades": summary.open_trades,
        "closed_trades": summary.closed_trades,
        "total_realized_pnl": summary.total_realized_pnl,
        "total_unrealized_pnl": summary.total_unrealized_pnl,
        "avg_r_multiple": summary.avg_r_multiple,
        "win_rate": summary.win_rate,
        "best_bucket": summary.best_bucket,
        "best_bucket_pnl": summary.best_bucket_pnl,
        "worst_bucket": summary.worst_bucket,
        "worst_bucket_pnl": summary.worst_bucket_pnl,
        "best_mode": summary.best_mode,
        "worst_mode": summary.worst_mode,
        "bucket_summary": get_bucket_summary(),
        "mode_summary": get_mode_summary(),
        "regime_summary": get_regime_summary(),
        "open_positions": [
            {
                "symbol": t.symbol,
                "side": t.side,
                "entry": t.entry_price,
                "current": t.current_price,
                "unrealized_pnl": t.unrealized_pnl,
                "playbook": t.playbook,
                "mode": t.market_mode,
                "r_multiple": t.r_multiple,
            }
            for t in open_trades
        ],
    }


# ── Expectancy helpers (used by self_tuning) ────────────────────────

def get_bucket_expectancy(bucket: str) -> Tuple[float, int]:
    """Return (expectancy_per_trade, sample_count) for a given bucket.

    Expectancy = avg(realized_pnl) across closed trades in that bucket.
    Returns (0.0, 0) when no closed trades exist.
    """
    closed = [
        t for t in _get_recent(closed_only=True)
        if t.engine_bucket == bucket
    ]
    if not closed:
        return 0.0, 0
    avg = sum(t.realized_pnl for t in closed) / len(closed)
    return round(avg, 2), len(closed)


def get_mode_expectancy(mode: str) -> Tuple[float, int]:
    """Return (expectancy_per_trade, sample_count) for a given market mode."""
    closed = [
        t for t in _get_recent(closed_only=True)
        if t.market_mode == mode
    ]
    if not closed:
        return 0.0, 0
    avg = sum(t.realized_pnl for t in closed) / len(closed)
    return round(avg, 2), len(closed)


def get_playbook_mode_expectancy(playbook: str, mode: str) -> Tuple[float, int]:
    """Return (expectancy, count) for a playbook × mode cross."""
    closed = [
        t for t in _get_recent(closed_only=True)
        if t.playbook == playbook and t.market_mode == mode
    ]
    if not closed:
        return 0.0, 0
    avg = sum(t.realized_pnl for t in closed) / len(closed)
    return round(avg, 2), len(closed)


def get_open_count() -> int:
    """Return number of currently open attributed trades."""
    return len(_open_index)


def get_closed_count() -> int:
    """Return total closed trades."""
    return _closed_count
