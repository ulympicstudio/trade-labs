"""Bracket / trailing-stop correctness against the mocked IB.

Covers the P0 bracket fixes: two-leg bracket (LIMIT entry + STOP), genuine
last-leg transmit, parentId linkage, BUY⇒SELL children, equal qty, minTick
rounding, broker-ack verification, degraded cleanup, trail-after-fill, and the
armed/paper guard.
"""

from tests.fake_ib import FakeIB

from src.execution.bracket_orders import (
    BracketParams,
    place_limit_tp_trail_bracket,
    place_trailing_stop,
)


def _params(**kw):
    base = dict(symbol="AAPL", qty=10, entry_limit=100.00, stop_loss=95.00,
                trail_amount=0.0, tif="DAY")
    base.update(kw)
    return BracketParams(**base)


def test_bracket_two_legs_only_no_tp(armed_paper):
    ib = FakeIB()
    res = place_limit_tp_trail_bracket(ib, _params())
    assert res.ok is True
    # Exactly two legs placed: parent LIMIT + STOP. No take-profit leg.
    assert len(ib.placed_orders) == 2
    types = [getattr(o, "orderType", "") for o in ib.placed_orders]
    assert types[0] in ("LMT", "LIMIT")
    assert types[1] == "STP"
    assert all(t != "LMT SELL" for t in types[1:])  # no sell-limit (TP) leg


def test_exactly_one_transmit_true_and_it_is_last(armed_paper):
    ib = FakeIB()
    place_limit_tp_trail_bracket(ib, _params())
    transmits = [getattr(o, "transmit", False) for o in ib.placed_orders]
    assert transmits.count(True) == 1, transmits
    # The single transmit=True leg must be the LAST placed leg.
    assert transmits[-1] is True
    assert transmits[0] is False


def test_children_parent_id_links_to_parent(armed_paper):
    ib = FakeIB()
    res = place_limit_tp_trail_bracket(ib, _params())
    parent = ib.placed_orders[0]
    stop = ib.placed_orders[1]
    assert parent.orderId == res.parent_id
    assert stop.parentId == parent.orderId


def test_buy_parent_implies_sell_children_equal_qty(armed_paper):
    ib = FakeIB()
    place_limit_tp_trail_bracket(ib, _params(qty=33))
    parent, stop = ib.placed_orders
    assert parent.action == "BUY"
    assert stop.action == "SELL"
    assert parent.totalQuantity == stop.totalQuantity == 33


def test_stop_in_oca_group_with_ocatype_1(armed_paper):
    ib = FakeIB()
    place_limit_tp_trail_bracket(ib, _params())
    stop = ib.placed_orders[1]
    assert stop.ocaGroup
    assert stop.ocaType == 1


def test_prices_rounded_to_min_tick(armed_paper):
    ib = FakeIB(min_tick=0.05)
    # 100.03 and 95.02 are off a 0.05 tick → must snap to 100.05 / 95.00.
    place_limit_tp_trail_bracket(ib, _params(entry_limit=100.03, stop_loss=95.02))
    parent, stop = ib.placed_orders
    assert abs(parent.lmtPrice - 100.05) < 1e-9
    assert abs(stop.auxPrice - 95.00) < 1e-9


def test_min_tick_fallback_to_penny(armed_paper):
    # Contract details returns no usable tick → fall back to 0.01.
    ib = FakeIB(min_tick=0.0)
    place_limit_tp_trail_bracket(ib, _params(entry_limit=100.123, stop_loss=95.117))
    parent, stop = ib.placed_orders
    assert abs(parent.lmtPrice - 100.12) < 1e-9
    assert abs(stop.auxPrice - 95.12) < 1e-9


def test_leg_rejection_cancels_all_placed_legs(armed_paper):
    # Reject the STOP child (2nd placed order → orderId 2).
    ib = FakeIB(reject_predicate=lambda o: getattr(o, "orderType", "") == "STP")
    res = place_limit_tp_trail_bracket(ib, _params())
    assert res.ok is False
    assert res.degraded is True
    # ALL placed legs cancelled (parent + stop), not just the parent.
    assert len(ib.cancelled_orders) == len(ib.placed_orders) == 2


def test_acceptance_checked_by_status_not_orderid(armed_paper):
    # An order that has a non-zero orderId but Inactive status must be treated
    # as NOT accepted (the old bug trusted orderId).
    ib = FakeIB(default_status="Inactive")
    res = place_limit_tp_trail_bracket(ib, _params())
    assert res.ok is False
    assert res.degraded is True


def test_trail_only_attached_after_fill(armed_paper):
    ib = FakeIB()
    res = place_limit_tp_trail_bracket(ib, _params())
    assert res.ok
    # No TRAIL leg was placed at entry time.
    assert all(getattr(o, "orderType", "") != "TRAIL" for o in ib.placed_orders)

    # Simulate a confirmed fill, then attach the trailing stop.
    ib.fill_order(res.parent_id, filled=10, avg_price=100.0)
    ib.set_position("AAPL", 10, avg_cost=100.0)
    trail_res = place_trailing_stop(ib, "AAPL", 10, trail_amount=1.50)
    assert trail_res.ok is True
    trail_orders = [o for o in ib.placed_orders if getattr(o, "orderType", "") == "TRAIL"]
    assert len(trail_orders) == 1


def test_trail_rejection_cancels_leg(armed_paper):
    ib = FakeIB(reject_predicate=lambda o: getattr(o, "orderType", "") == "TRAIL")
    res = place_trailing_stop(ib, "AAPL", 10, trail_amount=1.50)
    assert res.ok is False
    assert len(ib.cancelled_orders) == 1


def test_bracket_refuses_without_armed_flag(disarmed):
    ib = FakeIB()
    res = place_limit_tp_trail_bracket(ib, _params())
    assert res.ok is False
    assert "ARMED" in res.message
    # Nothing was sent to the broker.
    assert ib.placed_orders == []


def test_trail_refuses_without_armed_flag(disarmed):
    ib = FakeIB()
    res = place_trailing_stop(ib, "AAPL", 10, trail_amount=1.5)
    assert res.ok is False
    assert ib.placed_orders == []
