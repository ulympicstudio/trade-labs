#!/usr/bin/env python3
"""Smoke test — verifies all Upgrade A-L imports, env-var parsing, and function signatures.

Usage:
    .venv/bin/python scripts/smoke_upgrade.py

Exits 0 if everything is importable and correctly wired.
No broker, bus, or network connectivity required.
"""

from __future__ import annotations

import importlib
import os
import sys
import traceback

# Ensure project root is on sys.path
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

_PASS = 0
_FAIL = 0


def check(label: str, fn):
    global _PASS, _FAIL
    try:
        fn()
        _PASS += 1
        print(f"  ✅  {label}")
    except Exception as exc:
        _FAIL += 1
        print(f"  ❌  {label}: {exc}")
        traceback.print_exc()


# ── E: News category weighting ─────────────────────────────────────
def test_event_score_categories():
    from src.signals.event_score import _CATEGORY_WEIGHTS, _CAT_MAX_BONUS
    assert isinstance(_CATEGORY_WEIGHTS, dict), "_CATEGORY_WEIGHTS not a dict"
    assert len(_CATEGORY_WEIGHTS) >= 5, f"Only {len(_CATEGORY_WEIGHTS)} categories"
    assert isinstance(_CAT_MAX_BONUS, (int, float))


# ── G (regime): Vol regime detection ───────────────────────────────
def test_vol_regime():
    from src.signals.regime import VOL_LOW, VOL_NORMAL, VOL_HIGH, _classify_vol, RegimeState
    assert VOL_LOW == "LOW"
    assert VOL_NORMAL == "NORMAL"
    assert VOL_HIGH == "HIGH"
    assert _classify_vol(0.02, 0.02) == VOL_NORMAL
    assert _classify_vol(0.04, 0.02) == VOL_HIGH
    assert _classify_vol(0.01, 0.02) == VOL_LOW
    rs = RegimeState()
    assert hasattr(rs, "vol_regime")


# ── D: Strategy gate map ──────────────────────────────────────────
def test_strategy_gate():
    from src.signals.regime import STRATEGY_GATE
    assert "TREND_UP" in STRATEGY_GATE
    assert "PANIC" in STRATEGY_GATE
    assert "consensus_news" in STRATEGY_GATE["PANIC"]


# ── J: KillSwitch additions ───────────────────────────────────────
def test_kill_switch():
    from src.risk.kill_switch import update_atr_spike, status_summary
    # Should not raise when called with floats
    update_atr_spike(0.02, 0.01)
    ss = status_summary()
    assert "atr_spike" in ss, "atr_spike missing from status_summary"


# ── B: Consensus RSI bypass env vars ──────────────────────────────
def test_consensus_bypass_vars():
    mod = importlib.import_module("src.arms.signal_main")
    assert hasattr(mod, "_CONSENSUS_BYPASS_RSI"), "Missing _CONSENSUS_BYPASS_RSI"
    assert hasattr(mod, "_CONSENSUS_BYPASS_MIN_PROVIDERS"), "Missing _CONSENSUS_BYPASS_MIN_PROVIDERS"


# ── D: Regime gate env var ────────────────────────────────────────
def test_regime_gate_var():
    mod = importlib.import_module("src.arms.signal_main")
    assert hasattr(mod, "_REGIME_GATE_ENABLED"), "Missing _REGIME_GATE_ENABLED"


# ── F: Adaptive spread env vars ──────────────────────────────────
def test_adaptive_spread():
    mod = importlib.import_module("src.arms.signal_main")
    assert hasattr(mod, "_SPREAD_ATR_MULT")
    assert hasattr(mod, "_SPREAD_MIN")
    assert hasattr(mod, "_SPREAD_MAX")
    assert hasattr(mod, "_adaptive_spread_limit")


# ── I: Session awareness ─────────────────────────────────────────
def test_session_awareness():
    mod = importlib.import_module("src.arms.signal_main")
    assert hasattr(mod, "_SESSION_AWARE")
    assert hasattr(mod, "_get_session_phase")
    phase = mod._get_session_phase()
    assert phase in (
        "PREMARKET", "OPEN", "MIDDAY", "POWER_HOUR",
        "RTH", "AFTERHOURS", "OFF_HOURS",
    ), f"Unexpected phase: {phase}"


# ── A: EventScore sizing env vars ────────────────────────────────
def test_eventsize_vars():
    mod = importlib.import_module("src.arms.risk_main")
    assert hasattr(mod, "_EVENTSIZE_ENABLED")
    assert hasattr(mod, "_EVENTSIZE_BASE")
    assert hasattr(mod, "_EVENTSIZE_MIN")
    assert hasattr(mod, "_EVENTSIZE_MAX")


# ── G (risk): Vol stop/qty multipliers ───────────────────────────
def test_vol_multipliers():
    mod = importlib.import_module("src.arms.risk_main")
    assert hasattr(mod, "_VOL_STOP_MULT")
    assert hasattr(mod, "_VOL_QTY_MULT")


# ── H: PAPER slippage env var ────────────────────────────────────
def test_slippage():
    mod = importlib.import_module("src.arms.execution_main")
    assert hasattr(mod, "_SLIPPAGE_MULT")
    assert isinstance(mod._SLIPPAGE_MULT, float)


# ── C: Squeeze watchlist env vars ────────────────────────────────
def test_squeeze_vars():
    mod = importlib.import_module("src.arms.ingest_main")
    assert hasattr(mod, "_SQUEEZE_UNIVERSE_TOP_N")
    assert hasattr(mod, "_SQUEEZE_MIN_SCORE")


# ── Compile all modified files ───────────────────────────────────
def test_compile_all():
    import py_compile
    files = [
        "src/signals/event_score.py",
        "src/signals/regime.py",
        "src/risk/kill_switch.py",
        "src/arms/signal_main.py",
        "src/arms/risk_main.py",
        "src/arms/execution_main.py",
        "src/arms/ingest_main.py",
    ]
    for f in files:
        py_compile.compile(f, doraise=True)


def main():
    print("=" * 56)
    print("  Trade Labs — Upgrade A-L Smoke Test")
    print("=" * 56)
    print()

    checks = [
        ("E: Category weights importable", test_event_score_categories),
        ("G: Vol regime constants & classifier", test_vol_regime),
        ("D: Strategy gate map", test_strategy_gate),
        ("J: KillSwitch update_atr_spike + status", test_kill_switch),
        ("B: Consensus bypass RSI env vars", test_consensus_bypass_vars),
        ("D: Regime gate env var", test_regime_gate_var),
        ("F: Adaptive spread env vars + fn", test_adaptive_spread),
        ("I: Session awareness phase fn", test_session_awareness),
        ("A: EventScore sizing env vars", test_eventsize_vars),
        ("G: Vol stop/qty multipliers", test_vol_multipliers),
        ("H: PAPER slippage mult", test_slippage),
        ("C: Squeeze watchlist env vars", test_squeeze_vars),
        ("Compile: all modified files", test_compile_all),
    ]

    for label, fn in checks:
        check(label, fn)

    print()
    print(f"Results: {_PASS} passed, {_FAIL} failed")
    print("=" * 56)
    sys.exit(1 if _FAIL > 0 else 0)


if __name__ == "__main__":
    main()
