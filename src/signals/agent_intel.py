"""
Agent Intelligence Feed — read-only advisory layer.

Reads structured intelligence from an external AI assistant
(Perplexity Computer) via a JSON file. The signal arm uses this
to boost or penalize candidate scores during off-hours and
premarket scoring.

The agent CANNOT:
  - Execute trades or modify positions
  - Override risk rules or circuit breakers
  - Set position sizes or entry/exit prices
  - Modify code or system configuration

The agent CAN:
  - Provide catalyst analysis (earnings, FDA, M&A, macro)
  - Score sentiment (-1.0 to +1.0)
  - Flag risks (sector headwinds, earnings traps, etc.)
  - Rank symbols by conviction
  - Set a TTL so stale intelligence is automatically ignored

File format (data/agent_intel.json):
{
    "generated_at": "2026-03-18T13:00:00Z",
    "session_date": "2026-03-18",
    "model": "perplexity-computer",
    "symbols": {
        "NVDA": {
            "catalyst_score": 8,
            "catalyst_type": "GTC_KEYNOTE",
            "catalyst_summary": "GTC keynote today, new Blackwell Ultra GPU expected",
            "sentiment": 0.7,
            "risk_flags": [],
            "conviction": "HIGH",
            "ttl_minutes": 480
        }
    },
    "market_context": {
        "regime": "RISK_ON",
        "sector_rotation": "Tech and Energy leading",
        "macro_risk": "Fed meeting next week, CPI above expectations",
        "vix_assessment": "LOW"
    }
}

Field definitions:
  catalyst_score: 0-10 integer. 0=no catalyst, 10=extreme catalyst (IPO, FDA approval)
  catalyst_type: string tag (EARNINGS, FDA, MACRO, MERGER, ASR_BUYBACK, GTC_KEYNOTE, etc.)
  catalyst_summary: 1-2 sentence human-readable summary
  sentiment: -1.0 to +1.0 float. Negative=bearish, positive=bullish
  risk_flags: list of string tags (falling_knife, earnings_trap, sector_headwind, overbought, etc.)
  conviction: HIGH / MEDIUM / LOW / AVOID
  ttl_minutes: how long this intel is valid (default 480 = 8 hours)
  market_context: optional global context (not per-symbol)
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional

from src.monitoring.logger import get_logger

log = get_logger("agent_intel")

_INTEL_FILE = Path(os.environ.get("TL_AGENT_INTEL_FILE", "data/agent_intel.json"))

# Score contribution weights (agent can boost up to this many points)
_AGENT_CATALYST_WEIGHT = float(os.environ.get("TL_AGENT_CATALYST_WEIGHT", "3.0"))
_AGENT_SENTIMENT_WEIGHT = float(os.environ.get("TL_AGENT_SENTIMENT_WEIGHT", "2.0"))
_AGENT_RISK_PENALTY = float(os.environ.get("TL_AGENT_RISK_PENALTY", "2.0"))
_AGENT_CONVICTION_BONUS = float(os.environ.get("TL_AGENT_CONVICTION_BONUS", "2.0"))

# Maximum total score contribution from agent (cap prevents agent from dominating)
_AGENT_MAX_CONTRIBUTION = float(os.environ.get("TL_AGENT_MAX_CONTRIBUTION", "6.0"))

# Cache: reload file at most every N seconds
_RELOAD_INTERVAL_S = 30
_last_load_ts: float = 0.0
_cached_intel: Dict[str, "SymbolIntel"] = {}
_cached_market: Optional["MarketContext"] = None


@dataclass
class SymbolIntel:
    """Parsed intelligence for a single symbol."""
    symbol: str
    catalyst_score: int = 0          # 0-10
    catalyst_type: str = ""
    catalyst_summary: str = ""
    sentiment: float = 0.0           # -1.0 to +1.0
    risk_flags: List[str] = field(default_factory=list)
    conviction: str = "LOW"          # HIGH / MEDIUM / LOW / AVOID
    ttl_minutes: int = 480
    generated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def is_expired(self) -> bool:
        """Check if this intel entry has exceeded its TTL."""
        age = datetime.now(timezone.utc) - self.generated_at
        return age > timedelta(minutes=self.ttl_minutes)

    @property
    def score_contribution(self) -> float:
        """Calculate the net score contribution from this intel.

        Positive = boost, Negative = penalty.
        Capped at _AGENT_MAX_CONTRIBUTION in either direction.
        """
        if self.is_expired:
            return 0.0

        points = 0.0

        # Catalyst: 0-10 scaled to 0-_AGENT_CATALYST_WEIGHT
        if self.catalyst_score > 0:
            points += (self.catalyst_score / 10.0) * _AGENT_CATALYST_WEIGHT

        # Sentiment: -1..+1 scaled to -/+_AGENT_SENTIMENT_WEIGHT
        points += self.sentiment * _AGENT_SENTIMENT_WEIGHT

        # Risk flags: each flag penalizes
        points -= len(self.risk_flags) * _AGENT_RISK_PENALTY

        # Conviction bonus
        conviction_map = {"HIGH": 1.0, "MEDIUM": 0.5, "LOW": 0.0, "AVOID": -1.0}
        points += conviction_map.get(self.conviction, 0.0) * _AGENT_CONVICTION_BONUS

        # Cap contribution
        return max(-_AGENT_MAX_CONTRIBUTION, min(_AGENT_MAX_CONTRIBUTION, points))


@dataclass
class MarketContext:
    """Global market context from the agent."""
    regime: str = ""
    sector_rotation: str = ""
    macro_risk: str = ""
    vix_assessment: str = ""


def _load_intel() -> None:
    """Load or reload the agent intel file if stale."""
    global _last_load_ts, _cached_intel, _cached_market

    import time
    now = time.time()
    if now - _last_load_ts < _RELOAD_INTERVAL_S:
        return  # use cache

    _last_load_ts = now

    if not _INTEL_FILE.exists():
        if _cached_intel:  # file was deleted — clear cache
            log.info("Agent intel file removed — clearing cache")
            _cached_intel.clear()
            _cached_market = None
        return

    try:
        raw = json.loads(_INTEL_FILE.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("Agent intel file malformed or unreadable: %s", exc)
        return

    # Parse generated_at for TTL calculation
    try:
        gen_at = datetime.fromisoformat(raw["generated_at"].replace("Z", "+00:00"))
    except (KeyError, ValueError):
        gen_at = datetime.now(timezone.utc)

    # Parse symbols
    new_intel: Dict[str, SymbolIntel] = {}
    # symbols coerce: accept list-of-dicts, list-of-strings, or dict
    symbols_raw = raw.get("symbols", {})
    if isinstance(symbols_raw, list):
        coerced: Dict[str, dict] = {}
        for item in symbols_raw:
            if isinstance(item, dict) and "symbol" in item:
                coerced[str(item["symbol"])] = item
            elif isinstance(item, str):
                coerced[item] = {}
        symbols_raw = coerced
    elif not isinstance(symbols_raw, dict):
        log.debug("Agent intel 'symbols' field is not a dict — ignoring")
        symbols_raw = {}
    for sym, data in symbols_raw.items():
        if not isinstance(data, dict):
            continue
        sym_upper = sym.upper()
        new_intel[sym_upper] = SymbolIntel(
            symbol=sym_upper,
            catalyst_score=min(10, max(0, int(data.get("catalyst_score", 0)))),
            catalyst_type=str(data.get("catalyst_type", "")),
            catalyst_summary=str(data.get("catalyst_summary", ""))[:200],
            sentiment=max(-1.0, min(1.0, float(data.get("sentiment", 0.0)))),
            risk_flags=[str(f) for f in data.get("risk_flags", []) if isinstance(f, str)],
            conviction=str(data.get("conviction", "LOW")).upper(),
            ttl_minutes=int(data.get("ttl_minutes", 480)),
            generated_at=gen_at,
        )

    # Parse market context
    mc_raw = raw.get("market_context", {})
    if not isinstance(mc_raw, dict):
        mc_raw = {}
    new_market = MarketContext(
        regime=str(mc_raw.get("regime", "")),
        sector_rotation=str(mc_raw.get("sector_rotation", "")),
        macro_risk=str(mc_raw.get("macro_risk", "")),
        vix_assessment=str(mc_raw.get("vix_assessment", "")),
    )

    # Only log if data changed
    if set(new_intel.keys()) != set(_cached_intel.keys()):
        active = [s for s, si in new_intel.items() if not si.is_expired]
        expired = [s for s, si in new_intel.items() if si.is_expired]
        log.info(
            "Agent intel loaded: %d symbols (%d active, %d expired) from %s",
            len(new_intel), len(active), len(expired), _INTEL_FILE,
        )
        for sym, si in sorted(new_intel.items()):
            if not si.is_expired:
                log.info(
                    "  agent_intel sym=%s catalyst=%d(%s) sent=%.1f conv=%s risk=%s score=%.1f",
                    sym, si.catalyst_score, si.catalyst_type,
                    si.sentiment, si.conviction,
                    si.risk_flags or "none",
                    si.score_contribution,
                )

    _cached_intel = new_intel
    _cached_market = new_market


def get_symbol_intel(symbol: str) -> Optional[SymbolIntel]:
    """Get agent intelligence for a symbol, or None if unavailable/expired."""
    _load_intel()
    intel = _cached_intel.get(symbol.upper())
    if intel is None or intel.is_expired:
        return None
    return intel


def get_agent_score_boost(symbol: str) -> float:
    """Get the score adjustment for a symbol from agent intelligence.

    Returns a float that should be ADDED to the symbol's total score.
    Positive = boost, Negative = penalty, 0.0 = no agent data.
    """
    intel = get_symbol_intel(symbol)
    if intel is None:
        return 0.0
    return intel.score_contribution


def get_market_context() -> Optional[MarketContext]:
    """Get the agent's global market context, or None if unavailable."""
    _load_intel()
    return _cached_market


def get_all_active_intel() -> Dict[str, SymbolIntel]:
    """Get all non-expired agent intelligence entries."""
    _load_intel()
    return {
        sym: si for sym, si in _cached_intel.items()
        if not si.is_expired
    }
