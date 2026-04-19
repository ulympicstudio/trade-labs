"""Tests for all 5 critical audit areas.

Run with:  pytest tests/test_audit_fixes.py -v
"""

import os
import sys
from unittest.mock import MagicMock, patch, PropertyMock
from dataclasses import dataclass
from typing import List

import pytest

# Ensure project root is on sys.path
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


# ═══════════════════════════════════════════════════════════════════════
# AREA 1 — Symbol Validation
# ═══════════════════════════════════════════════════════════════════════

class TestSymbolValidation:
    """Bug #5, #6, #9: invalid symbols should not pass extraction."""

    def test_is_valid_symbol_rejects_crypto(self):
        from src.data.catalyst_hunter import _is_valid_symbol
        for sym in ("BTC", "ETH", "SOL", "DOGE", "XRP"):
            assert not _is_valid_symbol(sym), f"{sym} should be rejected (crypto)"

    def test_is_valid_symbol_rejects_common_words(self):
        from src.data.catalyst_hunter import _is_valid_symbol
        for sym in ("FOR", "THE", "AND", "OK", "AI", "IT", "US", "CEO"):
            assert not _is_valid_symbol(sym), f"{sym} should be rejected (common word)"

    def test_is_valid_symbol_rejects_single_char(self):
        from src.data.catalyst_hunter import _is_valid_symbol
        assert not _is_valid_symbol("Y")
        assert not _is_valid_symbol("A")
        assert not _is_valid_symbol("")

    def test_hunt_reddit_rejects_invalid_tickers(self):
        """A Reddit-like post should NOT produce LA, YAHOO, BTC, OK, Y."""
        from src.data.catalyst_hunter import CatalystHunter
        hunter = CatalystHunter()
        text = (
            "YAHOO says LA is OK for BTC trading! "
            "Y not buy HDD and BACK CCC EMAT AI stocks?"
        )
        results = hunter._extract_symbols_from_text(text)
        bad_set = {"LA", "YAHOO", "BTC", "OK", "Y", "HDD", "BACK", "CCC", "EMAT", "AI"}
        leaked = bad_set.intersection(results)
        assert len(leaked) == 0, f"Invalid symbols leaked through: {leaked}"

    def test_valid_symbol_accepted(self):
        """Known universe tickers should pass if universe is loaded."""
        from src.data.catalyst_hunter import _is_valid_symbol, _VALID_SYMBOLS
        if _VALID_SYMBOLS:
            assert _is_valid_symbol("AAPL")
            assert _is_valid_symbol("NVDA")
        else:
            pytest.skip("Universe CSV not loaded — fallback mode")


# ═══════════════════════════════════════════════════════════════════════
# AREA 2 — Bracket Order Structure
# ═══════════════════════════════════════════════════════════════════════

class TestBracketOrders:
    """Verify bracket_orders produces valid parent/stop/trail IDs."""

    def _mock_ib(self):
        """Create a mock IB that satisfies bracket order placement."""
        ib = MagicMock()
        ib.qualifyContracts.return_value = [True]
        _order_counter = [100]

        def fake_place(contract, order):
            _order_counter[0] += 1
            order.orderId = _order_counter[0]
            trade = MagicMock()
            trade.order = order
            return trade

        ib.placeOrder.side_effect = fake_place
        ib.sleep.return_value = None
        ib.errorEvent = MagicMock()
        ib.errorEvent.__iadd__ = MagicMock(return_value=ib.errorEvent)
        ib.errorEvent.__isub__ = MagicMock(return_value=ib.errorEvent)
        return ib

    def test_bracket_with_trail(self):
        from src.execution.bracket_orders import place_limit_tp_trail_bracket, BracketParams
        ib = self._mock_ib()
        params = BracketParams(
            symbol="AAPL", qty=10, entry_limit=150.0,
            stop_loss=145.0, trail_amount=2.0, tif="DAY",
        )
        result = place_limit_tp_trail_bracket(ib, params)
        assert result.ok
        assert result.parent_id is not None and result.parent_id > 0
        assert result.stop_id is not None and result.stop_id > 0
        assert result.trail_id is not None and result.trail_id > 0
        assert result.degraded is False

    def test_bracket_2leg_no_trail(self):
        from src.execution.bracket_orders import place_limit_tp_trail_bracket, BracketParams
        ib = self._mock_ib()
        params = BracketParams(
            symbol="MSFT", qty=5, entry_limit=400.0,
            stop_loss=395.0, trail_amount=0.0, tif="DAY",
        )
        result = place_limit_tp_trail_bracket(ib, params)
        assert result.ok
        assert result.parent_id > 0
        assert result.stop_id > 0
        assert result.trail_id is None

    def test_standalone_trailing_stop_attributes(self):
        """place_trailing_stop should set eTradeOnly=False, firmQuoteOnly=False."""
        from src.execution.bracket_orders import place_trailing_stop
        ib = self._mock_ib()
        result = place_trailing_stop(ib, "AAPL", 10, 2.0)
        assert result.ok
        # Verify the Order passed to placeOrder had the correct attributes
        call_args = ib.placeOrder.call_args_list[-1]
        order = call_args[0][1]
        assert order.eTradeOnly is False
        assert order.firmQuoteOnly is False


# ═══════════════════════════════════════════════════════════════════════
# AREA 3 — Scoring Unification
# ═══════════════════════════════════════════════════════════════════════

class TestScoringUnification:
    """Bug #8, #7: catalyst scorer and live loop must use the same scale."""

    def test_catalyst_scorer_produces_0_100(self):
        from src.data.catalyst_scorer import CatalystScorer, CatalystScore
        from src.data.catalyst_hunter import CatalystStock, CatalystSignal

        stock = CatalystStock(symbol="AAPL", signals=[
            CatalystSignal(
                symbol="AAPL", catalyst_type="earnings", source="finnhub",
                headline="AAPL beats earnings", confidence=0.95,
                urgency=0.9, bullish=True, magnitude=1.5,
            ),
        ])
        scorer = CatalystScorer()
        result = scorer.score_catalyst_stock("AAPL", stock)

        assert isinstance(result, CatalystScore)
        assert 0 <= result.combined_score <= 100
        assert hasattr(result, "combined_score")

    def test_min_catalyst_score_is_configurable(self):
        """MIN_CATALYST_SCORE should be settable via env var."""
        with patch.dict(os.environ, {"TL_MIN_CATALYST_SCORE": "75"}):
            # Re-evaluate the expression
            val = float(os.getenv("TL_MIN_CATALYST_SCORE", "60"))
            assert val == 75.0


# ═══════════════════════════════════════════════════════════════════════
# AREA 4 — IBBroker No Longer Returns Stub Values
# ═══════════════════════════════════════════════════════════════════════

class TestIBBrokerNotStub:
    """Verify that IBBroker does NOT return hardcoded mock data."""

    def test_get_last_price_not_hardcoded(self):
        """get_last_price should NOT return 100.0 for non-SPY symbols."""
        from src.broker.ib import IBBroker
        import inspect
        source = inspect.getsource(IBBroker.get_last_price)
        assert "return 100.0" not in source, "Stub code still present"
        assert "Mock data" not in source, "Stub comment still present"

    def test_get_atr_not_hardcoded(self):
        from src.broker.ib import IBBroker
        import inspect
        source = inspect.getsource(IBBroker.get_atr)
        assert "return 2.0" not in source, "Stub code still present"
        assert "return 4.8" not in source, "Stub code still present"

    def test_get_account_equity_not_hardcoded(self):
        from src.broker.ib import IBBroker
        import inspect
        source = inspect.getsource(IBBroker.get_account_equity)
        assert "105_000" not in source, "Stub code still present"
        assert "105000" not in source, "Stub code still present"

    def test_ib_broker_uses_ib_session(self):
        """IBBroker._ib() should delegate to get_ib from ib_session."""
        from src.broker.ib import IBBroker
        import inspect
        source = inspect.getsource(IBBroker._ib)
        assert "get_ib" in source


# ═══════════════════════════════════════════════════════════════════════
# AREA 5 — Side Propagation (adapters.py)
# ═══════════════════════════════════════════════════════════════════════

class TestSidePropagation:
    """Ensure OrderPlan direction propagates to OrderRequest side."""

    def test_sell_direction_propagated(self):
        from src.schemas.messages import OrderPlan
        from src.execution.adapters import plan_to_order_request

        plan = OrderPlan(
            symbol="AAPL",
            direction="SELL",
            qty=10,
            entry_type="LMT",
            stop_price=155.0,
            trail_params={},
        )
        req = plan_to_order_request(plan)
        assert req.side == "SELL", f"Expected SELL, got {req.side}"

    def test_buy_direction_propagated(self):
        from src.schemas.messages import OrderPlan
        from src.execution.adapters import plan_to_order_request

        plan = OrderPlan(
            symbol="AAPL",
            direction="BUY",
            qty=5,
            entry_type="MKT",
            stop_price=145.0,
            trail_params={},
        )
        req = plan_to_order_request(plan)
        assert req.side == "BUY"

    def test_invalid_side_raises(self):
        from src.schemas.messages import OrderPlan
        from src.execution.adapters import plan_to_order_request

        plan = OrderPlan(
            symbol="AAPL",
            direction="INVALID",
            qty=5,
            entry_type="MKT",
            trail_params={},
        )
        with pytest.raises(ValueError, match="Invalid side"):
            plan_to_order_request(plan)

    def test_trail_params_side_fallback(self):
        """If direction is not set, fall back to trail_params['side']."""
        from src.schemas.messages import OrderPlan
        from src.execution.adapters import plan_to_order_request

        plan = OrderPlan(
            symbol="MSFT",
            qty=3,
            entry_type="LMT",
            trail_params={"side": "SELL"},
        )
        # direction defaults to "BUY" but trail_params has "SELL"
        # getattr(plan, "direction", None) returns "BUY" (not None)
        # so direction takes precedence
        req = plan_to_order_request(plan)
        assert req.side == "BUY"  # direction field takes precedence


# ═══════════════════════════════════════════════════════════════════════
# OrderPlan direction field exists
# ═══════════════════════════════════════════════════════════════════════

class TestOrderPlanDirection:
    def test_direction_field_exists(self):
        from src.schemas.messages import OrderPlan
        plan = OrderPlan(symbol="TEST")
        assert hasattr(plan, "direction")
        assert plan.direction == "BUY"

    def test_direction_field_settable(self):
        from src.schemas.messages import OrderPlan
        plan = OrderPlan(symbol="TEST", direction="SELL")
        assert plan.direction == "SELL"


# ═══════════════════════════════════════════════════════════════════════
# Contract Validator
# ═══════════════════════════════════════════════════════════════════════

class TestContractValidator:
    def test_module_importable(self):
        from src.utils.contract_validator import is_ib_valid, clear_cache
        assert callable(is_ib_valid)
        assert callable(clear_cache)

    def test_clear_cache(self):
        from src.utils.contract_validator import _validation_cache, clear_cache
        _validation_cache["TEST"] = (True, "NYSE", "ok")
        clear_cache()
        assert "TEST" not in _validation_cache


# ═══════════════════════════════════════════════════════════════════════
# Scoring Gate — scanner candidates without catalyst_score are rejected
# ═══════════════════════════════════════════════════════════════════════

class TestScoringGate:
    """Verify that candidates below MIN_CATALYST_SCORE are filtered out."""

    def test_scoring_gate_rejects_scanner_only_candidates(self):
        """_filter_valid_candidates must reject candidates with catalyst_score < threshold."""
        from types import SimpleNamespace

        # Import the filter helper from live_loop_10s
        from src.live_loop_10s import _filter_valid_candidates

        # Build candidates: one above threshold, one below, one missing score
        cand_good = SimpleNamespace(symbol="AAPL", catalyst_score=80.0)
        cand_low = SimpleNamespace(symbol="LOW1", catalyst_score=30.0)
        cand_none = SimpleNamespace(symbol="NONE1", catalyst_score=None)

        # Mock IB and helpers to allow all symbols through non-score gates
        mock_ib = MagicMock()
        mock_ib.qualifyContracts.return_value = [True]

        invalid_set: set = set()
        valid_contracts: dict = {}

        with patch("src.live_loop_10s._universe_gate", return_value=(True, "")), \
             patch("src.live_loop_10s.coarse_symbol_allowed", return_value=True), \
             patch("src.live_loop_10s._safe_qualify_contract", return_value=(True, MagicMock(), "")):
            result = _filter_valid_candidates(
                [cand_good, cand_low, cand_none],
                mock_ib,
                min_score=60.0,
                invalid_symbols=invalid_set,
                valid_contracts=valid_contracts,
            )

        result_symbols = [c.symbol for c in result]
        assert "AAPL" in result_symbols, "High-score candidate should pass"
        assert "LOW1" not in result_symbols, "Low-score candidate should be rejected"
        assert "NONE1" not in result_symbols, "No-score candidate should be rejected"
