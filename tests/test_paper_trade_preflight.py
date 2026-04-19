"""Paper-trade preflight tests for _compute_limit_price() robustness.

Verifies the 3-tier fallback chain:
  ask → historical bar close → fallback_price → ValueError

Run with:  pytest tests/test_paper_trade_preflight.py -v
"""

import math
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from src.execution.orders import _compute_limit_price, _compute_stop


# ── Helpers ──────────────────────────────────────────────────────────

def _make_ticker(ask=float("nan"), last=float("nan")):
    t = MagicMock()
    t.ask = ask
    t.last = last
    return t


def _make_bar(close=150.0, high=155.0, low=145.0):
    b = MagicMock()
    b.close = close
    b.high = high
    b.low = low
    return b


def _make_ib(ticker=None, bars=None, mktdata_exc=None, hist_exc=None):
    """Create a mock IB with configurable reqMktData / reqHistoricalData."""
    ib = MagicMock()
    ib.sleep.return_value = None

    if mktdata_exc:
        ib.reqMktData.side_effect = mktdata_exc
    elif ticker is not None:
        ib.reqMktData.return_value = ticker
    else:
        ib.reqMktData.return_value = _make_ticker()

    if hist_exc:
        ib.reqHistoricalData.side_effect = hist_exc
    elif bars is not None:
        ib.reqHistoricalData.return_value = bars
    else:
        ib.reqHistoricalData.return_value = []

    ib.cancelMktData.return_value = None
    return ib


# ═══════════════════════════════════════════════════════════════════════
# _compute_limit_price tests
# ═══════════════════════════════════════════════════════════════════════

class TestComputeLimitPrice:

    def test_ask_price_used_when_available(self):
        """Tier 1: valid ask → used with offset."""
        ib = _make_ib(ticker=_make_ticker(ask=100.0, last=99.0))
        price = _compute_limit_price(ib, "AAPL")
        expected = round(100.0 * 1.001, 2)
        assert price == expected

    def test_last_price_fallback_when_ask_nan(self):
        """Tier 1 fallback: ask NaN → use last."""
        ib = _make_ib(ticker=_make_ticker(ask=float("nan"), last=99.0))
        price = _compute_limit_price(ib, "AAPL")
        expected = round(99.0 * 1.001, 2)
        assert price == expected

    def test_historical_bar_fallback_when_ask_and_last_nan(self):
        """Tier 2: ask and last both NaN → use historical bar close."""
        bar = _make_bar(close=98.0)
        ib = _make_ib(ticker=_make_ticker(), bars=[bar])
        price = _compute_limit_price(ib, "AAPL")
        expected = round(98.0 * 1.001, 2)
        assert price == expected

    def test_fallback_price_used_when_all_else_fails(self):
        """Tier 3: ask/last NaN + no historical data → use fallback_price."""
        ib = _make_ib(ticker=_make_ticker(), bars=[])
        price = _compute_limit_price(ib, "AAPL", fallback_price=95.0)
        expected = round(95.0 * 1.001, 2)
        assert price == expected

    def test_raises_when_all_exhausted(self):
        """All tiers exhausted → ValueError."""
        ib = _make_ib(ticker=_make_ticker(), bars=[])
        with pytest.raises(ValueError, match="all exhausted"):
            _compute_limit_price(ib, "AAPL")

    def test_reqmktdata_exception_falls_through_to_historical(self):
        """reqMktData throws → gracefully falls to tier 2."""
        bar = _make_bar(close=97.0)
        ib = _make_ib(
            mktdata_exc=ConnectionError("no connection"),
            bars=[bar],
        )
        price = _compute_limit_price(ib, "AAPL")
        expected = round(97.0 * 1.001, 2)
        assert price == expected


# ═══════════════════════════════════════════════════════════════════════
# _compute_stop tests
# ═══════════════════════════════════════════════════════════════════════

class TestComputeStop:

    def test_atr_stop_calculation(self):
        """Verify ATR-based stop from 5 bars."""
        bars = [_make_bar(close=100 + i, high=105 + i, low=95 + i) for i in range(5)]
        ib = MagicMock()
        ib.reqHistoricalData.return_value = bars
        stop = _compute_stop(ib, "AAPL")
        # Each bar: high-low = 10, ATR = 10, last_close = bars[-1].close = 104
        expected = round(104.0 - 10.0 * 1.5, 2)
        assert stop == expected

    def test_insufficient_bars_raises(self):
        """Fewer than 2 bars → ValueError."""
        ib = MagicMock()
        ib.reqHistoricalData.return_value = [_make_bar()]
        with pytest.raises(ValueError, match="Insufficient bar data"):
            _compute_stop(ib, "AAPL")
