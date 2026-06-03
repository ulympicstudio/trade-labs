"""P1 fill-accounting: submission must NOT be booked as a fill.

A submitted-but-unfilled LIMIT entry must not trigger the kill-switch ledger,
exit registration, or attribution. A confirmed Filled status must.
"""

import os

import pytest

from tests.fake_ib import FakeIB
from src.schemas.messages import OrderBlueprint
from src.execution.bracket_orders import BracketResult


@pytest.fixture
def exec_main(monkeypatch):
    monkeypatch.setenv("TRADE_LABS_MODE", "PAPER")
    monkeypatch.setenv("TRADE_LABS_ARMED", "1")
    from src.arms import execution_main as em
    # Force the IB bracket path on.
    monkeypatch.setattr(em, "_EXECUTION_ENABLED", True, raising=False)
    monkeypatch.setattr(em, "_EXIT_ENABLED", True, raising=False)
    monkeypatch.setattr(em, "_ATTRIB_ENABLED", True, raising=False)
    monkeypatch.setattr(em, "_SC_ENABLED", False, raising=False)
    # Always allow session.
    monkeypatch.setattr(em, "is_test_session_forced", lambda: True, raising=False)
    return em


def _blueprint():
    return OrderBlueprint(
        symbol="AAPL",
        direction="LONG",
        qty=10,
        entry_ladder=[99.0, 100.0, 101.0],
        stop_price=95.0,
        trail_pct=1.0,
        risk_usd=50.0,
    )


def _wire(monkeypatch, em, ib, *, confirmed: bool):
    """Inject FakeIB, a deterministic bracket result, and capture ledger calls."""
    fills, exits, attribs, events = [], [], [], []
    monkeypatch.setattr(em, "_ib", ib, raising=False)

    def fake_bracket(_ib, params, oca_group=None):
        # Place a parent order on the fake IB so confirmation can inspect it.
        from ib_insync import LimitOrder
        order = LimitOrder("BUY", params.qty, params.entry_limit)
        ib.placeOrder(_fake_contract(params.symbol), order)
        if confirmed:
            ib.fill_order(order.orderId, filled=params.qty, avg_price=params.entry_limit)
        return BracketResult(ok=True, message="ok", parent_id=order.orderId,
                             stop_id=order.orderId + 1)

    monkeypatch.setattr(em, "_ks_record_fill",
                        lambda *a, **k: fills.append((a, k)), raising=False)
    monkeypatch.setattr(em, "_exit_register",
                        lambda *a, **k: exits.append((a, k)), raising=False)
    monkeypatch.setattr(em, "_attrib_open", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(em, "_attrib_fill",
                        lambda *a, **k: attribs.append((a, k)), raising=False)
    monkeypatch.setattr(em, "_publish_event",
                        lambda ev: events.append(ev), raising=False)

    # Patch the imported bracket function used inside the handler.
    import src.execution.bracket_orders as bo
    monkeypatch.setattr(bo, "place_limit_tp_trail_bracket", fake_bracket, raising=False)
    return fills, exits, attribs, events


def _fake_contract(symbol):
    from tests.fake_ib import FakeContract
    return FakeContract(symbol=symbol)


def test_submitted_unfilled_does_not_record_fill(exec_main, monkeypatch):
    em = exec_main
    ib = FakeIB(default_status="Submitted")  # working, NOT filled
    fills, exits, attribs, events = _wire(monkeypatch, em, ib, confirmed=False)

    em._on_order_blueprint(_blueprint())

    assert fills == [], "submission must not record a fill"
    assert exits == [], "submission must not register an exit"
    assert attribs == [], "submission must not attribute a fill"
    statuses = [getattr(e, "status", None) for e in events]
    assert "submitted_unfilled" in statuses
    assert "live_filled" not in statuses


def test_confirmed_fill_records_fill(exec_main, monkeypatch):
    em = exec_main
    ib = FakeIB(default_status="Submitted")
    fills, exits, attribs, events = _wire(monkeypatch, em, ib, confirmed=True)

    em._on_order_blueprint(_blueprint())

    assert len(fills) == 1, "confirmed fill must record exactly one fill"
    assert len(exits) == 1
    assert len(attribs) == 1
    statuses = [getattr(e, "status", None) for e in events]
    assert "live_filled" in statuses


def _order_plan():
    from src.schemas.messages import OrderPlan
    return OrderPlan(
        symbol="AAPL",
        intent_id="i1",
        qty=10,
        entry_type="LMT",
        limit_prices=[100.0],
        stop_price=95.0,
        trail_params={"side": "BUY", "trail_pct": 1.0, "total_risk": 50.0},
    )


def test_order_plan_live_routes_through_bracket_unfilled(exec_main, monkeypatch):
    """An approved OrderPlan submits via the single bracket builder, not
    place_order; an unfilled entry must not be booked as a fill."""
    em = exec_main
    ib = FakeIB(default_status="Submitted")
    fills, exits, attribs, events = _wire(monkeypatch, em, ib, confirmed=False)
    # place_order must NOT be invoked on the live path.
    import src.execution.orders as orders_mod
    called = []
    monkeypatch.setattr(orders_mod, "place_order",
                        lambda *a, **k: called.append(1), raising=False)

    em._on_order_plan(_order_plan())

    assert called == [], "live OrderPlan must not call legacy place_order"
    assert fills == [], "submission must not record a fill"
    statuses = [getattr(e, "status", None) for e in events]
    assert "submitted_unfilled" in statuses
    assert "live_filled" not in statuses


def test_order_plan_live_confirmed_fill_records(exec_main, monkeypatch):
    em = exec_main
    ib = FakeIB(default_status="Submitted")
    fills, exits, attribs, events = _wire(monkeypatch, em, ib, confirmed=True)

    em._on_order_plan(_order_plan())

    assert len(fills) == 1
    assert len(exits) == 1
    assert len(attribs) == 1
    statuses = [getattr(e, "status", None) for e in events]
    assert "live_filled" in statuses


def test_bracket_params_from_plan_and_blueprint():
    from src.execution.bracket_orders import BracketParams
    plan = _order_plan()
    p = BracketParams.from_plan(plan)
    assert p.entry_limit == 100.0
    assert p.stop_loss == 95.0
    assert p.trail_amount == 1.0  # 100.0 * 1.0%
    assert p.qty == 10

    bp = _blueprint()
    b = BracketParams.from_blueprint(bp)
    assert b.entry_limit == 100.0  # mid of [99,100,101]
    assert b.stop_loss == 95.0
    assert b.qty == 10


def test_confirm_parent_filled_helper(exec_main):
    em = exec_main
    ib = FakeIB(default_status="Submitted")
    from ib_insync import LimitOrder
    order = LimitOrder("BUY", 10, 100.0)
    ib.placeOrder(_fake_contract("AAPL"), order)

    # Not filled yet.
    filled, qty, px = em._confirm_parent_filled(ib, order.orderId, 10, 100.0)
    assert filled is False

    # Now fill it.
    ib.fill_order(order.orderId, filled=10, avg_price=100.25)
    filled, qty, px = em._confirm_parent_filled(ib, order.orderId, 10, 100.0)
    assert filled is True
    assert qty == 10
    assert px == 100.25
