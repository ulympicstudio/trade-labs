#!/usr/bin/env python3
"""
Export top U.T.S. trade candidates to a clean JSON file for external analysis.

Reads from the latest playbook/signal output if available,
otherwise generates mock data so the script always produces output.

Output: data/uts_summary.json + terminal print

Usage:
    python scripts/export_uts_summary.py
    python scripts/export_uts_summary.py --top 10

No IB connection required. No execution/risk/order imports.
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

# Ensure project root on path
_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

PLAYBOOK_PATH = _ROOT / "data" / "playbook_latest.json"
OUTPUT_PATH = _ROOT / "data" / "uts_summary.json"


# ── Data loading ─────────────────────────────────────────────────

def _load_playbook(path: Path) -> Optional[List[Dict[str, Any]]]:
    """Load drafts from playbook_latest.json if it exists and is recent."""
    if not path.exists():
        return None
    try:
        with open(path) as f:
            data = json.load(f)
        drafts = data.get("drafts", [])
        if not drafts:
            return None
        return drafts
    except (json.JSONDecodeError, KeyError, TypeError):
        return None


def _extract_candidate(draft: Dict[str, Any]) -> Dict[str, Any]:
    """Map a playbook draft to the export schema."""
    symbol = draft.get("symbol", "")
    price = draft.get("entry", 0.0)
    rvol = draft.get("rvol", 0.0)

    # volume_spike_pct: derive from rvol (relative volume).
    # rvol=1.5 means 50% above average → volume_spike_pct=50.0
    volume_spike_pct = round((rvol - 1.0) * 100, 1) if rvol else 0.0

    return {
        "symbol": symbol,
        "price": round(price, 2),
        "rsi": None,                      # not available in playbook; placeholder for future
        "volume_spike_pct": volume_spike_pct,
        "premarket_change_pct": None,      # not available offline; placeholder for future
        "confidence": round(draft.get("confidence", 0.0), 3),
        "total_score": round(draft.get("total_score", 0.0), 1),
        "quality": draft.get("quality", ""),
        "latest_headline": draft.get("latest_headline", ""),
    }


def _load_candidates(top_n: int) -> tuple[List[Dict[str, Any]], str]:
    """Try real data, fall back to mock. Returns (candidates, source)."""
    drafts = _load_playbook(PLAYBOOK_PATH)
    if drafts:
        # Sort by total_score desc, take top N
        drafts.sort(key=lambda d: d.get("total_score", 0.0), reverse=True)
        candidates = [_extract_candidate(d) for d in drafts[:top_n]]
        return candidates, "playbook_latest.json"

    # Fallback: mock data
    return _mock_candidates(), "mock"


def _mock_candidates() -> List[Dict[str, Any]]:
    """Generate 3 mock symbols so the script always runs."""
    return [
        {
            "symbol": "NVDA",
            "price": 142.50,
            "rsi": 62.3,
            "volume_spike_pct": 35.0,
            "premarket_change_pct": 1.8,
            "confidence": 0.85,
            "total_score": 18.0,
            "quality": "HIGH",
            "latest_headline": "(mock) NVDA data center revenue beats expectations",
        },
        {
            "symbol": "AAPL",
            "price": 198.20,
            "rsi": 55.1,
            "volume_spike_pct": 12.0,
            "premarket_change_pct": 0.4,
            "confidence": 0.60,
            "total_score": 14.0,
            "quality": "MEDIUM",
            "latest_headline": "(mock) Apple announces new product event",
        },
        {
            "symbol": "TSLA",
            "price": 178.90,
            "rsi": 48.7,
            "volume_spike_pct": 58.0,
            "premarket_change_pct": -1.2,
            "confidence": 0.52,
            "total_score": 12.0,
            "quality": "MEDIUM",
            "latest_headline": "(mock) Tesla delivery numbers in focus",
        },
    ]


# ── Output ───────────────────────────────────────────────────────

def build_summary(top_n: int = 10) -> Dict[str, Any]:
    candidates, source = _load_candidates(top_n)
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "source": source,
        "count": len(candidates),
        "top_candidates": candidates,
    }


def export_summary(top_n: int = 10) -> Path:
    summary = build_summary(top_n)

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_PATH, "w") as f:
        json.dump(summary, f, indent=2, default=str)
        f.write("\n")

    return OUTPUT_PATH


# ── CLI ──────────────────────────────────────────────────────────

def main():
    top_n = 10
    for i, arg in enumerate(sys.argv[1:], 1):
        if arg == "--top" and i < len(sys.argv) - 1:
            try:
                top_n = int(sys.argv[i + 1])
            except ValueError:
                pass

    out = export_summary(top_n)

    with open(out) as f:
        payload = json.load(f)

    print(json.dumps(payload, indent=2))
    print(f"\nSaved to {out}")
    print(f"Source: {payload['source']} | Candidates: {payload['count']}")


if __name__ == "__main__":
    main()
