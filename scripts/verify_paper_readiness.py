#!/usr/bin/env python3
"""
U.T.S. Paper-Readiness Certification Verifier

Parses a captured log from paper_readiness_cert.sh and reports
pass/fail for each required lifecycle metric.

Usage:
    python scripts/verify_paper_readiness.py [logfile]

Default logfile: /tmp/tl_paper_cert.log
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

DEFAULT_LOG = "/tmp/tl_paper_cert.log"

# ── Required metrics: (label, pattern, min_count) ────────────────────
# pattern is a plain substring unless it starts with "re:" (regex)
REQUIRED_METRICS = [
    ("paper_cert_start",       "paper_cert_start",         1),
    ("Trade APPROVED",         "Trade APPROVED",           1),
    ("PAPER_FILL",             "PAPER_FILL",               5),
    ("exit_register",          "exit_register ",           1),
    ("exit_decision",          "exit_decision ",           1),
    ("exit_trim|full|time",    "re:exit_trim |exit_full |exit_time_stop ", 1),
    ("exit_action_executed",   "exit_action_executed ",    1),
    ("attrib_open",            "attrib_open ",             1),
    ("attrib_fill",            "attrib_fill ",             1),
    ("attrib_close",           "attrib_close ",            1),
    ("scorecard_open",         "scorecard_open ",          1),
    ("scorecard_close",        "scorecard_close ",         1),
    ("playbook_score_update",  "playbook_score_update ",   1),
    ("open_positions",         "open_positions ",          1),
    ("exit_watchlist",         "exit_watchlist ",          1),
]

# ── Heartbeat checks: each arm must have at least 1 heartbeat ───────
HEARTBEAT_ARMS = ["ingest", "signal", "risk", "execution", "monitor"]

# ── Zero-count checks: these must be absent ─────────────────────────
ZERO_CHECKS = [
    ("Traceback",     "Traceback"),
    ("ERROR",         " ERROR "),
    ("CRITICAL",      " CRITICAL "),
    ("Arm crashed",   "re:Arm .* crashed"),
]


def count_pattern(lines: list[str], pattern: str) -> int:
    """Count lines matching a pattern (plain substring or regex)."""
    if pattern.startswith("re:"):
        rx = re.compile(pattern[3:])
        return sum(1 for line in lines if rx.search(line))
    return sum(1 for line in lines if pattern in line)


def count_heartbeat(lines: list[str], arm: str) -> int:
    """Count heartbeat evidence for a specific arm."""
    rx = re.compile(
        rf"heartbeat.*arm={arm}|{arm}.*heartbeat|arm_status.*{arm}|"
        rf"\[INFO \] {arm} \| heartbeat"
    )
    return sum(1 for line in lines if rx.search(line))


def main() -> int:
    logfile = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_LOG
    path = Path(logfile)

    if not path.exists():
        print(f"ERROR: Log file not found: {logfile}")
        print("Run scripts/paper_readiness_cert.sh first.")
        return 1

    lines = path.read_text().splitlines()
    total_lines = len(lines)

    print("=" * 60)
    print("  U.T.S. Paper-Readiness Certification Verifier")
    print(f"  Log: {logfile}  ({total_lines} lines)")
    print("=" * 60)
    print()

    passed = 0
    failed = 0
    results: list[tuple[str, str, int, int]] = []

    # ── Required lifecycle metrics ───────────────────────────────
    print("LIFECYCLE METRICS")
    print("-" * 60)
    for label, pattern, min_count in REQUIRED_METRICS:
        count = count_pattern(lines, pattern)
        ok = count >= min_count
        status = "PASS" if ok else "FAIL"
        if ok:
            passed += 1
        else:
            failed += 1
        results.append((label, status, count, min_count))
        marker = "+" if ok else "X"
        print(f"  [{marker}] {label:<30s}  count={count:<5d}  min={min_count}  {status}")
    print()

    # ── Heartbeats per arm ───────────────────────────────────────
    print("HEARTBEATS (per arm)")
    print("-" * 60)
    for arm in HEARTBEAT_ARMS:
        count = count_heartbeat(lines, arm)
        ok = count >= 1
        status = "PASS" if ok else "FAIL"
        if ok:
            passed += 1
        else:
            failed += 1
        results.append((f"heartbeat_{arm}", status, count, 1))
        marker = "+" if ok else "X"
        print(f"  [{marker}] {arm:<30s}  count={count:<5d}  min=1     {status}")
    print()

    # ── Zero-count checks (errors / crashes) ─────────────────────
    print("ERROR / CRASH CHECKS (must be zero)")
    print("-" * 60)
    for label, pattern in ZERO_CHECKS:
        count = count_pattern(lines, pattern)
        ok = count == 0
        status = "PASS" if ok else "FAIL"
        if ok:
            passed += 1
        else:
            failed += 1
        results.append((label, status, count, 0))
        marker = "+" if ok else "X"
        print(f"  [{marker}] {label:<30s}  count={count:<5d}  max=0     {status}")
    print()

    # ── Final verdict ────────────────────────────────────────────
    total = passed + failed
    print("=" * 60)
    if failed == 0:
        print(f"  CERTIFICATION: PASSED  ({passed}/{total} checks)")
        print("  U.T.S. is READY for IB paper trading.")
    else:
        print(f"  CERTIFICATION: FAILED  ({passed}/{total} passed, {failed} failed)")
        print("  Fix the above failures before proceeding.")
        print()
        print("  Failed checks:")
        for label, status, count, threshold in results:
            if status == "FAIL":
                print(f"    - {label}: count={count} (expected {'==' if threshold == 0 else '>='}{threshold})")
    print("=" * 60)

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
