from dataclasses import dataclass
from typing import List, Optional, Tuple

from ib_insync import IB, ScannerSubscription, Stock


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


def _looks_like_etf(long_name: str) -> bool:
    name = (long_name or "").upper()
    return any(tag in name for tag in (" ETF", "ETN", " TRUST", " FUND", " INDEX"))


def scan_us_most_active(ib: IB, limit: int = 50) -> List[ScanResult]:
    sub = ScannerSubscription(
        instrument="STK",
        locationCode="STK.US.MAJOR",
        scanCode="MOST_ACTIVE",
    )

    results = ib.reqScannerData(sub)
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
