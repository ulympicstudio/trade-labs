"""Risk decomposition — configurable thresholds and shadow-approval telemetry.

Centralises every risk lever in one place so they can be tuned via env
vars without code edits.  Also provides ``shadow_check()`` which counts
how many intents would have passed under +20% looser limits.

Usage::

    from src.risk.risk_decomposition import risk_config, shadow_check

    cfg = risk_config()   # read-once per process startup
    shadow_check(intent, final_qty, final_risk, cfg)  # counts near-misses

All thresholds are env-overridable via ``TL_RISKD_*`` prefix.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

_log = logging.getLogger("trade_labs.risk_decomposition")


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except (ValueError, TypeError):
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except (ValueError, TypeError):
        return default


@dataclass(frozen=True)
class RiskConfig:
    """All tunable risk levers in one place.

    Read once at startup; arms reference this instead of scattered
    ``os.environ.get(...)`` calls.
    """
    # Per-trade
    max_risk_usd_per_trade: float
    max_notional_per_trade: float
    min_notional_per_trade: float
    min_r_mult: float            # minimum reward:risk ratio
    max_spread_bps: float        # max spread in basis points

    # Per-day
    daily_dd_limit_usd: float    # max daily drawdown before kill
    daily_dd_limit_pct: float    # max daily DD as % of equity
    max_trades_per_day: int

    # Per-symbol
    max_exposure_per_symbol_pct: float  # max % of equity in one name
    max_positions_per_symbol: int

    # Per-sector
    max_sector_exposure_pct: float

    # Portfolio-level  
    max_open_positions: int
    max_portfolio_heat_pct: float   # total risk as % of equity

    # Probe sizing
    probe_risk_pct: float          # risk fraction for MIN_PROBE mode
    reduced_risk_pct: float        # risk fraction for REDUCED mode

    # Shadow approval looser factor
    shadow_looseness: float        # e.g. 1.2 = test with +20% limits


def risk_config() -> RiskConfig:
    """Build RiskConfig from env vars + defaults."""
    return RiskConfig(
        max_risk_usd_per_trade=_env_float("TL_RISKD_MAX_RISK_USD", 100.0),
        max_notional_per_trade=_env_float("TL_RISKD_MAX_NOTIONAL", 25000.0),
        min_notional_per_trade=_env_float("TL_RISKD_MIN_NOTIONAL", 100.0),
        min_r_mult=_env_float("TL_RISKD_MIN_R_MULT", 1.5),
        max_spread_bps=_env_float("TL_RISKD_MAX_SPREAD_BPS", 30.0),
        daily_dd_limit_usd=_env_float("TL_RISKD_DAILY_DD_USD", 500.0),
        daily_dd_limit_pct=_env_float("TL_RISKD_DAILY_DD_PCT", 0.5),
        max_trades_per_day=_env_int("TL_RISKD_MAX_TRADES_DAY", 20),
        max_exposure_per_symbol_pct=_env_float("TL_RISKD_MAX_SYMBOL_PCT", 5.0),
        max_positions_per_symbol=_env_int("TL_RISKD_MAX_POS_SYMBOL", 1),
        max_sector_exposure_pct=_env_float("TL_RISKD_MAX_SECTOR_PCT", 25.0),
        max_open_positions=_env_int("TL_RISKD_MAX_OPEN_POS", 5),
        max_portfolio_heat_pct=_env_float("TL_RISKD_MAX_HEAT_PCT", 2.0),
        probe_risk_pct=_env_float("TL_RISKD_PROBE_RISK_PCT", 0.05),
        reduced_risk_pct=_env_float("TL_RISKD_REDUCED_RISK_PCT", 0.10),
        shadow_looseness=_env_float("TL_RISKD_SHADOW_LOOSE", 1.2),
    )


def shadow_check(
    *,
    final_qty: int,
    final_risk_usd: float,
    reject_reason: str,
    cfg: RiskConfig,
) -> bool:
    """Return True if the intent *would have* passed with +N% looser limits.

    This only checks the specific constraint that caused rejection:
    if the reject reason is ``risk_budget`` we test if the risk would
    fit under ``max_risk * looseness``, etc.

    Does NOT mutate any state — caller must call
    ``risk_telemetry.record_shadow_approval()`` if True.
    """
    L = cfg.shadow_looseness

    if reject_reason in ("risk_budget", "risk_cap"):
        return final_risk_usd <= cfg.max_risk_usd_per_trade * L

    if reject_reason in ("max_positions", "portfolio_heat"):
        # Can't numerically loosen a count — treat as shadow-pass
        return True

    if reject_reason == "notional_small":
        return True  # loosening min notional always passes

    if reject_reason in ("spread_bad", "vol_spike"):
        return True  # near-miss on spread/vol is always interesting

    if reject_reason == "regime_throttle":
        return final_qty >= 1 and final_risk_usd > 0

    return False
