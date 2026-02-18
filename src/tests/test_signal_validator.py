"""
Test harness — Phase 2 Signal Validator
Run:  python -m src.tests.test_signal_validator

Pulls metrics for SPY, NVDA, and a sample small-cap.  Prints all computed
metrics + pass/fail reasons.  No order placement.
"""

import sys
import os
import math

# Ensure project root is on path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from ib_insync import IB

from config.ib_config import IB_HOST, IB_PORT, IB_CLIENT_ID
from config.risk_limits import (
    MIN_ADV20_DOLLARS, MIN_ATR_PCT, MIN_VOLUME_ACCEL, MIN_RS_VS_SPY,
    PRICE_MIN, PRICE_MAX, PRICE_MAX_ALLOWLIST, MIN_UNIFIED_SCORE,
)
from src.signals.signal_validator import (
    compute_candidate_metrics,
    passes_hyper_swing_filters,
    fetch_spy_5m,
)
from src.quant.hyper_swing_filters import calc_momentum, quant_score, quant_score_components
from src.risk.regime import get_regime


def _print_metrics(label, m, hs_pass, hs_reason, cat_score=70.0, unified=None, components=None):
    print(f"\n{'='*60}")
    print(f"  {label}  ({m.symbol})")
    print(f"{'='*60}")
    print(f"  price          : ${m.price:.2f}")
    print(f"  atr14          : ${m.atr14:.4f}")
    print(f"  atr %          : {m.atr_percent*100:.2f}%")
    print(f"  atr expansion  : {m.atr_expansion:.2f}")
    print(f"  adv20 ($)      : ${m.adv20_dollars/1e6:.1f}M")
    print(f"  momentum 30m   : {m.momentum_30m*100:+.3f}%")
    print(f"  volume accel   : {m.volume_accel:.2f}")
    print(f"  RS30m Δ vs SPY : {m.rel_strength_vs_spy*100:+.2f}%")
    print(f"  VWAP           : ${m.vwap:.2f}" if math.isfinite(m.vwap) else "  VWAP           : n/a")
    print(f"  above VWAP     : {m.price_above_vwap}")
    print(f"  trend structure: {m.trend_structure_score:.0f}/100")
    print(f"  quant score    : {m.quant_score:.1f}/100")
    if components:
        print(
            "  quant norms    : "
            f"momentum_norm={components.get('momentum_norm', 0):.1f} "
            f"vol_norm={components.get('vol_norm', 0):.1f} "
            f"rs_norm={components.get('rs_norm', 0):.1f} "
            f"atr_exp_norm={components.get('atr_exp_norm', 0):.1f} "
            f"trend_norm={components.get('trend_norm', 0):.1f}"
        )
    if unified is not None:
        print(f"  unified score  : {unified:.1f}/100  (threshold={MIN_UNIFIED_SCORE})")
    print(f"  hyper-swing    : {'✅ PASS' if hs_pass else '❌ FAIL'}  {hs_reason}")
    print(f"  ok             : {m.ok}  {m.error}")


def _run_target_hyper_swing_profile() -> None:
    """
    Deterministic target-profile quant sanity check (no IB dependency).

    Uses a strong hyper-swing profile and asserts quant_score >= 70.
    """
    target_profile = {
        "momentum_30m": 0.012,          # +1.2%
        "volume_accel": 2.2,
        "rel_strength_vs_spy": 0.006,   # +0.6%
        "atr_expansion": 1.3,
        "trend_structure_score": 85.0,
    }

    target_quant = quant_score(target_profile)
    target_components = quant_score_components(target_profile)

    print(f"\n{'='*60}")
    print("  Target Hyper Swing Profile (deterministic)")
    print(f"{'='*60}")
    print("  profile        : mom=+1.20%, RS30mΔ=+0.60%, vol_accel=2.20, atr_exp=1.30, trend=85")
    print(f"  quant score    : {target_quant:.1f}/100  (expected >=70)")
    print(
        "  quant norms    : "
        f"momentum_norm={target_components.get('momentum_norm', 0):.1f} "
        f"vol_norm={target_components.get('vol_norm', 0):.1f} "
        f"rs_norm={target_components.get('rs_norm', 0):.1f} "
        f"atr_exp_norm={target_components.get('atr_exp_norm', 0):.1f} "
        f"trend_norm={target_components.get('trend_norm', 0):.1f}"
    )
    if target_quant > 99:
        print("  WARN: quant score saturated")

    assert target_quant >= 70.0, (
        f"Target hyper swing profile failed: got {target_quant:.1f}, expected >= 70.0."
    )
    print("  sanity         : ✅ PASS")


def main():
    print("=" * 60)
    print("  Phase 2 — Signal Validator Test Harness")
    print("=" * 60)

    _run_target_hyper_swing_profile()

    ib = IB()
    ib.connect(IB_HOST, IB_PORT, clientId=IB_CLIENT_ID + 50, timeout=10)

    # Pre-fetch SPY 5m once
    spy_df = fetch_spy_5m(ib)
    spy_mom = calc_momentum(spy_df, 30) if spy_df is not None else 0.0
    print(f"\n[SPY] 30m momentum: {spy_mom*100:+.3f}%")

    # Regime
    regime = get_regime(ib, breadth_pct=None)
    print(f"[REGIME] {regime.regime}  reasons={regime.reasons}")
    print(f"  spy_30m_return={regime.spy_30m_return*100:+.3f}%  spy_above_vwap={regime.spy_above_vwap}")

    # Build hyper-swing config
    hs_cfg = dict(
        PRICE_MIN=PRICE_MIN,
        PRICE_MAX=PRICE_MAX,
        MIN_ATR_PCT=MIN_ATR_PCT,
        MIN_ADV20_DOLLARS=MIN_ADV20_DOLLARS,
        MIN_VOLUME_ACCEL=MIN_VOLUME_ACCEL,
        MIN_RS_VS_SPY=MIN_RS_VS_SPY,
        REQUIRE_ABOVE_VWAP=True,
        PRICE_MAX_ALLOWLIST=PRICE_MAX_ALLOWLIST,
    )

    # Test symbols
    test_symbols = [
        ("SPY (benchmark)", "SPY"),
        ("NVDA (large-cap allowlist)", "NVDA"),
        ("SOFI (mid-cap swing)", "SOFI"),
    ]

    for label, sym in test_symbols:
        try:
            m = compute_candidate_metrics(ib, sym, spy_mom_30m=spy_mom)
            components = quant_score_components({
                "momentum_30m": m.momentum_30m,
                "volume_accel": m.volume_accel,
                "rel_strength_vs_spy": m.rel_strength_vs_spy,
                "atr_expansion": m.atr_expansion,
                "trend_structure_score": m.trend_structure_score,
            })
            hs_pass, hs_reason = passes_hyper_swing_filters(m, config=hs_cfg)
            cat = 70.0  # mock catalyst score
            unified = 0.60 * cat + 0.40 * m.quant_score
            _print_metrics(
                label,
                m,
                hs_pass,
                hs_reason,
                cat_score=cat,
                unified=unified,
                components=components,
            )
        except Exception as e:
            print(f"\n[ERROR] {sym}: {e}")

    ib.disconnect()
    print("\n✅ Test harness complete. No orders placed.")


if __name__ == "__main__":
    main()
