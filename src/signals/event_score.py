"""
Event Scoring Layer — unified score that drives all trade decisions.

Replaces ad-hoc "if RSI < X then trade" logic with a composable,
multi-factor score that determines:

    1. Whether to trade (event_score >= threshold)
    2. How much confidence / sizing to use
    3. Which playbook applies (breakout / meanrevert / news_momo)

Inputs
------
- impact_score (from CONSENSUS:N / news shock)
- consensus provider count
- category tags (EARNINGS, FDA, MACRO, …)
- sentiment (−1..+1)
- RSI14 (oversold/overbought detection)
- volume spike / RVOL
- spread tightness
- regime (from regime.py)

Output
------
- event_score: 0–100
- risk_mode: LOW / MED / HIGH
- playbook: "breakout" / "meanrevert" / "news_momo" / "consensus_only"
- confidence: 0.0–1.0
- reasons: list of contributing factors
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List, Optional


# ── Tunables ─────────────────────────────────────────────────────────

_THRESHOLD_TRADE = int(os.environ.get("TL_EVENT_SCORE_TRADE_MIN", "40"))
_THRESHOLD_HIGH_CONV = int(os.environ.get("TL_EVENT_SCORE_HIGH_CONV", "70"))

# Weight table (each factor contributes 0–max_pts to the 0–100 score)
_W_CONSENSUS = float(os.environ.get("TL_ES_W_CONSENSUS", "25"))
_W_IMPACT = float(os.environ.get("TL_ES_W_IMPACT", "20"))
_W_RSI = float(os.environ.get("TL_ES_W_RSI", "15"))
_W_RVOL = float(os.environ.get("TL_ES_W_RVOL", "10"))
_W_SPREAD = float(os.environ.get("TL_ES_W_SPREAD", "10"))
_W_REGIME = float(os.environ.get("TL_ES_W_REGIME", "10"))
_W_CATEGORY = float(os.environ.get("TL_ES_W_CATEGORY", "10"))
_W_SECTOR = float(os.environ.get("TL_ES_W_SECTOR", "10"))  # sector alignment (0–10)

# Category bonus table (env-overridable weights → normalised to 0–10 internal)
_CAT_EARNINGS = int(os.environ.get("TL_EVENT_CAT_EARNINGS", "20"))
_CAT_FDA = int(os.environ.get("TL_EVENT_CAT_FDA", "25"))
_CAT_MACRO = int(os.environ.get("TL_EVENT_CAT_MACRO", "15"))
_CAT_ANALYST = int(os.environ.get("TL_EVENT_CAT_ANALYST", "10"))
_CAT_MERGER = int(os.environ.get("TL_EVENT_CAT_MERGER", "20"))
_CAT_BANKRUPTCY = int(os.environ.get("TL_EVENT_CAT_BANKRUPTCY", "25"))
_CAT_CEO_CHANGE = int(os.environ.get("TL_EVENT_CAT_CEO_CHANGE", "12"))
_CAT_MGMT = int(os.environ.get("TL_EVENT_CAT_MGMT", "8"))
_CAT_GENERAL = int(os.environ.get("TL_EVENT_CAT_GENERAL", "5"))
_CAT_MAX_BONUS = 25  # cap: even max-weight category can't exceed this

_CATEGORY_WEIGHTS: dict[str, int] = {
    "EARNINGS": _CAT_EARNINGS,
    "FDA": _CAT_FDA,
    "MACRO": _CAT_MACRO,
    "ANALYST": _CAT_ANALYST,
    "MERGER": _CAT_MERGER,
    "BANKRUPTCY": _CAT_BANKRUPTCY,
    "CEO_CHANGE": _CAT_CEO_CHANGE,
    "MGMT": _CAT_MGMT,
    "GENERAL": _CAT_GENERAL,
}

# Legacy compat mapping (internal 0–10 scale used by _W_CATEGORY)
_CATEGORY_SCORES = {k: min(10, int(v / 2.5)) for k, v in _CATEGORY_WEIGHTS.items()}

# Risk mode thresholds
LOW = "LOW"
MED = "MED"
HIGH = "HIGH"


@dataclass(frozen=True)
class EventScoreResult:
    """Output of the unified event scoring layer."""

    event_score: int = 0                # 0–100
    risk_mode: str = LOW                # LOW / MED / HIGH
    playbook: str = ""                  # recommended strategy
    confidence: float = 0.0             # 0.0–1.0
    reasons: List[str] = field(default_factory=list)

    # ── Per-component point breakdown (for observability) ────────────
    pts_consensus: float = 0.0
    pts_category: float = 0.0
    pts_rsi: float = 0.0
    pts_rvol: float = 0.0
    pts_spread: float = 0.0
    pts_regime: float = 0.0
    pts_impact: float = 0.0

    # ── Sector intelligence ──────────────────────────────────────────
    pts_sector: float = 0.0
    sector: str = ""              # sector name
    industry: str = ""            # industry name
    sector_state: str = ""        # LEADING / NEUTRAL / WEAK

    @property
    def tradeable(self) -> bool:
        return self.event_score >= _THRESHOLD_TRADE

    @property
    def high_conviction(self) -> bool:
        return self.event_score >= _THRESHOLD_HIGH_CONV


def compute_event_score(
    *,
    consensus_n: int = 0,
    impact_score: int = 0,
    category_tags: Optional[List[str]] = None,
    sentiment: Optional[float] = None,
    rsi14: Optional[float] = None,
    rvol: Optional[float] = None,
    spread_pct: Optional[float] = None,
    regime: str = "CHOP",
    regime_confidence: float = 0.5,
    # Sector intelligence inputs (optional, zero contribution if absent)
    sector_align_pts: float = 0.0,
    sector_rs_pts: float = 0.0,
    sector_heat_pts: float = 0.0,
    sector_sympathy_pts: float = 0.0,
    sector_name: str = "",
    industry_name: str = "",
    sector_state: str = "",
) -> EventScoreResult:
    """Compute a unified 0–100 event score from all available factors.

    Each factor is normalized to [0, 1] then multiplied by its weight.
    Missing factors contribute 0 (safe default).
    """
    score = 0.0
    reasons: List[str] = []

    # ── 1. Consensus (0–25 pts) ──────────────────────────────────────
    if consensus_n >= 2:
        # 2 providers = 60% of max, 3 = 80%, 4+ = 100%
        frac = min(1.0, 0.4 + 0.2 * (consensus_n - 1))
        pts = _W_CONSENSUS * frac
        score += pts
        reasons.append(f"consensus={consensus_n}→{pts:.0f}pts")

    # ── 2. Impact score (0–20 pts) ───────────────────────────────────
    if impact_score > 0:
        # impact 1–10 maps linearly
        frac = min(1.0, impact_score / 8.0)
        pts = _W_IMPACT * frac
        score += pts
        reasons.append(f"impact={impact_score}→{pts:.0f}pts")

    # ── 3. RSI signal (0–15 pts) ─────────────────────────────────────
    if rsi14 is not None:
        # Oversold bonus: RSI < 30 → full points; RSI 30–50 → partial
        if rsi14 < 30:
            frac = 1.0
        elif rsi14 < 50:
            frac = (50 - rsi14) / 20.0
        elif rsi14 > 70:
            frac = (rsi14 - 70) / 30.0  # overbought = potential short signal
        else:
            frac = 0.0
        pts = _W_RSI * frac
        score += pts
        if pts > 0:
            reasons.append(f"rsi14={rsi14:.1f}→{pts:.0f}pts")

    # ── 4. Relative volume (0–10 pts) ────────────────────────────────
    if rvol is not None and rvol > 1.0:
        # rvol 1.5 → 50%, 2.0 → 75%, 3.0+ → 100%
        frac = min(1.0, (rvol - 1.0) / 2.0)
        pts = _W_RVOL * frac
        score += pts
        reasons.append(f"rvol={rvol:.1f}→{pts:.0f}pts")

    # ── 5. Spread tightness (0–10 pts) ───────────────────────────────
    if spread_pct is not None and spread_pct > 0:
        # tight spread (<0.05%) → full marks; wide (>0.3%) → 0
        if spread_pct < 0.0005:
            frac = 1.0
        elif spread_pct < 0.003:
            frac = max(0.0, 1.0 - (spread_pct - 0.0005) / 0.0025)
        else:
            frac = 0.0
        pts = _W_SPREAD * frac
        score += pts
        if pts > 0:
            reasons.append(f"spread={spread_pct:.4f}→{pts:.0f}pts")

    # ── 6. Regime alignment (0–10 pts) ───────────────────────────────
    regime_bonus = {
        "TREND_UP": 1.0,
        "TREND_DOWN": 0.5,
        "CHOP": 0.3,
        "PANIC": 0.2,
    }
    frac = regime_bonus.get(regime, 0.3) * regime_confidence
    pts = _W_REGIME * frac
    score += pts
    if pts > 2:
        reasons.append(f"regime={regime}→{pts:.0f}pts")

    # ── 7. Category tags (0–10 pts, env-weighted) ────────────────────
    if category_tags:
        best_cat_wt = 0
        best_cat = ""
        for tag in category_tags:
            cat_wt = _CATEGORY_WEIGHTS.get(tag.upper(), 0)
            if cat_wt > best_cat_wt:
                best_cat_wt = cat_wt
                best_cat = tag.upper()
        if best_cat_wt > 0:
            capped_wt = min(best_cat_wt, _CAT_MAX_BONUS)
            frac = capped_wt / _CAT_MAX_BONUS  # normalise to 0–1
            pts = _W_CATEGORY * frac
            score += pts
            reasons.append(f"cat={best_cat}(+{capped_wt})")

    # ── 8. Sector alignment (0–10 pts) ───────────────────────────────
    _sector_total = sector_align_pts + sector_rs_pts + sector_heat_pts + sector_sympathy_pts
    if _sector_total != 0:
        # Normalise combined sector pts to 0–1 range (max raw ~10 typical)
        frac = max(-1.0, min(1.0, _sector_total / 10.0))
        pts = _W_SECTOR * max(0.0, frac)  # only positive contribution to score
        score += pts
        if abs(_sector_total) > 1:
            reasons.append(f"sector={sector_name}({sector_state})→{pts:.0f}pts")

    # ── Track per-component points for breakdown logging ────────────
    _pts_consensus = 0.0
    _pts_impact = 0.0
    _pts_rsi = 0.0
    _pts_rvol = 0.0
    _pts_spread = 0.0
    _pts_regime = 0.0
    _pts_category = 0.0
    _pts_sector = 0.0

    # Re-derive component points (mirror logic above, simpler to track)
    # Consensus
    if consensus_n >= 2:
        _frac = min(1.0, 0.4 + 0.2 * (consensus_n - 1))
        _pts_consensus = _W_CONSENSUS * _frac
    # Impact
    if impact_score > 0:
        _pts_impact = _W_IMPACT * min(1.0, impact_score / 8.0)
    # RSI
    if rsi14 is not None:
        if rsi14 < 30:
            _pts_rsi = _W_RSI * 1.0
        elif rsi14 < 50:
            _pts_rsi = _W_RSI * (50 - rsi14) / 20.0
        elif rsi14 > 70:
            _pts_rsi = _W_RSI * (rsi14 - 70) / 30.0
    # RVOL
    if rvol is not None and rvol > 1.0:
        _pts_rvol = _W_RVOL * min(1.0, (rvol - 1.0) / 2.0)
    # Spread
    if spread_pct is not None and spread_pct > 0:
        if spread_pct < 0.0005:
            _pts_spread = _W_SPREAD * 1.0
        elif spread_pct < 0.003:
            _pts_spread = _W_SPREAD * max(0.0, 1.0 - (spread_pct - 0.0005) / 0.0025)
    # Regime
    _regime_frac = regime_bonus.get(regime, 0.3) * regime_confidence
    _pts_regime = _W_REGIME * _regime_frac
    # Category
    if category_tags:
        _best_wt = max((_CATEGORY_WEIGHTS.get(t.upper(), 0) for t in category_tags), default=0)
        if _best_wt > 0:
            _pts_category = _W_CATEGORY * min(_best_wt, _CAT_MAX_BONUS) / _CAT_MAX_BONUS
    # Sector
    _sector_raw = sector_align_pts + sector_rs_pts + sector_heat_pts + sector_sympathy_pts
    if _sector_raw != 0:
        _pts_sector = _W_SECTOR * max(0.0, min(1.0, _sector_raw / 10.0))

    # ── Final score ──────────────────────────────────────────────────
    event_score = max(0, min(100, int(round(score))))

    # ── Risk mode ────────────────────────────────────────────────────
    if event_score >= _THRESHOLD_HIGH_CONV:
        risk_mode = HIGH
    elif event_score >= _THRESHOLD_TRADE:
        risk_mode = MED
    else:
        risk_mode = LOW

    # ── Playbook selection ───────────────────────────────────────────
    playbook = _select_playbook(
        consensus_n=consensus_n,
        impact_score=impact_score,
        rsi14=rsi14,
        regime=regime,
    )

    # ── Confidence (score normalized to 0–1, with floor) ─────────────
    confidence = min(1.0, max(0.10, event_score / 100.0))

    return EventScoreResult(
        event_score=event_score,
        risk_mode=risk_mode,
        playbook=playbook,
        confidence=round(confidence, 3),
        reasons=reasons,
        pts_consensus=round(_pts_consensus, 1),
        pts_category=round(_pts_category, 1),
        pts_rsi=round(_pts_rsi, 1),
        pts_rvol=round(_pts_rvol, 1),
        pts_spread=round(_pts_spread, 1),
        pts_regime=round(_pts_regime, 1),
        pts_impact=round(_pts_impact, 1),
        pts_sector=round(_pts_sector, 1),
        sector=sector_name,
        industry=industry_name,
        sector_state=sector_state,
    )


def _select_playbook(
    *,
    consensus_n: int,
    impact_score: int,
    rsi14: Optional[float],
    regime: str,
) -> str:
    """Choose the best playbook based on available signals."""
    # High-impact consensus news → news momentum
    if consensus_n >= 2 and impact_score >= 3:
        return "news_momo"

    # Pure consensus without RSI → consensus_only
    if consensus_n >= 2 and rsi14 is None:
        return "consensus_only"

    # RSI oversold in chop → mean reversion
    if rsi14 is not None and rsi14 < 35 and regime in ("CHOP", "TREND_UP"):
        return "meanrevert"

    # Trending up → breakout/momentum
    if regime == "TREND_UP":
        return "breakout"

    # Default
    return "meanrevert"
