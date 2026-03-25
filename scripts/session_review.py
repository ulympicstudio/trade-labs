#!/usr/bin/env python3
"""
session_review.py — U.T.S. Post-Session Review
Run: python3 scripts/session_review.py
"""
import json, os, sys, pathlib
from datetime import datetime

# ── Load scorecard state ──────────────────────────────────────────
STATE_PATH = pathlib.Path(os.environ.get(
    "TL_SC_PERSIST_PATH",
    pathlib.Path(__file__).parent.parent / "data/scorecard_state.json"
))

if not STATE_PATH.exists():
    print("No scorecard state found. Run a paper session first.")
    sys.exit(0)

records = json.loads(STATE_PATH.read_text())
if not records:
    print("Scorecard state is empty — no closed trades yet.")
    sys.exit(0)

# ── Aggregate ─────────────────────────────────────────────────────
total     = len(records)
wins      = [r for r in records if r["pnl"] > 0]
losses    = [r for r in records if r["pnl"] < 0]
gross_pnl = sum(r["pnl"] for r in records)
win_rate  = len(wins) / total * 100 if total else 0
r_mults   = [r["r_multiple"] for r in records if r.get("r_multiple")]
avg_r     = sum(r_mults) / len(r_mults) if r_mults else 0.0

# Max drawdown on cumulative PnL
cum, peak, max_dd = 0.0, 0.0, 0.0
for r in records:
    cum += r["pnl"]
    peak = max(peak, cum)
    max_dd = max(max_dd, peak - cum)

# Best / worst trade
best  = max(records, key=lambda r: r["pnl"])
worst = min(records, key=lambda r: r["pnl"])

# Per-bucket breakdown
buckets = {}
for r in records:
    b = r.get("playbook") or "unknown"
    if b not in buckets:
        buckets[b] = {"trades": 0, "wins": 0, "pnl": 0.0, "r": []}
    buckets[b]["trades"] += 1
    if r["pnl"] > 0:
        buckets[b]["wins"] += 1
    buckets[b]["pnl"] += r["pnl"]
    if r.get("r_multiple"):
        buckets[b]["r"].append(r["r_multiple"])

# ── Print ─────────────────────────────────────────────────────────
W = 54
print("=" * W)
print("  U.T.S. SESSION REVIEW  —  " + datetime.now().strftime("%Y-%m-%d %H:%M"))
print("=" * W)
print(f"  Trades : {total}   Wins: {len(wins)}   Losses: {len(losses)}")
print(f"  Win Rate: {win_rate:.1f}%   Avg R: {avg_r:.2f}x")
sign = "+" if gross_pnl >= 0 else ""
print(f"  Gross PnL: {sign}${gross_pnl:.2f}   Max DD: ${max_dd:.2f}")
print()

print("  BUCKET BREAKDOWN")
print(f"  {'Bucket':<20} {'T':>4} {'WR%':>6} {'AvgR':>6} {'PnL':>9}")
print("  " + "-" * 48)
for b, s in sorted(buckets.items(), key=lambda x: -x[1]["pnl"]):
    wr  = s["wins"] / s["trades"] * 100 if s["trades"] else 0
    ar  = sum(s["r"]) / len(s["r"]) if s["r"] else 0.0
    sgn = "+" if s["pnl"] >= 0 else ""
    print(f"  {b:<20} {s['trades']:>4} {wr:>5.1f}% {ar:>6.2f}x {sgn}${s['pnl']:>7.2f}")

print()
b_sym  = best.get("symbol","?")
b_pnl  = best["pnl"]
b_r    = best.get("r_multiple", 0)
b_bkt  = best.get("playbook","?")
w_sym  = worst.get("symbol","?")
w_pnl  = worst["pnl"]
w_r    = worst.get("r_multiple", 0)
w_bkt  = worst.get("playbook","?")
print(f"  BEST  : {b_sym:<6} +${b_pnl:.2f}  R={b_r:.2f}x  ({b_bkt})")
print(f"  WORST : {w_sym:<6}  ${w_pnl:.2f}  R={w_r:.2f}x  ({w_bkt})")
print("=" * W)
