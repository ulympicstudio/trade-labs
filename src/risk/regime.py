"""
risk/regime.py

Thin delegation layer — all real logic lives in src/signals/regime.py.
This file exists only for backwards compatibility with any imports.
"""
from src.signals.regime import (  # noqa: F401
    get_regime,
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

# Backwards compat alias used by live_loop_10s
def get_market_regime(*args, **kwargs):
    return get_regime()