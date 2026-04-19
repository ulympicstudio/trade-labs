"""
Shared data schemas for the arms event bus.

All messages exchanged between arms are plain dataclasses so they stay
dependency-free and trivially serialisable via :mod:`src.schemas.codec`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


# ── Market data ──────────────────────────────────────────────────────

@dataclass
class MarketSnapshot:
    """Point-in-time quote + derived indicators for a single symbol."""

    symbol: str
    ts: datetime = field(default_factory=_utcnow)
    session: str = "RTH"          # "RTH" | "ETH" | "PRE" | "POST"
    last: float = 0.0
    bid: float = 0.0
    ask: float = 0.0
    volume: int = 0
    cum_volume: int = 0               # cumulative volume (for RVOL computation)
    vwap: float = 0.0
    atr: float = 0.0
    rsi14: Optional[float] = None
    rvol: Optional[float] = None      # relative volume (vs lookback avg)


# ── News ─────────────────────────────────────────────────────────────

@dataclass
class UniverseCandidates:
    """Current ingest universe snapshot (up to UNIVERSE_MAX symbols)."""

    symbols: List[str] = field(default_factory=list)
    ts: datetime = field(default_factory=_utcnow)
    size: int = 0


@dataclass
class NewsEvent:
    """A single news item associated with a symbol."""

    symbol: str
    ts: datetime = field(default_factory=_utcnow)
    source: str = "unknown"
    headline: str = ""
    url: Optional[str] = None
    sentiment: Optional[float] = None     # −1.0 … +1.0
    relevance: Optional[float] = None     #  0.0 … 1.0
    # ── News Shock Engine v1 fields ──
    impact_score: int = 0
    impact_tags: List[str] = field(default_factory=list)
    burst_flag: bool = False
    source_provider: str = "unknown"


# ── Signal / intent ──────────────────────────────────────────────────

@dataclass
class TradeIntent:
    """A scored trade idea ready for risk review."""

    symbol: str
    ts: datetime = field(default_factory=_utcnow)
    intent_id: str = field(default_factory=_new_id)
    candidate_id: str = ""
    setup_type: str = ""          # e.g. "breakout", "mean_revert"
    direction: str = "LONG"       # "LONG" | "SHORT"
    confidence: float = 0.0       # 0.0 … 1.0
    entry_zone_low: float = 0.0
    entry_zone_high: float = 0.0
    invalidation: float = 0.0     # price where thesis fails
    reason_codes: List[str] = field(default_factory=list)
    # ── Sizing mode (hybrid sizing) ──
    mode: str = "FULL"            # "FULL" | "REDUCED" | "MIN_PROBE"
    # ── Signal-to-risk contract (enriched) ──
    unified_score: float = 0.0    # composite score from signal scoring
    regime: str = ""              # regime at emission time
    session: str = ""             # session at emission time
    sector_heat: str = ""         # sector state: LEADING / NEUTRAL / WEAK
    rotation_state: str = ""      # industry rotation state
    risk_slot: str = ""           # precomputed risk bucket + cap_mult hint
    regime_score_mult: float = 1.0   # regime-derived score multiplier
    regime_cap_mult: float = 1.0     # regime-derived position cap multiplier
    regime_max_pos: int = 5          # regime-derived max simultaneous positions
    spread_bps: float = 0.0          # bid-ask spread in basis points


# ── Off-hours candidates ─────────────────────────────────────────────

@dataclass
class WatchCandidate:
    """Symbol flagged for watching during off-hours (not actionable yet)."""

    symbol: str
    ts: datetime = field(default_factory=_utcnow)
    score: float = 0.0
    reason_codes: List[str] = field(default_factory=list)
    session: str = "OFF_HOURS"  # session when generated
    news_count_2h: int = 0
    latest_headline: str = ""
    # Score component breakdown (for monitor display)
    news_points: float = 0.0
    momentum_pts: float = 0.0
    vol_points: float = 0.0
    spread_points: float = 0.0
    rsi_points: float = 0.0
    liq_points: float = 0.0
    total_score: float = 0.0
    quality: str = ""  # HIGH / MED / LOW
    # ── Composite Intelligence Layer ──
    symbol_score: float = 0.0
    sector_score: float = 0.0
    industry_score: float = 0.0
    market_score: float = 0.0
    composite_score: float = 0.0
    # ── Agent intel fields ──
    priority: float = 0.0
    catalyst_tags: List[str] = field(default_factory=list)
    source: str = ""


@dataclass
class OpenPlanCandidate:
    """Suggested trade plan generated off-hours; needs RTH confirmation."""

    symbol: str
    ts: datetime = field(default_factory=_utcnow)
    suggested_entry: float = 0.0
    suggested_stop: float = 0.0
    confidence: float = 0.0
    session: str = "OFF_HOURS"
    reason_codes: List[str] = field(default_factory=list)
    news_count_2h: int = 0
    latest_headline: str = ""
    vol_pct: float = 0.0
    # Score component breakdown (for monitor display)
    news_points: float = 0.0
    momentum_pts: float = 0.0
    vol_points: float = 0.0
    spread_points: float = 0.0
    rsi_points: float = 0.0
    liq_points: float = 0.0
    total_score: float = 0.0
    quality: str = ""  # HIGH / MED / LOW
    # ── News Shock Engine v1 ──
    impact_score: int = 0
    burst_flag: bool = False
    # ── Event-gate fields (structural gating) ──
    event_score: int = 0
    strategy: str = ""            # e.g. "off_hours_board", "premarket_board"
    event_gate_pass: bool = False # True only if event gate approved
    tradeable: bool = False       # True when gate + quality checks pass
    regime: str = ""              # regime at publish time
    # ── Sector Intelligence ──
    sector: str = ""              # e.g. "Technology"
    industry: str = ""            # e.g. "Semiconductors"
    sector_state: str = ""        # LEADING / NEUTRAL / WEAK
    # ── Composite Intelligence Layer ──
    symbol_score: float = 0.0
    sector_score: float = 0.0
    industry_score: float = 0.0
    market_score: float = 0.0
    composite_score: float = 0.0


@dataclass
class PlanDraft:
    """Risk-reviewed draft plan (no order placement)."""

    symbol: str
    ts: datetime = field(default_factory=_utcnow)
    suggested_entry: float = 0.0
    suggested_stop: float = 0.0
    qty: int = 0
    risk_usd: float = 0.0
    confidence: float = 0.0
    notes: str = ""
    reason_codes: List[str] = field(default_factory=list)
    news_count_2h: int = 0
    latest_headline: str = ""
    vol_pct: float = 0.0
    stop_distance_pct: float = 0.0
    # Score component breakdown (for monitor display)
    news_points: float = 0.0
    momentum_pts: float = 0.0
    vol_points: float = 0.0
    spread_points: float = 0.0
    rsi_points: float = 0.0
    liq_points: float = 0.0
    total_score: float = 0.0
    quality: str = ""  # HIGH / MED / LOW
    # ── News Shock Engine v1 ──
    impact_score: int = 0
    burst_flag: bool = False
    # ── Sector Intelligence ──
    sector: str = ""              # e.g. "Technology"
    industry: str = ""            # e.g. "Semiconductors"
    sector_state: str = ""        # LEADING / NEUTRAL / WEAK

# ── Order blueprint (execution-ready, paper or live) ─────────────────

@dataclass
class OrderBlueprint:
    """Fully-parameterised bracket order ready for paper/live submission.

    Built by the risk arm from a PlanDraft during PREMARKET.
    Execution arm will only submit if EXECUTION_ENABLED=true.
    """

    symbol: str
    ts: datetime = field(default_factory=_utcnow)
    direction: str = "LONG"
    qty: int = 0
    entry_ladder: List[float] = field(default_factory=list)  # 3-5 price levels
    stop_price: float = 0.0
    trail_pct: float = 0.5           # trailing stop pct (0.3 – 1.0)
    take_profit_levels: List[float] = field(default_factory=list)  # optional
    timeout_s: int = 120             # cancel if not filled within N seconds
    max_spread_pct: float = 0.25     # max bid-ask spread % to enter
    risk_usd: float = 0.0
    confidence: float = 0.0
    total_score: float = 0.0
    quality: str = ""                # HIGH / MED / LOW
    stop_distance_pct: float = 0.0
    reason_codes: List[str] = field(default_factory=list)
    notes: str = ""
    # ── News Shock Engine v1 ──
    impact_score: int = 0
    burst_flag: bool = False
    escalation: bool = False
    source: str = ""              # "open_plan" | "trade_intent"
    # ── Sector Intelligence ──
    session: str = ""             # session at blueprint creation: OFF_HOURS | PREMARKET | RTH
    sector: str = ""              # e.g. "Technology"
    industry: str = ""            # e.g. "Semiconductors"
    sector_state: str = ""        # LEADING / NEUTRAL / WEAK


# ── Order planning ───────────────────────────────────────────────────

@dataclass
class OrderPlan:
    """Concrete order instructions produced by risk/sizing arm."""

    symbol: str
    ts: datetime = field(default_factory=_utcnow)
    intent_id: str = ""
    candidate_id: str = ""
    direction: str = "BUY"        # "BUY" | "SELL"
    qty: int = 0
    entry_type: str = "LMT"      # "LMT" | "MKT" | "STP_LMT"
    limit_prices: List[float] = field(default_factory=list)
    stop_price: float = 0.0
    trail_params: Dict[str, Any] = field(default_factory=dict)
    tif: str = "DAY"             # "DAY" | "GTC" | "IOC"
    timeout_s: float = 60.0
    mode: str = "FULL"            # "FULL" | "REDUCED" | "MIN_PROBE"


# ── Execution events ────────────────────────────────────────────────

@dataclass
class OrderEvent:
    """Lifecycle event for a broker order."""

    symbol: str
    ts: datetime = field(default_factory=_utcnow)
    event_type: str = ""          # "NEW" | "PARTIAL" | "FILLED" | "CANCELLED" | "REJECTED"
    order_id: str = ""
    status: str = ""
    filled_qty: int = 0
    avg_fill_price: float = 0.0
    message: str = ""              # human-readable detail (e.g. error reason)


# ── Heartbeat ────────────────────────────────────────────────────────

@dataclass
class Heartbeat:
    """Periodic heartbeat emitted by every arm."""

    arm: str
    ts: datetime = field(default_factory=_utcnow)
    status: str = "ok"            # "ok" | "degraded" | "error"


# ─── Legacy aliases (keep old imports working) ───────────────────────
# These were the original placeholder names.  Downstream code that
# imported them will continue to work.

MarketTick = MarketSnapshot  # alias


@dataclass
class Signal:
    """Placeholder kept for backwards compat — prefer TradeIntent."""

    symbol: str
    direction: str = "LONG"
    strength: float = 0.0
    source: str = "unknown"
    ts: datetime = field(default_factory=_utcnow)
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class OrderRequest:
    """Placeholder kept for backwards compat — prefer OrderPlan."""

    symbol: str
    side: str = "BUY"
    qty: int = 0
    order_type: str = "LMT"
    limit_price: Optional[float] = None
    ts: datetime = field(default_factory=_utcnow)
    metadata: Dict[str, Any] = field(default_factory=dict)
