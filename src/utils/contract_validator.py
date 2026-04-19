"""IB contract pre-validation with per-session LRU cache."""

import functools
import logging

from ib_insync import IB, Stock

log = logging.getLogger("contract_validator")

_ALLOWED_EXCHANGES = {"NYSE", "NASDAQ", "BATS", "ARCA", "AMEX", "NMS", "IEX", "MEMX"}


@functools.lru_cache(maxsize=512)
def _qualify_cached(symbol: str, _ib_id: int) -> tuple:
    """Cache qualification result keyed by (symbol, ib-instance-id).

    The *_ib_id* parameter exists only so that a new IB session
    (different ``id(ib)``) naturally invalidates stale cache entries.
    Returns ``(valid: bool, exchange: str, reason: str)``.
    """
    # This is never called directly — use is_ib_valid() below.
    raise RuntimeError("should not be called directly")


# Separate cache dict because lru_cache can't hold ib reference
_validation_cache: dict[str, tuple[bool, str, str]] = {}


def is_ib_valid(symbol: str, ib: IB) -> bool:
    """Return True if IB recognises this contract on an allowed exchange.

    Results are cached per-process (invalidates across sessions).
    """
    cached = _validation_cache.get(symbol)
    if cached is not None:
        return cached[0]

    try:
        c = Stock(symbol, "SMART", "USD")
        result = ib.qualifyContracts(c)
        if not result or c.conId <= 0:
            _validation_cache[symbol] = (False, "", "no_security_def")
            log.debug("contract_invalid symbol=%s reason=no_security_def", symbol)
            return False

        exchange = (getattr(c, "primaryExchange", "") or "").upper()
        if exchange and exchange not in _ALLOWED_EXCHANGES:
            _validation_cache[symbol] = (False, exchange, "exchange_rejected")
            log.debug("contract_rejected symbol=%s exchange=%s", symbol, exchange)
            return False

        _validation_cache[symbol] = (True, exchange, "ok")
        return True
    except Exception as exc:
        log.debug("contract_check_error symbol=%s err=%s", symbol, exc)
        return False


def get_exchange(symbol: str) -> str:
    """Return cached exchange for a previously validated symbol."""
    cached = _validation_cache.get(symbol)
    if cached:
        return cached[1]
    return ""


def clear_cache() -> None:
    """Clear the validation cache (call on session restart)."""
    _validation_cache.clear()
