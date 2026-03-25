"""AgentIntelLoader — polls data/agent_intel.json, filters symbols,
and publishes WatchCandidate messages onto the bus.

Instantiated in monitor_main and polled once per heartbeat cycle.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from src.bus.topics import WATCH_CANDIDATE
from src.monitoring.logger import get_logger
from src.schemas.messages import WatchCandidate

log = get_logger("agent_intel_loader")

# Catalyst tags that elevate a LOW-conviction symbol to watchable
_HIGH_TAGS = frozenset({
    "EARNINGS", "FDA", "MERGER", "ACQUISITION", "BUYOUT",
    "ANALYST_ACTION", "INSIDER_BUY", "GOVERNMENT_CONTRACT",
})

_CONVICTION_PRIORITY = {
    "HIGH": 1.0,
    "MEDIUM": 0.6,
}

_LIVE_STATUS_PATH = Path("dashboard/live_status.json")


class AgentIntelLoader:
    """Poll *path* for fresh agent-intel JSON and publish WatchCandidates."""

    def __init__(
        self,
        bus: Any,
        path: str = "data/agent_intel.json",
        staleness_hours: float = 4,
    ) -> None:
        self._bus = bus
        self._path = Path(path)
        self._staleness_s = staleness_hours * 3600
        self._last_mtime: float = 0.0

    # ── public ───────────────────────────────────────────────────────

    def poll(self) -> None:
        """Check file mtime; skip if unchanged or stale.  Otherwise
        parse, filter, and publish candidates."""
        if not self._path.exists():
            return

        try:
            mtime = self._path.stat().st_mtime
        except OSError:
            return

        # Same file — already processed
        if mtime == self._last_mtime:
            return

        # Too old — skip
        age_s = time.time() - mtime
        if age_s > self._staleness_s:
            log.debug(
                "agent_intel file stale (%.1f h) — skipping",
                age_s / 3600,
            )
            return

        self._last_mtime = mtime
        self._process(mtime)

    # ── internals ────────────────────────────────────────────────────

    def _process(self, mtime: float) -> None:
        try:
            raw = json.loads(self._path.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("agent_intel parse error: %s", exc)
            return

        symbols: Dict[str, Dict] = raw.get("symbols", {})
        if not symbols:
            log.info("agent_intel file has no symbols")
            return

        filled = self._filled_symbols()
        published = 0

        for sym, info in symbols.items():
            sym = sym.upper()
            conviction = (info.get("conviction") or "").upper()
            catalyst_type = info.get("catalyst_type", "")
            tags: List[str] = info.get("risk_flags", [])
            if catalyst_type:
                tags = [catalyst_type] + tags

            # Skip AVOID
            if conviction == "AVOID":
                continue

            # Skip LOW unless it has a high-value catalyst tag
            if conviction == "LOW":
                if not any(t.upper() in _HIGH_TAGS for t in tags):
                    continue

            # Skip already-filled positions
            if sym in filled:
                continue

            priority = _CONVICTION_PRIORITY.get(conviction, 0.3)

            candidate = WatchCandidate(
                symbol=sym,
                score=priority,
                reason_codes=tags,
                source="agent_intel",
                priority=priority,
                catalyst_tags=tags,
            )
            self._bus.publish(WATCH_CANDIDATE, candidate)
            published += 1

        log.info(
            "agent_intel poll  total=%d published=%d skipped=%d mtime=%.0f",
            len(symbols), published, len(symbols) - published, mtime,
        )

    @staticmethod
    def _filled_symbols() -> Set[str]:
        """Read filled_symbols from dashboard/live_status.json."""
        try:
            if not _LIVE_STATUS_PATH.exists():
                return set()
            data = json.loads(_LIVE_STATUS_PATH.read_text())
            return {s.upper() for s in data.get("filled_symbols", [])}
        except (json.JSONDecodeError, OSError):
            return set()
