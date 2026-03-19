"""
Sector Intelligence — sector-level state tracking and scoring.

Maintains per-sector rolling state from incoming MarketSnapshot and
NewsEvent data, then provides alignment / relative-strength / heat
signals that feed into event scoring and risk concentration.

Lifecycle
---------
1. Signal arm calls ``update_sector_from_snapshot()`` on every snapshot.
2. Signal arm calls ``update_sector_from_news()`` on every news event.
3. ``get_sector_state()`` returns current state for a sector.
4. ``get_sector_alignment()`` returns scoring bonuses for a symbol.
5. ``get_sector_summary()`` returns compact dict for monitor display.

All state is module-level (single process) — no external deps.

Tunables (env)
--------------
``TL_SECTOR_INTEL_ENABLED``      master on/off (default ``true``)
``TL_SECTOR_RS_WINDOW``          rolling-return window in snapshots (default ``20``)
``TL_SECTOR_HEAT_DECAY_S``       news heat half-life seconds (default ``1800``)
``TL_SECTOR_SYMPATHY_MIN_HEAT``  min sector heat for sympathy bonus (default ``3``)
"""

from __future__ import annotations

import math
import os
import time
import threading
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict, Optional

from src.universe.sector_mapper import classify_symbol, SECTOR_ETFS
from src.data.sector_map import SectorProfile
from src.monitoring.logger import get_logger

log = get_logger("sector_intel")

# ── Tunables ─────────────────────────────────────────────────────────

ENABLED = os.environ.get(
    "TL_SECTOR_INTEL_ENABLED", "true"
).lower() in ("1", "true", "yes")

_RS_WINDOW = int(os.environ.get("TL_SECTOR_RS_WINDOW", "20"))
_HEAT_DECAY_S = float(os.environ.get("TL_SECTOR_HEAT_DECAY_S", "1800"))
_SYMPATHY_MIN_HEAT = int(os.environ.get("TL_SECTOR_SYMPATHY_MIN_HEAT", "3"))

# ── Force-path env toggles (test only) ──────────────────────────────

_FORCE_SECTOR = os.environ.get("TL_TEST_FORCE_SECTOR", "").strip()
_FORCE_SECTOR_STATE = os.environ.get("TL_TEST_FORCE_SECTOR_STATE", "").strip()
_FORCE_SECTOR_SCORE = float(os.environ.get("TL_TEST_FORCE_SECTOR_SCORE", "0"))

if _FORCE_SECTOR and _FORCE_SECTOR_STATE:
    log.warning(
        "FORCE-SECTOR MODE ACTIVE sector=%s state=%s score=%.0f",
        _FORCE_SECTOR, _FORCE_SECTOR_STATE, _FORCE_SECTOR_SCORE,
    )

# ── Sector state constants ───────────────────────────────────────────

BULLISH = "BULLISH"
BEARISH = "BEARISH"
HOT = "HOT"
COLD = "COLD"
NEUTRAL = "NEUTRAL"

# Legacy compat
LEADING = BULLISH
WEAK = BEARISH


@dataclass
class SectorState:
    """Point-in-time state for one sector."""

    sector: str = "UNKNOWN"
    state: str = NEUTRAL          # LEADING / NEUTRAL / WEAK
    relative_strength: float = 0.0  # sector return vs SPY (pct)
    news_heat: float = 0.0       # decayed news event count
    news_count_raw: int = 0      # raw count in window
    breadth_pct: float = 0.0     # % of sector symbols advancing
    etf: str = ""
    n_symbols: int = 0           # tracked symbols in sector
    last_update_ts: float = 0.0


@dataclass
class SectorAlignment:
    """Scoring adjustments a symbol gets from its sector context."""

    sector: str = "UNKNOWN"
    industry: str = "UNKNOWN"
    sector_state: str = NEUTRAL
    pts_sector_align: float = 0.0     # +/- points for sector trend alignment
    pts_sector_rs: float = 0.0        # relative-strength bonus
    pts_sector_heat: float = 0.0      # news-heat bonus
    pts_sector_sympathy: float = 0.0  # sympathy / clustering bonus


# ── Internal per-sector tracker ───────────────────────────────────────

@dataclass
class _SectorTracker:
    """Rolling tracker for a single sector."""

    # Price tracking: sector ETF close-series for relative strength
    etf_prices: Deque[float] = field(default_factory=lambda: deque(maxlen=100))

    # Per-symbol last price (for breadth)
    sym_prices: Dict[str, float] = field(default_factory=dict)
    sym_prev_prices: Dict[str, float] = field(default_factory=dict)

    # News heat: list of (timestamp, impact_score)
    news_events: Deque[tuple] = field(default_factory=lambda: deque(maxlen=200))

    last_update_ts: float = 0.0


_lock = threading.Lock()
_trackers: Dict[str, _SectorTracker] = {}
_spy_last: float = 0.0
_spy_prices: Deque[float] = deque(maxlen=200)
_last_sector_states: Dict[str, str] = {}
_last_heartbeat_ts: float = 0.0
_HEARTBEAT_INTERVAL_S = 60.0


def _get_tracker(sector: str) -> _SectorTracker:
    """Get or create tracker for a sector (caller holds _lock)."""
    if sector not in _trackers:
        _trackers[sector] = _SectorTracker()
    return _trackers[sector]


# ── Public API ───────────────────────────────────────────────────────

def update_sector_from_snapshot(symbol: str, price: float) -> None:
    """Feed a snapshot price into sector tracking.

    Should be called on every MarketSnapshot in the signal arm.
    """
    if not ENABLED or price <= 0:
        return

    global _spy_last

    profile = classify_symbol(symbol)

    # Track SPY globally (only append when price actually changes)
    if symbol == "SPY":
        if price != _spy_last:
            _spy_prices.append(price)
        _spy_last = price

    if profile.sector == "UNKNOWN":
        return

    with _lock:
        trk = _get_tracker(profile.sector)
        trk.last_update_ts = time.time()

        # Track this symbol's price for breadth
        if symbol in trk.sym_prices:
            trk.sym_prev_prices[symbol] = trk.sym_prices[symbol]
        trk.sym_prices[symbol] = price

        # If this is the sector ETF, record for relative strength
        if symbol == profile.etf:
            trk.etf_prices.append(price)


def update_sector_from_news(symbol: str, impact_score: int = 0) -> None:
    """Feed a news event into sector heat tracking.

    Should be called on every NewsEvent in the signal arm.
    """
    if not ENABLED:
        return

    profile = classify_symbol(symbol)
    if profile.sector == "UNKNOWN":
        return

    now = time.time()
    with _lock:
        trk = _get_tracker(profile.sector)
        trk.news_events.append((now, max(1, impact_score)))


def get_sector_state(sector: str) -> SectorState:
    """Compute current state for a sector."""
    if not ENABLED:
        return SectorState(sector=sector)

    # ── Force-path override ──────────────────────────────────────────
    if _FORCE_SECTOR and sector == _FORCE_SECTOR and _FORCE_SECTOR_STATE:
        return SectorState(
            sector=sector,
            state=_FORCE_SECTOR_STATE,
            relative_strength=0.5 if _FORCE_SECTOR_STATE == BULLISH else (-0.5 if _FORCE_SECTOR_STATE == BEARISH else 0.0),
            news_heat=5.0 if _FORCE_SECTOR_STATE == HOT else 0.0,
            news_count_raw=3 if _FORCE_SECTOR_STATE == HOT else 0,
            breadth_pct=75.0 if _FORCE_SECTOR_STATE in (BULLISH, HOT) else (25.0 if _FORCE_SECTOR_STATE == BEARISH else 50.0),
            etf=SECTOR_ETFS.get(sector, ""),
            n_symbols=10,
            last_update_ts=time.time(),
        )

    with _lock:
        trk = _trackers.get(sector)
        if trk is None:
            return SectorState(sector=sector)

        # ── Relative strength ────────────────────────────────────────
        rs = _compute_rs_locked(trk)

        # ── News heat (exponential decay) ────────────────────────────
        now = time.time()
        heat = 0.0
        raw_count = 0
        for ts, imp in trk.news_events:
            age = now - ts
            if age < _HEAT_DECAY_S * 4:
                decay = math.exp(-age / _HEAT_DECAY_S) if _HEAT_DECAY_S > 0 else 0.0
                heat += imp * decay
                raw_count += 1

        # ── Breadth: % of symbols advancing ──────────────────────────
        advancing = 0
        total = 0
        for sym, px in trk.sym_prices.items():
            prev = trk.sym_prev_prices.get(sym, 0.0)
            if prev > 0:
                total += 1
                if px > prev:
                    advancing += 1
        breadth = (advancing / total * 100) if total > 0 else 50.0

        # ── State classification (multi-factor) ──────────────────────
        state = _classify_state(rs, breadth, heat, len(trk.sym_prices))

        etf = SECTOR_ETFS.get(sector, "")

        return SectorState(
            sector=sector,
            state=state,
            relative_strength=round(rs, 3),
            news_heat=round(heat, 2),
            news_count_raw=raw_count,
            breadth_pct=round(breadth, 1),
            etf=etf,
            n_symbols=len(trk.sym_prices),
            last_update_ts=trk.last_update_ts,
        )


def _compute_rs_locked(trk: _SectorTracker) -> float:
    """Compute relative strength — sector vs SPY (caller holds _lock).

    Uses ETF prices when available, otherwise falls back to
    average individual stock returns as proxy.
    """
    # Method 1: ETF vs SPY
    if len(trk.etf_prices) >= 2 and len(_spy_prices) >= 2:
        window = min(_RS_WINDOW, len(trk.etf_prices), len(_spy_prices))
        etf_list = list(trk.etf_prices)
        spy_list = list(_spy_prices)
        etf_ret = (etf_list[-1] / etf_list[-window] - 1.0) * 100 if etf_list[-window] > 0 else 0.0
        spy_ret = (spy_list[-1] / spy_list[-window] - 1.0) * 100 if spy_list[-window] > 0 else 0.0
        return etf_ret - spy_ret

    # Method 2: Average stock returns vs SPY (proxy RS)
    if len(trk.sym_prices) >= 1 and len(trk.sym_prev_prices) >= 1:
        rets = []
        for sym, px in trk.sym_prices.items():
            prev = trk.sym_prev_prices.get(sym, 0.0)
            if prev > 0 and px > 0:
                rets.append((px / prev - 1.0) * 100)
        if rets:
            avg_sector_ret = sum(rets) / len(rets)
            spy_ret = 0.0
            if len(_spy_prices) >= 2:
                spy_ret = (_spy_prices[-1] / _spy_prices[-2] - 1.0) * 100 if _spy_prices[-2] > 0 else 0.0
            return avg_sector_ret - spy_ret

    return 0.0


def _classify_state(rs: float, breadth: float, heat: float, n_symbols: int) -> str:
    """Classify sector state from metrics.

    BULLISH: strong breadth (>= 60%) or positive RS (>= 0.05)
    BEARISH: weak breadth (<= 40%) or negative RS (<= -0.05)
    HOT:     high news heat (>= 2.0) without clear direction
    COLD:    minimal activity
    NEUTRAL: default
    """
    # HOT takes priority when significant news + no clear direction
    if heat >= 2.0:
        if rs > 0.05 or breadth >= 60:
            return BULLISH
        if rs < -0.05 or breadth <= 40:
            return BEARISH
        return HOT

    # COLD: minimal activity
    if n_symbols <= 1 and heat < 0.5:
        return COLD

    # Direction-based states
    if breadth >= 60 or rs >= 0.05:
        return BULLISH
    if breadth <= 40 or rs <= -0.05:
        return BEARISH

    return NEUTRAL


def get_sector_score(sector: str) -> float:
    """Return a 0–100 score for *sector* based on RS, breadth, and heat.

    Used by the composite scoring layer.
    """
    # Force-path: blanket override when no specific sector is targeted
    if _FORCE_SECTOR_SCORE > 0 and not _FORCE_SECTOR:
        return _FORCE_SECTOR_SCORE
    # Force-path: per-sector override
    if _FORCE_SECTOR_SCORE > 0 and _FORCE_SECTOR and sector == _FORCE_SECTOR:
        return _FORCE_SECTOR_SCORE
    if not ENABLED:
        return 50.0

    ss = get_sector_state(sector)
    # Normalise components into 0-100
    rs_pts = max(0, min(40, (ss.relative_strength + 0.10) / 0.20 * 40))
    breadth_pts = max(0, min(30, ss.breadth_pct / 100.0 * 30))
    heat_pts = max(0, min(30, ss.news_heat / 5.0 * 30))
    return round(rs_pts + breadth_pts + heat_pts, 1)


def get_sector_alignment(symbol: str) -> SectorAlignment:
    """Return scoring adjustments for *symbol* based on its sector.

    Point contributions (each capped to ±5):
    - pts_sector_align:   +3 LEADING / 0 NEUTRAL / -3 WEAK
    - pts_sector_rs:      relative strength ×2 (capped ±5)
    - pts_sector_heat:    heat / 3 (capped 5)
    - pts_sector_sympathy: +3 if heat >= threshold AND sector LEADING
    """
    profile = classify_symbol(symbol)
    if not ENABLED or profile.sector == "UNKNOWN":
        return SectorAlignment(
            sector=profile.sector,
            industry=profile.industry,
            sector_state=NEUTRAL,
        )

    # Force-path override for sector score
    if (_FORCE_SECTOR and profile.sector == _FORCE_SECTOR
            and _FORCE_SECTOR_STATE and _FORCE_SECTOR_SCORE > 0):
        forced = _FORCE_SECTOR_SCORE
        return SectorAlignment(
            sector=profile.sector,
            industry=profile.industry,
            sector_state=_FORCE_SECTOR_STATE,
            pts_sector_align=round(min(3.0, forced * 0.3), 1),
            pts_sector_rs=round(min(5.0, forced * 0.4), 1),
            pts_sector_heat=round(min(5.0, forced * 0.2), 1),
            pts_sector_sympathy=round(min(3.0, forced * 0.1), 1),
        )

    ss = get_sector_state(profile.sector)

    # Alignment: state-based
    align_map = {BULLISH: 3.0, HOT: 2.0, NEUTRAL: 0.0, COLD: -1.0, BEARISH: -3.0}
    pts_align = align_map.get(ss.state, 0.0)

    # Relative strength
    pts_rs = max(-5.0, min(5.0, ss.relative_strength * 2.0))

    # News heat (increased sensitivity: /2 instead of /3)
    pts_heat = max(0.0, min(5.0, ss.news_heat / 2.0))

    # Sympathy
    pts_sympathy = 0.0
    if ss.news_heat >= _SYMPATHY_MIN_HEAT and ss.state in (BULLISH, HOT):
        pts_sympathy = 3.0
    elif ss.news_heat >= 1.0 and ss.state == BULLISH:
        pts_sympathy = 1.5

    return SectorAlignment(
        sector=profile.sector,
        industry=profile.industry,
        sector_state=ss.state,
        pts_sector_align=round(pts_align, 1),
        pts_sector_rs=round(pts_rs, 1),
        pts_sector_heat=round(pts_heat, 1),
        pts_sector_sympathy=round(pts_sympathy, 1),
    )


def check_state_changes() -> None:
    """Check for sector state changes and log them.

    Also emits a heartbeat summary every 60s.
    Called periodically from the signal arm.
    """
    global _last_heartbeat_ts

    if not ENABLED:
        return

    now = time.time()

    with _lock:
        sectors = list(_trackers.keys())

    for sector in sectors:
        ss = get_sector_state(sector)
        old_state = _last_sector_states.get(sector, NEUTRAL)
        if ss.state != old_state:
            log.info(
                "sector_state_change sector=%s old=%s new=%s rs=%.3f heat=%.1f breadth=%.1f%%",
                sector, old_state, ss.state,
                ss.relative_strength, ss.news_heat, ss.breadth_pct,
            )
            _last_sector_states[sector] = ss.state

    if now - _last_heartbeat_ts >= _HEARTBEAT_INTERVAL_S:
        _last_heartbeat_ts = now
        _emit_heartbeat(sectors)


def _emit_heartbeat(sectors: list) -> None:
    """Log compact sector summary."""
    if not sectors:
        return

    top_parts = []
    weak_parts = []
    for sector in sorted(sectors):
        ss = get_sector_state(sector)
        detail = (
            f"{sector}:{ss.state}"
            f"(rs={ss.relative_strength:+.2f},heat={ss.news_heat:.1f},breadth={ss.breadth_pct:.0f}%)"
        )
        if ss.state in (BULLISH, HOT):
            top_parts.append(detail)
        elif ss.state in (BEARISH, COLD):
            weak_parts.append(detail)

    log.info(
        "sector_monitor summary top=[%s] weak=[%s]",
        ", ".join(top_parts) if top_parts else "none",
        ", ".join(weak_parts) if weak_parts else "none",
    )


def get_sector_summary() -> Dict[str, dict]:
    """Return a compact dict of all tracked sectors for monitor display."""
    if not ENABLED:
        return {}

    result: Dict[str, dict] = {}
    with _lock:
        sectors = list(_trackers.keys())

    for sector in sectors:
        ss = get_sector_state(sector)
        result[sector] = {
            "state": ss.state,
            "rs": ss.relative_strength,
            "heat": ss.news_heat,
            "breadth": ss.breadth_pct,
            "etf": ss.etf,
            "n": ss.n_symbols,
        }
    return result
