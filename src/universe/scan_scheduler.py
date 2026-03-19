"""
Scan Scheduler — converts the dynamic universe into per-symbol scan
priority classes: ``HIGH``, ``NORMAL``, ``LOW``.

Public API
----------
``build_scan_schedule(dynamic_universe)`` → dict[str, str]
``get_last_schedule()``                   → most recent schedule
``get_schedule_counts()``                 → {HIGH: n, NORMAL: n, LOW: n}

Env toggles
-----------
``TL_SCAN_SCHEDULER_ENABLED``   master on/off (default ``true``)
"""

from __future__ import annotations

import os
import threading
from typing import Dict, Optional

from src.universe.dynamic_universe import DynamicUniverseDecision
from src.monitoring.logger import get_logger

log = get_logger("scan_scheduler")

# ── Constants ────────────────────────────────────────────────────────

HIGH = "HIGH"
NORMAL = "NORMAL"
LOW = "LOW"

SCAN_SCHEDULER_ENABLED = os.environ.get(
    "TL_SCAN_SCHEDULER_ENABLED", "true"
).lower() in ("1", "true", "yes")

# ── Internal state ───────────────────────────────────────────────────

_lock = threading.Lock()
_last_schedule: Dict[str, str] = {}


def get_last_schedule() -> Dict[str, str]:
    """Return the most recent scan schedule (symbol → priority)."""
    with _lock:
        return dict(_last_schedule)


def get_schedule_counts() -> Dict[str, int]:
    """Return {HIGH: n, NORMAL: n, LOW: n} from the last schedule."""
    with _lock:
        sched = _last_schedule
    counts = {HIGH: 0, NORMAL: 0, LOW: 0}
    for priority in sched.values():
        counts[priority] = counts.get(priority, 0) + 1
    return counts


# ── Core logic ───────────────────────────────────────────────────────

def build_scan_schedule(
    dynamic_universe: DynamicUniverseDecision,
) -> Dict[str, str]:
    """Convert a DynamicUniverseDecision into a symbol → priority map.

    Parameters
    ----------
    dynamic_universe : DynamicUniverseDecision
        The current dynamic universe assignment.

    Returns
    -------
    dict[str, str]   symbol → "HIGH" | "NORMAL" | "LOW"
    """
    global _last_schedule

    if not SCAN_SCHEDULER_ENABLED:
        # Treat everything as NORMAL when disabled
        all_syms = (
            dynamic_universe.priority_symbols
            + dynamic_universe.active_symbols
            + dynamic_universe.reduced_symbols
        )
        schedule = {sym: NORMAL for sym in all_syms}
        with _lock:
            _last_schedule = schedule
        return schedule

    schedule: Dict[str, str] = {}

    for sym in dynamic_universe.priority_symbols:
        schedule[sym] = HIGH

    for sym in dynamic_universe.active_symbols:
        schedule[sym] = NORMAL

    for sym in dynamic_universe.reduced_symbols:
        schedule[sym] = LOW

    with _lock:
        _last_schedule = dict(schedule)

    return schedule
