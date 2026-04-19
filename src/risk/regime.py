"""
risk/regime.py

Thin delegation layer — all real logic lives in src/signals/regime.py.
This file exists only for backwards compatibility with any imports.
"""
from src.signals.regime import (
    get_regime as _get_regime_base,
)
from src.signals.regime import (  # noqa: F401
    update_index,
    last_regime,
    RegimeState,
    TREND_UP,
    TREND_DOWN,
    CHOP,
    PANIC,
    STRATEGY_GATE,
    RISK_MULT,
)

_paper_first_seen_ts = None

# Compatibility wrapper for callers passing ib and breadth_pct
def get_regime(*args, **kwargs):
    """Accept optional ib and breadth_pct params for backwards compat; ignore them.
    
    PAPER mode: relax insufficient_data gate after 10+ bars accumulate.
    """
    # Extract symbol if first arg is a string, otherwise use default
    symbol = args[0] if args and isinstance(args[0], str) else ""
    result = _get_regime_base(symbol)
    
    # PAPER mode fallback: avoid indefinite insufficient_data gating in live loop.
    try:
        import time
        from config.runtime import is_paper
        global _paper_first_seen_ts
        if _paper_first_seen_ts is None:
            _paper_first_seen_ts = time.time()
        if is_paper and result.regime == "CHOP" and "insufficient_data" in result.reasons:
            # Access the internal state to check bar count
            import src.signals.regime as regime_module
            st = regime_module._get_state(symbol or "SPY")
            # Prefer bar-based relax, then time-based fallback if bars are not fed.
            if len(st.closes) >= 10 or (time.time() - _paper_first_seen_ts) >= 300:
                return RegimeState(
                    regime="CHOP",
                    confidence=0.5,
                    ema_fast=result.ema_fast,
                    ema_slow=result.ema_slow,
                    slope=result.slope,
                    atr_pct=result.atr_pct,
                    atr_baseline_pct=result.atr_baseline_pct,
                    vol_regime=result.vol_regime,
                    reasons=["chop_paper_mode"],
                )
    except (ImportError, AttributeError):
        # If imports fail, just return the original result
        pass
    
    return result

# Backwards compat alias used by live_loop_10s
def get_market_regime(*args, **kwargs):
    return get_regime()


# ── Regime gate decision (throttle-based, not binary) ────────────────

def regime_gate_decision(
    symbol_score: float,
    mode_fit: str,
    regime: str,
    spread_bps: float,
) -> tuple[bool, str]:
    """Decide whether to allow entry given regime state.

    Returns (allowed, reason).  Uses continuous throttle multipliers
    from ``regime_throttle`` so that high-quality names can trade
    through CHOP with reduced sizing, and probe trades are permitted
    even in worst regimes.
    """
    from src.risk.regime_throttle import get_throttle, probe_budget

    if regime == "HALT":
        return False, "hard_block_halt"

    throttle = get_throttle(regime, "REGULAR")

    # Apply throttle score_mult to symbol_score
    adjusted_score = symbol_score * throttle.score_mult

    if regime == "CHOP":
        # Override for high-quality names with strong fit
        if mode_fit == "HIGH" and adjusted_score >= 8.4 and spread_bps <= 25:
            return True, "chop_throttle_high_fit"
        if mode_fit == "MED" and adjusted_score >= 9.6 and spread_bps <= 20:
            return True, "chop_throttle_med_fit"
        # Probe fallback: allow micro-size trade for data collection
        if throttle.probe_allowed and probe_budget.can_probe(throttle):
            return True, "chop_probe_allowed"
        return False, "throttled_regime_chop"

    if regime == "PANIC":
        # Only probes in PANIC
        if throttle.probe_allowed and probe_budget.can_probe(throttle):
            return True, "panic_probe_allowed"
        if adjusted_score >= 6.0 and mode_fit == "HIGH":
            return True, "panic_throttle_high_fit"
        return False, "throttled_regime_panic"

    return True, "regime_ok"