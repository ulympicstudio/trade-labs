#!/usr/bin/env python3
"""
Integration-style smoke test for bracket math in SIM mode.

Key points:
- Does NOT place real orders.
- Does NOT prompt for input (pytest-friendly).
- Uses IB if available; otherwise skips.
"""

import os
import math
import pytest

# Force safe defaults
os.environ["TRADE_LABS_ARMED"] = "0"  # SIM mode - no actual orders
os.environ["TRADE_LABS_MODE"] = "PAPER"
os.environ["TRADE_LABS_EXECUTION_BACKEND"] = "SIM"


def test_bracket_live_sim():
    print("\n" + "=" * 70)
    print("BRACKET ORDER TEST - Corrected Structure (SIM Mode)")
    print("=" * 70 + "\n")

    # Import inside test so env vars above apply
    from src.broker.ib_session import get_ib
    from src.signals.market_scanner import scan_us_most_active, get_quote
    from src.signals.score_candidates import score_candidates

    # Connect to IB (or skip if not running)
    try:
        ib = get_ib()
    except Exception as e:
        pytest.skip(f"IB not available (TWS/IB Gateway not running): {e}")

    try:
        # 1) Scan candidates
        print("[SCAN] Fetching market candidates...")
        candidates = scan_us_most_active(ib, limit=30)
        print(f"[SCAN] Found {len(candidates)} candidates\n")

        if not candidates:
            pytest.skip("No candidates returned from scanner (market closed or scanner empty).")

        # 2) Score candidates (use IB explicitly so wrapper doesn't create its own connection)
        print("[SCORE] Scoring candidates...")
        scored = score_candidates(ib, candidates, top_n=5)
        print(f"[SCORE] Got {len(scored)} top candidates\n")

        if not scored:
            pytest.skip("No scored candidates returned.")

        top = scored[0]

        # ScoredCandidate fields:
        # ['symbol', 'rank', 'momentum_pct_60m', 'atr14', 'score', 'reason']
        symbol = top.symbol
        atr = float(top.atr14 or 0.0)
        momentum = float(top.momentum_pct_60m or 0.0)

        # 3) Fetch quote for price
        bid, ask, last = get_quote(ib, symbol)
        px = (
            last
            if last is not None
            else ((bid + ask) / 2.0 if (bid is not None and ask is not None) else None)
        )

        if px is None:
            pytest.skip(f"No quote available for {symbol} (bid/ask/last all None).")

        px = float(px)

        # IB can return NaN/inf (often due to missing real-time market data subscription).
        # If px is NaN/inf, skip instead of failing the suite.
        if not math.isfinite(px) or px <= 0:
            pytest.skip(
                f"Price not usable for {symbol} (px={px}). Likely delayed/unsubscribed market data."
            )

        print(f"[TEST] Top candidate: {symbol}")
        print(f"  Price (quote): ${px:.2f}")
        print(f"  ATR14:         ${atr:.2f}")
        print(f"  Momentum60m:   {momentum:.2%}")
        print(f"  Score:         {top.score:.2f}")
        if getattr(top, "reason", None):
            print(f"  Reason:        {top.reason}")
        print()

        # 4) Bracket math (same style as live_loop)
        ENTRY_OFFSET_PCT = 0.0005
        STOP_LOSS_R = 2.0
        TRAIL_ATR_MULT = 1.2
        RISK_PER_TRADE = 0.005  # 0.5%

        # Guard against bad ATR
        if atr <= 0:
            pytest.skip(f"ATR14 not usable for {symbol} (atr14={atr}).")

        entry = px * (1 - ENTRY_OFFSET_PCT)
        stop_dist = atr
        stop_loss = entry - (STOP_LOSS_R * stop_dist)
        trail_amt = atr * TRAIL_ATR_MULT

        # Assume $1M account for qty calculation (test only)
        account_equity = 1_000_000
        risk_dollars = account_equity * RISK_PER_TRADE
        qty = int(risk_dollars // stop_dist)

        print(f"[BRACKET] Structure for {symbol}:")
        print(f"  Entry (BUY LMT):      ${entry:.2f}")
        print(f"  Stop Loss (SELL STP): ${stop_loss:.2f}")
        print(f"  Trail Amount:         ${trail_amt:.2f}")
        print()
        print("[MATH]")
        print(f"  Risk per share:       ${entry - stop_loss:.2f}")
        print(f"  Risk per trade:       ${risk_dollars:,.0f}")
        print(f"  Quantity:             {qty} shares\n")

        # 5) Assertions (sanity checks)
        assert qty > 0
        assert entry > 0
        assert stop_loss < entry
        assert trail_amt > 0

    finally:
        try:
            if ib.isConnected():
                ib.disconnect()
        except Exception:
            pass