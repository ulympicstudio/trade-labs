from __future__ import annotations

import argparse
import json

from src.monitoring.reject_event_monitor import reject_monitor


def main() -> None:
    p = argparse.ArgumentParser(description="Reject event monitor query CLI")
    p.add_argument("--symbol", default="", help="Filter by symbol")
    p.add_argument("--reason", default="", help="Filter by reject reason code")
    p.add_argument("--stage", default="", help="Filter by stage")
    p.add_argument("--top", type=int, default=0, help="Show top reject reasons")
    p.add_argument("--report", action="store_true", help="Print current-session report")
    args = p.parse_args()

    if args.report:
        print(reject_monitor.format_report())
        return

    if args.top > 0:
        print(json.dumps(reject_monitor.top_reject_reasons(args.top), indent=2, sort_keys=True))
        return

    if args.symbol:
        rows = reject_monitor.rejects_by_symbol(args.symbol)
        print(json.dumps(rows, indent=2, sort_keys=True))
        return

    if args.reason:
        rows = reject_monitor.rejects_by_reason_code(args.reason)
        print(json.dumps(rows, indent=2, sort_keys=True))
        return

    if args.stage:
        rows = reject_monitor.rejects_by_stage(args.stage)
        print(json.dumps(rows, indent=2, sort_keys=True))
        return

    rows = reject_monitor.all_rejects_for_current_session()
    print(json.dumps(rows, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
