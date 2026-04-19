from __future__ import annotations

from src.monitoring.funnel_ledger import funnel_ledger


def main() -> None:
    print(funnel_ledger.format_report())


if __name__ == "__main__":
    main()
