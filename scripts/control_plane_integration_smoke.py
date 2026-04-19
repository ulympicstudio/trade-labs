"""Control-plane smoke test using temporary JSONL files under /tmp/.

This script drives a few deterministic synthetic candidates through the real
session-policy, funnel-ledger, and reject-monitor modules without touching the
live loop or production business logic.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


FUNNEL_PATH = "/tmp/cp_smoke_funnel.jsonl"
REJECT_PATH = "/tmp/cp_smoke_rejects.jsonl"

os.environ["TL_FUNNEL_LEDGER_PATH"] = FUNNEL_PATH
os.environ["TL_REJECT_EVENTS_PATH"] = REJECT_PATH

from src.monitoring.funnel_ledger import FunnelEvent, FunnelLedger, make_candidate_id
from src.monitoring.reject_event_monitor import (
    RejectEventMonitor,
    RejectStage,
    RejectType,
)
from src.policies.session_policy import (
    QuoteContext,
    SessionContext,
    VenuePolicy,
    can_emit_entry,
)


def _reset_tmp_artifacts() -> None:
    for path in (FUNNEL_PATH, REJECT_PATH):
        target = Path(path)
        if target.exists():
            target.unlink()


def main() -> None:
    _reset_tmp_artifacts()

    ledger = FunnelLedger(FUNNEL_PATH)
    monitor = RejectEventMonitor(REJECT_PATH)
    base_ts = time.time()

    # 1. AFTERHOURS session-blocked candidate.
    afterhours_candidate_id = make_candidate_id("AHBLK", "cp_smoke_signal", base_ts + 1.0)
    afterhours_decision = can_emit_entry(
        SessionContext(session="AFTERHOURS", halted=False),
        QuoteContext(quote_present=True, quote_age_s=0.5, is_synthetic=False),
        VenuePolicy(
            allow_entries_afterhours=False,
            require_live_quotes=True,
            synthetic_ok=False,
        ),
    )
    if afterhours_decision.entry_enabled:
        raise AssertionError("Expected AFTERHOURS candidate to be blocked")
    if afterhours_decision.block_reason != "ENTRY_DISABLED_AFTERHOURS":
        raise AssertionError(
            f"Unexpected AFTERHOURS block reason: {afterhours_decision.block_reason}"
        )

    ledger.record_candidate_created(
        candidate_id=afterhours_candidate_id,
        symbol="AHBLK",
        strategy_id="cp_smoke_signal",
        session_label=afterhours_decision.session_label,
        event_ts=base_ts + 1.0,
        notes="forced_afterhours_session_block",
    )
    monitor.record_reject(
        candidate_id=afterhours_candidate_id,
        symbol="AHBLK",
        stage=RejectStage.SIGNAL,
        reject_type=RejectType.POLICY,
        reject_reason_code=afterhours_decision.block_reason or "UNKNOWN",
        reject_message="Forced AFTERHOURS session-policy block",
        session_label=afterhours_decision.session_label,
        strategy_id="cp_smoke_signal",
        ts_event=base_ts + 2.0,
        raw_context={
            "mode": afterhours_decision.mode,
            "require_live_quotes": afterhours_decision.require_live_quotes,
            "synthetic_ok": afterhours_decision.synthetic_ok,
        },
    )

    # 2. RTH risk-blocked candidate.
    risk_candidate_id = make_candidate_id("RSKBLK", "cp_smoke_risk", base_ts + 3.0)
    ledger.record_candidate_created(
        candidate_id=risk_candidate_id,
        symbol="RSKBLK",
        strategy_id="cp_smoke_risk",
        session_label="RTH",
        event_ts=base_ts + 3.0,
        notes="forced_risk_block",
    )
    monitor.record_reject(
        candidate_id=risk_candidate_id,
        symbol="RSKBLK",
        stage=RejectStage.RISK,
        reject_type=RejectType.RISK,
        reject_reason_code="RISK_BUDGET",
        reject_message="Forced risk-budget rejection",
        session_label="RTH",
        strategy_id="cp_smoke_risk",
        intent_id="intent-risk-1",
        ts_event=base_ts + 4.0,
        raw_context={"risk_budget_usd": 5.0, "proposed_risk_usd": 7.5},
    )

    # 3. RTH execution-accepted candidate.
    accepted_candidate_id = make_candidate_id("ACCEPT", "cp_smoke_exec", base_ts + 5.0)
    ledger.record_candidate_created(
        candidate_id=accepted_candidate_id,
        symbol="ACCEPT",
        strategy_id="cp_smoke_exec",
        session_label="RTH",
        event_ts=base_ts + 5.0,
        notes="forced_execution_accept",
    )
    ledger.record(
        event_type=FunnelEvent.INTENT_EMITTED,
        candidate_id=accepted_candidate_id,
        symbol="ACCEPT",
        strategy_id="cp_smoke_exec",
        session_label="RTH",
        emitted_intent_id="intent-accept-1",
        notes="forced_intent_emit",
        event_ts=base_ts + 6.0,
    )
    ledger.record(
        event_type=FunnelEvent.EXECUTION_ACCEPTED,
        candidate_id=accepted_candidate_id,
        symbol="ACCEPT",
        strategy_id="cp_smoke_exec",
        session_label="RTH",
        emitted_intent_id="intent-accept-1",
        execution_order_id="order-accept-1",
        notes="forced_execution_accept",
        event_ts=base_ts + 7.0,
    )

    print(f"TL_FUNNEL_LEDGER_PATH={FUNNEL_PATH}")
    print(f"TL_REJECT_EVENTS_PATH={REJECT_PATH}")
    print("--- FUNNEL_RECONCILE ---")
    print(ledger.format_report())
    print("--- REJECT_REPORT ---")
    print(monitor.format_report())
    print("--- REJECT_TOP_10 ---")
    print(monitor.top_reject_reasons(limit=10))


if __name__ == "__main__":
    main()