"""
risk/session_gate.py

Time-of-day trade gating with quality scores.
Called by risk arm before approving any trade.
Quality score feeds into position_sizing.calculate_position_size().
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from enum import Enum
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class GateResult:
    allowed: bool
    reason: str
    quality_score: float        # 0.0 = blocked, 1.0 = full size


# (start, end, quality, label)
_WINDOWS = [
    (time(9, 30),  time(9, 46),  0.0,  "open_chaos_block"),
    (time(9, 46),  time(10, 30), 0.75, "early_momentum"),
    (time(10, 30), time(11, 45), 1.0,  "prime_window"),
    (time(11, 45), time(13, 30), 0.55, "midday_drift"),
    (time(13, 30), time(15, 30), 0.85, "afternoon_momentum"),
    (time(15, 30), time(15, 50), 0.60, "late_day"),
    (time(15, 50), time(16, 0),  0.0,  "close_chaos_block"),
]


def check_session_gate(now: datetime | None = None) -> GateResult:
    """
    Returns GateResult for current time (ET).
    Pass `now` explicitly for testing; omit for live use.
    """
    if now is None:
        now = datetime.now(ET)

    current_time = now.astimezone(ET).time()

    for start, end, quality, label in _WINDOWS:
        if start <= current_time < end:
            return GateResult(
                allowed=quality > 0.0,
                reason=label,
                quality_score=quality,
            )

    return GateResult(
        allowed=False,
        reason="outside_market_hours",
        quality_score=0.0,
    )


# ── After-hours entry admission gate ─────────────────────────────────

def can_submit_entry(now_session: str, cfg, quote) -> tuple[bool, str]:
    """Check whether an entry order is allowed given session, config, and quote.

    Returns (allowed, reason).  Designed to work with the AH config block
    in ``config.runtime`` (PAPER_AH_TEST / allow_extended / etc.).
    """
    if now_session == "AFTERHOURS" and not getattr(cfg, "ah_entry_enabled", False):
        return False, "ah_entry_disabled"

    if now_session == "AFTERHOURS" and not getattr(cfg, "allow_extended", False):
        return False, "extended_hours_disabled"

    is_synth = bool(
        getattr(quote, "is_synthetic", False)
        or getattr(quote, "session", "") == "SYNTH"
    )

    if getattr(cfg, "require_live_quotes", True) and is_synth:
        return False, "live_quotes_required"

    if is_synth and not getattr(cfg, "synthetic_ok", False):
        return False, "synthetic_quotes_blocked"

    if not getattr(cfg, "armed", False):
        return False, "system_not_armed"

    return True, "ok"


# ── Entry session policy ─────────────────────────────────────────────

@dataclass(frozen=True)
class EntrySessionPolicy:
    allow_entry: bool
    require_live_quotes: bool
    allow_synthetic_quotes: bool
    min_quote_quality: float
    reason: str


def compute_entry_session_policy(
    *,
    paper: bool,
    session: str,
    quote_quality: float,
    spread_bps: float,
) -> EntrySessionPolicy:
    """Return an entry policy for the given session/mode/quality context."""
    if session == "REGULAR":
        return EntrySessionPolicy(
            allow_entry=True,
            require_live_quotes=not paper,
            allow_synthetic_quotes=paper,
            min_quote_quality=0.70 if paper else 0.90,
            reason="regular_session",
        )

    if session in {"PREMARKET", "AFTERHOURS"}:
        if paper:
            if quote_quality >= 0.75 and spread_bps <= 35:
                return EntrySessionPolicy(
                    allow_entry=True,
                    require_live_quotes=False,
                    allow_synthetic_quotes=True,
                    min_quote_quality=0.75,
                    reason="paper_extended_hours_quality_ok",
                )
            return EntrySessionPolicy(
                allow_entry=False,
                require_live_quotes=False,
                allow_synthetic_quotes=True,
                min_quote_quality=0.75,
                reason="paper_extended_hours_quality_low",
            )

        return EntrySessionPolicy(
            allow_entry=False,
            require_live_quotes=True,
            allow_synthetic_quotes=False,
            min_quote_quality=0.95,
            reason="live_extended_hours_blocked",
        )

    return EntrySessionPolicy(
        allow_entry=False,
        require_live_quotes=True,
        allow_synthetic_quotes=False,
        min_quote_quality=1.00,
        reason="unknown_session",
    )


# ── Quote usability assessment ────────────────────────────────────────

@dataclass(frozen=True)
class QuoteUsability:
    signal_ok: bool
    entry_ok: bool
    quality_score: float
    spread_bps: float
    source: str  # live, cached, synthetic


def assess_quote_usability(
    last: float,
    bid: float,
    ask: float,
    source: str,
    session: str,
) -> QuoteUsability:
    """Score a quote for signal and execution usability.

    Returns a QuoteUsability indicating whether the quote is adequate
    for signal scoring (``signal_ok``) and/or order entry (``entry_ok``).
    """
    mid = (bid + ask) / 2 if bid and ask and bid > 0 and ask > bid else None
    spread_bps = 10000.0 * (ask - bid) / mid if mid else 9999.0

    quality = 1.0
    if source == "synthetic":
        quality -= 0.35
    if spread_bps > 20:
        quality -= 0.20
    if spread_bps > 35:
        quality -= 0.20
    if session != "REGULAR":
        quality -= 0.10

    quality = max(0.0, min(1.0, quality))
    signal_ok = quality >= 0.45
    entry_ok = quality >= 0.75 and spread_bps <= 35

    return QuoteUsability(signal_ok, entry_ok, quality, spread_bps, source)
