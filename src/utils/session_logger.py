"""
Session Logger — writes a structured JSON summary at session end.

Called automatically by dev_all_in_one.py on Ctrl+C / SIGTERM shutdown.
Also prints a human-readable summary block to stdout.

Usage
-----
    from src.utils.session_logger import SessionLogger

    logger = SessionLogger(start_ts=_start_ts)
    logger.write()          # collects state, writes JSON, prints summary
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

_LOGS_DIR = Path(os.environ.get("TL_LOGS_DIR", "logs"))


@dataclass
class SessionMetrics:
    session_date: str = ""
    session_duration_minutes: float = 0.0
    market_session: str = ""
    symbols_scanned: int = 0
    intents_emitted: int = 0
    intents_approved: int = 0
    intents_rejected: int = 0
    orders_processed: int = 0
    orders_filled: int = 0
    orders_cancelled: int = 0
    gross_pnl: float = 0.0
    net_pnl: float = 0.0
    max_drawdown: float = 0.0
    win_rate: float = 0.0
    avg_hold_seconds: float = 0.0
    top_symbols: List[Dict[str, Any]] = field(default_factory=list)
    arm_errors: List[str] = field(default_factory=list)
    notes: str = ""


class SessionLogger:
    """Collect end-of-session metrics, write JSON, and print a summary."""

    def __init__(self, start_ts: float = 0.0) -> None:
        self._start_ts = start_ts or time.time()

    # ── Collection helpers ────────────────────────────────────────────

    def _collect(self) -> SessionMetrics:
        now = time.time()
        elapsed_s = now - self._start_ts
        elapsed_min = round(elapsed_s / 60.0, 1)

        m = SessionMetrics(
            session_date=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            session_duration_minutes=elapsed_min,
        )

        # Market session
        try:
            from src.market.session import get_us_equity_session
            m.market_session = get_us_equity_session()
        except Exception:
            m.market_session = "UNKNOWN"

        # Signal counters
        try:
            from src.arms import signal_main as _sm
            m.symbols_scanned = len(getattr(_sm, "_cache", {}))
            m.intents_emitted = getattr(_sm, "_intents_emitted", 0)
        except Exception:
            pass

        # Risk counters
        try:
            from src.arms import risk_main as _rm
            m.intents_approved = getattr(_rm, "_approved", 0)
            m.intents_rejected = getattr(_rm, "_rejected", 0)
        except Exception:
            pass

        # Execution counters
        try:
            from src.arms import execution_main as _em
            m.orders_processed = getattr(_em, "_orders_processed", 0)
            m.orders_filled = getattr(_em, "_fills_total", 0)
            m.orders_cancelled = getattr(_em, "_cancels_total", 0)
        except Exception:
            pass

        # PnL Attribution
        try:
            from src.analysis.pnl_attribution import get_recent_attribution_snapshot
            pa = get_recent_attribution_snapshot()
            m.gross_pnl = float(pa.get("realized_pnl", 0.0) or 0.0)
            m.net_pnl = float(pa.get("net_pnl", pa.get("realized_pnl", 0.0)) or 0.0)
            m.win_rate = float(pa.get("win_rate", 0.0) or 0.0)
            m.avg_hold_seconds = float(pa.get("avg_hold_seconds", 0.0) or 0.0)
        except Exception:
            pass

        # Drawdown
        try:
            from src.risk.exit_intelligence import get_exit_summary
            es = get_exit_summary()
            m.max_drawdown = float(es.get("max_drawdown", 0.0) or 0.0)
        except Exception:
            pass

        # Top symbols from scorecard
        try:
            from src.analysis.playbook_scorecard import get_top_playbooks
            top = get_top_playbooks(n=5)
            m.top_symbols = [
                {
                    "symbol": pb.get("symbol", ""),
                    "score": pb.get("score", 0),
                    "outcome": pb.get("outcome", ""),
                }
                for pb in top
                if pb.get("symbol")
            ]
        except Exception:
            pass

        return m

    # ── JSON writer ───────────────────────────────────────────────────

    def write(self, notes: str = "") -> Path:
        """Collect metrics, write JSON to logs/, print summary. Returns path."""
        m = self._collect()
        if notes:
            m.notes = notes

        _LOGS_DIR.mkdir(parents=True, exist_ok=True)
        ts_tag = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_path = _LOGS_DIR / f"session_{ts_tag}.json"

        with open(out_path, "w", encoding="utf-8") as fh:
            json.dump(asdict(m), fh, indent=2, default=str)

        self._print_summary(m, out_path)
        return out_path

    # ── Human-readable summary ────────────────────────────────────────

    @staticmethod
    def _print_summary(m: SessionMetrics, out_path: Path) -> None:
        _W = 47
        bar = "═" * _W

        def _hold_str(secs: float) -> str:
            if secs <= 0:
                return "—"
            mins, s = divmod(int(secs), 60)
            return f"{mins}m {s:02d}s" if mins else f"{s}s"

        def _pnl_str(v: float) -> str:
            sign = "+" if v >= 0 else ""
            return f"{sign}${v:,.2f}"

        lines = [
            bar,
            f" U.T.S. SESSION SUMMARY  {m.session_date}",
            bar,
            f" Duration      : {m.session_duration_minutes:.0f} min  ({m.market_session})",
            f" Symbols       : {m.symbols_scanned}",
            f" Intents       : {m.intents_emitted}  →  approved {m.intents_approved}  rejected {m.intents_rejected}",
            f" Orders        : {m.orders_processed} processed  /  {m.orders_filled} filled  /  {m.orders_cancelled} cancelled",
            f" Gross PnL     : {_pnl_str(m.gross_pnl)}",
            f" Net PnL       : {_pnl_str(m.net_pnl)}",
            f" Win Rate      : {m.win_rate * 100:.1f}%",
            f" Max Drawdown  : {_pnl_str(m.max_drawdown)}",
            f" Avg Hold      : {_hold_str(m.avg_hold_seconds)}",
        ]

        if m.top_symbols:
            lines.append(f" Top Symbols   :")
            for s in m.top_symbols[:5]:
                outcome = f"  [{s['outcome']}]" if s.get("outcome") else ""
                lines.append(f"   {s['symbol']:<8} score={s['score']}{outcome}")

        if m.arm_errors:
            lines.append(f" Arm Errors    : {len(m.arm_errors)}")
            for e in m.arm_errors[:3]:
                lines.append(f"   {e[:60]}")

        lines.append(bar)
        lines.append(f" Log: {out_path}")
        lines.append(bar)

        print("\n" + "\n".join(lines) + "\n", flush=True)
