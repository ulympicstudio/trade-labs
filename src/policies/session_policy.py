from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


_SESSION_MAP = {
    "RTH": "RTH",
    "REGULAR": "RTH",
    "PRE": "PRE",
    "PREMARKET": "PRE",
    "AFTERHOURS": "AFTERHOURS",
    "POST": "AFTERHOURS",
    "OFF_HOURS": "CLOSED",
    "CLOSED": "CLOSED",
    "HALT": "HALT",
}


@dataclass(frozen=True)
class SessionContext:
    session: str
    halted: bool = False


@dataclass(frozen=True)
class QuoteContext:
    quote_present: bool
    quote_age_s: Optional[float]
    is_synthetic: bool = False


@dataclass(frozen=True)
class VenuePolicy:
    allow_entries_rth: bool = True
    allow_entries_pre: bool = False
    allow_entries_afterhours: bool = False
    manage_enabled_when_entry_blocked: bool = True
    require_live_quotes: bool = True
    synthetic_ok: bool = False
    quote_stale_after_s: float = 5.0
    shadow_mode: bool = False


@dataclass(frozen=True)
class SessionDecision:
    entry_enabled: bool
    manage_enabled: bool
    require_live_quotes: bool
    synthetic_ok: bool
    session_label: str
    block_reason: Optional[str]
    mode: str


def _normalize_session_label(session: str, halted: bool) -> str:
    if halted:
        return "HALT"
    return _SESSION_MAP.get((session or "").upper(), "UNKNOWN")


def _finalize(
    *,
    entry_enabled: bool,
    manage_enabled: bool,
    require_live_quotes: bool,
    synthetic_ok: bool,
    session_label: str,
    block_reason: Optional[str],
    shadow_mode: bool,
) -> SessionDecision:
    if entry_enabled:
        mode = "LIVE"
    elif manage_enabled and shadow_mode:
        mode = "SHADOW"
    elif manage_enabled:
        mode = "REDUCE_ONLY"
    else:
        mode = "BLOCKED"

    return SessionDecision(
        entry_enabled=entry_enabled,
        manage_enabled=manage_enabled,
        require_live_quotes=require_live_quotes,
        synthetic_ok=synthetic_ok,
        session_label=session_label,
        block_reason=block_reason,
        mode=mode,
    )


def can_emit_entry(
    session_ctx: SessionContext,
    quote_ctx: QuoteContext,
    venue_policy: VenuePolicy,
) -> SessionDecision:
    session_label = _normalize_session_label(session_ctx.session, session_ctx.halted)

    if session_label == "HALT":
        return _finalize(
            entry_enabled=False,
            manage_enabled=False,
            require_live_quotes=venue_policy.require_live_quotes,
            synthetic_ok=venue_policy.synthetic_ok,
            session_label=session_label,
            block_reason="VENUE_HALT",
            shadow_mode=venue_policy.shadow_mode,
        )

    if session_label == "UNKNOWN":
        return _finalize(
            entry_enabled=False,
            manage_enabled=venue_policy.manage_enabled_when_entry_blocked,
            require_live_quotes=venue_policy.require_live_quotes,
            synthetic_ok=venue_policy.synthetic_ok,
            session_label=session_label,
            block_reason="POLICY_UNKNOWN_SESSION",
            shadow_mode=venue_policy.shadow_mode,
        )

    if session_label == "CLOSED":
        return _finalize(
            entry_enabled=False,
            manage_enabled=venue_policy.manage_enabled_when_entry_blocked,
            require_live_quotes=venue_policy.require_live_quotes,
            synthetic_ok=venue_policy.synthetic_ok,
            session_label=session_label,
            block_reason="SESSION_CLOSED",
            shadow_mode=venue_policy.shadow_mode,
        )

    if session_label == "PRE" and not venue_policy.allow_entries_pre:
        return _finalize(
            entry_enabled=False,
            manage_enabled=venue_policy.manage_enabled_when_entry_blocked,
            require_live_quotes=venue_policy.require_live_quotes,
            synthetic_ok=venue_policy.synthetic_ok,
            session_label=session_label,
            block_reason="ENTRY_DISABLED_PREMARKET",
            shadow_mode=venue_policy.shadow_mode,
        )

    if session_label == "AFTERHOURS" and not venue_policy.allow_entries_afterhours:
        return _finalize(
            entry_enabled=False,
            manage_enabled=venue_policy.manage_enabled_when_entry_blocked,
            require_live_quotes=venue_policy.require_live_quotes,
            synthetic_ok=venue_policy.synthetic_ok,
            session_label=session_label,
            block_reason="ENTRY_DISABLED_AFTERHOURS",
            shadow_mode=venue_policy.shadow_mode,
        )

    if session_label == "RTH" and not venue_policy.allow_entries_rth:
        return _finalize(
            entry_enabled=False,
            manage_enabled=venue_policy.manage_enabled_when_entry_blocked,
            require_live_quotes=venue_policy.require_live_quotes,
            synthetic_ok=venue_policy.synthetic_ok,
            session_label=session_label,
            block_reason="SESSION_CLOSED",
            shadow_mode=venue_policy.shadow_mode,
        )

    if venue_policy.require_live_quotes:
        if not quote_ctx.quote_present:
            return _finalize(
                entry_enabled=False,
                manage_enabled=venue_policy.manage_enabled_when_entry_blocked,
                require_live_quotes=venue_policy.require_live_quotes,
                synthetic_ok=venue_policy.synthetic_ok,
                session_label=session_label,
                block_reason="QUOTES_MISSING",
                shadow_mode=venue_policy.shadow_mode,
            )

        if quote_ctx.quote_age_s is None or quote_ctx.quote_age_s > venue_policy.quote_stale_after_s:
            return _finalize(
                entry_enabled=False,
                manage_enabled=venue_policy.manage_enabled_when_entry_blocked,
                require_live_quotes=venue_policy.require_live_quotes,
                synthetic_ok=venue_policy.synthetic_ok,
                session_label=session_label,
                block_reason="QUOTES_STALE",
                shadow_mode=venue_policy.shadow_mode,
            )

    if quote_ctx.is_synthetic and not venue_policy.synthetic_ok:
        return _finalize(
            entry_enabled=False,
            manage_enabled=venue_policy.manage_enabled_when_entry_blocked,
            require_live_quotes=venue_policy.require_live_quotes,
            synthetic_ok=venue_policy.synthetic_ok,
            session_label=session_label,
            block_reason="QUOTES_MISSING",
            shadow_mode=venue_policy.shadow_mode,
        )

    return _finalize(
        entry_enabled=not venue_policy.shadow_mode,
        manage_enabled=True,
        require_live_quotes=venue_policy.require_live_quotes,
        synthetic_ok=venue_policy.synthetic_ok,
        session_label=session_label,
        block_reason=None,
        shadow_mode=venue_policy.shadow_mode,
    )