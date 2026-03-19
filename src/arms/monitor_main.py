"""
Monitor Arm — observability, health, and alerting.

Connects to the Redis event bus, subscribes to heartbeats from all
arms, and prints a rolling status table every check interval.
Arms silent for >30 s are flagged as MISSING.

Run:
    python -m src.arms.monitor_main
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from src.config.settings import settings
from src.monitoring.logger import get_logger
from src.bus.topics import (
    HEARTBEAT, NEWS_EVENT, WATCH_CANDIDATE, TRADE_INTENT,
    PLAN_DRAFT, OPEN_PLAN_CANDIDATE, ORDER_BLUEPRINT, ORDER_EVENT,
)
from src.schemas.messages import (
    Heartbeat, NewsEvent, WatchCandidate, TradeIntent,
    PlanDraft, OpenPlanCandidate, OrderBlueprint, OrderEvent,
)
from src.market.session import get_us_equity_session, OFF_HOURS, PREMARKET
from src.signals.sector_intel import get_sector_summary as _get_sector_summary
from src.signals.sector_intel import get_sector_score as _get_sector_score
from src.signals.volatility_leaders import get_top_leaders as _get_vol_top_leaders
from src.signals.industry_rotation import get_top_industries as _get_rotation_top
from src.signals.industry_rotation import ROTATION_ENABLED as _ROTATION_ENABLED
from src.signals.industry_rotation import get_industry_score as _get_industry_score
from src.universe.sector_mapper import get_all_sectors as _get_all_sectors
from src.universe.sector_mapper import get_all_industries as _get_all_industries
from src.universe.composite_score import COMPOSITE_ENABLED as _COMPOSITE_ENABLED
from src.risk.sector_limits import (
    get_concentration_summary as _get_sector_concentration,
    get_industry_concentration_summary as _get_industry_concentration,
)
from src.signals.sector_rotation_selector import (
    get_last_rotation_decision as _get_rotation_decision,
    ROTATION_SEL_ENABLED as _ROTATION_SEL_ENABLED,
)
from src.universe.dynamic_universe import (
    get_last_decision as _get_dyn_universe,
    DYNAMIC_UNIVERSE_ENABLED as _DYN_UNIVERSE_ENABLED,
)
from src.universe.scan_scheduler import (
    get_schedule_counts as _get_sched_counts,
    SCAN_SCHEDULER_ENABLED as _SCAN_SCHED_ENABLED,
)
from src.signals.allocation_engine import (
    get_last_decision as _get_alloc_decision,
    get_bucket_fills as _get_bucket_fills,
    get_total_fills as _get_total_fills,
    get_allocation_summary as _get_alloc_summary,
    ALLOC_ENABLED as _ALLOC_ENABLED,
)
from src.signals.market_mode import (
    get_last_mode as _get_mm_decision,
    get_market_mode_summary as _get_mm_summary,
    MODE_ENABLED as _MM_ENABLED,
)
from src.analysis.playbook_scorecard import (
    get_scorecard_snapshot as _get_sc_snapshot,
    get_top_playbooks as _sc_top_playbooks,
    get_weak_playbooks as _sc_weak_playbooks,
    get_scorecard_summary as _sc_summary,
    SCORECARD_ENABLED as _SC_ENABLED,
    MONITOR_ENABLED as _SC_MONITOR_ENABLED,
)
from src.risk.exit_intelligence import (
    get_exit_summary as _get_exit_summary,
    get_open_positions_snapshot as _get_exit_positions,
    EXIT_ENABLED as _EXIT_ENABLED,
    MONITOR_ENABLED as _EXIT_MONITOR_ENABLED,
)
from src.analysis.pnl_attribution import (
    compute_attribution_summary as _get_attrib_summary,
    get_top_winners as _get_top_winners,
    get_top_losers as _get_top_losers,
    get_bucket_summary as _get_bucket_agg,
    get_mode_summary as _get_mode_agg,
    ATTRIB_ENABLED as _ATTRIB_ENABLED,
    MONITOR_ENABLED as _ATTRIB_MONITOR_ENABLED,
)
from src.analysis.self_tuning import (
    get_tuning_snapshot as _get_tune_snapshot,
    get_live_knob_overrides as _get_tune_overrides,
    TUNING_ENABLED as _TUNING_ENABLED,
    MONITOR_ENABLED as _TUNING_MONITOR_ENABLED,
)
from src.utils.price_cache import save_prices as _save_prices
from src.analysis.dashboard_snapshot import DashboardSnapshot
from src.signals.agent_intel import get_all_active_intel as _get_all_agent_intel
from src.risk.kill_switch import status_summary as _ks_status_summary

_VOL_MONITOR_ENABLED = os.environ.get("TL_VOL_MONITOR_ENABLED", "true").lower() in ("1", "true", "yes")

log = get_logger("monitor")

# ── Tunables ─────────────────────────────────────────────────────────
_STALE_THRESHOLD_S = 30.0        # warn if no heartbeat for this long
_KNOWN_ARMS = ("ingest", "signal", "risk", "execution", "monitor")
_BOARD_PRINT_INTERVAL_S = 60.0   # print top-10 board every 60 s
_HEADLINE_WINDOW_S = 7200.0      # 2-hour rolling window for headline counts
_PLAYBOOK_PRINT_S: float = float(
    os.environ.get("PLAYBOOK_PRINT_S", "0")   # 0 = use session-aware default
)
_PLAYBOOK_DEFAULT_OFF_HOURS_S = 15.0
_PLAYBOOK_DEFAULT_OTHER_S = 60.0

# ── iMessage consensus alerts ────────────────────────────────────────
_IMESSAGE_ENABLED = os.environ.get("TL_IMESSAGE_ENABLED", "false").lower() in ("1", "true", "yes")
_IMESSAGE_TARGET = os.environ.get("TL_IMESSAGE_TARGET", "")  # phone number or Apple ID
_IMESSAGE_COOLDOWN_S = float(os.environ.get("TL_IMESSAGE_COOLDOWN_S", "300"))  # min secs between alerts per symbol
_imessage_last_sent: Dict[str, float] = {}  # symbol → epoch of last iMessage


def _send_imessage(target: str, body: str) -> bool:
    """Send an iMessage via macOS AppleScript.  Returns True on success."""
    if not target:
        return False
    script = (
        f'tell application "Messages"\n'
        f'  set targetService to 1st account whose service type = iMessage\n'
        f'  set targetBuddy to participant "{target}" of targetService\n'
        f'  send "{body}" to targetBuddy\n'
        f'end tell'
    )
    try:
        subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, timeout=10,
        )
        return True
    except Exception as exc:
        log.warning("iMessage send failed: %s", exc)
        return False


def _maybe_alert_consensus(msg: "NewsEvent") -> None:
    """If consensus detected, send iMessage alert (with per-symbol cooldown)."""
    if not _IMESSAGE_ENABLED or not _IMESSAGE_TARGET:
        return
    consensus_n = 0
    for tag in (msg.impact_tags or []):
        if tag.startswith("CONSENSUS:"):
            try:
                consensus_n = int(tag.split(":")[1])
            except (IndexError, ValueError):
                pass
    if consensus_n < 2:
        return
    now = time.time()
    last = _imessage_last_sent.get(msg.symbol, 0.0)
    if (now - last) < _IMESSAGE_COOLDOWN_S:
        return
    _imessage_last_sent[msg.symbol] = now
    body = (
        f"[TradeLabs] CONSENSUS {msg.symbol} "
        f"({consensus_n} providers, score={msg.impact_score}) "
        f"— {msg.headline[:80]}"
    )
    sent = _send_imessage(_IMESSAGE_TARGET, body)
    log.info("iMessage consensus alert  symbol=%s  sent=%s", msg.symbol, sent)

# ── State ────────────────────────────────────────────────────────────
_running = True
_stop_event = threading.Event()  # cooperative shutdown
_last_seen: Dict[str, float] = {}   # arm → epoch timestamp
_lock = threading.Lock()

# ── Dashboard snapshot ───────────────────────────────────────────────
_dashboard: DashboardSnapshot | None = None
_recent_order_events: List[Dict[str, Any]] = []  # ring buffer (last 20)
_order_events_lock = threading.Lock()

# ── IB broker (read-only, for dashboard equity / positions) ──────────
_ib_monitor: Any = None  # ib_insync.IB | None
_IB_MONITOR_CLIENT_ID = int(os.environ.get("TL_MONITOR_IB_CLIENT_ID", "50"))
_IB_RECONNECT_INTERVAL_S = 60.0
_ib_last_attempt: float = 0.0


def _try_connect_ib_monitor() -> Any:
    """Non-blocking attempt to open a read-only IB connection.

    Uses a dedicated client-id (default 50) so it never conflicts with
    the execution arm or ingest arm connections.  Returns the IB
    instance on success, or *None* on failure.
    """
    global _ib_last_attempt
    now = time.time()
    if now - _ib_last_attempt < _IB_RECONNECT_INTERVAL_S:
        return None
    _ib_last_attempt = now
    try:
        from ib_insync import IB
        ib = IB()
        ib.RequestTimeout = 5
        host = os.environ.get("IB_HOST", "127.0.0.1")
        port = int(os.environ.get("IB_PORT", "7497"))
        ib.connect(host, port, clientId=_IB_MONITOR_CLIENT_ID, timeout=8, readonly=True)
        if ib.isConnected():
            log.info("Monitor IB connected (clientId=%d, readonly)", _IB_MONITOR_CLIENT_ID)
            return ib
        ib.disconnect()
    except Exception as exc:
        log.debug("Monitor IB connect failed: %s", exc)
    return None


def _ib_get_equity(ib: Any) -> float:
    """Read NetLiquidation from account values."""
    for v in ib.accountValues():
        if v.tag == "NetLiquidation":
            return float(v.value)
    return 0.0


def _ib_get_positions(ib: Any) -> List[Dict[str, Any]]:
    """Return list of open positions with current market value & PnL."""
    positions: List[Dict[str, Any]] = []
    for p in ib.positions():
        if p.position == 0:
            continue
        sym = p.contract.symbol
        qty = int(p.position)
        avg_cost = p.avgCost
        # Try to get current price from portfolio items
        mkt_price = avg_cost  # fallback
        unrealized = 0.0
        for pf in ib.portfolio():
            if pf.contract.symbol == sym:
                mkt_price = pf.marketPrice if pf.marketPrice > 0 else avg_cost
                unrealized = pf.unrealizedPNL
                break
        positions.append({
            "symbol": sym,
            "side": "LONG" if qty > 0 else "SHORT",
            "qty": abs(qty),
            "entry": round(avg_cost, 2),
            "current": round(mkt_price, 2),
            "pnl": round(unrealized, 2),
            "r_mult": 0.0,
        })
    return positions


def _ib_get_open_orders(ib: Any) -> tuple[int, List[str]]:
    """Return (count, symbol_list) of working orders."""
    syms: List[str] = []
    for trade in ib.openTrades():
        st = getattr(trade.orderStatus, "status", "")
        if st not in ("Filled", "Cancelled", "Inactive"):
            sym = trade.contract.symbol
            if sym not in syms:
                syms.append(sym)
    return len(syms), syms


# ── Symbol board ──────────────────────────────────────────────────────

@dataclass
class _BoardEntry:
    """Per-symbol tracking data for the monitor board."""
    headline_timestamps: List[float] = field(default_factory=list)
    latest_headline: str = ""
    last_headline_ts: float = 0.0
    last_score: float = 0.0


_board: Dict[str, _BoardEntry] = {}   # symbol → _BoardEntry
_board_lock = threading.Lock()
_last_board_print_ts: float = 0.0


# ── Playbook (off-hours plan drafts) ─────────────────────────────────

@dataclass
class _PlaybookEntry:
    """Latest PlanDraft data for one symbol."""
    ts: str = ""
    entry: float = 0.0
    stop: float = 0.0
    qty: int = 0
    risk_usd: float = 0.0
    confidence: float = 0.0
    notes: str = ""
    reason_codes: List[str] = field(default_factory=list)
    news_count_2h: int = 0
    latest_headline: str = ""
    # Score component breakdown
    news_points: float = 0.0
    momentum_pts: float = 0.0
    vol_points: float = 0.0
    spread_points: float = 0.0
    rsi_points: float = 0.0
    liq_points: float = 0.0
    total_score: float = 0.0
    stop_distance_pct: float = 0.0
    quality: str = ""  # HIGH / MED / LOW
    # News Shock Engine v1
    impact_score: int = 0
    burst_flag: bool = False
    # Legend Phase 1
    rvol: float = 0.0


_playbook: Dict[str, _PlaybookEntry] = {}   # symbol → _PlaybookEntry
_playbook_lock = threading.Lock()
_last_playbook_print_ts: float = 0.0
_PLAYBOOK_DIR = Path(settings.data_dir)
_PLAYBOOK_JSON = _PLAYBOOK_DIR / "playbook_latest.json"
_PLAYBOOK_TXT = _PLAYBOOK_DIR / "playbook_latest.txt"


# ── Blueprint state ─────────────────────────────────────────────────

@dataclass
class _BlueprintEntry:
    """Latest OrderBlueprint for one symbol."""
    ts: str = ""
    direction: str = "LONG"
    qty: int = 0
    entry_ladder: List[float] = field(default_factory=list)
    stop_price: float = 0.0
    trail_pct: float = 0.0
    timeout_s: int = 120
    max_spread_pct: float = 0.25
    risk_usd: float = 0.0
    confidence: float = 0.0
    total_score: float = 0.0
    quality: str = ""
    stop_distance_pct: float = 0.0
    # News Shock Engine v1
    impact_score: int = 0
    burst_flag: bool = False
    escalation: bool = False
    # Legend Phase 1
    notes: str = ""
    reason_codes: List[str] = field(default_factory=list)


_blueprints: Dict[str, _BlueprintEntry] = {}
_blueprints_lock = threading.Lock()
_last_blueprints_print_ts: float = 0.0


def _handle_signal(signum, _frame):
    global _running
    log.info("Received shutdown signal (%s)", signum)
    _running = False
    _stop_event.set()


# ── Heartbeat handler ───────────────────────────────────────────────

def _on_heartbeat(msg: Heartbeat) -> None:
    """Called by the bus listener thread for every heartbeat message."""
    with _lock:
        _last_seen[msg.arm] = time.time()


# ── News & signal handlers ─────────────────────────────────────────

def _board_record_headline(symbol: str, headline: str) -> None:
    """Record a headline timestamp + text for *symbol*."""
    now = time.time()
    with _board_lock:
        entry = _board.setdefault(symbol, _BoardEntry())
        entry.headline_timestamps.append(now)
        entry.latest_headline = headline[:120]  # truncate for display
        entry.last_headline_ts = now


def _board_record_score(symbol: str, score: float) -> None:
    """Update the last signal score for *symbol*."""
    with _board_lock:
        entry = _board.setdefault(symbol, _BoardEntry())
        entry.last_score = max(entry.last_score, score)  # keep highest


def _on_news_event(msg: NewsEvent) -> None:
    """Ingest a NewsEvent into the board."""
    _board_record_headline(msg.symbol, msg.headline)
    _maybe_alert_consensus(msg)


def _on_watch_candidate(msg: WatchCandidate) -> None:
    """Ingest a WatchCandidate score into the board."""
    _board_record_score(msg.symbol, msg.score)


def _on_trade_intent(msg: TradeIntent) -> None:
    """Ingest a TradeIntent confidence into the board as a score."""
    _board_record_score(msg.symbol, msg.confidence)


# ── PlanDraft / OpenPlanCandidate handlers ───────────────────────────

def _parse_rvol_from_reasons(reason_codes: list) -> float:
    """Extract rvol value from reason codes like 'rvol=2.3x'."""
    for rc in reason_codes:
        if isinstance(rc, str) and rc.startswith("rvol="):
            try:
                return float(rc.split("=")[1].rstrip("x"))
            except (ValueError, IndexError):
                pass
    return 0.0


def _on_plan_draft(msg: PlanDraft) -> None:
    """Store the latest PlanDraft for a symbol in the playbook."""
    ts_str = msg.ts.isoformat() if hasattr(msg.ts, "isoformat") else str(msg.ts)
    with _playbook_lock:
        _playbook[msg.symbol] = _PlaybookEntry(
            ts=ts_str,
            entry=msg.suggested_entry,
            stop=msg.suggested_stop,
            qty=msg.qty,
            risk_usd=msg.risk_usd,
            confidence=msg.confidence,
            notes=msg.notes,
            reason_codes=list(getattr(msg, "reason_codes", []))[:8],
            news_count_2h=getattr(msg, "news_count_2h", 0),
            latest_headline=getattr(msg, "latest_headline", "")[:120],
            news_points=getattr(msg, "news_points", 0.0),
            momentum_pts=getattr(msg, "momentum_pts", 0.0),
            vol_points=getattr(msg, "vol_points", 0.0),
            spread_points=getattr(msg, "spread_points", 0.0),
            rsi_points=getattr(msg, "rsi_points", 0.0),
            liq_points=getattr(msg, "liq_points", 0.0),
            total_score=getattr(msg, "total_score", 0.0),
            stop_distance_pct=getattr(msg, "stop_distance_pct", 0.0),
            quality=getattr(msg, "quality", ""),
            impact_score=getattr(msg, "impact_score", 0),
            burst_flag=getattr(msg, "burst_flag", False),
            rvol=_parse_rvol_from_reasons(getattr(msg, "reason_codes", [])),
        )
    log.debug("Playbook updated  symbol=%s  conf=%.2f  risk=$%.2f  news_2h=%d  total=%.1f",
             msg.symbol, msg.confidence, msg.risk_usd,
             getattr(msg, "news_count_2h", 0),
             getattr(msg, "total_score", 0.0))


def _on_open_plan_candidate(msg: OpenPlanCandidate) -> None:
    """Store OpenPlanCandidate in the playbook (pre-risk, qty/risk unknown)."""
    ts_str = msg.ts.isoformat() if hasattr(msg.ts, "isoformat") else str(msg.ts)
    with _playbook_lock:
        existing = _playbook.get(msg.symbol)
        # Only overwrite if no PlanDraft exists yet (PlanDraft is more complete)
        if existing is None or (existing.qty == 0 and existing.risk_usd == 0):
            _playbook[msg.symbol] = _PlaybookEntry(
                ts=ts_str,
                entry=msg.suggested_entry,
                stop=msg.suggested_stop,
                qty=0,
                risk_usd=0.0,
                confidence=msg.confidence,
                notes=f"session={msg.session} (pre-risk)",
                reason_codes=list(getattr(msg, "reason_codes", []))[:8],
                news_count_2h=getattr(msg, "news_count_2h", 0),
                latest_headline=getattr(msg, "latest_headline", "")[:120],
                news_points=getattr(msg, "news_points", 0.0),
                momentum_pts=getattr(msg, "momentum_pts", 0.0),
                vol_points=getattr(msg, "vol_points", 0.0),
                spread_points=getattr(msg, "spread_points", 0.0),
                rsi_points=getattr(msg, "rsi_points", 0.0),
                liq_points=getattr(msg, "liq_points", 0.0),
                total_score=getattr(msg, "total_score", 0.0),
                quality=getattr(msg, "quality", ""),
            )


# ── Blueprint handler ─────────────────────────────────────────────

def _on_order_event(msg: OrderEvent) -> None:
    """Log execution order events (FILLED / REJECTED / CANCELLED / PAPER_FILL etc)."""
    log.info(
        "order_event symbol=%s type=%s status=%s qty=%d price=%.2f msg=%s",
        msg.symbol,
        msg.event_type,
        msg.status,
        msg.filled_qty,
        msg.avg_fill_price,
        msg.message[:120] if msg.message else "",
    )
    # Keep last 20 order events for dashboard snapshot
    evt = {
        "symbol": msg.symbol,
        "type": msg.event_type,
        "status": msg.status,
        "qty": msg.filled_qty,
        "price": msg.avg_fill_price,
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    with _order_events_lock:
        _recent_order_events.append(evt)
        if len(_recent_order_events) > 20:
            _recent_order_events[:] = _recent_order_events[-20:]


def _on_order_blueprint(msg: OrderBlueprint) -> None:
    """Store the latest OrderBlueprint for a symbol."""
    ts_str = msg.ts.isoformat() if hasattr(msg.ts, "isoformat") else str(msg.ts)
    with _blueprints_lock:
        _blueprints[msg.symbol] = _BlueprintEntry(
            ts=ts_str,
            direction=msg.direction,
            qty=msg.qty,
            entry_ladder=list(msg.entry_ladder),
            stop_price=msg.stop_price,
            trail_pct=msg.trail_pct,
            timeout_s=msg.timeout_s,
            max_spread_pct=msg.max_spread_pct,
            risk_usd=msg.risk_usd,
            confidence=msg.confidence,
            total_score=msg.total_score,
            quality=msg.quality,
            stop_distance_pct=msg.stop_distance_pct,
            impact_score=getattr(msg, "impact_score", 0),
            burst_flag=getattr(msg, "burst_flag", False),
            escalation=getattr(msg, "escalation", False),
            notes=getattr(msg, "notes", ""),
            reason_codes=list(getattr(msg, "reason_codes", [])),
        )
    log.debug("Blueprint stored  symbol=%s  qty=%d  ladder=%d  trail=%.2f%%",
             msg.symbol, msg.qty, len(msg.entry_ladder), msg.trail_pct)


# ── Board printer ───────────────────────────────────────────────────

def _print_board() -> None:
    """Log the Top 10 symbols ranked by (headline_count_2h + score)."""
    now = time.time()
    cutoff = now - _HEADLINE_WINDOW_S

    with _board_lock:
        scored: List[tuple] = []
        for sym, entry in _board.items():
            # Prune old headline timestamps
            entry.headline_timestamps = [
                ts for ts in entry.headline_timestamps if ts > cutoff
            ]
            count_2h = len(entry.headline_timestamps)
            rank_val = count_2h + entry.last_score
            scored.append((sym, count_2h, entry.last_score, rank_val,
                           entry.latest_headline, entry.last_headline_ts))

    if not scored:
        return

    # Sort descending by rank value
    scored.sort(key=lambda x: x[3], reverse=True)
    top10 = scored[:10]

    lines = [
        f"{'#':<3} {'symbol':<8} {'hdl_2h':>6} {'score':>6} {'rank':>7}  {'latest_headline'}",
        "-" * 80,
    ]
    for i, (sym, cnt, score, rank, hdl, hdl_ts) in enumerate(top10, 1):
        age = now - hdl_ts if hdl_ts > 0 else float("inf")
        age_str = f"{age:.0f}s" if age < 99999 else "never"
        lines.append(
            f"{i:<3} {sym:<8} {cnt:>6} {score:>6.2f} {rank:>7.2f}  "
            f"{hdl[:50]}  ({age_str} ago)"
        )

    log.info("Symbol board (Top 10):\n%s", "\n".join(lines))


# ── Sector Intelligence summary ─────────────────────────────────────

def _print_sector_summary() -> None:
    """Log a compact sector state table."""
    summary = _get_sector_summary()
    if not summary:
        return

    lines = [
        "SECTOR INTELLIGENCE",
        f"{'sector':<25} {'state':<8} {'RS':>6} {'heat':>6} {'breadth':>7} {'ETF':<5} {'n':>3}",
        "-" * 68,
    ]
    for sector in sorted(summary):
        s = summary[sector]
        lines.append(
            f"{sector:<25} {s['state']:<8} {s['rs']:>+6.2f} "
            f"{s['heat']:>6.1f} {s['breadth']:>6.1f}% {s['etf']:<5} {s['n']:>3}"
        )
    log.info("sector_monitor summary\n%s", "\n".join(lines))


# ── Volatility Leaders summary ────────────────────────────────────

def _print_volatility_summary() -> None:
    """Log a compact volatility leaders table."""
    if not _VOL_MONITOR_ENABLED:
        return
    leaders = _get_vol_top_leaders(8)
    if not leaders:
        return

    lines = [
        "VOLATILITY LEADERS",
        f"{'symbol':<8} {'score':>5} {'state':<10} {'rvol':>5} {'atrx':>5} {'spread':>7}",
        "-" * 48,
    ]
    for r in leaders:
        lines.append(
            f"{r.symbol:<8} {r.leader_score:>5} {r.leader_state:<10} "
            f"{r.rvol_ratio:>5.1f} {r.atr_expansion_ratio:>5.1f} {r.spread_pct:>7.4f}"
        )
    parts = [f"{r.symbol}:{r.leader_score}({r.leader_state})" for r in leaders if r.leader_score > 0]
    log.info("volatility_monitor summary top=[%s]\n%s", ", ".join(parts), "\n".join(lines))


# ── Industry Rotation summary ────────────────────────────────────

def _print_rotation_summary() -> None:
    """Log a compact industry rotation table."""
    if not _ROTATION_ENABLED:
        return
    industries = _get_rotation_top(10)
    if not industries:
        return

    lines = [
        "INDUSTRY ROTATION",
        f"{'sector':<25} {'industry':<22} {'state':<14} {'score':>5} {'RS':>6} {'breadth':>7} {'heat':>5} {'vol':>3} {'n':>3}",
        "-" * 96,
    ]
    for r in industries:
        lines.append(
            f"{r.sector:<25} {r.industry:<22} {r.rotation_state:<14} "
            f"{r.rotation_score:>5} {r.relative_strength:>+6.2f} "
            f"{r.breadth:>6.1f}% {r.news_heat:>5.1f} {r.vol_leaders:>3} {r.symbols_tracked:>3}"
        )
    parts = [f"{r.industry}:{r.rotation_score}({r.rotation_state})" for r in industries if r.rotation_score > 0]
    log.info("industry_rotation summary rotation_leaders=[%s]\n%s", ", ".join(parts), "\n".join(lines))


# ── Allocation summary ───────────────────────────────────────────────

def _print_allocation_summary() -> None:
    """Log a compact allocation engine status table."""
    if not _ALLOC_ENABLED:
        return
    d = _get_alloc_decision()
    if d is None:
        return
    fills = _get_bucket_fills()
    total = _get_total_fills()

    lines = [
        "ALLOCATION ENGINE",
        f"  regime={d.regime}  session={d.session_state}  bias={d.market_bias}  posture={d.risk_posture}",
        f"  weights: news={d.weight_news:.2f}  rotation={d.weight_rotation:.2f}  "
        f"vol={d.weight_volatility:.2f}  meanrev={d.weight_meanrevert:.2f}",
        f"  bucket_usage: news={fills.get('news', 0)}/{d.max_news_positions}"
        f"  rotation={fills.get('rotation', 0)}/{d.max_rotation_positions}"
        f"  vol={fills.get('volatility', 0)}/{d.max_vol_positions}"
        f"  meanrev={fills.get('meanrevert', 0)}/{d.max_meanrevert_positions}"
        f"  total={total}/{d.max_total_positions}",
    ]
    top_conf: List[str] = []
    log.info(
        "allocation_monitor summary posture=%s bucket_usage=[n=%d r=%d v=%d m=%d tot=%d/%d] "
        "top_confluence=%s\n%s",
        d.risk_posture,
        fills.get("news", 0), fills.get("rotation", 0),
        fills.get("volatility", 0), fills.get("meanrevert", 0),
        total, d.max_total_positions,
        top_conf or "(none)",
        "\n".join(lines),
    )


# ── Market Mode summary ─────────────────────────────────────────────

def _print_market_mode_summary() -> None:
    """Log a compact market mode status table."""
    if not _MM_ENABLED:
        return
    d = _get_mm_decision()
    if d is None:
        return

    lines = [
        "MARKET MODE / SESSION COMMANDER",
        f"  mode={d.mode}  conf={d.confidence:.2f}  posture={d.risk_posture}",
        f"  regime={d.regime}  session={d.session_state}",
        f"  breadth={d.breadth_state}  vol={d.volatility_state}  "
        f"rot={d.rotation_state}  news={d.news_state}",
        f"  weights: news={d.recommended_news_weight:.2f}  "
        f"rotation={d.recommended_rotation_weight:.2f}  "
        f"vol={d.recommended_vol_weight:.2f}  "
        f"meanrev={d.recommended_meanrev_weight:.2f}",
        f"  cap_mult={d.position_cap_mult:.2f}",
    ]
    log.info(
        "market_mode summary mode=%s conf=%.2f posture=%s cap_mult=%.2f "
        "breadth=%s vol=%s rot=%s news=%s",
        d.mode, d.confidence, d.risk_posture, d.position_cap_mult,
        d.breadth_state, d.volatility_state, d.rotation_state, d.news_state,
    )
    log.info(
        "mode_posture mode=%s posture=%s reasons=%s\n%s",
        d.mode, d.risk_posture, d.reasons, "\n".join(lines),
    )


# ── Scorecard summary ───────────────────────────────────────────────

def _print_scorecard_summary() -> None:
    """Log a compact playbook scorecard status table."""
    if not _SC_ENABLED or not _SC_MONITOR_ENABLED:
        return

    snap = _get_sc_snapshot()
    if not snap.playbook_scores and not snap.sector_scores:
        return

    lines = [
        "PLAYBOOK SCORECARD",
        f"  overall_conf={snap.overall_confidence:.3f}  "
        f"playbooks={len(snap.playbook_scores)}  "
        f"sectors={len(snap.sector_scores)}",
    ]

    # Playbook scores
    if snap.playbook_scores:
        lines.append(f"  {'bucket':<14} {'trades':>6} {'wins':>5} {'wr':>6} "
                      f"{'pnl':>8} {'exp':>8} {'avg_r':>6} {'conf':>7}")
        lines.append("  " + "-" * 62)
        for k, c in sorted(snap.playbook_scores.items()):
            lines.append(
                f"  {k:<14} {c.trades:>6} {c.wins:>5} {c.win_rate:>6.2f} "
                f"{c.gross_pnl:>8.2f} {c.expectancy:>8.2f} "
                f"{c.avg_r_multiple:>6.2f} {c.confidence_score:>7.3f}"
            )

    # Top playbooks
    tops = _sc_top_playbooks(3)
    if tops:
        top_str = ", ".join(f"{c.key}={c.confidence_score:.3f}" for c in tops)
        lines.append(f"  top: {top_str}")

    # Weak playbooks
    weaks = _sc_weak_playbooks()
    if weaks:
        weak_str = ", ".join(f"{c.key}={c.confidence_score:.3f}" for c in weaks)
        lines.append(f"  weak: {weak_str}")

    log.info(
        "scorecard_monitor summary overall_conf=%.3f playbooks=%d "
        "sectors=%d top_playbooks=%s weak_playbooks=%s",
        snap.overall_confidence,
        len(snap.playbook_scores),
        len(snap.sector_scores),
        [c.key for c in tops] if tops else "(none)",
        [c.key for c in weaks] if weaks else "(none)",
    )
    log.info("scorecard_detail\n%s", "\n".join(lines))


# ── Exit Intelligence summary ───────────────────────────────────────

def _print_exit_summary() -> None:
    """Log a compact exit intelligence table for open positions."""
    if not _EXIT_ENABLED or not _EXIT_MONITOR_ENABLED:
        return

    summary = _get_exit_summary()
    if summary["open_count"] == 0:
        log.info("exit_monitor summary open=0")
        return

    positions = summary["positions"]
    lines = [
        "EXIT INTELLIGENCE",
        f"  open={summary['open_count']}  total_pnl=${summary['total_unrealized_pnl']:.2f}  "
        f"avg_R={summary['avg_r_mult']:.2f}  runners={summary['runners']}  "
        f"weak={summary['weakest']}  near_exit={summary['near_exit']}  "
        f"trims={summary['trims_total']}  exits={summary['exits_total']}  "
        f"time_stops={summary['time_stops_total']}",
        f"  {'sym':<7} {'side':<5} {'qty':>4} {'entry':>8} {'curr':>8} {'pnl':>8} "
        f"{'R':>5} {'mfe':>8} {'mae':>8} {'age':>6} {'pb':<10} {'mode':<16} "
        f"{'action':<14} {'conf':>5}",
        "  " + "-" * 130,
    ]

    for p in positions:
        age_m = p["elapsed_s"] // 60
        lines.append(
            f"  {p['symbol']:<7} {p['side']:<5} {p['qty']:>4} {p['entry']:>8.2f} "
            f"{p['current']:>8.2f} {p['pnl']:>8.2f} {p['r_mult']:>5.2f} "
            f"{p['mfe']:>8.2f} {p['mae']:>8.2f} {age_m:>5}m {p['playbook']:<10} "
            f"{p['mode']:<16} {p['action']:<14} {p['confidence']:>5.3f}"
        )

    if summary["runner_symbols"]:
        lines.append(f"  runners: {', '.join(summary['runner_symbols'])}")
    if summary["weak_symbols"]:
        lines.append(f"  weak: {', '.join(summary['weak_symbols'])}")
    if summary["exit_watchlist"]:
        lines.append(f"  exit_watchlist: {', '.join(summary['exit_watchlist'])}")

    log.info(
        "exit_monitor summary open=%d pnl=%.2f avg_R=%.2f runners=%d weak=%d "
        "near_exit=%d trims=%d exits=%d time_stops=%d",
        summary["open_count"], summary["total_unrealized_pnl"],
        summary["avg_r_mult"], summary["runners"], summary["weakest"],
        summary["near_exit"], summary["trims_total"], summary["exits_total"],
        summary["time_stops_total"],
    )
    if summary["open_count"] > 0:
        log.info(
            "open_positions count=%d symbols=%s",
            summary["open_count"],
            [p["symbol"] for p in positions],
        )
    if summary["exit_watchlist"]:
        log.info(
            "exit_watchlist count=%d symbols=%s",
            len(summary["exit_watchlist"]),
            summary["exit_watchlist"],
        )
    log.info("exit_detail\n%s", "\n".join(lines))


# ── Attribution summary printer ─────────────────────────────────────────

def _print_attribution_summary() -> None:
    """Log PnL attribution summary table."""
    if not _ATTRIB_ENABLED or not _ATTRIB_MONITOR_ENABLED:
        return

    s = _get_attrib_summary()
    log.info(
        "attribution_summary total=%d open=%d closed=%d "
        "realized=$%.2f unrealized=$%.2f avg_R=%.2f "
        "wins=%d losses=%d win_rate=%.3f avg_hold=%ds "
        "best_bucket=%s($%.2f) worst_bucket=%s($%.2f) "
        "best_mode=%s($%.2f) worst_mode=%s($%.2f)",
        s.total_trades, s.open_trades, s.closed_trades,
        s.total_realized_pnl, s.total_unrealized_pnl, s.avg_r_multiple,
        s.win_count, s.loss_count, s.win_rate, int(s.avg_hold_time_s),
        s.best_bucket, s.best_bucket_pnl,
        s.worst_bucket, s.worst_bucket_pnl,
        s.best_mode, s.best_mode_pnl,
        s.worst_mode, s.worst_mode_pnl,
    )

    # Top winners / losers
    winners = _get_top_winners(3)
    if winners:
        log.info(
            "top_winners %s",
            [(w.symbol, round(w.realized_pnl, 2), w.playbook) for w in winners],
        )
    losers = _get_top_losers(3)
    if losers:
        log.info(
            "top_losers %s",
            [(l.symbol, round(l.realized_pnl, 2), l.playbook) for l in losers],
        )

    # Bucket breakdown
    bucket_agg = _get_bucket_agg()
    if bucket_agg:
        log.info(
            "attrib_buckets %s",
            {k: {"pnl": round(v["pnl"], 2), "n": v["count"]} for k, v in bucket_agg.items()},
        )

    # Mode breakdown
    mode_agg = _get_mode_agg()
    if mode_agg:
        log.info(
            "attrib_modes %s",
            {k: {"pnl": round(v["pnl"], 2), "n": v["count"]} for k, v in mode_agg.items()},
        )


# ── Tuning summary printer ─────────────────────────────────────────────

def _print_tuning_summary() -> None:
    """Log self-tuning snapshot."""
    if not _TUNING_ENABLED or not _TUNING_MONITOR_ENABLED:
        return

    snap = _get_tune_snapshot()
    overrides = _get_tune_overrides()
    active = sum(len(v) for v in overrides.values())

    log.info(
        "tuning_summary decisions=%d active_overrides=%d "
        "bucket_nudges=%s priority_nudges=%s threshold_nudges=%s "
        "cap_mult_nudges=%s qty_mult_nudges=%s",
        snap.total_decisions, active,
        {k: round(v, 4) for k, v in snap.bucket_nudges.items() if v != 0},
        {k: round(v, 2) for k, v in snap.priority_nudges.items() if v != 0},
        {k: round(v, 1) for k, v in snap.threshold_nudges.items() if v != 0},
        {k: round(v, 4) for k, v in snap.cap_mult_nudges.items() if v != 0},
        {k: round(v, 4) for k, v in snap.qty_mult_nudges.items() if v != 0},
    )

    if active > 0:
        log.info(
            "active_overrides count=%d details=%s",
            active, overrides,
        )


# ── Composite Intelligence Summary ──────────────────────────────────────

def _print_intelligence_summary() -> None:
    """Log a composite intelligence table: top sectors & industries by score,
    plus industry concentration exposure."""
    if not _COMPOSITE_ENABLED:
        return

    # ── Top Sectors by score ─────────────────────────────────────────
    sec_summary = _get_sector_summary() or {}
    sec_scores: list = []
    for sector in sec_summary:
        score = _get_sector_score(sector)
        sec_scores.append((sector, score))
    sec_scores.sort(key=lambda x: -x[1])

    lines = [
        "COMPOSITE INTELLIGENCE",
        "",
        f"  {'SECTOR':<25} {'SCORE':>6}",
        "  " + "-" * 33,
    ]
    for sector, score in sec_scores[:8]:
        bar = "█" * int(score / 10)
        lines.append(f"  {sector:<25} {score:>6.1f}  {bar}")

    # ── Top Industries by score ──────────────────────────────────────
    ind_scores: list = []
    for ind in _get_all_industries():
        score = _get_industry_score(ind)
        if score > 0:
            ind_scores.append((ind, score))
    ind_scores.sort(key=lambda x: -x[1])

    lines.append("")
    lines.append(f"  {'INDUSTRY':<30} {'SCORE':>6}")
    lines.append("  " + "-" * 38)
    for ind, score in ind_scores[:10]:
        bar = "█" * int(score / 10)
        lines.append(f"  {ind:<30} {score:>6.1f}  {bar}")

    # ── Industry concentration exposure ──────────────────────────────
    ind_conc = _get_industry_concentration()
    if ind_conc:
        lines.append("")
        lines.append(f"  {'INDUSTRY EXPOSURE':<30} {'ACTIVE':>6} {'EXPOSURE':>8}")
        lines.append("  " + "-" * 46)
        ranked = sorted(ind_conc.items(), key=lambda x: -x[1].get("exposure", 0))
        for ind, d in ranked[:8]:
            if d.get("active", 0) > 0 or d.get("exposure", 0) > 0:
                lines.append(
                    f"  {ind:<30} {d['active']:>6} {d['exposure']:>7.1%}"
                )

    log.info("intelligence_summary\n%s", "\n".join(lines))


# ── Dynamic Universe + Rotation Summary ────────────────────────────────

def _print_dynamic_universe_summary() -> None:
    """Log dynamic universe tiers, rotation decisions, and scan schedule."""
    if not _DYN_UNIVERSE_ENABLED and not _ROTATION_SEL_ENABLED:
        return

    lines = ["DYNAMIC UNIVERSE + ROTATION", ""]

    # ── Sector rotation decision ─────────────────────────────────────
    rot = _get_rotation_decision() if _ROTATION_SEL_ENABLED else None
    if rot:
        lines.append(f"  {'TOP SECTORS':<30} {'SCORE':>6}")
        lines.append("  " + "-" * 38)
        for sector, score in rot.top_sectors[:6]:
            bar = "█" * int(score / 10)
            lines.append(f"  {sector:<30} {score:>6.1f}  {bar}")
        if rot.rotating_in:
            lines.append(f"  ↑ Rotating IN : {', '.join(rot.rotating_in)}")
        if rot.rotating_out:
            lines.append(f"  ↓ Rotating OUT: {', '.join(rot.rotating_out)}")
        lines.append("")

    # ── Dynamic universe tiers ───────────────────────────────────────
    dyn = _get_dyn_universe() if _DYN_UNIVERSE_ENABLED else None
    if dyn:
        lines.append(
            f"  Priority: {len(dyn.priority_symbols):>3}  "
            f"Active: {len(dyn.active_symbols):>3}  "
            f"Reduced: {len(dyn.reduced_symbols):>3}"
        )
        if dyn.priority_symbols:
            preview = ", ".join(sorted(dyn.priority_symbols)[:10])
            lines.append(f"  Priority preview: {preview}")
        if dyn.cold_sectors:
            lines.append(f"  Cold sectors  : {', '.join(sorted(dyn.cold_sectors))}")
        if dyn.cold_industries:
            lines.append(f"  Cold industries: {', '.join(sorted(dyn.cold_industries)[:6])}")
        lines.append("")

    # ── Scan schedule counts ─────────────────────────────────────────
    counts = _get_sched_counts() if _SCAN_SCHED_ENABLED else None
    if counts:
        lines.append(
            f"  Scan schedule  HIGH: {counts.get('HIGH', 0):>3}  "
            f"NORMAL: {counts.get('NORMAL', 0):>3}  "
            f"LOW: {counts.get('LOW', 0):>3}"
        )

    log.info("dynamic_universe_summary\n%s", "\n".join(lines))


# ── Top News printer ────────────────────────────────────────────────────

def _print_top_news() -> None:
    """Log Top 10 symbols ranked by 2h headline count, then recency."""
    now = time.time()
    cutoff = now - _HEADLINE_WINDOW_S

    with _board_lock:
        rows: List[tuple] = []
        for sym, entry in _board.items():
            # prune expired timestamps in-place
            entry.headline_timestamps = [
                ts for ts in entry.headline_timestamps if ts > cutoff
            ]
            count_2h = len(entry.headline_timestamps)
            if count_2h == 0:
                continue
            rows.append((
                sym,
                count_2h,
                entry.last_headline_ts,
                entry.latest_headline,
            ))

    if not rows:
        return

    # Primary sort: count desc; secondary: most recent first
    rows.sort(key=lambda r: (-r[1], -r[2]))
    top10 = rows[:10]

    lines = [
        "TOP NEWS (2h window)",
        f"{'#':<3} {'symbol':<8} {'count':>5} {'ago':>8}  {'latest_headline'}",
        "-" * 80,
    ]
    for i, (sym, cnt, ts, hdl) in enumerate(top10, 1):
        age = now - ts
        if age < 60:
            age_str = f"{age:.0f}s"
        elif age < 3600:
            age_str = f"{age / 60:.0f}m"
        else:
            age_str = f"{age / 3600:.1f}h"
        lines.append(f"{i:<3} {sym:<8} {cnt:>5} {age_str:>8}  {hdl[:55]}")

    log.info("%s", "\n".join(lines))


# ── Playbook printer + file writer ───────────────────────────────────

def _trim_reasons(notes: str, max_items: int = 2) -> str:
    """Return at most *max_items* reasons from a delimited notes string."""
    # Split on semicolons first, fall back to commas
    parts = [p.strip() for p in re.split(r"[;,]", notes) if p.strip()]
    if len(parts) <= max_items:
        return "; ".join(parts)
    return "; ".join(parts[:max_items]) + " …"


def _get_playbook_interval() -> float:
    """Return the playbook print interval for the current session."""
    if _PLAYBOOK_PRINT_S > 0:
        return _PLAYBOOK_PRINT_S
    session = get_us_equity_session()
    if session in (OFF_HOURS, PREMARKET):
        return _PLAYBOOK_DEFAULT_OFF_HOURS_S
    return _PLAYBOOK_DEFAULT_OTHER_S


def _print_playbook() -> None:
    """Log + write the Top 20 plan drafts.

    v2: Prints top 5 headlines first, then playbook with component columns.
    Prints when session is OFF_HOURS or PREMARKET and there are drafts.
    In PREMARKET: shows OPENING PLAN label with gap/mom column replacing liq.
    Sorted by total_score desc, then news_count desc, then recency desc.
    """
    session = get_us_equity_session()
    if session not in (OFF_HOURS, PREMARKET):
        return

    with _playbook_lock:
        if not _playbook:
            return
        entries: List[Dict[str, Any]] = []
        for sym, pe in _playbook.items():
            entries.append({
                "symbol": sym,
                "ts": pe.ts,
                "entry": pe.entry,
                "stop": pe.stop,
                "qty": pe.qty,
                "risk_usd": pe.risk_usd,
                "confidence": pe.confidence,
                "notes": pe.notes,
                "reason_codes": pe.reason_codes,
                "news_count_2h": pe.news_count_2h,
                "latest_headline": pe.latest_headline,
                "news_points": pe.news_points,
                "momentum_pts": pe.momentum_pts,
                "vol_points": pe.vol_points,
                "spread_points": pe.spread_points,
                "rsi_points": pe.rsi_points,
                "liq_points": pe.liq_points,
                "total_score": pe.total_score,
                "stop_distance_pct": pe.stop_distance_pct,
                "quality": pe.quality,
                "impact_score": pe.impact_score,
                "burst_flag": pe.burst_flag,
                "rvol": pe.rvol,
            })

    # Sort: total_score desc → news_count desc → recency desc
    entries.sort(key=lambda e: e["ts"], reverse=True)
    entries.sort(key=lambda e: (-e["total_score"], -e["news_count_2h"]))
    top20 = entries[:20]

    # ── Top 5 Headlines (sorted by news_points desc, then recency desc) ──
    news_entries = [e for e in top20 if e.get("latest_headline")]
    news_entries.sort(key=lambda e: e["ts"], reverse=True)
    news_entries.sort(key=lambda e: e.get("news_points", 0), reverse=True)
    lines: List[str] = []
    if news_entries:
        lines.append("TOP 5 HEADLINES (by news_points then recency)")
        for i, e in enumerate(news_entries[:5], 1):
            lines.append(
                f"  {i}. [{e['symbol']:<6} n2h={e['news_count_2h']} pts={e.get('news_points',0):.0f}]  "
                f"{e['latest_headline'][:80]}"
            )
        lines.append("")

    # ── Table header varies by session ───────────────────────────────
    if session == PREMARKET:
        lines.append("OPENING PLAN (PREMARKET) v2  [weighted: gap×3 vol×2 news×2 sprd×1 rsi×1 +rvol]")
        lines.append(
            f"{'#':<3} {'sym':<7} {'total':>5} {'conf':>5} {'Q':<4} "
            f"{'Imp':>3} {'B':>1} "
            f"{'gap':>3} {'vol':>3} {'nws':>3} {'spr':>3} {'rsi':>3} {'rvol':>4} "
            f"{'n2h':>3} {'entry':>8} {'stop':>8} {'s%':>5} "
            f"{'qty':>5} {'risk':>7}  {'headline'}"
        )
    else:
        lines.append("PLAYBOOK (OFF_HOURS) v6  [weighted: news×3 liq×2 vol×2 mom×1 sprd×1 rsi×1]")
        lines.append(
            f"{'#':<3} {'sym':<7} {'total':>5} {'conf':>5} {'Q':<4} "
            f"{'Imp':>3} {'B':>1} "
            f"{'nws':>3} {'liq':>3} {'vol':>3} {'mom':>3} {'spr':>3} {'rsi':>3} "
            f"{'n2h':>3} {'entry':>8} {'stop':>8} {'s%':>5} "
            f"{'qty':>5} {'risk':>7}  {'headline'}"
        )
    lines.append("-" * 134)

    for i, e in enumerate(top20, 1):
        hl = e.get("latest_headline", "")
        hl_short = hl[:30] + "…" if len(hl) > 30 else hl
        stop_d = e.get("stop_distance_pct", 0.0)
        q = e.get("quality", "") or "-"
        imp = e.get("impact_score", 0)
        b_str = "Y" if e.get("burst_flag", False) else "N"
        if session == PREMARKET:
            # In PREMARKET: momentum_pts holds gap_points
            rvol_str = f"{e.get('rvol', 0.0):>4.1f}" if e.get('rvol', 0.0) > 0 else "   -"
            lines.append(
                f"{i:<3} {e['symbol']:<7} {e['total_score']:>5.1f} {e['confidence']:>5.2f} {q:<4} "
                f"{imp:>3} {b_str:>1} "
                f"{e['momentum_pts']:>3.0f} {e['vol_points']:>3.0f} {e['news_points']:>3.0f} "
                f"{e['spread_points']:>3.0f} {e['rsi_points']:>3.0f} {rvol_str} "
                f"{e['news_count_2h']:>3} {e['entry']:>8.2f} {e['stop']:>8.2f} "
                f"{stop_d:>5.2f} "
                f"{e['qty']:>5} {e['risk_usd']:>7.2f}  {hl_short}"
            )
        else:
            lines.append(
                f"{i:<3} {e['symbol']:<7} {e['total_score']:>5.1f} {e['confidence']:>5.2f} {q:<4} "
                f"{imp:>3} {b_str:>1} "
                f"{e['news_points']:>3.0f} {e['liq_points']:>3.0f} {e['vol_points']:>3.0f} "
                f"{e['momentum_pts']:>3.0f} {e['spread_points']:>3.0f} {e['rsi_points']:>3.0f} "
                f"{e['news_count_2h']:>3} {e['entry']:>8.2f} {e['stop']:>8.2f} "
                f"{stop_d:>5.2f} "
                f"{e['qty']:>5} {e['risk_usd']:>7.2f}  {hl_short}"
            )
    log.info("Playbook:\n%s", "\n".join(lines))

    # ── Write files ──────────────────────────────────────────────────
    try:
        _PLAYBOOK_DIR.mkdir(parents=True, exist_ok=True)

        # JSON — full data for all drafts (only write in OFF_HOURS to avoid
        # overwriting the playbook that PREMARKET relies on)
        if session == OFF_HOURS:
            with open(_PLAYBOOK_JSON, "w") as f:
                json.dump(
                    {"updated": datetime.now(timezone.utc).isoformat(), "drafts": entries},
                    f, indent=2,
                )

        # TXT — human-readable table (always write)
        with open(_PLAYBOOK_TXT, "w") as f:
            f.write("\n".join(lines) + "\n")

        log.debug("Playbook written to %s and %s", _PLAYBOOK_JSON, _PLAYBOOK_TXT)
    except Exception:
        log.exception("Failed to write playbook files")


# ── Blueprint table printer ──────────────────────────────────────────

def _print_blueprints() -> None:
    """Log ORDER BLUEPRINTS table during PREMARKET."""
    session = get_us_equity_session()
    if session != PREMARKET:
        return

    with _blueprints_lock:
        if not _blueprints:
            return
        rows: List[Dict[str, Any]] = []
        for sym, be in _blueprints.items():
            rows.append({
                "symbol": sym,
                "qty": be.qty,
                "entry_ladder": be.entry_ladder,
                "stop_price": be.stop_price,
                "trail_pct": be.trail_pct,
                "timeout_s": be.timeout_s,
                "max_spread_pct": be.max_spread_pct,
                "risk_usd": be.risk_usd,
                "total_score": be.total_score,
                "quality": be.quality,
                "stop_distance_pct": be.stop_distance_pct,
                "impact_score": be.impact_score,
                "burst_flag": be.burst_flag,
                "escalation": be.escalation,
                "reason_codes": be.reason_codes,
            })

    # Sort by total_score desc
    rows.sort(key=lambda r: -r["total_score"])
    top20 = rows[:20]

    lines: List[str] = [
        "ORDER BLUEPRINTS (DRY RUN)  [premarket bracket-ready]",
        f"{'#':<3} {'sym':<7} {'Q':<4} {'score':>5} {'qty':>5} "
        f"{'Imp':>3} {'B':>1} {'Esc':>3} "
        f"{'entry_ladder':>28} {'stop':>8} {'trail%':>6} "
        f"{'timeout':>7} {'mxSprd':>6} {'risk':>7}",
        "-" * 120,
    ]

    for i, r in enumerate(top20, 1):
        ladder = r["entry_ladder"]
        if len(ladder) <= 3:
            ladder_str = ", ".join(f"{p:.2f}" for p in ladder)
        else:
            ladder_str = f"{ladder[0]:.2f} .. {ladder[len(ladder)//2]:.2f} .. {ladder[-1]:.2f}"
        q = r.get("quality", "") or "-"
        imp = r.get("impact_score", 0)
        b_str = "Y" if r.get("burst_flag", False) else "N"
        # Esc column: H=heat_blocked, Y=escalation, N=normal
        is_heat = "HEAT_BLOCK" in r.get("reason_codes", [])
        esc_str = "H" if is_heat else ("Y" if r.get("escalation", False) else "N")
        lines.append(
            f"{i:<3} {q:<4} {r['symbol']:<7} {r['total_score']:>5.1f} {r['qty']:>5} "
            f"{imp:>3} {b_str:>1} {esc_str:>3} "
            f"{ladder_str:>28} {r['stop_price']:>8.2f} {r['trail_pct']:>6.2f} "
            f"{r['timeout_s']:>5}s {r['max_spread_pct']:>5.2f}% {r['risk_usd']:>7.2f}"
        )

    log.info("Blueprints:\n%s", "\n".join(lines))


# ── Status table ────────────────────────────────────────────────────

def _print_status() -> None:
    """Log a compact status table and warn about missing arms."""
    now = time.time()
    session = get_us_equity_session()
    lines = []
    with _lock:
        snapshot = dict(_last_seen)

    # header
    lines.append(f"session={session}")
    lines.append(f"{'arm':<12} {'last_seen_s':>12} {'status':<10}")
    lines.append("-" * 38)

    for arm in _KNOWN_ARMS:
        last = snapshot.get(arm)
        if last is None:
            age_s = float("inf")
            age_str = "never"
        else:
            age_s = now - last
            age_str = f"{age_s:>8.1f}s"

        if age_s > _STALE_THRESHOLD_S:
            status = "MISSING"
        else:
            status = "ok"

        lines.append(f"{arm:<12} {age_str:>12} {status:<10}")

        if status == "MISSING":
            log.warning("Arm %s heartbeat missing (last seen %s ago)", arm, age_str.strip())

    # Also show any unexpected arms that appeared
    for arm in sorted(snapshot):
        if arm not in _KNOWN_ARMS:
            age_s = now - snapshot[arm]
            lines.append(f"{arm:<12} {age_s:>11.1f}s {'ok' if age_s <= _STALE_THRESHOLD_S else 'MISSING':<10}")

    log.info("Status table:\n%s", "\n".join(lines))


# ── Bus connection (resilient) ──────────────────────────────────────

def _connect_bus():
    """Try to create an event bus and subscribe; return the bus or None.

    Uses ``max_retries=1`` so the monitor is never blocked waiting
    for an external service — it simply reports the bus as unavailable
    and retries on the next cycle.
    """
    try:
        from src.bus.bus_factory import get_bus
        bus = get_bus(max_retries=1)
        if not bus.is_connected:
            log.warning("Event bus unavailable — will retry next cycle")
            return None
        bus.subscribe(HEARTBEAT, _on_heartbeat, msg_type=Heartbeat)
        bus.subscribe(NEWS_EVENT, _on_news_event, msg_type=NewsEvent)
        bus.subscribe(WATCH_CANDIDATE, _on_watch_candidate, msg_type=WatchCandidate)
        bus.subscribe(TRADE_INTENT, _on_trade_intent, msg_type=TradeIntent)
        bus.subscribe(PLAN_DRAFT, _on_plan_draft, msg_type=PlanDraft)
        bus.subscribe(OPEN_PLAN_CANDIDATE, _on_open_plan_candidate, msg_type=OpenPlanCandidate)
        bus.subscribe(ORDER_BLUEPRINT, _on_order_blueprint, msg_type=OrderBlueprint)
        bus.subscribe(ORDER_EVENT, _on_order_event, msg_type=OrderEvent)
        log.info(
            "Subscribed to %s, %s, %s, %s, %s, %s, %s, %s on event bus",
            HEARTBEAT, NEWS_EVENT, WATCH_CANDIDATE, TRADE_INTENT,
            PLAN_DRAFT, OPEN_PLAN_CANDIDATE, ORDER_BLUEPRINT, ORDER_EVENT,
        )
        return bus
    except Exception:
        log.exception("Failed to initialise event bus — will retry")
        return None


# ── Dashboard snapshot writer ────────────────────────────────────────

def _write_dashboard_snapshot(tick: int) -> None:
    """Collect state from all subsystems and write logs/live_status.json."""
    if _dashboard is None:
        return
    try:
        session = get_us_equity_session()
        is_market_open = session not in (OFF_HOURS, PREMARKET)

        # ── Arm heartbeat info ────────────────────────────────
        with _lock:
            hb_snapshot = dict(_last_seen)
        now = time.time()
        arms_status = {}
        for arm in _KNOWN_ARMS:
            last = hb_snapshot.get(arm)
            arms_status[arm] = {
                "last_seen_s": round(now - last, 1) if last else None,
                "status": "ok" if last and (now - last) <= _STALE_THRESHOLD_S else "MISSING",
            }

        # ── Exit intelligence (positions + unrealized PnL) ────
        exit_data: Dict[str, Any] = {}
        n_positions = 0
        unrealized_pnl = 0.0
        positions_list: List[Dict[str, Any]] = []
        try:
            if _EXIT_ENABLED:
                exit_data = _get_exit_summary()
                n_positions = exit_data.get("open_count", 0)
                unrealized_pnl = exit_data.get("total_unrealized_pnl", 0.0)
                positions_list = exit_data.get("positions", [])
        except Exception:
            pass

        # ── Attribution (realized PnL) ────────────────────────
        realized_pnl = 0.0
        total_trades = 0
        win_rate = 0.0
        try:
            if _ATTRIB_ENABLED:
                attrib = _get_attrib_summary()
                realized_pnl = attrib.total_realized_pnl
                total_trades = attrib.total_trades
                win_rate = attrib.win_rate
        except Exception:
            pass

        # ── Allocation (positions, regime, bucket usage) ──────
        alloc_info: Dict[str, Any] = {}
        regime = None
        risk_posture = None
        try:
            if _ALLOC_ENABLED:
                d = _get_alloc_decision()
                if d:
                    regime = d.regime
                    risk_posture = d.risk_posture
                    fills = _get_bucket_fills()
                    alloc_info = {
                        "regime": d.regime,
                        "risk_posture": d.risk_posture,
                        "bucket_usage": {
                            "news": fills.get("news", 0),
                            "rotation": fills.get("rotation", 0),
                            "volatility": fills.get("volatility", 0),
                            "meanrevert": fills.get("meanrevert", 0),
                            "total": _get_total_fills(),
                            "max_total": d.max_total_positions,
                        },
                    }
        except Exception:
            pass

        # ── Kill switch status ────────────────────────────────
        ks_status: Dict[str, Any] = {}
        try:
            ks_status = _ks_status_summary()
        except Exception:
            pass

        # ── Board: news/signal counts ─────────────────────────
        with _board_lock:
            news_symbols_count = len(_board)
        with _playbook_lock:
            intents_count = len(_playbook)
        with _blueprints_lock:
            blueprints_count = len(_blueprints)

        # ── Dynamic universe size ─────────────────────────────
        universe_size = 0
        try:
            if _DYN_UNIVERSE_ENABLED:
                dyn = _get_dyn_universe()
                if dyn:
                    universe_size = (
                        len(dyn.priority_symbols)
                        + len(dyn.active_symbols)
                        + len(dyn.reduced_symbols)
                    )
        except Exception:
            pass

        # ── Agent intel ───────────────────────────────────────
        agent_info: Dict[str, Any] = {}
        try:
            active_intel = _get_all_agent_intel()
            agent_info = {
                "loaded": len(active_intel),
                "symbols": sorted(active_intel.keys())[:20],
            }
        except Exception:
            pass

        # ── Recent order events ───────────────────────────────
        with _order_events_lock:
            recent_events = list(_recent_order_events)

        # ── IB broker data (equity, positions, orders) ────────
        ib_equity = 0.0
        ib_positions: List[Dict[str, Any]] = []
        ib_working_count = 0
        ib_working_syms: List[str] = []
        ib_filled_syms: set = set()
        ib_unrealized = 0.0
        try:
            if _ib_monitor is not None and _ib_monitor.isConnected():
                ib_equity = _ib_get_equity(_ib_monitor)
                ib_positions = _ib_get_positions(_ib_monitor)
                ib_working_count, ib_working_syms = _ib_get_open_orders(_ib_monitor)
                ib_filled_syms = {p["symbol"] for p in ib_positions}
                ib_unrealized = sum(p["pnl"] for p in ib_positions)
        except Exception:
            log.debug("IB data fetch failed", exc_info=True)

        # Merge: prefer IB live data over internal subsystem data
        final_equity = ib_equity if ib_equity > 0 else 0.0
        final_positions = ib_positions if ib_positions else positions_list[:20]
        final_n_positions = len(final_positions) if ib_positions else n_positions
        final_unrealized = ib_unrealized if ib_positions else unrealized_pnl
        final_working = ib_working_count if ib_working_count else blueprints_count
        final_filled_syms = ib_filled_syms if ib_filled_syms else set()
        final_working_syms = ib_working_syms if ib_working_syms else []

        # ── Recent trades (from positions list) ───────────────
        recent_trades: List[Dict[str, Any]] = []
        for p in final_positions[:20]:
            recent_trades.append({
                "symbol": p.get("symbol"),
                "side": p.get("side"),
                "qty": p.get("qty"),
                "entry": p.get("entry"),
                "current": p.get("current"),
                "pnl": p.get("pnl"),
                "r_mult": p.get("r_mult", 0.0),
            })

        armed = os.environ.get("ARMED", "0") == "1"

        # ── Build extra payload ────────────────────────────────
        extra: Dict[str, Any] = {
            "arms": arms_status,
            "session_type": session,
            "pnl": {
                "realized": round(realized_pnl, 2),
                "unrealized": round(final_unrealized, 2),
                "total": round(realized_pnl + final_unrealized, 2),
            },
            "total_trades": total_trades,
            "win_rate": round(win_rate, 4),
            "kill_switch": ks_status,
            "allocation": alloc_info,
            "universe_size": universe_size,
            "agent_intel": agent_info,
            "blueprints_count": blueprints_count,
            "recent_trades": recent_trades,
            "ib_connected": _ib_monitor is not None and _ib_monitor.isConnected(),
        }

        _dashboard.update(
            armed=armed,
            equity=final_equity,
            regime=regime,
            n_positions=final_n_positions,
            n_working_orders=final_working,
            filled_symbols=final_filled_syms,
            working_symbols=set(final_working_syms),
            signals_count=news_symbols_count,
            intents_count=intents_count,
            risk_rejected_count=0,
            recent_events=recent_events,
            market_open=is_market_open,
            extra=extra,
        )
    except Exception:
        log.debug("Dashboard snapshot write failed", exc_info=True)


# ── Main loop ───────────────────────────────────────────────────────

def main() -> None:
    """Entry-point for the monitor arm."""
    global _dashboard
    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    log.info(
        "Monitor arm starting  mode=%s  check_interval=%ss  stale_threshold=%ss",
        settings.trade_mode.value,
        settings.heartbeat_interval_s,
        _STALE_THRESHOLD_S,
    )

    # ── Dashboard JSON writer ─────────────────────────────────
    _dashboard = DashboardSnapshot(
        session_id=f"monitor_{int(time.time())}",
        mode=settings.trade_mode.value,
        backend=os.environ.get("BUS_BACKEND", "local"),
    )
    log.info("Dashboard snapshot writer active → %s", _dashboard._path)

    # Attempt Redis connection (non-blocking retry each cycle)
    bus = _connect_bus()

    # ── IB read-only connection (non-blocking) ────────────────
    global _ib_monitor
    _ib_monitor = _try_connect_ib_monitor()

    tick = 0
    while _running:
        tick += 1
        now = time.time()

        # Lazy reconnect if bus failed on startup
        if bus is None:
            bus = _connect_bus()

        # Lazy reconnect IB if disconnected
        if _ib_monitor is None or not _ib_monitor.isConnected():
            _ib_monitor = _try_connect_ib_monitor()

        # Record own heartbeat
        with _lock:
            _last_seen["monitor"] = time.time()

        _print_status()
        _write_dashboard_snapshot(tick)

        # ── Symbol board (every 60 s) ─────────────────────────────
        global _last_board_print_ts, _last_playbook_print_ts
        if now - _last_board_print_ts >= _BOARD_PRINT_INTERVAL_S:
            _last_board_print_ts = now
            _print_top_news()
            _print_board()
            _print_sector_summary()
            _print_volatility_summary()
            _print_rotation_summary()
            _print_allocation_summary()
            _print_market_mode_summary()
            _print_scorecard_summary()
            _print_exit_summary()
            _print_attribution_summary()
            _print_tuning_summary()
            _print_intelligence_summary()
            _print_dynamic_universe_summary()

        # ── Playbook (independent cadence) ────────────────────────
        pb_interval = _get_playbook_interval()
        if now - _last_playbook_print_ts >= pb_interval:
            _last_playbook_print_ts = now
            _print_playbook()

            # Periodic price-cache save (piggyback on playbook cadence)
            try:
                from src.arms.ingest_main import _SYNTH_PREV_LAST, _close_cache
                _pc: dict[str, float] = {}
                for sym, price in _SYNTH_PREV_LAST.items():
                    if price > 0:
                        _pc[sym.upper()] = price
                for sym, closes in _close_cache.items():
                    if closes and sym.upper() not in _pc:
                        _pc[sym.upper()] = closes[-1]
                if _pc:
                    _save_prices(_pc)
            except Exception:
                log.debug("Periodic price-cache save skipped", exc_info=True)
        # ── Blueprints (same cadence as playbook) ──────────────
        global _last_blueprints_print_ts
        if now - _last_blueprints_print_ts >= pb_interval:
            _last_blueprints_print_ts = now
            _print_blueprints()
        _stop_event.wait(settings.heartbeat_interval_s)
        if _stop_event.is_set():
            break

    # Cleanup
    if bus is not None:
        try:
            bus.close()
        except Exception:
            pass
    if _ib_monitor is not None:
        try:
            _ib_monitor.disconnect()
        except Exception:
            pass
    log.info("Monitor arm stopped.")


if __name__ == "__main__":
    main()
