"""Market Mode / Session Commander — top-level tape classifier.

Sits above the allocation engine and classifies the current tape into
one of five modes, producing weight recommendations, position-cap
multipliers, and risk posture that the allocation engine consumes as
its top-level bias.

Supported modes
---------------
TREND_EXPANSION    – strong trend + healthy breadth + vol support
ROTATION_TAPE      – strong industry rotation leadership, uneven broad tape
VOLATILITY_SHOCK   – triggered vol across many names, stressed tape
CHOP_RANGE         – CHOP regime, weak breadth, low conviction
DEFENSIVE_RISK_OFF – PANIC or broad weakness

Decision inputs
---------------
- Current regime (from regime.py)
- Sector leaders / breadth (from sector_intel.py)
- Industry rotation leaders (from industry_rotation.py)
- Volatility leader intensity (from volatility_leaders.py)
- Event / news intensity (from event scoring)
- Session state (from session.py)

Env toggles
-----------
TL_MODE_ENABLED             master on/off (default true)
TL_MODE_FORCE               force specific mode
TL_MODE_FORCE_CONFIDENCE    force confidence (float)
TL_MODE_FORCE_NEWS          force news weight recommendation
TL_MODE_FORCE_ROTATION      force rotation weight recommendation
TL_MODE_FORCE_VOL           force vol weight recommendation
TL_MODE_FORCE_MEANREV       force mean-rev weight recommendation
TL_MODE_FORCE_CAP_MULT      force position cap multiplier
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from src.monitoring.logger import get_logger

log = get_logger("market_mode")

# ── Tunables ────────────────────────────────────────────────────────

MODE_ENABLED = os.environ.get(
    "TL_MODE_ENABLED", "true"
).lower() in ("1", "true", "yes")

_FORCE_MODE = os.environ.get("TL_MODE_FORCE", "").upper().strip()
_FORCE_CONFIDENCE = float(os.environ.get("TL_MODE_FORCE_CONFIDENCE", "0"))
_FORCE_NEWS = float(os.environ.get("TL_MODE_FORCE_NEWS", "0"))
_FORCE_ROTATION = float(os.environ.get("TL_MODE_FORCE_ROTATION", "0"))
_FORCE_VOL = float(os.environ.get("TL_MODE_FORCE_VOL", "0"))
_FORCE_MEANREV = float(os.environ.get("TL_MODE_FORCE_MEANREV", "0"))
_FORCE_CAP_MULT = float(os.environ.get("TL_MODE_FORCE_CAP_MULT", "0"))

if _FORCE_MODE or _FORCE_NEWS or _FORCE_ROTATION or _FORCE_VOL or _FORCE_MEANREV or _FORCE_CAP_MULT:
    log.warning(
        "FORCE-MARKET-MODE ACTIVE mode=%s conf=%.2f news=%.2f rot=%.2f "
        "vol=%.2f mr=%.2f cap_mult=%.2f",
        _FORCE_MODE or "(natural)", _FORCE_CONFIDENCE,
        _FORCE_NEWS, _FORCE_ROTATION, _FORCE_VOL,
        _FORCE_MEANREV, _FORCE_CAP_MULT,
    )

# ── Mode constants ──────────────────────────────────────────────────

TREND_EXPANSION = "TREND_EXPANSION"
ROTATION_TAPE = "ROTATION_TAPE"
VOLATILITY_SHOCK = "VOLATILITY_SHOCK"
CHOP_RANGE = "CHOP_RANGE"
DEFENSIVE_RISK_OFF = "DEFENSIVE_RISK_OFF"

_VALID_MODES = {
    TREND_EXPANSION, ROTATION_TAPE, VOLATILITY_SHOCK,
    CHOP_RANGE, DEFENSIVE_RISK_OFF,
}


# ── Dataclass ───────────────────────────────────────────────────────

@dataclass(frozen=True)
class MarketModeDecision:
    """Immutable snapshot of the current market mode classification."""

    mode: str = CHOP_RANGE
    confidence: float = 0.5
    session_state: str = "RTH"
    regime: str = "CHOP"
    breadth_state: str = "NEUTRAL"       # HEALTHY / NEUTRAL / WEAK
    volatility_state: str = "NORMAL"     # CALM / NORMAL / ELEVATED / SHOCK
    rotation_state: str = "NEUTRAL"      # STRONG / MODERATE / NEUTRAL / WEAK
    news_state: str = "QUIET"            # HOT / WARM / QUIET
    recommended_news_weight: float = 0.25
    recommended_rotation_weight: float = 0.25
    recommended_vol_weight: float = 0.25
    recommended_meanrev_weight: float = 0.25
    position_cap_mult: float = 1.0
    risk_posture: str = "NORMAL"         # AGGRESSIVE / NORMAL / DEFENSIVE / MINIMAL
    reasons: List[str] = field(default_factory=list)


# ── Mode profiles ───────────────────────────────────────────────────
# mode → (news, rotation, vol, meanrev, posture, cap_mult, conf_bonus_mult)
_MODE_PROFILES: Dict[str, tuple] = {
    TREND_EXPANSION:    (0.30, 0.30, 0.25, 0.15, "AGGRESSIVE", 1.20, 1.10),
    ROTATION_TAPE:      (0.20, 0.40, 0.20, 0.20, "NORMAL",     1.00, 1.05),
    VOLATILITY_SHOCK:   (0.30, 0.10, 0.45, 0.15, "DEFENSIVE",  0.75, 1.00),
    CHOP_RANGE:         (0.20, 0.15, 0.15, 0.50, "NORMAL",     0.85, 0.95),
    DEFENSIVE_RISK_OFF: (0.30, 0.10, 0.10, 0.50, "MINIMAL",    0.50, 0.80),
}

# ── Module state ────────────────────────────────────────────────────

_last_decision: Optional[MarketModeDecision] = None


# ── Core API ────────────────────────────────────────────────────────

def compute_market_mode(
    regime: str = "CHOP",
    session: str = "RTH",
    sector_states: Optional[Dict[str, str]] = None,
    sector_breadths: Optional[Dict[str, float]] = None,
    rotation_leaders: int = 0,
    rotation_top_score: int = 0,
    vol_triggered_count: int = 0,
    vol_top_score: int = 0,
    avg_event_score: float = 0.0,
    news_hot_count: int = 0,
) -> MarketModeDecision:
    """Classify the current tape and recommend allocation parameters.

    Called once per eval cycle in signal_main.  O(1).

    Parameters
    ----------
    regime : str
        Current market regime (TREND_UP, TREND_DOWN, CHOP, PANIC).
    session : str
        Current session (RTH, PREMARKET, AFTERHOURS, OFF_HOURS).
    sector_states : dict
        Sector → state mapping (BULLISH, NEUTRAL, BEARISH, HOT, COLD).
    sector_breadths : dict
        Sector → breadth_pct mapping.
    rotation_leaders : int
        Count of industries in LEADING or ROTATING_IN state.
    rotation_top_score : int
        Highest industry rotation score.
    vol_triggered_count : int
        Count of symbols in TRIGGERED state.
    vol_top_score : int
        Highest volatility leader score.
    avg_event_score : float
        Average event score across tracked symbols.
    news_hot_count : int
        Count of symbols with event_score > 60.
    """
    global _last_decision

    if not MODE_ENABLED:
        _last_decision = MarketModeDecision(regime=regime, session_state=session)
        return _last_decision

    reasons: List[str] = []

    # ── Compute sub-states ────────────────────────────────────────
    breadth_state = _classify_breadth(sector_states, sector_breadths)
    volatility_state = _classify_volatility(vol_triggered_count, vol_top_score, regime)
    rotation_st = _classify_rotation(rotation_leaders, rotation_top_score)
    news_st = _classify_news(avg_event_score, news_hot_count)

    reasons.append(f"regime={regime}")
    reasons.append(f"breadth={breadth_state}")
    reasons.append(f"vol={volatility_state}")
    reasons.append(f"rot={rotation_st}")
    reasons.append(f"news={news_st}")

    # ── Mode classification ───────────────────────────────────────
    mode, confidence = _classify_mode(
        regime, breadth_state, volatility_state, rotation_st, news_st,
    )
    reasons.append(f"mode={mode}")

    # ── Force-path override ───────────────────────────────────────
    if _FORCE_MODE and _FORCE_MODE in _VALID_MODES:
        mode = _FORCE_MODE
        reasons.append(f"force_mode={_FORCE_MODE}")
    if _FORCE_CONFIDENCE > 0:
        confidence = _FORCE_CONFIDENCE
        reasons.append(f"force_conf={_FORCE_CONFIDENCE}")

    # ── Derive weights from mode profile ──────────────────────────
    profile = _MODE_PROFILES.get(mode, _MODE_PROFILES[CHOP_RANGE])
    w_news, w_rot, w_vol, w_mr, posture, cap_mult, conf_bonus_mult = profile

    # ── Force-path weight/cap overrides ───────────────────────────
    if _FORCE_NEWS > 0:
        w_news = _FORCE_NEWS
        reasons.append(f"force_news={_FORCE_NEWS}")
    if _FORCE_ROTATION > 0:
        w_rot = _FORCE_ROTATION
        reasons.append(f"force_rot={_FORCE_ROTATION}")
    if _FORCE_VOL > 0:
        w_vol = _FORCE_VOL
        reasons.append(f"force_vol={_FORCE_VOL}")
    if _FORCE_MEANREV > 0:
        w_mr = _FORCE_MEANREV
        reasons.append(f"force_mr={_FORCE_MEANREV}")
    if _FORCE_CAP_MULT > 0:
        cap_mult = _FORCE_CAP_MULT
        reasons.append(f"force_cap={_FORCE_CAP_MULT}")

    # ── Normalise weights ─────────────────────────────────────────
    total_w = w_news + w_rot + w_vol + w_mr
    if total_w > 0:
        w_news /= total_w
        w_rot /= total_w
        w_vol /= total_w
        w_mr /= total_w

    # ── Session adjustment ────────────────────────────────────────
    if session in ("PREMARKET", "AFTERHOURS"):
        if posture not in ("DEFENSIVE", "MINIMAL"):
            posture = "DEFENSIVE"
        cap_mult *= 0.6
        reasons.append(f"session_adj={session}")
    elif session == "OFF_HOURS":
        posture = "MINIMAL"
        cap_mult *= 0.3
        reasons.append("session_adj=OFF_HOURS")

    decision = MarketModeDecision(
        mode=mode,
        confidence=round(confidence, 2),
        session_state=session,
        regime=regime,
        breadth_state=breadth_state,
        volatility_state=volatility_state,
        rotation_state=rotation_st,
        news_state=news_st,
        recommended_news_weight=round(w_news, 3),
        recommended_rotation_weight=round(w_rot, 3),
        recommended_vol_weight=round(w_vol, 3),
        recommended_meanrev_weight=round(w_mr, 3),
        position_cap_mult=round(cap_mult, 2),
        risk_posture=posture,
        reasons=reasons,
    )

    _last_decision = decision
    return decision


def get_last_mode() -> Optional[MarketModeDecision]:
    """Return the most recent market mode decision."""
    return _last_decision


def get_market_mode_summary() -> str:
    """One-liner summary for heartbeat/monitor display."""
    d = _last_decision
    if d is None:
        return "mode=(no_decision)"
    return (
        f"mode=({d.mode} conf={d.confidence:.2f} posture={d.risk_posture} "
        f"cap_mult={d.position_cap_mult:.2f} "
        f"w=[n={d.recommended_news_weight:.2f} "
        f"r={d.recommended_rotation_weight:.2f} "
        f"v={d.recommended_vol_weight:.2f} "
        f"m={d.recommended_meanrev_weight:.2f}])"
    )


def get_mode_conf_bonus_mult() -> float:
    """Return the confluence bonus sensitivity multiplier for the current mode."""
    d = _last_decision
    if d is None:
        return 1.0
    profile = _MODE_PROFILES.get(d.mode, _MODE_PROFILES[CHOP_RANGE])
    return profile[6]  # conf_bonus_mult


# ── Classification helpers ──────────────────────────────────────────

def _classify_breadth(
    sector_states: Optional[Dict[str, str]],
    sector_breadths: Optional[Dict[str, float]],
) -> str:
    """Classify overall market breadth from sector data.

    HEALTHY — majority of sectors bullish/hot, avg breadth >55%
    WEAK    — majority bearish/cold, avg breadth <45%
    NEUTRAL — mixed
    """
    if not sector_states:
        return "NEUTRAL"

    bullish_count = sum(
        1 for s in sector_states.values() if s in ("BULLISH", "HOT")
    )
    bearish_count = sum(
        1 for s in sector_states.values() if s in ("BEARISH", "COLD")
    )
    total = len(sector_states) or 1

    avg_breadth = 50.0
    if sector_breadths:
        vals = [v for v in sector_breadths.values() if v > 0]
        if vals:
            avg_breadth = sum(vals) / len(vals)

    if bullish_count / total >= 0.5 and avg_breadth >= 55:
        return "HEALTHY"
    if bearish_count / total >= 0.5 or avg_breadth < 45:
        return "WEAK"
    return "NEUTRAL"


def _classify_volatility(
    triggered_count: int,
    top_score: int,
    regime: str,
) -> str:
    """Classify volatility environment.

    SHOCK    — 3+ triggered leaders or PANIC regime + triggered
    ELEVATED — 1-2 triggered or top_score > 75
    CALM     — no triggered, low scores
    NORMAL   — default
    """
    if triggered_count >= 3 or (regime == "PANIC" and triggered_count >= 1):
        return "SHOCK"
    if triggered_count >= 1 or top_score >= 75:
        return "ELEVATED"
    if triggered_count == 0 and top_score < 40:
        return "CALM"
    return "NORMAL"


def _classify_rotation(leaders: int, top_score: int) -> str:
    """Classify rotation strength.

    STRONG   — 3+ leading industries or top_score > 80
    MODERATE — 1-2 leaders or top_score > 50
    WEAK     — no leaders, low scores
    NEUTRAL  — default
    """
    if leaders >= 3 or top_score > 80:
        return "STRONG"
    if leaders >= 1 or top_score > 50:
        return "MODERATE"
    if leaders == 0 and top_score < 30:
        return "WEAK"
    return "NEUTRAL"


def _classify_news(avg_score: float, hot_count: int) -> str:
    """Classify news intensity.

    HOT  — avg > 60 or 3+ hot symbols
    WARM — avg > 40 or 1+ hot symbols
    QUIET — default
    """
    if avg_score > 60 or hot_count >= 3:
        return "HOT"
    if avg_score > 40 or hot_count >= 1:
        return "WARM"
    return "QUIET"


def _classify_mode(
    regime: str,
    breadth_state: str,
    volatility_state: str,
    rotation_state: str,
    news_state: str,
) -> tuple:
    """Determine market mode + confidence from sub-states.

    Returns (mode, confidence).
    """
    # ── DEFENSIVE_RISK_OFF ────────────────────────────────────────
    if regime == "PANIC":
        return DEFENSIVE_RISK_OFF, 0.90
    if regime == "TREND_DOWN" and breadth_state == "WEAK":
        return DEFENSIVE_RISK_OFF, 0.80

    # ── VOLATILITY_SHOCK ──────────────────────────────────────────
    if volatility_state == "SHOCK":
        return VOLATILITY_SHOCK, 0.85
    if volatility_state == "ELEVATED" and breadth_state == "WEAK":
        return VOLATILITY_SHOCK, 0.70

    # ── TREND_EXPANSION ───────────────────────────────────────────
    if regime == "TREND_UP" and breadth_state == "HEALTHY":
        conf = 0.85
        if rotation_state in ("STRONG", "MODERATE"):
            conf = 0.90
        if volatility_state == "ELEVATED":
            conf = min(conf + 0.05, 0.95)
        return TREND_EXPANSION, conf

    if regime == "TREND_UP" and rotation_state in ("STRONG", "MODERATE"):
        return TREND_EXPANSION, 0.75

    # ── ROTATION_TAPE ─────────────────────────────────────────────
    if rotation_state == "STRONG":
        conf = 0.80
        if breadth_state != "HEALTHY":
            conf = 0.85  # rotation despite uneven tape → clearer signal
        return ROTATION_TAPE, conf

    if rotation_state == "MODERATE" and breadth_state != "HEALTHY":
        return ROTATION_TAPE, 0.65

    # ── CHOP_RANGE ────────────────────────────────────────────────
    if regime == "CHOP":
        conf = 0.70
        if breadth_state == "WEAK" and news_state == "QUIET":
            conf = 0.80
        return CHOP_RANGE, conf

    # ── Fallback: regime-aligned ──────────────────────────────────
    if regime == "TREND_UP":
        return TREND_EXPANSION, 0.60
    if regime == "TREND_DOWN":
        return DEFENSIVE_RISK_OFF, 0.60

    return CHOP_RANGE, 0.50
