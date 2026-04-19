from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.monitoring.funnel_ledger import FunnelEvent, funnel_ledger

_log = logging.getLogger("trade_labs.reject_monitor")


class RejectStage:
    SIGNAL = "signal"
    RISK = "risk"
    EXECUTION = "execution"
    CANCEL_REPLACE = "cancel_replace"


class RejectType:
    POLICY = "policy_reject"
    RISK = "risk_reject"
    EXECUTION = "execution_reject"
    CANCEL_REPLACE = "cancel_replace_reject"
    OPERATIONAL = "operational_reject"


@dataclass
class RejectEvent:
    reject_id: str
    candidate_id: str
    intent_id: str
    order_id: str
    symbol: str
    stage: str
    reject_type: str
    reject_reason_code: str
    reject_message: str
    session_label: str
    strategy_id: str
    ts_event: float
    raw_context: Dict[str, Any] = field(default_factory=dict)


def _now() -> float:
    return time.time()


def _today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _mk_reject_id(stage: str, symbol: str, ts_event: float) -> str:
    return f"{stage}:{symbol.upper()}:{int(ts_event * 1000)}"


class RejectEventMonitor:
    def __init__(self, path: Optional[str] = None):
        self._path = Path(path or os.environ.get("TL_REJECT_EVENTS_PATH", "data/reject_events.jsonl"))
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def _append(self, event: RejectEvent) -> None:
        payload = asdict(event)
        payload["session_date"] = datetime.fromtimestamp(event.ts_event, tz=timezone.utc).strftime("%Y-%m-%d")
        line = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        with self._path.open("a", encoding="utf-8") as f:
            f.write(line)
            f.write("\n")

    def record_reject(
        self,
        *,
        candidate_id: str,
        symbol: str,
        stage: str,
        reject_type: str,
        reject_reason_code: str,
        reject_message: str,
        session_label: str,
        strategy_id: str = "",
        intent_id: str = "",
        order_id: str = "",
        ts_event: Optional[float] = None,
        raw_context: Optional[Dict[str, Any]] = None,
    ) -> str:
        ts = ts_event or _now()
        rid = _mk_reject_id(stage, symbol, ts)
        evt = RejectEvent(
            reject_id=rid,
            candidate_id=candidate_id,
            intent_id=intent_id,
            order_id=order_id,
            symbol=symbol,
            stage=stage,
            reject_type=reject_type,
            reject_reason_code=reject_reason_code,
            reject_message=reject_message,
            session_label=session_label,
            strategy_id=strategy_id,
            ts_event=ts,
            raw_context=raw_context or {},
        )

        with self._lock:
            self._append(evt)

        if stage == RejectStage.SIGNAL:
            funnel_ledger.record(
                event_type=FunnelEvent.SESSION_BLOCKED,
                candidate_id=candidate_id,
                symbol=symbol,
                strategy_id=strategy_id,
                session_label=session_label,
                block_reason=reject_reason_code,
                emitted_intent_id=intent_id,
                notes=f"reject_id={rid}",
                event_ts=ts,
            )
        elif stage == RejectStage.RISK:
            funnel_ledger.record(
                event_type=FunnelEvent.RISK_BLOCKED,
                candidate_id=candidate_id,
                symbol=symbol,
                strategy_id=strategy_id,
                session_label=session_label,
                block_reason=reject_reason_code,
                emitted_intent_id=intent_id,
                notes=f"reject_id={rid}",
                event_ts=ts,
            )
        elif stage in (RejectStage.EXECUTION, RejectStage.CANCEL_REPLACE):
            funnel_ledger.record(
                event_type=FunnelEvent.EXECUTION_REJECTED,
                candidate_id=candidate_id,
                symbol=symbol,
                strategy_id=strategy_id,
                session_label=session_label,
                block_reason=reject_reason_code,
                emitted_intent_id=intent_id,
                execution_order_id=order_id,
                notes=f"reject_id={rid}",
                event_ts=ts,
            )

        _log.info(
            "REJECT_EVENT reject_id=%s stage=%s reason=%s symbol=%s candidate_id=%s intent_id=%s order_id=%s msg=%s",
            rid,
            stage,
            reject_reason_code,
            symbol,
            candidate_id,
            intent_id,
            order_id,
            reject_message,
        )
        return rid

    def _read_events(self) -> List[Dict[str, Any]]:
        if not self._path.exists():
            return []
        out: List[Dict[str, Any]] = []
        with self._path.open("r", encoding="utf-8") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    out.append(json.loads(raw))
                except json.JSONDecodeError:
                    continue
        return out

    def all_rejects_for_current_session(self) -> List[Dict[str, Any]]:
        today = _today_utc()
        return [e for e in self._read_events() if e.get("session_date") == today]

    def rejects_by_symbol(self, symbol: str) -> List[Dict[str, Any]]:
        s = symbol.upper().strip()
        return [e for e in self._read_events() if str(e.get("symbol", "")).upper() == s]

    def rejects_by_reason_code(self, reason_code: str) -> List[Dict[str, Any]]:
        rc = reason_code.strip().upper()
        return [e for e in self._read_events() if str(e.get("reject_reason_code", "")).upper() == rc]

    def rejects_by_stage(self, stage: str) -> List[Dict[str, Any]]:
        st = stage.strip().lower()
        return [e for e in self._read_events() if str(e.get("stage", "")).lower() == st]

    def top_reject_reasons(self, limit: int = 10) -> Dict[str, int]:
        c = Counter()
        for e in self._read_events():
            code = str(e.get("reject_reason_code", "")).upper().strip()
            if code:
                c[code] += 1
        return dict(c.most_common(max(1, limit)))

    def format_report(self, limit: int = 20) -> str:
        rows = self.all_rejects_for_current_session()
        by_stage = Counter(str(r.get("stage", "")).lower() for r in rows)
        by_reason = Counter(str(r.get("reject_reason_code", "")).upper() for r in rows)
        lines = [
            "REJECT_EVENT_REPORT",
            f"session_date={_today_utc()}",
            f"total_rejects={len(rows)}",
            f"by_stage={json.dumps(dict(by_stage), sort_keys=True)}",
            f"top_reasons={json.dumps(dict(by_reason.most_common(10)), sort_keys=True)}",
        ]
        for r in rows[-limit:]:
            lines.append(
                "REJECT "
                f"reject_id={r.get('reject_id','')} "
                f"stage={r.get('stage','')} "
                f"symbol={r.get('symbol','')} "
                f"reason={r.get('reject_reason_code','')} "
                f"candidate_id={r.get('candidate_id','')}"
            )
        return "\n".join(lines)


reject_monitor = RejectEventMonitor()
