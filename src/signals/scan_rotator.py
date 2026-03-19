"""
ScanRotator – cycles through multiple IB scanner subscriptions so the
CandidatePool receives a wider variety of symbols each refill instead of
the same ~30 MOST_ACTIVE names every time.
"""
from __future__ import annotations

import logging
from typing import List, Optional, Sequence, Tuple

from ib_insync import IB, ScannerSubscription

from src.signals.market_scanner import (
    ScanResult,
    _looks_like_etf,
    _req_scanner_with_retry,
)
from config.universe_filter import STOCK_ALLOWLIST, STOCK_BLOCKLIST

log = logging.getLogger(__name__)

# Default scan definitions: (locationCode, scanCode)
DEFAULT_SCANS: List[Tuple[str, str]] = [
    ("STK.US.MAJOR", "MOST_ACTIVE"),
    ("STK.US.MAJOR", "TOP_PERC_GAIN"),
    ("STK.US.MAJOR", "HOT_BY_VOLUME"),
    ("STK.US", "MOST_ACTIVE"),
]


class ScanRotator:
    """Round-robin over several IB scanner subscriptions.

    Each call to ``next_scan`` fires the *next* subscription in the list,
    converts the raw results to ``ScanResult`` items (with the same
    filters used by ``market_scanner``), and advances the index.

    If a subscription errors out the rotator automatically tries the next
    one, up to ``len(scans)`` attempts per call so it never loops forever.
    """

    def __init__(
        self,
        scans: Optional[Sequence[Tuple[str, str]]] = None,
    ) -> None:
        self._scans: List[Tuple[str, str]] = list(scans or DEFAULT_SCANS)
        self._idx: int = 0

    # ---- public API --------------------------------------------------

    def next_scan(self, ib: IB, limit: int = 60) -> List[ScanResult]:
        """Run the next scanner subscription and return up to *limit* results."""
        attempts = len(self._scans)
        for _ in range(attempts):
            loc, code = self._scans[self._idx]
            self._idx = (self._idx + 1) % len(self._scans)
            try:
                results = self._run_one(ib, loc, code, limit)
                print(f"[ROTATOR] {code}@{loc} → {len(results)} symbols")
                return results
            except Exception as e:
                log.warning("[ROTATOR] %s@%s failed: %s – trying next", code, loc, e)
        # All scans failed in this round.
        print("[ROTATOR] all scan definitions failed this round")
        return []

    # ---- internals ---------------------------------------------------

    def _run_one(
        self, ib: IB, location: str, scan_code: str, limit: int
    ) -> List[ScanResult]:
        sub = ScannerSubscription(
            instrument="STK",
            locationCode=location,
            scanCode=scan_code,
        )
        raw = _req_scanner_with_retry(ib, sub)

        out: List[ScanResult] = []
        for r in raw[:limit]:
            details = r.contractDetails
            c = details.contract
            symbol = c.symbol

            # Price floor
            scanner_price = getattr(r, "price", None)
            if scanner_price is not None:
                try:
                    if float(scanner_price) < 2.0:
                        continue
                except Exception:
                    pass

            if c.secType != "STK":
                continue

            if symbol in STOCK_BLOCKLIST:
                continue

            # ETF / product filter
            try:
                full = ib.reqContractDetails(c)
                if full:
                    long_name = full[0].longName or ""
                    if symbol not in STOCK_ALLOWLIST and _looks_like_etf(long_name):
                        continue
            except Exception:
                continue

            out.append(ScanResult(symbol=symbol, rank=int(r.rank)))
        return out
