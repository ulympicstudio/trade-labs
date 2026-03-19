#!/usr/bin/env python3
"""
Test script for Perplexity news intelligence.

Usage:
    python scripts/test_perplexity_news.py
    python scripts/test_perplexity_news.py TSLA MSFT
"""

import json
import sys
import os

# Ensure project root is on path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from src.intelligence.perplexity_client import (
    analyze_symbol_news,
    analyze_batch,
    SymbolNewsResult,
)


def print_result(r: SymbolNewsResult) -> None:
    print(f"\n{'='*60}")
    print(f"  {r.symbol}")
    print(f"{'='*60}")
    if r.error:
        print(f"  ERROR: {r.error}")
        print(f"  Latency: {r.latency_ms}ms")
        return

    print(f"  Catalyst:   {r.catalyst_type}")
    print(f"  Sentiment:  {r.sentiment_score:+.3f}  ", end="")
    if r.is_bullish:
        print("(BULLISH)")
    elif r.is_bearish:
        print("(BEARISH)")
    else:
        print("(NEUTRAL)")
    print(f"  Summary:    {r.summary}")
    if r.risk_flags:
        print(f"  Risk Flags: {', '.join(r.risk_flags)}")
    if r.citations:
        print(f"  Sources:    {len(r.citations)} citation(s)")
        for c in r.citations[:3]:
            print(f"              - {c}")
    print(f"  Latency:    {r.latency_ms}ms")

    print(f"\n  JSON payload:")
    print(f"  {json.dumps(r.to_dict(), indent=2)}")


def main():
    # Check API key
    key = os.environ.get("PERPLEXITY_API_KEY", "").strip()
    if not key:
        print("ERROR: PERPLEXITY_API_KEY not found in environment.")
        print("Add it to your .env file:  PERPLEXITY_API_KEY=pplx-...")
        sys.exit(1)

    # Use CLI args or defaults
    symbols = sys.argv[1:] if len(sys.argv) > 1 else ["NVDA", "AAPL", "ASTS"]

    print(f"Perplexity News Intelligence Test")
    print(f"Symbols: {', '.join(symbols)}")
    print(f"API key: ...{key[-6:]}")

    results = analyze_batch(symbols, delay=0.5)

    for r in results:
        print_result(r)

    # Summary table
    print(f"\n{'='*60}")
    print(f"  SUMMARY")
    print(f"{'='*60}")
    print(f"  {'Symbol':<8} {'Catalyst':<14} {'Sentiment':>10} {'Latency':>8}")
    print(f"  {'-'*8} {'-'*14} {'-'*10} {'-'*8}")
    for r in results:
        sent_str = f"{r.sentiment_score:+.3f}" if not r.error else "ERR"
        cat_str = r.catalyst_type if not r.error else r.error[:12]
        lat_str = f"{r.latency_ms}ms"
        print(f"  {r.symbol:<8} {cat_str:<14} {sent_str:>10} {lat_str:>8}")

    ok = sum(1 for r in results if not r.error)
    print(f"\n  {ok}/{len(results)} succeeded")


if __name__ == "__main__":
    main()
