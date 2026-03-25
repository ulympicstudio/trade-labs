"""Allocation / Orchestration Engine — balances playbooks across engines.

Combines regime, session, news momentum, sector rotation, volatility
leadership, and mean-reversion signals into a unified allocation
decision that tells the signal & risk arms how to prioritise symbols
and size positions.

Lifecycle
---------
1. ``compute_allocation_decision()`` — once per eval cycle.
2. ``score_symbol_confluence(...)``   — per symbol, returns priority + matched.
3. ``get_allocation_summary()``       — for monitor/heartbeat display.

All state is module-level (single process) — no external deps.

Env toggles
-----------
TL_ALLOC_ENABLED              master on/off (default true)
TL_ALLOC_NEWS_WEIGHT          override news weight
TL_ALLOC_ROTATION_WEIGHT      override rotation weight
TL_ALLOC_VOL_WEIGHT           override volatility weight
TL_ALLOC_MEANREV_WEIGHT       override mean-reversion weight
TL_ALLOC_MAX_POSITIONS         max total open positions
TL_ALLOC_FORCE_MODE            force posture: TREND / MIXED / DEFENSIVE

Force-path overrides (testing)
------------------------------
TL_ALLOC_FORCE_NEWS           force news weight (float)
TL_ALLOC_FORCE_ROTATION       force rotation weight (float)
TL_ALLOC_FORCE_VOL            force vol weight (float)
TL_ALLOC_FORCE_MEANREV        force mean-rev weight (float)
TL_ALLOC_FORCE_MAX_POSITIONS  force max positions (int)
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple, TYPE_CHECKING

from src.monitoring.logger import get_logger

if TYPE_CHECKING:
    from src.signals.market_mode import MarketModeDecision

log = get_logger("allocation")

# ── Tunables ────────────────────────────────────────────────────────
ALLOC_ENABLED = os.environ.get(
    "TL_ALLOC_ENABLED", "true"
).lower() in ("1", "true", "yes")

_NEWS_WEIGHT = float(os.environ.get("TL_ALLOC_NEWS_WEIGHT", "0"))
_ROTATION_WEIGHT = float(os.environ.get("TL_ALLOC_ROTATION_WEIGHT", "0"))
_VOL_WEIGHT = float(os.environ.get("TL_ALLOC_VOL_WEIGHT", "0"))
_MEANREV_WEIGHT = float(os.environ.get("TL_ALLOC_MEANREV_WEIGHT", "0"))
_MAX_POSITIONS = int(os.environ.get("TL_ALLOC_MAX_POSITIONS", "0"))
_FORCE_MODE = os.environ.get("TL_ALLOC_FORCE_MODE", "").upper().strip()

# Force-path overrides
_F_NEWS = float(os.environ.get("TL_ALLOC_FORCE_NEWS", "0"))
_F_ROTATION = float(os.environ.get("TL_ALLOC_FORCE_ROTATION", "0"))
_F_VOL = float(os.environ.get("TL_ALLOC_FORCE_VOL", "0"))
_F_MEANREV = float(os.environ.get("TL_ALLOC_FORCE_MEANREV", "0"))
_F_MAX_POS = int(os.environ.get("TL_ALLOC_FORCE_MAX_POSITIONS", "0"))

if _FORCE_MODE or _F_NEWS or _F_ROTATION or _F_VOL or _F_MEANREV or _F_MAX_POS:
    log.warning(
        "FORCE-ALLOCATION ACTIVE mode=%s news=%.2f rot=%.2f vol=%.2f "
        "mr=%.2f maxpos=%d",
        _FORCE_MODE or "(natural)", _F_NEWS, _F_ROTATION,
        _F_VOL, _F_MEANREV, _F_MAX_POS,
    )


# ── Dataclasses ─────────────────────────────────────────────────────

@dataclass(frozen=True)
class AllocationDecision:
    """Immutable snapshot of the current allocation posture."""
    regime: str = "CHOP"
    session_state: str = "RTH"
    market_bias: str = "NEUTRAL"       # BULLISH / NEUTRAL / BEARISH
    weight_news: float = 0.30
    weight_rotation: float = 0.25
    weight_volatility: float = 0.25
    weight_meanrevert: float = 0.20
    max_total_positions: int = 8
    max_news_positions: int = 3
    max_rotation_positions: int = 2
    max_vol_positions: int = 2
    max_meanrevert_positions: int = 2
    confluence_bonus: float = 0.0
    risk_posture: str = "NORMAL"       # AGGRESSIVE / NORMAL / DEFENSIVE / MINIMAL
    reasons: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class SymbolConfluence:
    """Per-symbol confluence scoring result."""
    symbol: str = ""
    priority_score: float = 0.0        # 0-100 normalised
    confluence_score: float = 0.0      # 0-1 how many engines agree
    matched_engines: List[str] = field(default_factory=list)
    bucket: str = "none"               # news / rotation / volatility / meanrevert
    reasons: List[str] = field(default_factory=list)


# ── Bucket fill tracking ────────────────────────────────────────────

_bucket_fills: Dict[str, int] = {
    "news": 0,
    "rotation": 0,
    "volatility": 0,
    "meanrevert": 0,
}
_total_fills: int = 0
_last_decision: Optional[AllocationDecision] = None
_last_decision_ts: float = 0.0


def reset_bucket_fills() -> None:
    """Reset bucket counts (call at start of each cycle or periodically)."""
    global _total_fills
    for k in _bucket_fills:
        _bucket_fills[k] = 0
    _total_fills = 0


def record_bucket_fill(bucket: str) -> None:
    """Record that a position was taken in this bucket."""
    global _total_fills
    if bucket in _bucket_fills:
        _bucket_fills[bucket] += 1
        _total_fills += 1


def get_bucket_fills() -> Dict[str, int]:
    """Return current bucket fill snapshot."""
    return dict(_bucket_fills)


def get_total_fills() -> int:
    return _total_fills


# ── Regime → weight profiles ────────────────────────────────────────

_PROFILES = {
    # regime → (news, rotation, vol, meanrev, bias, posture, max_pos)
    "TREND_UP": (0.30, 0.30, 0.25, 0.15, "BULLISH", "AGGRESSIVE", 10),
    "TREND_DOWN": (0.35, 0.20, 0.15, 0.30, "BEARISH", "DEFENSIVE", 6),
    "CHOP": (0.20, 0.15, 0.15, 0.50, "NEUTRAL", "NORMAL", 6),
    "PANIC": (0.40, 0.10, 0.10, 0.40, "BEARISH", "MINIMAL", 3),
}

_SESSION_PROFILES = {
    # session → (posture_override, max_pos_mult)
    "PREMARKET": ("DEFENSIVE", 0.5),
    "RTH": (None, 1.0),
    "AFTERHOURS": ("DEFENSIVE", 0.5),
    "OFF_HOURS": ("MINIMAL", 0.25),
}


# ── Core API ────────────────────────────────────────────────────────

def compute_allocation_decision(
    regime: str = "CHOP",
    session: str = "RTH",
    market_mode: "Optional[MarketModeDecision]" = None,
) -> AllocationDecision:
    """Compute the allocation posture for the current market state.

    Called once per evaluate cycle in signal_main.  O(1).
    When *market_mode* is supplied it acts as the top-level bias,
    overriding regime-derived weights, posture, and position cap.
    """
    global _last_decision, _last_decision_ts

    if not ALLOC_ENABLED:
        return AllocationDecision(regime=regime, session_state=session)

    reasons: List[str] = []

    # ── Base weights from regime ──────────────────────────────────
    profile = _PROFILES.get(regime, _PROFILES["CHOP"])
    w_news, w_rot, w_vol, w_mr, bias, posture, max_pos = profile
    reasons.append(f"regime={regime}")

    # ── Market Mode top-level bias ────────────────────────────────
    _old_weights = (w_news, w_rot, w_vol, w_mr)
    if market_mode is not None and market_mode.mode != "":
        w_news = market_mode.recommended_news_weight
        w_rot = market_mode.recommended_rotation_weight
        w_vol = market_mode.recommended_vol_weight
        w_mr = market_mode.recommended_meanrev_weight
        posture = market_mode.risk_posture
        max_pos = max(1, int(max_pos * market_mode.position_cap_mult))
        reasons.append(f"mm={market_mode.mode}")
        log.info(
            "allocation_mode_adjustment mode=%s old_weights=[n=%.2f r=%.2f v=%.2f m=%.2f] "
            "new_weights=[n=%.2f r=%.2f v=%.2f m=%.2f] posture=%s cap_mult=%.2f",
            market_mode.mode,
            _old_weights[0], _old_weights[1], _old_weights[2], _old_weights[3],
            w_news, w_rot, w_vol, w_mr,
            posture, market_mode.position_cap_mult,
        )

    # ── Force mode override ───────────────────────────────────────
    if _FORCE_MODE == "TREND":
        w_news, w_rot, w_vol, w_mr = 0.30, 0.30, 0.25, 0.15
        bias, posture, max_pos = "BULLISH", "AGGRESSIVE", 10
        reasons.append("force_mode=TREND")
    elif _FORCE_MODE == "MIXED":
        w_news, w_rot, w_vol, w_mr = 0.25, 0.25, 0.25, 0.25
        bias, posture, max_pos = "NEUTRAL", "NORMAL", 8
        reasons.append("force_mode=MIXED")
    elif _FORCE_MODE == "DEFENSIVE":
        w_news, w_rot, w_vol, w_mr = 0.25, 0.15, 0.10, 0.50
        bias, posture, max_pos = "BEARISH", "DEFENSIVE", 4
        reasons.append("force_mode=DEFENSIVE")

    # ── Env weight overrides (non-zero = active) ──────────────────
    if _NEWS_WEIGHT > 0:
        w_news = _NEWS_WEIGHT
    if _ROTATION_WEIGHT > 0:
        w_rot = _ROTATION_WEIGHT
    if _VOL_WEIGHT > 0:
        w_vol = _VOL_WEIGHT
    if _MEANREV_WEIGHT > 0:
        w_mr = _MEANREV_WEIGHT
    if _MAX_POSITIONS > 0:
        max_pos = _MAX_POSITIONS

    # ── Force-path test overrides ─────────────────────────────────
    if _F_NEWS > 0:
        w_news = _F_NEWS
        reasons.append(f"force_news={_F_NEWS}")
    if _F_ROTATION > 0:
        w_rot = _F_ROTATION
        reasons.append(f"force_rot={_F_ROTATION}")
    if _F_VOL > 0:
        w_vol = _F_VOL
        reasons.append(f"force_vol={_F_VOL}")
    if _F_MEANREV > 0:
        w_mr = _F_MEANREV
        reasons.append(f"force_mr={_F_MEANREV}")
    if _F_MAX_POS > 0:
        max_pos = _F_MAX_POS
        reasons.append(f"force_maxpos={_F_MAX_POS}")

    # ── Normalise weights ─────────────────────────────────────────
    total_w = w_news + w_rot + w_vol + w_mr
    if total_w > 0:
        w_news /= total_w
        w_rot /= total_w
        w_vol /= total_w
        w_mr /= total_w

    # ── Scorecard weight adjustment ───────────────────────────────
    from src.analysis.playbook_scorecard import get_weight_adjustment, SCORECARD_ENABLED
    if SCORECARD_ENABLED:
        _sc_pre = (w_news, w_rot, w_vol, w_mr)
        _sc_n = get_weight_adjustment("news")
        _sc_r = get_weight_adjustment("rotation")
        _sc_v = get_weight_adjustment("volatility")
        _sc_m = get_weight_adjustment("meanrevert")
        w_news *= _sc_n
        w_rot *= _sc_r
        w_vol *= _sc_v
        w_mr *= _sc_m
        # Re-normalise after scorecard adjustments
        _sc_total = w_news + w_rot + w_vol + w_mr
        if _sc_total > 0:
            w_news /= _sc_total
            w_rot /= _sc_total
            w_vol /= _sc_total
            w_mr /= _sc_total
        if any(m != 1.0 for m in (_sc_n, _sc_r, _sc_v, _sc_m)):
            reasons.append("scorecard_adj")
            log.info(
                "scorecard_weight_adjustment pre=[n=%.3f r=%.3f v=%.3f m=%.3f] "
                "mults=[n=%.3f r=%.3f v=%.3f m=%.3f] "
                "post=[n=%.3f r=%.3f v=%.3f m=%.3f]",
                _sc_pre[0], _sc_pre[1], _sc_pre[2], _sc_pre[3],
                _sc_n, _sc_r, _sc_v, _sc_m,
                w_news, w_rot, w_vol, w_mr,
            )

    # ── Sector Rotation Leadership adjustment (Phase B) ─────────
    from src.signals.sector_rotation_selector import (
        get_last_rotation_decision as _get_rot_decision,
        ROTATION_SEL_ENABLED as _ROT_SEL_ENABLED,
    )
    if _ROT_SEL_ENABLED:
        _rot_d = _get_rot_decision()
        if _rot_d is not None and _rot_d.top_sectors:
            _rot_top_avg = sum(sc for _, sc in _rot_d.top_sectors) / len(_rot_d.top_sectors) if _rot_d.top_sectors else 50.0
            # When top sectors are strong (>65), nudge rotation weight up
            if _rot_top_avg > 65:
                _rot_boost = min(0.10, (_rot_top_avg - 65) / 350.0)
                _pre_rot = w_rot
                w_rot = min(0.50, w_rot + _rot_boost)
                # Re-normalise
                _rot_total = w_news + w_rot + w_vol + w_mr
                if _rot_total > 0:
                    w_news /= _rot_total
                    w_rot /= _rot_total
                    w_vol /= _rot_total
                    w_mr /= _rot_total
                reasons.append(f"rotation_leadership_boost={_rot_boost:.3f}")
                log.info(
                    "allocation_rotation_boost rot_avg=%.1f boost=%.3f "
                    "weights=[n=%.3f r=%.3f v=%.3f m=%.3f]",
                    _rot_top_avg, _rot_boost,
                    w_news, w_rot, w_vol, w_mr,
                )
            # When strong sectors are rotating out, nudge rotation weight down
            elif _rot_d.rotating_out and _rot_top_avg < 40:
                _rot_penalty = min(0.05, (40 - _rot_top_avg) / 400.0)
                w_rot = max(0.05, w_rot - _rot_penalty)
                _rot_total = w_news + w_rot + w_vol + w_mr
                if _rot_total > 0:
                    w_news /= _rot_total
                    w_rot /= _rot_total
                    w_vol /= _rot_total
                    w_mr /= _rot_total
                reasons.append(f"rotation_penalty={_rot_penalty:.3f}")
                log.info(
                    "allocation_rotation_boost rot_avg=%.1f penalty=%.3f "
                    "weights=[n=%.3f r=%.3f v=%.3f m=%.3f]",
                    _rot_top_avg, _rot_penalty,
                    w_news, w_rot, w_vol, w_mr,
                )

    # ── Session adjustment ────────────────────────────────────────
    sess_profile = _SESSION_PROFILES.get(session, (None, 1.0))
    sess_posture, sess_mult = sess_profile
    if sess_posture is not None:
        posture = sess_posture
        reasons.append(f"session={session}")
    max_pos = max(1, int(max_pos * sess_mult))

    # ── Bucket caps (proportional to weights) ─────────────────────
    max_news = max(1, int(max_pos * w_news * 1.5))
    max_rot = max(1, int(max_pos * w_rot * 1.5))
    max_vol_pos = max(1, int(max_pos * w_vol * 1.5))
    max_mr = max(1, int(max_pos * w_mr * 1.5))

    decision = AllocationDecision(
        regime=regime,
        session_state=session,
        market_bias=bias,
        weight_news=round(w_news, 3),
        weight_rotation=round(w_rot, 3),
        weight_volatility=round(w_vol, 3),
        weight_meanrevert=round(w_mr, 3),
        max_total_positions=max_pos,
        max_news_positions=max_news,
        max_rotation_positions=max_rot,
        max_vol_positions=max_vol_pos,
        max_meanrevert_positions=max_mr,
        confluence_bonus=0.0,
        risk_posture=posture,
        reasons=reasons,
    )

    _last_decision = decision
    _last_decision_ts = time.time()
    return decision


def score_symbol_confluence(
    symbol: str,
    event_score: int = 0,
    sector_state: str = "NEUTRAL",
    sector_score: float = 0.0,
    rotation_state: str = "NEUTRAL",
    rotation_score: int = 0,
    vol_state: str = "QUIET",
    vol_score: int = 0,
    regime: str = "CHOP",
    spread_pct: float = 0.0,
    session: str = "RTH",
    decision: Optional[AllocationDecision] = None,
) -> SymbolConfluence:
    """Score a symbol's priority based on multi-engine confluence.

    Called for every symbol in _evaluate().  O(1).
    Returns SymbolConfluence with priority_score, confluence, matched engines.
    """
    if not ALLOC_ENABLED:
        return SymbolConfluence(symbol=symbol)

    if decision is None:
        decision = _last_decision or AllocationDecision()

    matched: List[str] = []
    reasons: List[str] = []
    score = 0.0

    # ── News / Event score contribution ───────────────────────────
    # event_score is 0-100; contribute up to 25 pts weighted by news weight
    news_pts = min(25.0, (event_score / 100.0) * 25.0) * (decision.weight_news / 0.30) if event_score > 30 else 0.0
    if news_pts > 5:
        matched.append("news")
        reasons.append(f"es={event_score}")
    score += news_pts

    # ── Sector contribution ───────────────────────────────────────
    sector_pts = 0.0
    if sector_state in ("BULLISH", "HOT"):
        # Scale 8–15 pts by sector_score strength (0.5–1.0 → 1.0–1.5x)
        _sec_mult = 1.0 + max(0.0, min(0.5, sector_score - 0.5))
        sector_pts = round(10.0 * _sec_mult, 2)
        reasons.append(f"sector={sector_state}(sc={sector_score:.2f},pts={sector_pts:.1f})")
    elif sector_state == "BEARISH":
        sector_pts = -8.0 * (1.0 + max(0.0, min(0.5, sector_score - 0.5)))
        reasons.append(f"sector_penalty={sector_state}(sc={sector_score:.2f})")
    elif sector_state == "NEUTRAL":
        sector_pts = 3.0
    score += sector_pts

    # ── Rotation contribution ─────────────────────────────────────
    rot_pts = 0.0
    if rotation_state == "LEADING":
        rot_pts = 20.0 * (decision.weight_rotation / 0.25)
        matched.append("rotation")
        reasons.append(f"rot={rotation_state}")
    elif rotation_state == "ROTATING_IN":
        rot_pts = 12.0 * (decision.weight_rotation / 0.25)
        matched.append("rotation")
        reasons.append(f"rot={rotation_state}")
    elif rotation_state == "OVERBOUGHT":
        rot_pts = 5.0
    elif rotation_state in ("ROTATING_OUT", "COLD"):
        rot_pts = -5.0
        reasons.append(f"rot_penalty={rotation_state}")
    score += rot_pts

    # ── Volatility contribution ───────────────────────────────────
    vol_pts = 0.0
    if vol_state == "TRIGGERED":
        vol_pts = 20.0 * (decision.weight_volatility / 0.25)
        matched.append("volatility")
        reasons.append(f"vol={vol_state}")
    elif vol_state == "BUILDING":
        vol_pts = 10.0 * (decision.weight_volatility / 0.25)
        matched.append("volatility")
        reasons.append(f"vol={vol_state}")
    elif vol_state == "WATCH":
        vol_pts = 3.0
    score += vol_pts

    # ── Mean-reversion suitability ────────────────────────────────
    mr_pts = 0.0
    if regime == "CHOP":
        mr_pts = 15.0 * (decision.weight_meanrevert / 0.20)
        matched.append("meanrevert")
        reasons.append("regime=CHOP→mr")
    elif regime == "TREND_DOWN":
        mr_pts = 8.0 * (decision.weight_meanrevert / 0.20)
    score += mr_pts

    # ── Regime fit bonus ──────────────────────────────────────────
    if regime == "TREND_UP" and vol_state in ("TRIGGERED", "BUILDING"):
        score += 5.0
        reasons.append("regime_vol_fit")
    if regime == "TREND_UP" and rotation_state in ("LEADING", "ROTATING_IN"):
        score += 5.0
        reasons.append("regime_rot_fit")

    # ── Spread penalty ────────────────────────────────────────────
    if spread_pct > 0.003:
        penalty = min(10.0, (spread_pct - 0.003) * 2000.0)
        score -= penalty
        reasons.append(f"spread_pen={penalty:.1f}")

    # ── Session suitability ───────────────────────────────────────
    if session in ("PREMARKET", "AFTERHOURS"):
        score *= 0.7
        reasons.append("session_discount")
    elif session == "OFF_HOURS":
        score *= 0.3
        reasons.append("off_hours_discount")

    # ── Confluence bonus ──────────────────────────────────────────
    # Sensitivity is scaled by market_mode conf_bonus_mult when available
    try:
        from src.signals.market_mode import get_mode_conf_bonus_mult as _mm_cbm
        _cbm = _mm_cbm()
    except Exception:
        _cbm = 1.0
    n_matched = len(matched)
    confluence = 0.0
    if n_matched >= 3:
        confluence = round(0.30 * _cbm, 2)
        score += 15.0 * _cbm
        reasons.append(f"confluence_3+={n_matched}")
    elif n_matched == 2:
        confluence = round(0.20 * _cbm, 2)
        score += 8.0 * _cbm
        reasons.append(f"confluence_2={n_matched}")
    elif n_matched == 1:
        confluence = 0.10

    # ── Determine primary bucket ──────────────────────────────────
    bucket = "none"
    if "volatility" in matched and vol_pts >= rot_pts and vol_pts >= news_pts:
        bucket = "volatility"
    elif "rotation" in matched and rot_pts >= news_pts:
        bucket = "rotation"
    elif "news" in matched:
        bucket = "news"
    elif "meanrevert" in matched:
        bucket = "meanrevert"
    elif news_pts > 0:
        bucket = "news"

    # Clamp score
    priority = max(0.0, min(100.0, score))

    return SymbolConfluence(
        symbol=symbol,
        priority_score=round(priority, 1),
        confluence_score=round(confluence, 2),
        matched_engines=matched,
        bucket=bucket,
        reasons=reasons,
    )


def check_bucket_capacity(bucket: str, decision: Optional[AllocationDecision] = None) -> str:
    """Check if a bucket still has capacity.

    Returns: "PASS" / "REDUCE" / "BLOCK"
    """
    if not ALLOC_ENABLED:
        return "PASS"
    if decision is None:
        decision = _last_decision or AllocationDecision()

    # Total cap
    if _total_fills >= decision.max_total_positions:
        return "BLOCK"

    cap_map = {
        "news": decision.max_news_positions,
        "rotation": decision.max_rotation_positions,
        "volatility": decision.max_vol_positions,
        "meanrevert": decision.max_meanrevert_positions,
    }
    cap = cap_map.get(bucket, decision.max_total_positions)
    current = _bucket_fills.get(bucket, 0)

    if current >= cap:
        return "BLOCK"
    if current >= cap - 1:
        return "REDUCE"
    return "PASS"


def get_allocation_summary() -> str:
    """One-liner summary for heartbeat/monitor display."""
    d = _last_decision
    if d is None:
        return "alloc=(no_decision)"
    fills = _bucket_fills
    return (
        f"alloc=({d.risk_posture} n={fills.get('news', 0)}"
        f" r={fills.get('rotation', 0)}"
        f" v={fills.get('volatility', 0)}"
        f" m={fills.get('meanrevert', 0)}"
        f" tot={_total_fills}/{d.max_total_positions})"
    )


def get_last_decision() -> Optional[AllocationDecision]:
    """Return the most recent allocation decision."""
    return _last_decision


def get_confluence_qty_mult(confluence_score: float, bucket_verdict: str) -> float:
    """Return qty multiplier based on confluence + bucket capacity.

    High confluence → normal sizing.
    Low confluence + saturated bucket → reduced sizing.
    """
    mult = 1.0
    if confluence_score >= 0.20:
        mult = 1.0  # strong multi-engine agreement
    elif confluence_score >= 0.10:
        mult = 0.90
    else:
        mult = 0.80  # single-engine or no match

    if bucket_verdict == "REDUCE":
        mult *= 0.75
    elif bucket_verdict == "BLOCK":
        mult *= 0.0  # should not reach here; caller should reject

    return round(mult, 3)
