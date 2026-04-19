# Module 2 Assumptions and Validation Checklist

## Assumptions

- candidate_id is propagated on TradeIntent and OrderPlan; when absent, a stable fallback is generated from symbol/strategy/timestamp.
- Funnel ledger is append-only JSONL at data/funnel_ledger.jsonl (overridable via TL_FUNNEL_LEDGER_PATH).
- Reconciliation treats candidates without terminal state as inflight and flags stale inflight as MISSING_TERMINAL_EVENT.
- Existing trading logic and decision outcomes are unchanged; this module is observational/audit only.

## Validation Checklist

- normal accounted-for live flow:
  - expect candidate_created -> intent_emitted -> execution_accepted
  - reconciliation: total_unaccounted_for=0, no discrepancy for candidate
- session-blocked candidate:
  - expect candidate_created -> session_blocked
  - counts_by_block_reason includes session policy code
- risk-blocked candidate:
  - expect candidate_created -> intent_emitted -> risk_blocked
  - counts_by_terminal_state includes risk_blocked
- duplicate-suppressed candidate:
  - expect duplicate_suppressed with COOLDOWN_ACTIVE or HOURLY_CAP
  - included in terminal counts
- intentionally deferred candidate:
  - expect deferred with THROTTLE_SUPPRESS or budget reason
  - included in terminal counts
- missing terminal event discrepancy alert:
  - create candidate with no terminal follow-up beyond TL_FUNNEL_INFLIGHT_STALE_S
  - reconciliation includes discrepancy record with reason=MISSING_TERMINAL_EVENT

## OFFHOURS_PAPER_TEST Checklist (Module 2)

- run harness:
  - `bash scripts/run_offhours_paper_test.sh`
- run funnel reconcile CLI:
  - `python -m src.monitoring.funnel_reconcile_cli`
- fields to verify in report:
  - `total_in`
  - `total_accounted_for`
  - `total_unaccounted_for`
  - `conservation_ok`
  - `counts_by_terminal_state`
- expected normal-case checks:
  - `conservation_ok=true`
  - `total_unaccounted_for=0`
  - no `DISCREPANCY` rows for recent candidates
