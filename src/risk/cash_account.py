"""Cash account mode — settled-cash exposure guard.

Provides the single authoritative check that prevents any order from
exceeding available settled cash.  Designed purely as a control-plane
guard: it does not touch strategy logic, scoring, or session policy.

Key behaviour
-------------
- cash_mode is True when account_type == "CASH".
- When cash_mode is True:
    * All exposure arithmetic uses settled_cash only, never buying power.
    * PDT counters and 25 k equity checks are bypassed (not tracked).
    * Margin, leverage, and short entries are blocked unconditionally.
    * Unsettled cash is tracked separately and excluded from capacity.
- When cash_mode is False (margin account) this module is a no-op passthrough.

Thread-safety
-------------
_CashAccountState uses a threading.Lock for all mutations.
The module-level singleton ``cash_account_state`` is safe to call from
multiple arm threads concurrently.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Optional, Dict

_log = logging.getLogger("trade_labs.cash_account")

# ── Constants ────────────────────────────────────────────────────────

ACCOUNT_TYPE_CASH = "CASH"
ACCOUNT_TYPE_MARGIN = "MARGIN"
ACCOUNT_TYPE_PORTFOLIO_MARGIN = "PORTFOLIO_MARGIN"
ACCOUNT_TYPE_UNKNOWN = "UNKNOWN"

# IBKR accountSummary tag names for cash fields
_TAG_TOTAL_CASH = "TotalCashValue"        # includes unsettled
_TAG_SETTLED_CASH = "SettledCash"         # fully settled; preferred
_TAG_CASH_BALANCE = "CashBalance"         # fallback if SettledCash absent
_TAG_NET_LIQ = "NetLiquidation"
_TAG_ACCT_TYPE = "AccountType"

# Fees buffer applied to every order to absorb commissions + exchange fees.
# Overridable via TL_CASH_FEES_BUFFER_USD.
_FEES_BUFFER_USD: float = float(os.environ.get("TL_CASH_FEES_BUFFER_USD", "2.0"))

# Staleness threshold: if account data is older than this, treat as stale.
_STALE_THRESHOLD_S: float = float(os.environ.get("TL_CASH_STALE_THRESHOLD_S", "120.0"))


# ── Data structures ──────────────────────────────────────────────────

@dataclass(frozen=True)
class CashCheckResult:
    """Result of a cash-mode order admission check."""
    approved: bool
    reject_reason: Optional[str]
    settled_cash: float
    current_gross_exposure: float
    order_cost: float
    total_exposure_if_filled: float
    headroom_usd: float
    account_type: str
    cash_mode: bool

    def log_line(self) -> str:
        """Compact, grep-friendly summary for every order decision."""
        return (
            f"CASH_GUARD approved={self.approved} "
            f"account_type={self.account_type} cash_mode={self.cash_mode} "
            f"settled_cash={self.settled_cash:.2f} "
            f"current_gross_exposure={self.current_gross_exposure:.2f} "
            f"order_cost={self.order_cost:.2f} "
            f"total_exposure_if_filled={self.total_exposure_if_filled:.2f} "
            f"headroom_usd={self.headroom_usd:.2f} "
            + (f"reject_reason={self.reject_reason}" if self.reject_reason else "")
        )


@dataclass
class _CashAccountState:
    """Mutable singleton state for the cash account mode guard."""
    account_type: str = ACCOUNT_TYPE_UNKNOWN
    cash_mode: bool = False

    settled_cash: float = 0.0
    unsettled_cash: float = 0.0
    total_cash_value: float = 0.0
    net_liquidation: float = 0.0

    # symbol → notional (qty * avg_entry_price) for all open long positions
    open_notionals: Dict[str, float] = field(default_factory=dict)

    last_updated_ts: float = 0.0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    # ── account state mutations ──────────────────────────────────────

    def update_from_ib_summary(self, tags: Dict[str, str]) -> None:
        """Parse a flat {tag: value} dict from IBKR reqAccountSummary."""
        with self._lock:
            raw_type = str(tags.get(_TAG_ACCT_TYPE, "")).upper()
            if raw_type:
                self.account_type = _normalise_account_type(raw_type)
                self.cash_mode = self.account_type == ACCOUNT_TYPE_CASH

            def _f(tag: str) -> float:
                try:
                    return float(tags.get(tag, 0.0) or 0.0)
                except (ValueError, TypeError):
                    return 0.0

            self.total_cash_value = _f(_TAG_TOTAL_CASH)
            self.net_liquidation = _f(_TAG_NET_LIQ)

            # Prefer SettledCash; fall back to CashBalance
            settled_raw = tags.get(_TAG_SETTLED_CASH)
            if settled_raw is not None:
                self.settled_cash = _f(_TAG_SETTLED_CASH)
                self.unsettled_cash = max(0.0, self.total_cash_value - self.settled_cash)
            else:
                # SettledCash not present: use CashBalance as best proxy
                self.settled_cash = _f(_TAG_CASH_BALANCE) or self.total_cash_value
                self.unsettled_cash = 0.0
                _log.warning(
                    "cash_account SettledCash tag absent; using CashBalance=%.2f as proxy",
                    self.settled_cash,
                )

            self.last_updated_ts = time.time()
            _log.info(
                "cash_account_update account_type=%s cash_mode=%s "
                "settled_cash=%.2f unsettled_cash=%.2f net_liq=%.2f",
                self.account_type, self.cash_mode,
                self.settled_cash, self.unsettled_cash, self.net_liquidation,
            )

    def set_account_type_manual(self, account_type: str) -> None:
        """Override account type (e.g. from env var at startup)."""
        with self._lock:
            self.account_type = _normalise_account_type(account_type.upper())
            self.cash_mode = self.account_type == ACCOUNT_TYPE_CASH
            _log.info(
                "cash_account_type_override account_type=%s cash_mode=%s",
                self.account_type, self.cash_mode,
            )

    # ── position notional tracking ───────────────────────────────────

    def record_fill(self, symbol: str, qty: int, fill_price: float) -> None:
        """Register a new long fill as open notional."""
        with self._lock:
            notional = round(qty * fill_price, 2)
            self.open_notionals[symbol] = notional
            _log.info(
                "cash_account_fill symbol=%s qty=%d fill=%.4f notional=%.2f "
                "gross_exposure=%.2f",
                symbol, qty, fill_price, notional, self._gross_exposure_locked(),
            )

    def record_close(self, symbol: str) -> None:
        """Remove a position from open notional tracking."""
        with self._lock:
            removed = self.open_notionals.pop(symbol, None)
            if removed is not None:
                _log.info(
                    "cash_account_close symbol=%s freed_notional=%.2f "
                    "gross_exposure=%.2f",
                    symbol, removed, self._gross_exposure_locked(),
                )

    # ── queries ──────────────────────────────────────────────────────

    def gross_exposure(self) -> float:
        with self._lock:
            return self._gross_exposure_locked()

    def _gross_exposure_locked(self) -> float:
        return round(sum(self.open_notionals.values()), 2)

    def headroom(self) -> float:
        """Maximum additional notional deployable right now."""
        with self._lock:
            return max(0.0, round(self.settled_cash - self._gross_exposure_locked(), 2))

    def capacity_snapshot(self) -> Dict[str, float]:
        """Return the current settled_cash, gross_exposure, and headroom."""
        with self._lock:
            ge = self._gross_exposure_locked()
            return {
                "settled_cash": self.settled_cash,
                "current_gross_exposure": ge,
                "max_additional_notional": max(0.0, round(self.settled_cash - ge, 2)),
                "unsettled_cash": self.unsettled_cash,
                "cash_mode": self.cash_mode,
                "account_type": self.account_type,
                "last_updated_ts": self.last_updated_ts,
            }

    def is_data_stale(self) -> bool:
        return (time.time() - self.last_updated_ts) > _STALE_THRESHOLD_S


# ── Module-level singleton ────────────────────────────────────────────

cash_account_state = _CashAccountState()

# Allow env-var bootstrap of account type before IB connects
_ENV_ACCOUNT_TYPE = os.environ.get("TL_ACCOUNT_TYPE", "").strip().upper()
if _ENV_ACCOUNT_TYPE:
    cash_account_state.set_account_type_manual(_ENV_ACCOUNT_TYPE)


# ── Core admission check ─────────────────────────────────────────────

def check_cash_order(
    symbol: str,
    direction: str,           # "LONG" or "SHORT"
    qty: int,
    limit_price: float,
    fees_buffer_usd: Optional[float] = None,
) -> CashCheckResult:
    """Evaluate whether an order fits within settled cash constraints.

    Must be called by the risk arm before emitting an OrderPlan.
    Always returns quickly (never blocks waiting for IB).

    Rejection reasons
    -----------------
    CASH_SHORT_BLOCKED      : short entries are never allowed in cash mode.
    CASH_DATA_STALE         : account data older than TL_CASH_STALE_THRESHOLD_S.
    CASH_ZERO_SETTLED       : settled_cash is zero (data not yet loaded).
    CASH_EXPOSURE_EXCEEDED  : total_exposure_if_filled > settled_cash.
    """
    state = cash_account_state
    fees = fees_buffer_usd if fees_buffer_usd is not None else _FEES_BUFFER_USD

    with state._lock:
        cash_mode = state.cash_mode
        account_type = state.account_type
        settled = state.settled_cash
        gross_exp = state._gross_exposure_locked()

    order_cost = round(qty * limit_price + fees, 2)
    total_exp = round(gross_exp + order_cost, 2)
    headroom = max(0.0, round(settled - gross_exp, 2))

    def _reject(reason: str) -> CashCheckResult:
        _log.info(
            "CASH_GUARD approved=False reason=%s account_type=%s cash_mode=%s "
            "settled_cash=%.2f current_gross_exposure=%.2f order_cost=%.2f "
            "total_exposure_if_filled=%.2f headroom_usd=%.2f symbol=%s qty=%d",
            reason, account_type, cash_mode,
            settled, gross_exp, order_cost, total_exp, headroom,
            symbol, qty,
        )
        return CashCheckResult(
            approved=False,
            reject_reason=reason,
            settled_cash=settled,
            current_gross_exposure=gross_exp,
            order_cost=order_cost,
            total_exposure_if_filled=total_exp,
            headroom_usd=headroom,
            account_type=account_type,
            cash_mode=cash_mode,
        )

    # ── pass-through when not in cash mode ───────────────────────────
    if not cash_mode:
        result = CashCheckResult(
            approved=True,
            reject_reason=None,
            settled_cash=settled,
            current_gross_exposure=gross_exp,
            order_cost=order_cost,
            total_exposure_if_filled=total_exp,
            headroom_usd=headroom,
            account_type=account_type,
            cash_mode=False,
        )
        _log.debug(result.log_line())
        return result

    # ── cash-mode checks ─────────────────────────────────────────────

    # 1. No short positions in a cash account
    if direction.upper() == "SHORT":
        return _reject("CASH_SHORT_BLOCKED")

    # 2. Stale data guard
    if state.is_data_stale():
        return _reject("CASH_DATA_STALE")

    # 3. Zero settled cash (data not yet loaded)
    if settled <= 0.0:
        return _reject("CASH_ZERO_SETTLED")

    # 4. Exposure check
    if total_exp > settled:
        return _reject("CASH_EXPOSURE_EXCEEDED")

    result = CashCheckResult(
        approved=True,
        reject_reason=None,
        settled_cash=settled,
        current_gross_exposure=gross_exp,
        order_cost=order_cost,
        total_exposure_if_filled=total_exp,
        headroom_usd=headroom,
        account_type=account_type,
        cash_mode=True,
    )
    _log.info(result.log_line())
    return result


# ── IB account summary poller ────────────────────────────────────────

def refresh_from_ib(ib: "IB") -> None:  # type: ignore[name-defined]
    """Pull a fresh accountSummary from a connected IB instance and update state.

    Safe to call from a background thread (e.g. every 60 s from the execution arm).
    """
    try:
        acct = ib.managedAccounts()[0]
        summary = ib.accountSummary(acct)
        tags: Dict[str, str] = {}
        for item in summary:
            if item.currency in ("USD", "BASE", ""):
                tags[item.tag] = item.value
        cash_account_state.update_from_ib_summary(tags)
    except Exception as exc:
        _log.warning("cash_account refresh_from_ib failed: %s", exc)


# ── Helpers ──────────────────────────────────────────────────────────

def _normalise_account_type(raw: str) -> str:
    if "CASH" in raw:
        return ACCOUNT_TYPE_CASH
    if "PORTFOLIO" in raw:
        return ACCOUNT_TYPE_PORTFOLIO_MARGIN
    if "MARGIN" in raw:
        return ACCOUNT_TYPE_MARGIN
    return ACCOUNT_TYPE_UNKNOWN
