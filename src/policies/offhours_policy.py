"""Off-hours order lifecycle policy.

Decision values
---------------
HOLD      – do not stage or transmit; discard the blueprint quietly.
STAGE     – record the order plan for later action; do not transmit.
AMEND     – update a previously staged plan with revised prices.
CANCEL    – discard a previously staged plan.
TRANSMIT  – proceed with normal order submission.
SKIP      – no state change due to safety rails (throttle, caps, stale/synth quote).

This module is intentionally stateful: execution updates it from blueprint,
market snapshot, and news-event callbacks so off-hours staged symbols can be
managed without introducing a second event system.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from src.config.settings import settings

log = logging.getLogger(__name__)

# ── Environment feature flags ────────────────────────────────────────────────


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.lower() in ("1", "true", "yes")

# Paper-safe defaults:
# - manage defaults ON in paper, OFF otherwise (explicit env can override)
# - entry defaults OFF (no automatic off-hours transmit)
# - require-live-quotes remains ON unless explicitly disabled by env override
_MANAGE = _env_bool("TL_EXEC_OFFHOURS_MANAGE", bool(settings.is_paper))
_ENTRY = _env_bool("TL_EXEC_OFFHOURS_ENTRY", False)
_REQUIRE_LIVE = _env_bool("TL_EXEC_OFFHOURS_REQUIRE_LIVE_QUOTES", True)
_MAX_SPREAD_PCT = float(os.environ.get("TL_EXEC_OFFHOURS_MAX_SPREAD_PCT", "0.25"))
_MAX_POSITIONS = int(os.environ.get("TL_EXEC_OFFHOURS_MAX_POSITIONS", "1"))
_CANCEL_REPRICE_SECS = float(
    os.environ.get("TL_EXEC_OFFHOURS_CANCEL_REPRICE_SECS", "300")
)
_QUOTE_STALE_SECS = float(os.environ.get("TL_EXEC_OFFHOURS_QUOTE_STALE_SECS", "120"))
_AMEND_TICKS = int(os.environ.get("TL_EXEC_OFFHOURS_AMEND_TICKS", "3"))
_MAX_AMENDS = int(os.environ.get("TL_EXEC_OFFHOURS_MAX_AMENDS", "5"))
_AMEND_COOLDOWN_SECS = float(os.environ.get("TL_EXEC_OFFHOURS_AMEND_COOLDOWN_SECS", "30"))
_NEWS_TTL_SECS = float(os.environ.get("TL_EXEC_OFFHOURS_NEWS_TTL_SECS", "3600"))
_CANCEL_ON_SYNTH = _env_bool("TL_EXEC_OFFHOURS_CANCEL_ON_SYNTH", True)

# Session labels that trigger off-hours handling
_RTH = "RTH"
_NON_RTH_SESSIONS = {"OFF_HOURS", "PREMARKET", "AFTERHOURS", "ETH", "PRE", "POST"}


# ── In-memory staged-plan registry ──────────────────────────────────────────


@dataclass
class StagedPlan:
    """Snapshot of an off-hours blueprint held for later evaluation."""

    symbol: str
    session: str
    entry_price: float
    stop_price: float
    qty: int
    risk_usd: float
    confidence: float
    staged_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    last_amend_ts: float = 0.0
    amended_count: int = 0
    max_spread_pct: float = 0.25
    stop_distance: float = 0.0
    initial_thesis_score: float = 0.0
    thesis_score: float = 0.0
    thesis_decay_score: float = 0.0
    thesis_broken: bool = False
    contradiction_count: int = 0
    last_news_ts: float = field(default_factory=time.time)
    last_quote_ts: float = 0.0
    last_quote_age_s: float = 9_999.0
    last_spread_pct: float = 0.0
    last_provider: str = ""
    quote_synthetic: bool = False
    backend: str = "SIM"
    notes: str = ""


_lock = threading.Lock()
_staged: dict[str, StagedPlan] = {}


# ── Internal helpers ─────────────────────────────────────────────────────────


def _staged_count() -> int:
    with _lock:
        return len(_staged)


def _prune_stale() -> None:
    """Remove and log staged plans that have exceeded the reprice timeout."""
    now = time.time()
    with _lock:
        stale = [
            sym
            for sym, plan in _staged.items()
            if (now - plan.staged_at) > _CANCEL_REPRICE_SECS
        ]
        for sym in stale:
            plan = _staged.pop(sym)
            log.info(
                "offhours_cancel symbol=%s reason=stale_timeout staged_secs=%.0f",
                sym,
                now - plan.staged_at,
            )


def _tick_size(price: float) -> float:
    if price < 1.0:
        return 0.0001
    if price < 10.0:
        return 0.001
    return 0.01


def _quote_mid(bid: float, ask: float, last: float) -> float:
    if bid > 0 and ask > 0:
        return (bid + ask) / 2.0
    return max(last, 0.0)


def _calc_decay(plan: StagedPlan, now: float) -> tuple[float, float]:
    # Base time decay: ~0.35 points/min from initial thesis score.
    elapsed_min = max(0.0, (now - plan.staged_at) / 60.0)
    decay = elapsed_min * 0.35
    # Contradictory updates accelerate decay.
    decay += plan.contradiction_count * 8.0
    new_score = max(0.0, plan.initial_thesis_score - decay)
    return new_score, decay


def _is_blocked_session(session: str) -> bool:
    return session not in _NON_RTH_SESSIONS


def _news_is_contradictory(sentiment: Optional[float], impact_tags: list[str]) -> bool:
    if sentiment is not None and sentiment <= -0.2:
        return True
    lowered = {t.lower() for t in impact_tags}
    bad_tags = {
        "downgrade", "lawsuit", "probe", "fraud", "bankruptcy", "default",
        "guidance_cut", "miss", "layoff", "recall",
    }
    return any(t in bad_tags for t in lowered)


# ── Public API ───────────────────────────────────────────────────────────────


def decide(
    *,
    symbol: str,
    session: str,
    source: str,
    confidence: float,
    entry_price: float,
    stop_price: float,
    qty: int,
    risk_usd: float,
    spread_pct: float,
    quote_age_s: float,
    is_paper: bool,
    notes: str = "",
) -> tuple[str, str]:
    """Return ``(decision, reason)`` for a blueprint that may be off-hours.

    For RTH blueprints this immediately returns ``("TRANSMIT", "rth_session")``
    so the normal execution path is unaffected.

    Parameters
    ----------
    session:
        Session label stamped on the blueprint (``OrderBlueprint.session``).
    source:
        Blueprint source tag, e.g. ``"open_plan"``.
    spread_pct:
        Bid-ask spread as a percentage (same unit as ``max_spread_pct``).
        Pass ``0.0`` if live spread is unknown.
    quote_age_s:
        Seconds since the last market snapshot for the symbol.
        Pass a large sentinel (e.g. ``9999``) if no live quote is available.
    is_paper:
        True when running in paper / simulation mode.
    """
    result = on_blueprint(
        symbol=symbol,
        session=session,
        source=source,
        confidence=confidence,
        entry_price=entry_price,
        stop_price=stop_price,
        qty=qty,
        risk_usd=risk_usd,
        spread_pct=spread_pct,
        quote_age_s=quote_age_s,
        is_paper=is_paper,
        notes=notes,
    )
    return str(result.get("decision", "HOLD")), str(result.get("reason", "unknown"))


def on_blueprint(
    *,
    symbol: str,
    session: str,
    source: str,
    confidence: float,
    entry_price: float,
    stop_price: float,
    qty: int,
    risk_usd: float,
    spread_pct: float,
    quote_age_s: float,
    is_paper: bool,
    notes: str = "",
    thesis_score: Optional[float] = None,
    backend: str = "SIM",
) -> dict[str, Any]:
    """Apply off-hours policy at blueprint arrival.

    Returns a dict with at least ``decision`` and ``reason``.
    """
    _prune_stale()
    now = time.time()

    if session not in _NON_RTH_SESSIONS:
        return {"decision": "TRANSMIT", "reason": "rth_session"}

    with _lock:
        existing = _staged.get(symbol)

    if existing is not None:
        existing.updated_at = now
        existing.session = session
        existing.last_quote_age_s = quote_age_s
        if spread_pct > 0:
            existing.last_spread_pct = spread_pct

        if spread_pct > existing.max_spread_pct:
            with _lock:
                _staged.pop(symbol, None)
            return {"decision": "CANCEL", "reason": "spread_breach"}

        if _REQUIRE_LIVE and quote_age_s > _QUOTE_STALE_SECS:
            return {"decision": "SKIP", "reason": "stale_quote"}

        if existing.amended_count >= _MAX_AMENDS:
            return {"decision": "SKIP", "reason": "max_amends"}

        if now - existing.last_amend_ts < _AMEND_COOLDOWN_SECS:
            return {"decision": "SKIP", "reason": "throttle"}

        threshold = _AMEND_TICKS * _tick_size(existing.entry_price)
        if abs(entry_price - existing.entry_price) < threshold:
            return {"decision": "HOLD", "reason": "price_move_below_threshold"}

        old_entry = existing.entry_price
        existing.entry_price = entry_price
        existing.stop_price = stop_price
        existing.qty = qty
        existing.risk_usd = risk_usd
        existing.last_amend_ts = now
        existing.amended_count += 1
        existing.updated_at = now
        return {
            "decision": "AMEND",
            "reason": "price_delta",
            "old_price": old_entry,
            "new_price": entry_price,
            "amend_count": existing.amended_count,
        }

    if not _MANAGE:
        return {"decision": "HOLD", "reason": "offhours_manage_disabled"}

    if spread_pct > _MAX_SPREAD_PCT:
        return {
            "decision": "HOLD",
            "reason": f"spread_too_wide spread={spread_pct:.4f} max={_MAX_SPREAD_PCT:.4f}",
        }

    if _REQUIRE_LIVE and quote_age_s > _QUOTE_STALE_SECS:
        return {
            "decision": "SKIP",
            "reason": "stale_quote",
        }

    if _staged_count() >= _MAX_POSITIONS:
        return {
            "decision": "HOLD",
            "reason": f"staging_full count={_staged_count()} max={_MAX_POSITIONS}",
        }

    if _ENTRY:
        return {"decision": "TRANSMIT", "reason": "offhours_entry_enabled"}

    init_thesis = float(thesis_score if thesis_score is not None else max(confidence * 100.0, 0.0))
    with _lock:
        _staged[symbol] = StagedPlan(
            symbol=symbol,
            session=session,
            entry_price=entry_price,
            stop_price=stop_price,
            qty=qty,
            risk_usd=risk_usd,
            confidence=confidence,
            max_spread_pct=max(_MAX_SPREAD_PCT, spread_pct if spread_pct > 0 else _MAX_SPREAD_PCT),
            stop_distance=max(0.0, entry_price - stop_price),
            initial_thesis_score=init_thesis,
            thesis_score=init_thesis,
            thesis_decay_score=0.0,
            last_quote_age_s=quote_age_s,
            backend=backend,
            notes=notes,
        )
    return {"decision": "STAGE", "reason": "staged_for_rth", "thesis_score": init_thesis}


def on_market_snapshot(
    *,
    symbol: str,
    session: str,
    bid: float,
    ask: float,
    last: float,
    quote_age_s: float,
    is_synthetic: bool,
) -> dict[str, Any]:
    """Update staged-plan state from a market snapshot and decide next action."""
    now = time.time()
    with _lock:
        plan = _staged.get(symbol)

    if plan is None:
        return {"decision": "HOLD", "reason": "not_staged"}

    if _is_blocked_session(session):
        with _lock:
            _staged.pop(symbol, None)
        return {"decision": "CANCEL", "reason": "blocked_session"}

    if is_synthetic:
        if _CANCEL_ON_SYNTH:
            with _lock:
                _staged.pop(symbol, None)
            return {"decision": "CANCEL", "reason": "synthetic_quote"}
        return {"decision": "SKIP", "reason": "synthetic_quote"}

    if quote_age_s > _QUOTE_STALE_SECS:
        with _lock:
            _staged.pop(symbol, None)
        return {"decision": "CANCEL", "reason": "stale_quote"}

    mid = _quote_mid(bid, ask, last)
    spread_pct = ((ask - bid) / mid * 100.0) if bid > 0 and ask > 0 and mid > 0 else 0.0
    plan.last_spread_pct = spread_pct
    plan.last_quote_age_s = quote_age_s
    plan.last_quote_ts = now
    plan.quote_synthetic = is_synthetic

    if spread_pct > plan.max_spread_pct:
        with _lock:
            _staged.pop(symbol, None)
        return {"decision": "CANCEL", "reason": "spread_wide"}

    if (now - plan.last_news_ts) > _NEWS_TTL_SECS:
        with _lock:
            _staged.pop(symbol, None)
        return {"decision": "CANCEL", "reason": "news_ttl"}

    new_thesis, decay = _calc_decay(plan, now)
    old_thesis = plan.thesis_score
    plan.thesis_score = new_thesis
    plan.thesis_decay_score = decay
    plan.thesis_broken = new_thesis <= max(12.0, plan.initial_thesis_score * 0.20)
    if plan.thesis_broken:
        with _lock:
            _staged.pop(symbol, None)
        return {
            "decision": "CANCEL",
            "reason": "thesis_broken",
            "old_thesis": old_thesis,
            "new_thesis": new_thesis,
            "thesis_broken": True,
            "decay_score": decay,
        }

    if plan.amended_count >= _MAX_AMENDS:
        return {"decision": "SKIP", "reason": "max_amends"}

    if now - plan.last_amend_ts < _AMEND_COOLDOWN_SECS:
        return {"decision": "SKIP", "reason": "throttle"}

    threshold = _AMEND_TICKS * _tick_size(plan.entry_price)
    if abs(mid - plan.entry_price) < threshold:
        return {
            "decision": "HOLD",
            "reason": "price_move_below_threshold",
            "old_thesis": old_thesis,
            "new_thesis": new_thesis,
            "thesis_broken": plan.thesis_broken,
            "decay_score": decay,
        }

    old_price = plan.entry_price
    plan.entry_price = round(mid, 2)
    plan.stop_price = round(max(0.01, plan.entry_price - plan.stop_distance), 2)
    plan.last_amend_ts = now
    plan.amended_count += 1
    plan.updated_at = now
    return {
        "decision": "AMEND",
        "reason": "quote_move",
        "old_price": old_price,
        "new_price": plan.entry_price,
        "amend_count": plan.amended_count,
        "old_thesis": old_thesis,
        "new_thesis": new_thesis,
        "thesis_broken": plan.thesis_broken,
        "decay_score": decay,
    }


def on_news_event(
    *,
    symbol: str,
    sentiment: Optional[float],
    impact_score: int,
    impact_tags: Optional[list[str]] = None,
    source_provider: str = "",
    news_age_s: float = 0.0,
) -> dict[str, Any]:
    """Update staged-plan state from a news delta and decide next action."""
    with _lock:
        plan = _staged.get(symbol)
    if plan is None:
        return {"decision": "HOLD", "reason": "not_staged"}

    now = time.time()
    old_thesis = plan.thesis_score
    tags = impact_tags or []
    contradictory = _news_is_contradictory(sentiment, tags)
    if contradictory:
        plan.contradiction_count += 1
    if source_provider and plan.last_provider and source_provider != plan.last_provider:
        # Provider flip with unresolved thesis adds uncertainty penalty.
        plan.contradiction_count += 1

    # Positive impact slightly replenishes thesis; contradictions dominate.
    plan.thesis_score = max(0.0, plan.thesis_score + min(max(impact_score, 0), 20) * 0.15)
    plan.last_news_ts = now - max(news_age_s, 0.0)
    plan.last_provider = source_provider or plan.last_provider
    new_thesis, decay = _calc_decay(plan, now)
    plan.thesis_score = new_thesis
    plan.thesis_decay_score = decay
    plan.thesis_broken = contradictory or new_thesis <= max(12.0, plan.initial_thesis_score * 0.20)

    if plan.thesis_broken:
        with _lock:
            _staged.pop(symbol, None)
        return {
            "decision": "CANCEL",
            "reason": "contradictory_news" if contradictory else "thesis_broken",
            "old_thesis": old_thesis,
            "new_thesis": new_thesis,
            "thesis_broken": True,
            "decay_score": decay,
        }

    return {
        "decision": "HOLD",
        "reason": "news_update",
        "old_thesis": old_thesis,
        "new_thesis": new_thesis,
        "thesis_broken": False,
        "decay_score": decay,
    }


def get_staged(symbol: str) -> Optional[StagedPlan]:
    """Return the staged plan for *symbol*, or ``None``."""
    with _lock:
        return _staged.get(symbol)


def cancel_staged(symbol: str, reason: str = "manual") -> bool:
    """Remove and log a staged plan.  Returns ``True`` if a plan existed."""
    with _lock:
        plan = _staged.pop(symbol, None)
    if plan is not None:
        log.info("offhours_cancel symbol=%s reason=%s", symbol, reason)
        return True
    return False


def all_staged() -> list[StagedPlan]:
    """Snapshot of all currently staged plans (copy of values)."""
    with _lock:
        return list(_staged.values())


def staged_count() -> int:
    """Number of currently staged off-hours plans."""
    with _lock:
        return len(_staged)


def runtime_flags() -> dict[str, bool]:
    """Effective runtime config used by execution diagnostics."""
    return {
        "manage": _MANAGE,
        "entry": _ENTRY,
        "require_live_quotes": _REQUIRE_LIVE,
        "cancel_on_synth": _CANCEL_ON_SYNTH,
    }
