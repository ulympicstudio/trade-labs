from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

_log = logging.getLogger("trade_labs.funnel_ledger")


class FunnelEvent:
    CANDIDATE_CREATED = "candidate_created"
    SESSION_BLOCKED = "session_blocked"
    RISK_BLOCKED = "risk_blocked"
    DEFERRED = "deferred"
    WATCHLIST_ONLY = "watchlist_only"
    INTENT_EMITTED = "intent_emitted"
    DUPLICATE_SUPPRESSED = "duplicate_suppressed"
    EXECUTION_REJECTED = "execution_rejected"
    EXECUTION_ACCEPTED = "execution_accepted"


_TERMINAL_STATES = {
    FunnelEvent.SESSION_BLOCKED,
    FunnelEvent.RISK_BLOCKED,
    FunnelEvent.DEFERRED,
    FunnelEvent.WATCHLIST_ONLY,
    FunnelEvent.INTENT_EMITTED,
    FunnelEvent.DUPLICATE_SUPPRESSED,
    FunnelEvent.EXECUTION_REJECTED,
    FunnelEvent.EXECUTION_ACCEPTED,
}


@dataclass
class CandidateRecord:
    candidate_id: str
    symbol: str = ""
    strategy_id: str = ""
    created_at: float = 0.0
    session_label: str = "UNKNOWN"
    block_reason: Optional[str] = None
    current_state: str = FunnelEvent.CANDIDATE_CREATED
    terminal_state: Optional[str] = None
    last_event_at: float = 0.0
    emitted_intent_id: str = ""
    execution_order_id: str = ""
    notes: str = ""


def _now() -> float:
    return time.time()


class FunnelLedger:
    def __init__(self, event_path: Optional[str] = None):
        self._lock = threading.Lock()
        self._records: Dict[str, CandidateRecord] = {}
        self._event_path = Path(event_path or os.environ.get("TL_FUNNEL_LEDGER_PATH", "data/funnel_ledger.jsonl"))
        self._event_path.parent.mkdir(parents=True, exist_ok=True)
        self._inflight_stale_s = float(os.environ.get("TL_FUNNEL_INFLIGHT_STALE_S", "300"))

    def _append_event(self, payload: Dict[str, Any]) -> None:
        line = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        with self._event_path.open("a", encoding="utf-8") as f:
            f.write(line)
            f.write("\n")

    def _upsert(self, candidate_id: str) -> CandidateRecord:
        rec = self._records.get(candidate_id)
        if rec is None:
            rec = CandidateRecord(candidate_id=candidate_id, last_event_at=_now())
            self._records[candidate_id] = rec
        return rec

    def record(
        self,
        *,
        event_type: str,
        candidate_id: str,
        symbol: str,
        strategy_id: str = "",
        session_label: str = "UNKNOWN",
        block_reason: Optional[str] = None,
        emitted_intent_id: str = "",
        execution_order_id: str = "",
        notes: str = "",
        event_ts: Optional[float] = None,
    ) -> None:
        ts = event_ts or _now()
        payload = {
            "event_type": event_type,
            "candidate_id": candidate_id,
            "symbol": symbol,
            "strategy_id": strategy_id,
            "session_label": session_label,
            "block_reason": block_reason,
            "emitted_intent_id": emitted_intent_id,
            "execution_order_id": execution_order_id,
            "notes": notes,
            "event_ts": ts,
        }

        with self._lock:
            rec = self._upsert(candidate_id)
            if symbol:
                rec.symbol = symbol
            if strategy_id:
                rec.strategy_id = strategy_id
            if session_label:
                rec.session_label = session_label
            if block_reason:
                rec.block_reason = block_reason
            if emitted_intent_id:
                rec.emitted_intent_id = emitted_intent_id
            if execution_order_id:
                rec.execution_order_id = execution_order_id
            if notes:
                rec.notes = notes
            rec.current_state = event_type
            rec.last_event_at = ts
            if event_type == FunnelEvent.CANDIDATE_CREATED and rec.created_at <= 0:
                rec.created_at = ts
            if event_type in _TERMINAL_STATES:
                rec.terminal_state = event_type

            self._append_event(payload)

    def record_candidate_created(
        self,
        *,
        candidate_id: str,
        symbol: str,
        strategy_id: str,
        session_label: str,
        event_ts: Optional[float] = None,
        notes: str = "",
    ) -> None:
        self.record(
            event_type=FunnelEvent.CANDIDATE_CREATED,
            candidate_id=candidate_id,
            symbol=symbol,
            strategy_id=strategy_id,
            session_label=session_label,
            notes=notes,
            event_ts=event_ts,
        )

    def _build_records_from_file(self) -> Dict[str, CandidateRecord]:
        records: Dict[str, CandidateRecord] = {}
        if not self._event_path.exists():
            return records

        with self._event_path.open("r", encoding="utf-8") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    ev = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                cid = str(ev.get("candidate_id", "")).strip()
                if not cid:
                    continue
                rec = records.get(cid)
                if rec is None:
                    rec = CandidateRecord(candidate_id=cid)
                    records[cid] = rec

                rec.symbol = str(ev.get("symbol") or rec.symbol)
                rec.strategy_id = str(ev.get("strategy_id") or rec.strategy_id)
                rec.session_label = str(ev.get("session_label") or rec.session_label)
                rec.block_reason = ev.get("block_reason") or rec.block_reason
                rec.emitted_intent_id = str(ev.get("emitted_intent_id") or rec.emitted_intent_id)
                rec.execution_order_id = str(ev.get("execution_order_id") or rec.execution_order_id)
                rec.notes = str(ev.get("notes") or rec.notes)
                ev_type = str(ev.get("event_type") or "")
                ev_ts = float(ev.get("event_ts") or 0.0)

                if ev_type == FunnelEvent.CANDIDATE_CREATED and rec.created_at <= 0:
                    rec.created_at = ev_ts
                rec.current_state = ev_type or rec.current_state
                rec.last_event_at = max(rec.last_event_at, ev_ts)
                if ev_type in _TERMINAL_STATES:
                    rec.terminal_state = ev_type

        return records

    def reconcile(self) -> Dict[str, Any]:
        with self._lock:
            records = self._build_records_from_file()

        total_in = len(records)
        terminal_counts: Counter = Counter()
        block_reason_counts: Counter = Counter()
        session_counts: Counter = Counter()
        discrepancies: list[dict[str, Any]] = []
        open_inflight = 0
        now = _now()

        for cid, rec in records.items():
            session_counts[rec.session_label or "UNKNOWN"] += 1
            if rec.block_reason:
                block_reason_counts[rec.block_reason] += 1

            if rec.terminal_state:
                terminal_counts[rec.terminal_state] += 1
                continue

            open_inflight += 1
            if rec.last_event_at > 0 and (now - rec.last_event_at) > self._inflight_stale_s:
                discrepancies.append(
                    {
                        "candidate_id": cid,
                        "symbol": rec.symbol,
                        "strategy_id": rec.strategy_id,
                        "last_state": rec.current_state,
                        "last_event_at": rec.last_event_at,
                        "session_label": rec.session_label,
                        "block_reason": rec.block_reason,
                        "reason": "MISSING_TERMINAL_EVENT",
                    }
                )

        accounted_for = sum(terminal_counts.values()) + open_inflight
        unaccounted = max(0, total_in - accounted_for)
        conservation_ok = total_in == accounted_for

        return {
            "total_in": total_in,
            "total_accounted_for": accounted_for,
            "total_unaccounted_for": unaccounted,
            "open_inflight": open_inflight,
            "counts_by_terminal_state": dict(terminal_counts),
            "counts_by_block_reason": dict(block_reason_counts),
            "counts_by_session_label": dict(session_counts),
            "conservation_ok": conservation_ok,
            "discrepancies": discrepancies,
        }

    def format_report(self) -> str:
        summary = self.reconcile()
        lines = [
            "FUNNEL_RECONCILE",
            f"total_in={summary['total_in']}",
            f"total_accounted_for={summary['total_accounted_for']}",
            f"total_unaccounted_for={summary['total_unaccounted_for']}",
            f"open_inflight={summary['open_inflight']}",
            f"conservation_ok={summary['conservation_ok']}",
            f"counts_by_terminal_state={json.dumps(summary['counts_by_terminal_state'], sort_keys=True)}",
            f"counts_by_block_reason={json.dumps(summary['counts_by_block_reason'], sort_keys=True)}",
            f"counts_by_session_label={json.dumps(summary['counts_by_session_label'], sort_keys=True)}",
            f"discrepancy_count={len(summary['discrepancies'])}",
        ]
        for d in summary["discrepancies"][:20]:
            lines.append(
                "DISCREPANCY "
                f"candidate_id={d['candidate_id']} symbol={d['symbol']} "
                f"state={d['last_state']} reason={d['reason']}"
            )
        return "\n".join(lines)

    def log_reconciliation(self) -> None:
        _log.info(self.format_report())


funnel_ledger = FunnelLedger()


def make_candidate_id(symbol: str, strategy_id: str, event_ts: Optional[float] = None) -> str:
    ts = int((event_ts or _now()) * 1000)
    return f"{symbol.upper()}:{strategy_id}:{ts}"
