#!/usr/bin/env python3
"""Minimal OFFHOURS_PAPER_TEST sanity printer."""

from __future__ import annotations

from config.runtime import get_resolved_config


def main() -> None:
    cfg = get_resolved_config()
    print(
        "OFFHOURS_PAPER_TEST_SANITY "
        f"paper_ah_test={cfg.paper_ah_test} "
        f"allow_extended={cfg.allow_extended} "
        f"ah_entry_enabled={cfg.ah_entry_enabled} "
        f"synthetic_ok={cfg.synthetic_ok} "
        f"require_live_quotes={cfg.require_live_quotes} "
        f"ah_max_open_pos={cfg.ah_max_open_pos} "
        f"ah_risk_per_trade={cfg.ah_risk_per_trade} "
        f"ah_armed={cfg.ah_armed}"
    )


if __name__ == "__main__":
    main()
