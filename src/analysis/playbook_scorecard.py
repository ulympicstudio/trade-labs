"""Playbook Scorecard Engine — realized-performance feedback loop.

Tracks how each playbook bucket (news, rotation, volatility, meanrevert,
consensus, breakout), sector, industry, regime, market_mode, and session
is actually performing, then exposes scores that adapt future allocation
and risk decisions.

All thresholds are overridable via ``TL_SCORECARD_*`` env vars.
"""

from __future__ import annotations

import os
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Deque, Dict, List, Optional, Tuple

from src.monitoring.logger import get_logger

log = get_logger("scorecard")

# ── Tunables ────────────────────────────────────────────────────────

SCORECARD_ENABLED: bool = os.environ.get(
    "TL_SCORECARD_ENABLED", "true"
).lower() in ("1", "true", "yes")

_LOOKBACK_TRADES: int = int(os.environ.get("TL_SCORECARD_LOOKBACK_TRADES", "25"))
_MIN_TRADES: int = int(os.environ.get("TL_SCORECARD_MIN_TRADES", "5"))
_BOOST_MAX: float = float(os.environ.get("TL_SCORECARD_BOOST_MAX", "1.20"))
_CUT_MAX: float = float(os.environ.get("TL_SCORECARD_CUT_MAX", "0.75"))

# Force-path for testing
_FORCE_PLAYBOOK: str = os.environ.get("TL_SCORECARD_FORCE_PLAYBOOK", "").strip().lower()
_FORCE_SCORE: float = float(os.environ.get("TL_SCORECARD_FORCE_SCORE", "0"))

MONITOR_ENABLED: bool = os.environ.get(
    "TL_SCORECARD_MONITOR_ENABLED", "true"
).lower() in ("1", "true", "yes")


# ── Data structures ─────────────────────────────────────────────────

@dataclass
class TradeRecord:
    """Minimal record of one closed trade for scorecard accounting."""
    symbol: str = ""
    playbook: str = ""          # news / rotation / volatility / meanrevert / consensus / breakout
    sector: str = ""
    industry: str = ""
    regime: str = ""
    market_mode: str = ""
    session_state: str = ""
    entry_price: float = 0.0
    exit_price: float = 0.0
    qty: int = 0
    pnl: float = 0.0
    pnl_pct: float = 0.0
    risk_usd: float = 0.0
    r_multiple: float = 0.0     # pnl / risk_usd
    open_ts: float = 0.0
    close_ts: float = 0.0


@dataclass
class PlaybookScorecard:
    """Rolling performance statistics for a single bucket key."""
    key: str = ""
    bucket_type: str = ""       # playbook / sector / industry / regime / market_mode / session
    trades: int = 0
    wins: int = 0
    losses: int = 0
    win_rate: float = 0.0
    gross_pnl: float = 0.0
    avg_pnl: float = 0.0
    expectancy: float = 0.0     # avg_win × win_rate − avg_loss × loss_rate
    avg_r_multiple: float = 0.0
    last_n_pnl: List[float] = field(default_factory=list)
    current_drawdown: float = 0.0
    confidence_score: float = 1.0   # multiplier: <1 = weak, >1 = strong


@dataclass
class ScorecardSnapshot:
    """Full snapshot across all bucket dimensions."""
    ts: float = 0.0
    playbook_scores: Dict[str, PlaybookScorecard] = field(default_factory=dict)
    sector_scores: Dict[str, PlaybookScorecard] = field(default_factory=dict)
    industry_scores: Dict[str, PlaybookScorecard] = field(default_factory=dict)
    regime_scores: Dict[str, PlaybookScorecard] = field(default_factory=dict)
    mode_scores: Dict[str, PlaybookScorecard] = field(default_factory=dict)
    session_scores: Dict[str, PlaybookScorecard] = field(default_factory=dict)
    overall_confidence: float = 1.0


# ── Module state ────────────────────────────────────────────────────

# Rolling trade records (bounded by 2 × _LOOKBACK_TRADES to allow re-calc)
_trades: Deque[TradeRecord] = deque(maxlen=max(200, _LOOKBACK_TRADES * 4))

# Open trade tracking: intent_id → partial record
_open_trades: Dict[str, TradeRecord] = {}

# Pre-computed scorecards by dimension
_playbook_cards: Dict[str, PlaybookScorecard] = {}
_sector_cards: Dict[str, PlaybookScorecard] = {}
_industry_cards: Dict[str, PlaybookScorecard] = {}
_regime_cards: Dict[str, PlaybookScorecard] = {}
_mode_cards: Dict[str, PlaybookScorecard] = {}
_session_cards: Dict[str, PlaybookScorecard] = {}
_last_snapshot: Optional[ScorecardSnapshot] = None
_last_recompute_ts: float = 0.0


# ── Trade lifecycle ─────────────────────────────────────────────────

def record_trade_open(
    intent_id: str,
    symbol: str,
    playbook: str,
    sector: str = "",
    industry: str = "",
    regime: str = "",
    market_mode: str = "",
    session_state: str = "",
    entry_price: float = 0.0,
    qty: int = 0,
    risk_usd: float = 0.0,
) -> None:
    """Register an open trade for future close-out accounting."""
    if not SCORECARD_ENABLED:
        return
    rec = TradeRecord(
        symbol=symbol,
        playbook=playbook.lower(),
        sector=sector,
        industry=industry,
        regime=regime,
        market_mode=market_mode,
        session_state=session_state,
        entry_price=entry_price,
        qty=qty,
        risk_usd=risk_usd,
        open_ts=time.time(),
    )
    _open_trades[intent_id] = rec
    log.info(
        "scorecard_open intent=%s sym=%s playbook=%s sector=%s regime=%s "
        "mode=%s session=%s entry=%.2f qty=%d risk=$%.2f",
        intent_id, symbol, playbook, sector, regime,
        market_mode, session_state, entry_price, qty, risk_usd,
    )


def record_trade_close(
    intent_id: str,
    exit_price: float,
    pnl: float = 0.0,
    pnl_pct: float = 0.0,
) -> None:
    """Close out a tracked trade and update scorecard statistics."""
    if not SCORECARD_ENABLED:
        return
    rec = _open_trades.pop(intent_id, None)
    if rec is None:
        log.debug("scorecard_close_unknown intent=%s (no open record)", intent_id)
        return

    rec.exit_price = exit_price
    rec.close_ts = time.time()

    # Calculate PnL if not supplied
    if pnl == 0.0 and rec.entry_price > 0 and rec.qty > 0:
        pnl = (exit_price - rec.entry_price) * rec.qty
        pnl_pct = ((exit_price / rec.entry_price) - 1.0) * 100.0 if rec.entry_price > 0 else 0.0
    rec.pnl = round(pnl, 2)
    rec.pnl_pct = round(pnl_pct, 4)
    rec.r_multiple = round(pnl / rec.risk_usd, 2) if rec.risk_usd > 0 else 0.0

    _trades.append(rec)

    log.info(
        "scorecard_close intent=%s sym=%s playbook=%s pnl=$%.2f pnl_pct=%.2f%% "
        "r_mult=%.2f risk=$%.2f regime=%s mode=%s",
        intent_id, rec.symbol, rec.playbook, rec.pnl, rec.pnl_pct,
        rec.r_multiple, rec.risk_usd, rec.regime, rec.market_mode,
    )

    # Recompute scorecards
    _recompute_all()


def _simulate_trade_close_for_dev(
    intent_id: str,
    fill_price: float,
    qty: int,
) -> None:
    """Dev-mode approximation: close trade immediately at fill price.

    In PAPER_FILL / dev, we don't have a real exit, so we record
    a near-zero PnL trade to keep scorecard statistics flowing.
    The scorecard will adapt as real close events arrive.
    """
    rec = _open_trades.get(intent_id)
    if rec is None:
        return
    # Approximate: 0 PnL (entry ≈ fill) — avoids noise in win-rate
    # This still feeds the trade-count dimension so min_trades are reached.
    record_trade_close(intent_id, exit_price=fill_price, pnl=0.0, pnl_pct=0.0)


# ── Scorecard computation ───────────────────────────────────────────

def _compute_card(
    key: str,
    bucket_type: str,
    records: List[TradeRecord],
) -> PlaybookScorecard:
    """Compute a scorecard from the last N trade records for a bucket."""
    recent = records[-_LOOKBACK_TRADES:]
    n = len(recent)

    card = PlaybookScorecard(key=key, bucket_type=bucket_type, trades=n)
    if n == 0:
        return card

    wins = [r for r in recent if r.pnl > 0]
    losses = [r for r in recent if r.pnl < 0]
    flat = [r for r in recent if r.pnl == 0]

    card.wins = len(wins)
    card.losses = len(losses)
    card.win_rate = card.wins / n if n > 0 else 0.0
    card.gross_pnl = round(sum(r.pnl for r in recent), 2)
    card.avg_pnl = round(card.gross_pnl / n, 2)

    # Expectancy = avg_win × win_rate − avg_loss × loss_rate
    avg_win = sum(r.pnl for r in wins) / len(wins) if wins else 0.0
    avg_loss = abs(sum(r.pnl for r in losses) / len(losses)) if losses else 0.0
    loss_rate = card.losses / n if n > 0 else 0.0
    card.expectancy = round(avg_win * card.win_rate - avg_loss * loss_rate, 2)

    # R-multiple
    r_mults = [r.r_multiple for r in recent if r.r_multiple != 0]
    card.avg_r_multiple = round(sum(r_mults) / len(r_mults), 2) if r_mults else 0.0

    # Last-N PnL trail
    card.last_n_pnl = [r.pnl for r in recent[-10:]]

    # Drawdown (peak-to-trough on cumulative PnL of last N)
    cum = 0.0
    peak = 0.0
    dd = 0.0
    for r in recent:
        cum += r.pnl
        if cum > peak:
            peak = cum
        trail_dd = peak - cum
        if trail_dd > dd:
            dd = trail_dd
    card.current_drawdown = round(dd, 2)

    # Confidence score — synthesised from win_rate, expectancy, r_multiple
    card.confidence_score = _compute_confidence(card)

    return card


def _compute_confidence(card: PlaybookScorecard) -> float:
    """Derive a [CUT_MAX .. BOOST_MAX] multiplier from scorecard stats.

    - Insufficient trades → 1.0 (neutral)
    - High win-rate + positive expectancy → boost
    - Low win-rate + negative expectancy → cut
    """
    if card.trades < _MIN_TRADES:
        return 1.0

    # Win-rate component: centered on 0.5
    wr_factor = (card.win_rate - 0.5) * 0.6  # ±0.3 max

    # Expectancy component (normalised loosely to ±0.2)
    exp_factor = max(-0.2, min(0.2, card.expectancy / max(abs(card.avg_pnl), 1.0) * 0.1))

    # R-multiple component
    r_factor = max(-0.1, min(0.1, (card.avg_r_multiple - 1.0) * 0.05))

    raw = 1.0 + wr_factor + exp_factor + r_factor
    return round(max(_CUT_MAX, min(_BOOST_MAX, raw)), 3)


def _recompute_all() -> None:
    """Recompute all scorecard dimensions from the trade deque."""
    global _last_recompute_ts, _last_snapshot

    now = time.time()
    _last_recompute_ts = now

    all_records = list(_trades)
    if not all_records:
        return

    # Group by each dimension
    by_playbook: Dict[str, List[TradeRecord]] = defaultdict(list)
    by_sector: Dict[str, List[TradeRecord]] = defaultdict(list)
    by_industry: Dict[str, List[TradeRecord]] = defaultdict(list)
    by_regime: Dict[str, List[TradeRecord]] = defaultdict(list)
    by_mode: Dict[str, List[TradeRecord]] = defaultdict(list)
    by_session: Dict[str, List[TradeRecord]] = defaultdict(list)

    for r in all_records:
        if r.playbook:
            by_playbook[r.playbook].append(r)
        if r.sector:
            by_sector[r.sector].append(r)
        if r.industry:
            by_industry[r.industry].append(r)
        if r.regime:
            by_regime[r.regime].append(r)
        if r.market_mode:
            by_mode[r.market_mode].append(r)
        if r.session_state:
            by_session[r.session_state].append(r)

    # Rebuild cards
    _playbook_cards.clear()
    for k, recs in by_playbook.items():
        _playbook_cards[k] = _compute_card(k, "playbook", recs)
        if _playbook_cards[k].trades >= _MIN_TRADES:
            c = _playbook_cards[k]
            log.info(
                "playbook_score_update bucket=%s trades=%d wins=%d wr=%.2f "
                "pnl=$%.2f exp=$%.2f avg_r=%.2f conf=%.3f",
                k, c.trades, c.wins, c.win_rate,
                c.gross_pnl, c.expectancy, c.avg_r_multiple, c.confidence_score,
            )

    _sector_cards.clear()
    for k, recs in by_sector.items():
        _sector_cards[k] = _compute_card(k, "sector", recs)

    _industry_cards.clear()
    for k, recs in by_industry.items():
        _industry_cards[k] = _compute_card(k, "industry", recs)

    _regime_cards.clear()
    for k, recs in by_regime.items():
        _regime_cards[k] = _compute_card(k, "regime", recs)

    _mode_cards.clear()
    for k, recs in by_mode.items():
        _mode_cards[k] = _compute_card(k, "market_mode", recs)

    _session_cards.clear()
    for k, recs in by_session.items():
        _session_cards[k] = _compute_card(k, "session", recs)

    _last_snapshot = ScorecardSnapshot(
        ts=now,
        playbook_scores=dict(_playbook_cards),
        sector_scores=dict(_sector_cards),
        industry_scores=dict(_industry_cards),
        regime_scores=dict(_regime_cards),
        mode_scores=dict(_mode_cards),
        session_scores=dict(_session_cards),
        overall_confidence=_compute_overall_confidence(),
    )


def _compute_overall_confidence() -> float:
    """Average confidence across playbook buckets (or 1.0 if none)."""
    if not _playbook_cards:
        return 1.0
    scores = [c.confidence_score for c in _playbook_cards.values() if c.trades >= _MIN_TRADES]
    return round(sum(scores) / len(scores), 3) if scores else 1.0


# ── Public query API ────────────────────────────────────────────────

def get_playbook_scorecard(playbook: str) -> PlaybookScorecard:
    """Return scorecard for a specific playbook bucket."""
    if not SCORECARD_ENABLED:
        return PlaybookScorecard(key=playbook, bucket_type="playbook")

    # Force-path override
    if _FORCE_PLAYBOOK and _FORCE_SCORE > 0:
        if playbook.lower() == _FORCE_PLAYBOOK:
            return PlaybookScorecard(
                key=playbook,
                bucket_type="playbook",
                trades=_MIN_TRADES + 1,
                wins=_MIN_TRADES,
                win_rate=0.80,
                gross_pnl=100.0,
                avg_pnl=20.0,
                expectancy=15.0,
                avg_r_multiple=1.5,
                confidence_score=_FORCE_SCORE,
            )

    return _playbook_cards.get(playbook.lower(), PlaybookScorecard(key=playbook, bucket_type="playbook"))


def get_sector_scorecard(sector: str) -> PlaybookScorecard:
    """Return scorecard for a specific sector."""
    return _sector_cards.get(sector, PlaybookScorecard(key=sector, bucket_type="sector"))


def get_regime_scorecard(regime: str) -> PlaybookScorecard:
    """Return scorecard for a specific regime."""
    return _regime_cards.get(regime, PlaybookScorecard(key=regime, bucket_type="regime"))


def get_mode_scorecard(mode: str) -> PlaybookScorecard:
    """Return scorecard for a specific market mode."""
    return _mode_cards.get(mode, PlaybookScorecard(key=mode, bucket_type="market_mode"))


def get_scorecard_snapshot() -> ScorecardSnapshot:
    """Return the latest full snapshot across all dimensions."""
    if _last_snapshot is not None:
        return _last_snapshot
    return ScorecardSnapshot(ts=time.time())


def get_top_playbooks(n: int = 5) -> List[PlaybookScorecard]:
    """Return top N playbooks by confidence score (descending)."""
    cards = [c for c in _playbook_cards.values() if c.trades >= _MIN_TRADES]
    cards.sort(key=lambda c: c.confidence_score, reverse=True)
    return cards[:n]


def get_weak_playbooks(threshold: float = 0.95) -> List[PlaybookScorecard]:
    """Return playbooks with confidence below threshold."""
    return [
        c for c in _playbook_cards.values()
        if c.trades >= _MIN_TRADES and c.confidence_score < threshold
    ]


def get_weight_adjustment(playbook: str) -> float:
    """Return a weight multiplier for allocation engine integration.

    Returns a value in [CUT_MAX .. BOOST_MAX] clamped range.
    1.0 = neutral (default / insufficient data).
    """
    if not SCORECARD_ENABLED:
        return 1.0
    card = get_playbook_scorecard(playbook)
    return card.confidence_score


def get_risk_sizing_mult(playbook: str) -> float:
    """Return a qty multiplier for risk engine integration.

    More conservative than allocation: boost capped at 1.05, cuts at CUT_MAX.
    """
    if not SCORECARD_ENABLED:
        return 1.0
    card = get_playbook_scorecard(playbook)
    # Dampen: allocation can boost up to _BOOST_MAX, risk only up to 1.05
    if card.confidence_score > 1.0:
        return min(1.05, 1.0 + (card.confidence_score - 1.0) * 0.25)
    return max(_CUT_MAX, card.confidence_score)


def get_priority_bias(playbook: str) -> float:
    """Return a priority score adjustment for signal integration.

    Positive for strong playbooks, negative for weak.  Range approx ±10.
    """
    if not SCORECARD_ENABLED:
        return 0.0
    card = get_playbook_scorecard(playbook)
    if card.trades < _MIN_TRADES:
        return 0.0
    return round((card.confidence_score - 1.0) * 50.0, 1)  # ±10 for ±0.20


def get_scorecard_summary() -> Dict[str, Any]:
    """Return a summary dict suitable for monitor display."""
    snap = get_scorecard_snapshot()
    return {
        "ts": snap.ts,
        "overall_confidence": snap.overall_confidence,
        "playbooks": {
            k: {
                "trades": c.trades,
                "win_rate": c.win_rate,
                "pnl": c.gross_pnl,
                "expectancy": c.expectancy,
                "confidence": c.confidence_score,
            }
            for k, c in snap.playbook_scores.items()
        },
        "open_trades": len(_open_trades),
        "total_closed": len(_trades),
    }
