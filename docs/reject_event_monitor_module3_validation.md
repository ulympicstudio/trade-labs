# Module 3 Assumptions and Validation Checklist

## Assumptions

- Reject events are persisted to data/reject_events.jsonl and are append-only.
- Reject monitor uses machine-stable stage and reason-code fields for filtering.
- Funnel ledger linkage is performed on every reject monitor write using candidate_id.
- No explicit cancel/replace workflow object was found in current arm flows; schema supports cancel_replace stage for future hooks.

## Validation Checklist

- expected session-policy reject:
  - trigger signal/execution session gate block
  - verify reject event stage=signal or stage=execution with policy reason code
- risk-limit reject:
  - trigger risk cap or risk guard rejection
  - verify reject event stage=risk and reason code from risk taxonomy
- broker/execution reject:
  - trigger place_order result ok=False or execution exception
  - verify reject event stage=execution and reason code BROKER_REJECT or EXECUTION_ERROR
- searchable reject list by symbol:
  - run python -m src.monitoring.reject_event_cli --symbol AAPL
  - verify only AAPL reject rows are returned
- top reject reasons summary:
  - run python -m src.monitoring.reject_event_cli --top 10
  - verify aggregated reason counts are returned
- reconciliation linkage to terminal reject outcomes:
  - run python -m src.monitoring.funnel_reconcile_cli
  - verify reject candidates appear under terminal reject outcomes and conservation remains valid

## OFFHOURS_PAPER_TEST Checklist (Module 3)

- run harness:
  - `bash scripts/run_offhours_paper_test.sh`
- run reject report CLI:
  - `python -m src.monitoring.reject_event_cli --report`
- filter rejects by symbol:
  - `python -m src.monitoring.reject_event_cli --symbol AAPL`
- filter rejects by reason code:
  - `python -m src.monitoring.reject_event_cli --reason SESSION_POLICY_BLOCK`
- fields to verify in report:
  - `total_rejects`
  - `by_stage`
  - `top_reasons`
