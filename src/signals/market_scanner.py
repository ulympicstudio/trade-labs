import logging
from dataclasses import dataclass
from typing import List, Optional, Tuple

from ib_insync import IB, ScannerSubscription, Stock

log = logging.getLogger(__name__)


@dataclass
class ScanResult:
    symbol: str
    rank: int


LEVERAGED_OR_INVERSE_BLOCKLIST = {
    "TQQQ", "SQQQ", "SOXL", "SOXS", "TNA", "TZA",
    "SPXL", "SPXS", "UVXY", "SVXY", "ZSL", "UGL",
    "TSLL", "TSLS", "UVIX", "DUST",
}

ETF_ALLOWLIST = {"SPY", "QQQ"}
ETF_BLOCKLIST = {"BITO"}

# ---- Scanner retry config ----
_SCANNER_MAX_RETRIES = 3
_SCANNER_BACKOFF_BASE = 2.0  # seconds; retries wait 2, 4, 8 …


def _looks_like_etf(long_name: str) -> bool:
    name = (long_name or "").upper()
    return any(tag in name for tag in (" ETF", "ETN", " TRUST", " FUND", " INDEX"))


def _req_scanner_with_retry(ib: IB, sub: ScannerSubscription) -> list:
    """
    Call reqScannerData with retry + exponential backoff.

    Error 162 usually fires on the *cancel* step after data is already
    received — that is harmless.  We only retry when 162 fires AND the
    result set is empty (meaning the subscription itself failed).
    """
    last_err: Optional[Exception] = None
    captured_162 = False

    def _on_error(*args):
        nonlocal captured_162
        # args: (reqId, errorCode, errorString[, contract])
        errorCode = args[1] if len(args) > 1 else None
        if errorCode == 162:
            captured_162 = True
            log.debug("IB error 162 (scanner cancel noise) – suppressed")

    ib.errorEvent += _on_error

    try:
        for attempt in range(1, _SCANNER_MAX_RETRIES + 1):
            captured_162 = False
            try:
                results = ib.reqScannerData(sub)

                # Data came back → return it (162 on cancel is harmless)
                if results:
                    return results

                # No data and no 162 → market closed / no matches
                if not captured_162:
                    return results

                # Empty + 162  → subscription itself failed, worth retrying
                log.info("Scanner attempt %d/%d: empty + 162, retrying …",
                         attempt, _SCANNER_MAX_RETRIES)

            except Exception as e:
                last_err = e
                log.warning("Scanner attempt %d/%d exception: %s",
                            attempt, _SCANNER_MAX_RETRIES, e)

            wait = _SCANNER_BACKOFF_BASE * (2 ** (attempt - 1))
            ib.sleep(wait)

        if last_err:
            raise last_err
        return []
    finally:
        ib.errorEvent -= _on_error


def scan_us_most_active(ib: IB, limit: int = 50) -> List[ScanResult]:
    sub = ScannerSubscription(
        instrument="STK",
        locationCode="STK.US.MAJOR",
        scanCode="MOST_ACTIVE",
        numberOfRows=max(limit, 50),   # ask IB for enough rows up front
    )

    results = _req_scanner_with_retry(ib, sub)
    out: List[ScanResult] = []
    for r in results[:limit]:
        details = r.contractDetails
        c = details.contract
        symbol = c.symbol

        if symbol in ETF_BLOCKLIST:
            continue

        if symbol not in ETF_ALLOWLIST and _looks_like_etf(details.longName):
            continue

        out.append(ScanResult(symbol=symbol, rank=int(r.rank)))
    return out


# Backwards compatible name expected by some modules
def scan_us_most_active_stocks(ib: IB, limit: int = 50) -> List[ScanResult]:
    return scan_us_most_active(ib, limit=limit)


def to_contract(symbol: str) -> Stock:
    """
    Contract helper used by execution pipeline.
    Always SMART for US stocks.
    """
    return Stock(symbol, "SMART", "USD")


def get_quote(ib: IB, symbol: str) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    """
    Snapshot quote (bid/ask/last).
    """
    contract = to_contract(symbol)
    ib.qualifyContracts(contract)
    t = ib.reqMktData(contract, "", True, False)  # snapshot=True
    ib.sleep(1.0)

    bid = float(t.bid) if t.bid is not None else None
    ask = float(t.ask) if t.ask is not None else None
    last = float(t.last) if t.last is not None else None
    return bid, ask, last


def passes_quality_filters(
    symbol: str,
    bid: Optional[float],
    ask: Optional[float],
    last: Optional[float],
    min_price: float = 5.0,
    max_spread_pct: float = 0.0015,
    block_leveraged_etfs: bool = True,
) -> bool:
    if block_leveraged_etfs and symbol in LEVERAGED_OR_INVERSE_BLOCKLIST:
        return False

    # determine price
    if last is None:
        if bid is None or ask is None:
            return False
        price = (bid + ask) / 2.0
    else:
        price = last

    if price < min_price:
        return False

    if bid is None or ask is None or bid <= 0:
        return False

    spread = ask - bid
    if spread / price > max_spread_pct:
        return False

    return True
