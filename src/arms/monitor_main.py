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
    PLAN_DRAFT, OPEN_PLAN_CANDIDATE, ORDER_BLUEPRINT,
)
from src.schemas.messages import (
    Heartbeat, NewsEvent, WatchCandidate, TradeIntent,
    PlanDraft, OpenPlanCandidate, OrderBlueprint,
)
from src.market.session import get_us_equity_session, OFF_HOURS, PREMARKET

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

# ── State ────────────────────────────────────────────────────────────
_running = True
_last_seen: Dict[str, float] = {}   # arm → epoch timestamp
_lock = threading.Lock()


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


# ── Top News printer ────────────────────────────────────────────────

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
        log.info(
            "Subscribed to %s, %s, %s, %s, %s, %s, %s on event bus",
            HEARTBEAT, NEWS_EVENT, WATCH_CANDIDATE, TRADE_INTENT,
            PLAN_DRAFT, OPEN_PLAN_CANDIDATE, ORDER_BLUEPRINT,
        )
        return bus
    except Exception:
        log.exception("Failed to initialise event bus — will retry")
        return None


# ── Main loop ───────────────────────────────────────────────────────

def main() -> None:
    """Entry-point for the monitor arm."""
    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    log.info(
        "Monitor arm starting  mode=%s  check_interval=%ss  stale_threshold=%ss",
        settings.trade_mode.value,
        settings.heartbeat_interval_s,
        _STALE_THRESHOLD_S,
    )

    # Attempt Redis connection (non-blocking retry each cycle)
    bus = _connect_bus()

    tick = 0
    while _running:
        tick += 1
        now = time.time()

        # Lazy reconnect if bus failed on startup
        if bus is None:
            bus = _connect_bus()

        # Record own heartbeat
        with _lock:
            _last_seen["monitor"] = time.time()

        _print_status()

        # ── Symbol board (every 60 s) ─────────────────────────────
        global _last_board_print_ts, _last_playbook_print_ts
        if now - _last_board_print_ts >= _BOARD_PRINT_INTERVAL_S:
            _last_board_print_ts = now
            _print_top_news()
            _print_board()

        # ── Playbook (independent cadence) ────────────────────────
        pb_interval = _get_playbook_interval()
        if now - _last_playbook_print_ts >= pb_interval:
            _last_playbook_print_ts = now
            _print_playbook()
        # ── Blueprints (same cadence as playbook) ──────────────
        global _last_blueprints_print_ts
        if now - _last_blueprints_print_ts >= pb_interval:
            _last_blueprints_print_ts = now
            _print_blueprints()
        time.sleep(settings.heartbeat_interval_s)

    # Cleanup
    if bus is not None:
        try:
            bus.close()
        except Exception:
            pass
    log.info("Monitor arm stopped.")


if __name__ == "__main__":
    main()
