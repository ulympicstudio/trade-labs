"""
Sector Limits — concentration risk tracking by sector.

Tracks active drafts and approved orders per sector to prevent
over-concentration.  Returns PASS / REDUCE / BLOCK verdicts.

Also supports *exposure-fraction* mode: each approved/filled trade
records its notional as a fraction of account equity, and limits
fire when that fraction crosses soft / hard thresholds.

Tunables (env)
--------------
``TL_RISK_MAX_ACTIVE_PER_SECTOR``   max open positions per sector (default ``3``)
``TL_RISK_MAX_DRAFTS_PER_SECTOR``   max plan drafts per sector   (default ``5``)
``TL_SECTOR_LIMIT_ENABLED``         master on/off (default ``true``)
``TL_SECTOR_WEAK_QTY_MULT``         qty multiplier for WEAK sectors (default ``0.7``)
``TL_SECTOR_LEADING_QTY_MULT``      qty multiplier for LEADING sectors (default ``1.0``)
``TL_TEST_SECTOR_SOFT_LIMIT``       test-override: soft exposure fraction (optional)
``TL_TEST_SECTOR_HARD_LIMIT``       test-override: hard exposure fraction (optional)
``TL_RISK_SECTOR_EQUITY``           assumed equity for exposure calc (default ``100_000``)
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass
from typing import Dict

from src.universe.sector_mapper import classify_symbol, get_industry
from src.monitoring.logger import get_logger

# ── Tunables ─────────────────────────────────────────────────────────

ENABLED = os.environ.get(
    "TL_SECTOR_LIMIT_ENABLED", "true"
).lower() in ("1", "true", "yes")

_MAX_ACTIVE = int(os.environ.get("TL_RISK_MAX_ACTIVE_PER_SECTOR", "3"))
_MAX_DRAFTS = int(os.environ.get("TL_RISK_MAX_DRAFTS_PER_SECTOR", "5"))
_WEAK_QTY_MULT = float(os.environ.get("TL_SECTOR_WEAK_QTY_MULT", "0.7"))
_LEADING_QTY_MULT = float(os.environ.get("TL_SECTOR_LEADING_QTY_MULT", "1.0"))

# Exposure-fraction limits (test overrides)
_TEST_SOFT = os.environ.get("TL_TEST_SECTOR_SOFT_LIMIT", "")
_TEST_HARD = os.environ.get("TL_TEST_SECTOR_HARD_LIMIT", "")
_SOFT_LIMIT: float = float(_TEST_SOFT) if _TEST_SOFT else 0.0
_HARD_LIMIT: float = float(_TEST_HARD) if _TEST_HARD else 0.0
_EXPOSURE_MODE = _SOFT_LIMIT > 0 or _HARD_LIMIT > 0
_EQUITY = float(os.environ.get("TL_RISK_SECTOR_EQUITY", "100000"))

_log = get_logger("sector_limits")

# ── Verdict constants ────────────────────────────────────────────────

PASS = "PASS"
REDUCE = "REDUCE"
BLOCK = "BLOCK"


@dataclass(frozen=True)
class SectorLimitResult:
    """Outcome of a sector concentration check."""

    verdict: str = PASS          # PASS / REDUCE / BLOCK
    sector: str = "UNKNOWN"
    active_count: int = 0
    draft_count: int = 0
    max_active: int = _MAX_ACTIVE
    max_drafts: int = _MAX_DRAFTS
    qty_mult: float = 1.0        # multiplier to apply to qty
    reason: str = ""
    exposure: float = 0.0        # current exposure fraction
    soft_limit: float = _SOFT_LIMIT
    hard_limit: float = _HARD_LIMIT


# ── Internal state ───────────────────────────────────────────────────

_lock = threading.Lock()
_active_by_sector: Dict[str, int] = {}   # sector → count of live positions
_drafts_by_sector: Dict[str, int] = {}   # sector → count of pending drafts
_notional_by_sector: Dict[str, float] = {}  # sector → cumulative notional $

_startup_logged = False


def _log_startup() -> None:
    """Log force-limit activation once."""
    global _startup_logged
    if _startup_logged:
        return
    _startup_logged = True
    if _EXPOSURE_MODE:
        _log.info(
            "FORCE-SECTOR-LIMITS ACTIVE soft=%.2f hard=%.2f equity=%.0f",
            _SOFT_LIMIT, _HARD_LIMIT, _EQUITY,
        )


def record_draft(symbol: str) -> None:
    """Increment draft count for *symbol*'s sector."""
    if not ENABLED:
        return
    _log_startup()
    sector = classify_symbol(symbol).sector
    if sector == "UNKNOWN":
        return
    with _lock:
        _drafts_by_sector[sector] = _drafts_by_sector.get(sector, 0) + 1


def record_fill(symbol: str, notional: float = 0.0) -> None:
    """Increment active position count and notional for *symbol*'s sector."""
    if not ENABLED:
        return
    _log_startup()
    sector = classify_symbol(symbol).sector
    if sector == "UNKNOWN":
        return
    with _lock:
        _active_by_sector[sector] = _active_by_sector.get(sector, 0) + 1
        # Accumulate notional exposure
        _notional_by_sector[sector] = _notional_by_sector.get(sector, 0.0) + notional
        # Decrement from drafts (it graduated to active)
        if _drafts_by_sector.get(sector, 0) > 0:
            _drafts_by_sector[sector] -= 1


def record_close(symbol: str, notional: float = 0.0) -> None:
    """Decrement active position count for *symbol*'s sector."""
    if not ENABLED:
        return
    sector = classify_symbol(symbol).sector
    if sector == "UNKNOWN":
        return
    with _lock:
        if _active_by_sector.get(sector, 0) > 0:
            _active_by_sector[sector] -= 1
        cur = _notional_by_sector.get(sector, 0.0)
        _notional_by_sector[sector] = max(0.0, cur - notional)


def _get_exposure(sector: str) -> float:
    """Return current exposure fraction for *sector*."""
    with _lock:
        notional = _notional_by_sector.get(sector, 0.0)
    return notional / _EQUITY if _EQUITY > 0 else 0.0


def check_sector_limit(
    symbol: str,
    sector_state: str = "NEUTRAL",
    proposed_notional: float = 0.0,
) -> SectorLimitResult:
    """Check whether a new position in *symbol* would breach sector limits.

    Parameters
    ----------
    symbol:
        Ticker being evaluated.
    sector_state:
        Current sector state from sector_intel (BULLISH / NEUTRAL / BEARISH …).
    proposed_notional:
        Notional value of the proposed trade for exposure calculation.

    Returns
    -------
    SectorLimitResult with verdict PASS / REDUCE / BLOCK.
    """
    if not ENABLED:
        return SectorLimitResult()

    _log_startup()

    sector = classify_symbol(symbol).sector
    if sector == "UNKNOWN":
        return SectorLimitResult(sector=sector)

    with _lock:
        active = _active_by_sector.get(sector, 0)
        drafts = _drafts_by_sector.get(sector, 0)

    # Determine qty multiplier from sector state
    if sector_state in ("WEAK", "BEARISH", "COLD"):
        qty_mult = _WEAK_QTY_MULT
    elif sector_state in ("LEADING", "BULLISH"):
        qty_mult = _LEADING_QTY_MULT
    else:
        qty_mult = 1.0

    # ── Exposure-fraction mode (when test limits active) ─────────
    if _EXPOSURE_MODE:
        cur_exposure = _get_exposure(sector)
        est_exposure = cur_exposure + (proposed_notional / _EQUITY if _EQUITY > 0 else 0.0)

        if _HARD_LIMIT > 0 and est_exposure >= _HARD_LIMIT:
            return SectorLimitResult(
                verdict=BLOCK,
                sector=sector,
                active_count=active,
                draft_count=drafts,
                qty_mult=qty_mult,
                reason=f"exposure={est_exposure:.2f}>=hard={_HARD_LIMIT:.2f}",
                exposure=cur_exposure,
                soft_limit=_SOFT_LIMIT,
                hard_limit=_HARD_LIMIT,
            )

        if _SOFT_LIMIT > 0 and est_exposure >= _SOFT_LIMIT:
            # Scale down proportionally: the closer to hard, the smaller mult
            if _HARD_LIMIT > _SOFT_LIMIT:
                ratio = (est_exposure - _SOFT_LIMIT) / (_HARD_LIMIT - _SOFT_LIMIT)
                reduce_mult = max(0.3, 1.0 - ratio * 0.7)
            else:
                reduce_mult = 0.5
            return SectorLimitResult(
                verdict=REDUCE,
                sector=sector,
                active_count=active,
                draft_count=drafts,
                qty_mult=qty_mult * reduce_mult,
                reason=f"exposure={est_exposure:.2f}>=soft={_SOFT_LIMIT:.2f}",
                exposure=cur_exposure,
                soft_limit=_SOFT_LIMIT,
                hard_limit=_HARD_LIMIT,
            )

        return SectorLimitResult(
            verdict=PASS,
            sector=sector,
            active_count=active,
            draft_count=drafts,
            qty_mult=qty_mult,
            exposure=cur_exposure,
            soft_limit=_SOFT_LIMIT,
            hard_limit=_HARD_LIMIT,
        )

    # ── Count-based mode (original logic) ────────────────────────

    # BLOCK: active positions at limit
    if active >= _MAX_ACTIVE:
        return SectorLimitResult(
            verdict=BLOCK,
            sector=sector,
            active_count=active,
            draft_count=drafts,
            qty_mult=qty_mult,
            reason=f"active={active}>={_MAX_ACTIVE}",
        )

    # BLOCK: drafts at limit
    if drafts >= _MAX_DRAFTS:
        return SectorLimitResult(
            verdict=BLOCK,
            sector=sector,
            active_count=active,
            draft_count=drafts,
            qty_mult=qty_mult,
            reason=f"drafts={drafts}>={_MAX_DRAFTS}",
        )

    # REDUCE: approaching limit (1 away)
    if active >= _MAX_ACTIVE - 1 or drafts >= _MAX_DRAFTS - 1:
        return SectorLimitResult(
            verdict=REDUCE,
            sector=sector,
            active_count=active,
            draft_count=drafts,
            qty_mult=qty_mult * 0.7,  # extra reduction when near limit
            reason="approaching_limit",
        )

    return SectorLimitResult(
        verdict=PASS,
        sector=sector,
        active_count=active,
        draft_count=drafts,
        qty_mult=qty_mult,
    )


def get_concentration_summary() -> Dict[str, dict]:
    """Return current sector concentration state for monitoring."""
    with _lock:
        result: Dict[str, dict] = {}
        sectors = set(
            list(_active_by_sector.keys())
            + list(_drafts_by_sector.keys())
            + list(_notional_by_sector.keys())
        )
        for sector in sorted(sectors):
            result[sector] = {
                "active": _active_by_sector.get(sector, 0),
                "drafts": _drafts_by_sector.get(sector, 0),
                "max_active": _MAX_ACTIVE,
                "max_drafts": _MAX_DRAFTS,
                "notional": _notional_by_sector.get(sector, 0.0),
                "exposure": _notional_by_sector.get(sector, 0.0) / _EQUITY if _EQUITY > 0 else 0.0,
            }
    return result


def get_sector_top(n: int = 3) -> str:
    """Return a compact string of top-N sectors by exposure for heartbeat."""
    summary = get_concentration_summary()
    if not summary:
        return "[]"
    ranked = sorted(summary.items(), key=lambda x: x[1]["exposure"], reverse=True)
    parts = [f"{s}:{d['exposure']:.2f}" for s, d in ranked[:n] if d["exposure"] > 0]
    return f"[{', '.join(parts)}]" if parts else "[none]"


# ═══════════════════════════════════════════════════════════════════════
#  Industry-level concentration limits
# ═══════════════════════════════════════════════════════════════════════

_INDUSTRY_LIMIT_ENABLED = os.environ.get(
    "TL_INDUSTRY_LIMIT_ENABLED", "true"
).lower() in ("1", "true", "yes")

_MAX_ACTIVE_PER_INDUSTRY = int(os.environ.get("TL_RISK_MAX_ACTIVE_PER_INDUSTRY", "2"))
_MAX_INDUSTRY_EXPOSURE = float(os.environ.get("TL_RISK_MAX_INDUSTRY_EXPOSURE", "0.20"))

_industry_active: Dict[str, int] = {}
_industry_notional: Dict[str, float] = {}


@dataclass(frozen=True)
class IndustryLimitResult:
    """Outcome of an industry concentration check."""

    verdict: str = PASS
    industry: str = "UNKNOWN"
    active_count: int = 0
    max_active: int = _MAX_ACTIVE_PER_INDUSTRY
    qty_mult: float = 1.0
    reason: str = ""
    exposure: float = 0.0
    hard_limit: float = _MAX_INDUSTRY_EXPOSURE


def record_industry_fill(symbol: str, notional: float = 0.0) -> None:
    """Increment active count for *symbol*'s industry."""
    if not _INDUSTRY_LIMIT_ENABLED:
        return
    industry = get_industry(symbol)
    if industry == "UNKNOWN":
        return
    with _lock:
        _industry_active[industry] = _industry_active.get(industry, 0) + 1
        _industry_notional[industry] = _industry_notional.get(industry, 0.0) + notional


def record_industry_close(symbol: str, notional: float = 0.0) -> None:
    """Decrement active count for *symbol*'s industry."""
    if not _INDUSTRY_LIMIT_ENABLED:
        return
    industry = get_industry(symbol)
    if industry == "UNKNOWN":
        return
    with _lock:
        if _industry_active.get(industry, 0) > 0:
            _industry_active[industry] -= 1
        cur = _industry_notional.get(industry, 0.0)
        _industry_notional[industry] = max(0.0, cur - notional)


def check_industry_limit(
    symbol: str,
    proposed_notional: float = 0.0,
) -> IndustryLimitResult:
    """Check whether a new position in *symbol* would breach industry limits."""
    if not _INDUSTRY_LIMIT_ENABLED:
        return IndustryLimitResult()

    industry = get_industry(symbol)
    if industry == "UNKNOWN":
        return IndustryLimitResult(industry=industry)

    with _lock:
        active = _industry_active.get(industry, 0)
        notional = _industry_notional.get(industry, 0.0)

    exposure = notional / _EQUITY if _EQUITY > 0 else 0.0
    est_exposure = exposure + (proposed_notional / _EQUITY if _EQUITY > 0 else 0.0)

    # BLOCK: too many in one industry
    if active >= _MAX_ACTIVE_PER_INDUSTRY:
        return IndustryLimitResult(
            verdict=BLOCK,
            industry=industry,
            active_count=active,
            reason=f"industry_active={active}>={_MAX_ACTIVE_PER_INDUSTRY}",
            exposure=exposure,
        )

    # BLOCK: exposure too high
    if _MAX_INDUSTRY_EXPOSURE > 0 and est_exposure >= _MAX_INDUSTRY_EXPOSURE:
        return IndustryLimitResult(
            verdict=BLOCK,
            industry=industry,
            active_count=active,
            reason=f"industry_exposure={est_exposure:.2f}>={_MAX_INDUSTRY_EXPOSURE:.2f}",
            exposure=exposure,
        )

    # REDUCE: approaching limit
    if active >= _MAX_ACTIVE_PER_INDUSTRY - 1:
        return IndustryLimitResult(
            verdict=REDUCE,
            industry=industry,
            active_count=active,
            qty_mult=0.7,
            reason="approaching_industry_limit",
            exposure=exposure,
        )

    return IndustryLimitResult(
        verdict=PASS,
        industry=industry,
        active_count=active,
        exposure=exposure,
    )


def get_industry_concentration_summary() -> Dict[str, dict]:
    """Return current industry concentration state for monitoring."""
    with _lock:
        result: Dict[str, dict] = {}
        industries = set(
            list(_industry_active.keys()) + list(_industry_notional.keys())
        )
        for ind in sorted(industries):
            result[ind] = {
                "active": _industry_active.get(ind, 0),
                "max_active": _MAX_ACTIVE_PER_INDUSTRY,
                "notional": _industry_notional.get(ind, 0.0),
                "exposure": _industry_notional.get(ind, 0.0) / _EQUITY if _EQUITY > 0 else 0.0,
            }
    return result
