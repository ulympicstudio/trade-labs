"""
Dashboard snapshot writer for live_status.json.

Writes a compact JSON file atomically every loop iteration so an
external Mac / iPad UI can poll it without risk of reading partial data.
"""

import json
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set


SNAPSHOT_PATH = Path("logs/live_status.json")


class DashboardSnapshot:
    """Builds and atomically writes a JSON status snapshot each loop."""

    def __init__(self, session_id: str, *, mode: str = "PAPER",
                 backend: str = "SIM", path: Optional[Path] = None):
        self._sid = session_id
        self._mode = mode
        self._backend = backend
        self._path = path or SNAPSHOT_PATH
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._loop_count = 0
        self._start_ts = time.time()

    # ── public API ──────────────────────────────────────────────

    def update(self, *,
               armed: bool,
               equity: float,
               regime: Optional[str] = None,
               breadth_pct: Optional[float] = None,
               open_risk_pct: float = 0.0,
               filled_risk_pct: float = 0.0,
               pending_risk_pct: float = 0.0,
               n_positions: int = 0,
               n_working_orders: int = 0,
               filled_symbols: Optional[Set[str]] = None,
               working_symbols: Optional[Set[str]] = None,
               trail_active_symbols: Optional[Set[str]] = None,
               confirmed_fills: Optional[Set[str]] = None,
               signals_count: int = 0,
               intents_count: int = 0,
               orders_placed_count: int = 0,
               risk_rejected_count: int = 0,
               errors_count: int = 0,
               recent_events: Optional[List[Dict[str, Any]]] = None,
               market_open: bool = False,
               extra: Optional[Dict[str, Any]] = None) -> None:
        """Build the snapshot dict and write it atomically."""
        self._loop_count += 1
        now_utc = datetime.now(timezone.utc)

        payload: Dict[str, Any] = {
            # ── identity ──
            "session_id": self._sid,
            "mode": self._mode,
            "backend": self._backend,

            # ── timing ──
            "timestamp_utc": now_utc.isoformat(),
            "timestamp_epoch": round(time.time(), 3),
            "uptime_seconds": round(time.time() - self._start_ts, 1),
            "loop_count": self._loop_count,

            # ── market & control ──
            "armed": armed,
            "market_open": market_open,
            "regime": regime,
            "breadth_pct": round(breadth_pct, 4) if breadth_pct is not None else None,

            # ── account ──
            "equity": round(equity, 2),

            # ── risk ──
            "open_risk_total_pct": round(open_risk_pct, 5),
            "open_risk_filled_pct": round(filled_risk_pct, 5),
            "open_risk_pending_pct": round(pending_risk_pct, 5),

            # ── positions & orders ──
            "active_positions": n_positions,
            "working_orders": n_working_orders,
            "filled_symbols": sorted(filled_symbols) if filled_symbols else [],
            "working_symbols": sorted(working_symbols) if working_symbols else [],
            "trail_active_symbols": sorted(trail_active_symbols) if trail_active_symbols else [],
            "confirmed_fills": sorted(confirmed_fills) if confirmed_fills else [],

            # ── pipeline counts (cumulative) ──
            "signals_generated": signals_count,
            "intents_created": intents_count,
            "orders_placed": orders_placed_count,
            "risk_rejected": risk_rejected_count,
            "errors": errors_count,

            # ── recent lifecycle events (last 10) ──
            "recent_events": (recent_events or [])[-10:],
        }

        if extra:
            payload.update(extra)

        self._atomic_write(payload)

    # ── internals ───────────────────────────────────────────────

    def _atomic_write(self, payload: Dict[str, Any]) -> None:
        """Write JSON to a temp file then atomically rename into place."""
        target = str(self._path)
        dir_path = os.path.dirname(target) or "."
        try:
            fd, tmp_path = tempfile.mkstemp(dir=dir_path, suffix=".tmp",
                                            prefix=".live_status_")
            try:
                with os.fdopen(fd, "w") as f:
                    json.dump(payload, f, indent=2, default=str)
                    f.write("\n")
                os.replace(tmp_path, target)
            except BaseException:
                # Clean up temp file on any error
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
        except Exception:
            # Silently swallow — dashboard is best-effort, never blocks trading
            pass
