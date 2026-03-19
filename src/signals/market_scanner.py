import logging
from dataclasses import dataclass
from typing import List, Optional, Tuple

from ib_insync import IB, ScannerSubscription, Stock

from src.broker.ib_session import get_ib
from config.universe_filter import STOCK_ALLOWLIST, STOCK_BLOCKLIST

log = logging.getLogger(__name__)


@dataclass
class ScanResult:
    symbol: str
    rank: int


# ---- Scanner retry config ----
_SCANNER_MAX_RETRIES = 3
_SCANNER_BACKOFF_BASE = 2.0  # seconds; retries wait 2, 4, 8 …


def _looks_like_etf(long_name: str) -> bool:
    """
    Check if a security is an ETF/ETN/FUND/leveraged product by examining longName.
    Be conservative: only reject if it's CLEARLY not a tradeable common stock.
    """
    if not long_name:
        return False
    name = long_name.upper()

    # High-confidence ETF/product keywords
    if " ETF" in name or " ETN" in name:
        return True

    # Leveraged products (ProShares ULTRA, ULTRAPRO, etc.)
    if "ULTRA" in name or "PROSHARES" in name or "DIREXION" in name:
        return True

    # Explicit leverage multipliers (2X, 3X, etc.)
    if "2X" in name or "3X" in name or "BULL" in name or "BEAR" in name or "SHORT" in name:
        return True

    # Crypto/blockchain-specific products (these are nearly always problematic)
    if "BITCOIN" in name or "ETHEREUM" in name or "CRYPTO" in name or "BLOCKCHAIN" in name:
        if "TRUST" in name or "FUND" in name or "NOTE" in name:
            return True

    return False


def _req_scanner_with_retry(ib: IB, sub: ScannerSubscription) -> list:
    """
    Call reqScannerData with retry + exponential backoff.

    Error 162 usually fires on the *cancel* step after data is already
    received — that is harmless. We only retry when 162 fires AND the
    result set is empty (meaning the subscription itself failed).
    """
    last_err: Optional[Exception] = None
    captured_162 = False

    def _on_error(*args):
        nonlocal captured_162
        # args: (reqId, errorCode, errorString[, contract])
        error_code = args[1] if len(args) > 1 else None
        if error_code == 162:
            captured_162 = True
            log.debug("IB error 162 (scanner cancel noise)")

    ib.errorEvent += _on_error

    try:
        for attempt in range(1, _SCANNER_MAX_RETRIES + 1):
            captured_162 = False
            try:
                results = ib.reqScannerData(sub)

                # Data came back → return it (162 on cancel is harmless)
                if results:
                    log.info("Scanner returned %d results (162 seen: %s)", len(results), captured_162)
                    return results

                # No data. If 162 fired → subscription failed, retry.
                # If no 162 → market closed / no matches, return empty.
                if captured_162:
                    log.info("Scanner attempt %d/%d: empty + 162, retrying …", attempt, _SCANNER_MAX_RETRIES)
                else:
                    log.info("Scanner returned 0 results, no error")
                    return results

            except Exception as e:
                last_err = e
                log.warning("Scanner attempt %d/%d exception: %s", attempt, _SCANNER_MAX_RETRIES, e)

            wait = _SCANNER_BACKOFF_BASE * (2 ** (attempt - 1))
            ib.sleep(wait)

        if last_err:
            raise last_err
        return []
    finally:
        ib.errorEvent -= _on_error


def _ensure_ib(ib: Optional[IB]) -> Tuple[IB, bool]:
    """
    Return an IB instance and whether we created it.
    If we create it, we will connect and later should disconnect.
    """
    if ib is not None:
        return ib, False

    new_ib = get_ib()
    return new_ib, True


def scan_us_most_active(ib: Optional[IB] = None, limit: int = 50) -> List[ScanResult]:
    """
    Scan US most-active stocks. If `ib` is None, we create and connect an IB instance
    (requires TWS/IB Gateway running). If you run this without TWS/Gateway, it will fail.
    """
    ib, created_ib = _ensure_ib(ib)

    try:
        sub = ScannerSubscription(
            instrument="STK",
            locationCode="STK.US.MAJOR",
            scanCode="MOST_ACTIVE",
        )

        results = _req_scanner_with_retry(ib, sub)
        out: List[ScanResult] = []
        filtered_count = 0

        for r in results[:limit]:
            details = r.contractDetails
            c = details.contract
            symbol = c.symbol

            # Quick price filter using scanner's reported price
            scanner_price = getattr(r, "price", None)
            if scanner_price is not None:
                try:
                    if float(scanner_price) < 2.0:
                        log.debug("Scanner: %s rejected (scanner price=$%.2f < $2.0)", symbol, float(scanner_price))
                        filtered_count += 1
                        continue
                except Exception:
                    # If price isn't parseable, don't use it as a filter
                    pass

            # Must be STK secType (explicit check)
            if c.secType != "STK":
                log.debug("Scanner: %s rejected (secType=%s, not STK)", symbol, c.secType)
                filtered_count += 1
                continue

            # Blocklist
            if symbol in STOCK_BLOCKLIST:
                log.debug("Scanner: %s rejected (in blocklist)", symbol)
                filtered_count += 1
                continue

            # Fetch full contract details to get accurate longName
            try:
                full_details = ib.reqContractDetails(c)
                if full_details:
                    long_name = (full_details[0].longName or "")
                    # Reject ETFs/products unless explicitly allowlisted
                    if symbol not in STOCK_ALLOWLIST and _looks_like_etf(long_name):
                        log.debug("Scanner: %s rejected (longName='%s')", symbol, long_name)
                        filtered_count += 1
                        continue
            except Exception as e:
                log.warning("Scanner: %s could not fetch details: %s", symbol, e)
                filtered_count += 1
                continue

            out.append(ScanResult(symbol=symbol, rank=int(r.rank)))

        if filtered_count > 0:
            log.info("Scanner: filtered %d ETF/products, kept %d stocks", filtered_count, len(out))

        return out

    finally:
        if created_ib:
            try:
                if ib.isConnected():
                    ib.disconnect()
            except Exception:
                pass


# Backwards compatible name expected by some modules
def scan_us_most_active_stocks(ib: Optional[IB] = None, limit: int = 50) -> List[ScanResult]:
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
    min_price: float = 2.0,
    max_spread_pct: float = 0.0015,
) -> bool:
    """
    Basic liquidity/quality filters.
    """
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

    # blocklist safety
    if symbol in STOCK_BLOCKLIST:
        return False

    return True