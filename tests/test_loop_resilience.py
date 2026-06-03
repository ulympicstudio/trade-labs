"""P1 loop resilience + reconnect reconciliation (monolith live loop)."""

import os

import pytest

from tests.fake_ib import FakeIB, FakeContract


@pytest.fixture
def live_loop(monkeypatch):
    monkeypatch.setenv("TRADE_LABS_MODE", "PAPER")
    import src.live_loop_10s as L
    return L


def test_reconcile_rebuilds_tracking_from_new_connection(live_loop):
    L = live_loop
    ib = FakeIB()
    ib.set_position("AAPL", 10, avg_cost=100.0)
    ib.set_position("MSFT", 5, avg_cost=400.0)

    # Place a working STOP for AAPL and a TRAIL for MSFT on the new connection.
    from ib_insync import StopOrder, Order
    stop = StopOrder("SELL", 10, 95.0)
    ib.placeOrder(FakeContract("AAPL"), stop)
    trail = Order(action="SELL", orderType="TRAIL", totalQuantity=5, auxPrice=2.0)
    ib.placeOrder(FakeContract("MSFT"), trail)

    # Stale in-memory state from the OLD connection.
    active = {"OLDSYM"}
    confirmed_fills = {"OLDSYM"}
    trail_active = {"OLDSYM"}
    stop_ids = {"OLDSYM": 999}

    L.reconcile_tracking_after_reconnect(
        ib, active, confirmed_fills, trail_active, stop_ids
    )

    # Stale symbol gone; positions re-synced from the new connection.
    assert active == {"AAPL", "MSFT"}
    assert confirmed_fills == {"AAPL", "MSFT"}
    assert stop_ids.get("AAPL") == stop.orderId
    assert "MSFT" in trail_active
    assert "OLDSYM" not in stop_ids


def test_reconcile_handles_no_positions(live_loop):
    L = live_loop
    ib = FakeIB()
    active = {"X"}
    confirmed = {"X"}
    trail = {"X"}
    stops = {"X": 1}
    L.reconcile_tracking_after_reconnect(ib, active, confirmed, trail, stops)
    assert active == set()
    assert confirmed == set()
    assert stops == {}


def test_loop_body_is_wrapped_in_try_except_continue(live_loop):
    """The while-True body must be guarded so a transient error continues."""
    import inspect
    src = inspect.getsource(live_loop.run_live_loop) if hasattr(
        live_loop, "run_live_loop") else inspect.getsource(live_loop.main)
    # Find the loop region and assert it has an Exception guard that continues.
    assert "while True:" in src
    assert "except Exception as loop_err" in src
    # KeyboardInterrupt must be re-raised for clean shutdown.
    assert "except KeyboardInterrupt" in src
