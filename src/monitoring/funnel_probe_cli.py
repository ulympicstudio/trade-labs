"""Print a FUNNELPROBE_SNAPSHOT line from the current Python process.

NOTE: funnel_probe is an in-memory singleton.  Counters are only meaningful
when this module is called from within the same running process (e.g. imported
by the live loop or monitor arm).  Invoking this as a standalone subprocess
after the live loop will always print zeros.  For durable post-run validation
use the JSONL-backed CLIs instead:

    python -m src.monitoring.funnel_reconcile_cli
    python -m src.monitoring.reject_event_cli --report
"""
from __future__ import annotations

from src.monitoring.funnel_probe import funnel_probe


def main() -> None:
    s = funnel_probe.snapshot()
    print(
        f"FUNNELPROBE_SNAPSHOT "
        f"total_candidates_seen={s.total_candidates_seen} "
        f"signal_session_policy_rejects={s.signal_session_policy_rejects} "
        f"execution_session_policy_rejects={s.execution_session_policy_rejects}"
    )


if __name__ == "__main__":
    main()
