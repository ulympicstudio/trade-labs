"""Execution Arm — order routing & fill management.

Subscribes to ``ORDER_PLAN_APPROVED`` on the Redis bus, converts each
:class:`~src.schemas.messages.OrderPlan` into a legacy
:class:`~src.execution.orders.OrderRequest` via the adapter layer,
calls the existing ``place_order()`` function, and publishes an
:class:`~src.schemas.messages.OrderEvent` back onto the bus.

Run::

    python -m src.arms.execution_main
"""

from __future__ import annotations

import os
import signal
import threading
import time
from typing import Any, Optional

from src.config.settings import settings
from src.monitoring.logger import get_logger
from src.bus.topics import ORDER_PLAN_APPROVED, ORDER_EVENT, ORDER_BLUEPRINT, HEARTBEAT
from src.schemas.messages import OrderPlan, OrderEvent, OrderBlueprint, Heartbeat
from src.market.session import get_us_equity_session, RTH

log = get_logger("execution")

_ALLOW_EXTENDED = os.environ.get(
    "ALLOW_EXTENDED_HOURS", "false"
).lower() in ("1", "true", "yes")

_EXECUTION_ENABLED = os.environ.get(
    "EXECUTION_ENABLED", "false"
).lower() in ("1", "true", "yes")

# ── Sim Friction (PAPER mode execution realism) ─────────────────────
_SIM_FRICTION = os.environ.get(
    "TL_EXEC_SIM_FRICTION", "false"
).lower() in ("1", "true", "yes")
_SIM_DELAY_MS = int(os.environ.get("TL_EXEC_SIM_DELAY_MS", "250"))
_SIM_SLIPPAGE_BPS = float(os.environ.get("TL_EXEC_SIM_SLIPPAGE_BPS", "2"))
_SIM_PARTIAL_FILL_PCT = float(os.environ.get("TL_EXEC_SIM_PARTIAL_FILL_PCT", "0.6"))

_running = True
_bus = None          # type: Any  # RedisBus | None
_ib = None           # type: Any  # ib_insync.IB | None — set via connect_broker()
_orders_processed = 0
_blueprints_received = 0
_lock = threading.Lock()


def _handle_signal(signum, _frame):
    global _running
    log.info("Received shutdown signal (%s)", signum)
    _running = False


# ── Broker connection (lazy, optional) ───────────────────────────────

def connect_broker(ib: Any) -> None:
    """Inject a live ``ib_insync.IB`` connection for real order routing.

    If never called, orders are routed through the SIM backend.
    """
    global _ib
    _ib = ib
    log.info("Broker connection injected (IB)")


# ── Order handler ────────────────────────────────────────────────────

def _on_order_plan(plan: OrderPlan) -> None:
    """Handle an approved order plan from the bus."""
    global _orders_processed

    log.info(
        "Received OrderPlan  symbol=%s  qty=%d  entry=%s  stop=%.2f",
        plan.symbol,
        plan.qty,
        plan.entry_type,
        plan.stop_price,
    )

    # ── Session gate ─────────────────────────────────────────────────
    session = get_us_equity_session()
    if session != RTH and not _ALLOW_EXTENDED:
        log.info(
            "Skipping order  symbol=%s  session=%s  (not RTH, ALLOW_EXTENDED_HOURS=false)",
            plan.symbol, session,
        )
        _publish_event(OrderEvent(
            symbol=plan.symbol,
            event_type="REJECTED",
            status="session_gate",
            message=f"session={session}, not RTH",
        ))
        return

    # ── 1. Import adapter layer ──────────────────────────────────────
    try:
        from src.execution.adapters import plan_to_order_request, result_to_order_event
    except Exception as exc:
        err_msg = str(exc)[:200]
        log.error(
            "Adapter import failed for %s — rejecting plan and continuing: %s",
            plan.symbol, err_msg,
        )
        _publish_event(OrderEvent(
            symbol=plan.symbol,
            event_type="REJECTED",
            status="adapter_import_error",
            message=err_msg,
        ))
        return

    # ── 2. Adapt to legacy OrderRequest ──────────────────────────────
    try:
        legacy_req = plan_to_order_request(plan)
    except Exception as exc:
        err_msg = str(exc)[:200]
        log.error(
            "Failed to adapt OrderPlan → OrderRequest for %s: %s",
            plan.symbol, err_msg,
        )
        _publish_event(OrderEvent(
            symbol=plan.symbol,
            event_type="REJECTED",
            status="adapter_error",
            message=err_msg,
        ))
        return

    # ── 3. Import & invoke place_order ───────────────────────────────
    try:
        from src.execution.orders import place_order
    except Exception as exc:
        err_msg = str(exc)[:200]
        log.error(
            "Orders module import failed for %s — rejecting: %s",
            plan.symbol, err_msg,
        )
        _publish_event(OrderEvent(
            symbol=plan.symbol,
            event_type="REJECTED",
            status="adapter_import_error",
            message=err_msg,
        ))
        return

    try:
        result = place_order(legacy_req, ib=_ib)
    except Exception as exc:
        err_msg = str(exc)[:200]
        log.error(
            "place_order raised for %s: %s", plan.symbol, err_msg,
        )
        _publish_event(OrderEvent(
            symbol=plan.symbol,
            event_type="REJECTED",
            status="execution_error",
            message=err_msg,
        ))
        return

    log.info(
        "place_order result  ok=%s  msg=%s  parent_id=%s",
        result.ok,
        result.message,
        result.parent_order_id,
    )

    # ── 4. Convert result → OrderEvent and publish ───────────────────
    try:
        event = result_to_order_event(result, plan)
    except Exception as exc:
        err_msg = str(exc)[:200]
        log.error("Failed to build OrderEvent from result: %s", err_msg)
        return

    _publish_event(event)

    with _lock:
        _orders_processed += 1


# ── OrderBlueprint handler ─────────────────────────────────────────

def _on_order_blueprint(bp: OrderBlueprint) -> None:
    """Handle an OrderBlueprint from the risk arm.

    If EXECUTION_ENABLED is false (default), logs the blueprint as a
    DRY RUN and publishes a BLUEPRINT_READY OrderEvent.
    If true, maps to IB bracket orders (paper account only).
    """
    global _blueprints_received

    with _lock:
        _blueprints_received += 1

    if not _EXECUTION_ENABLED:
        # ── Sim friction for DRY RUN (PAPER mode) ───────────────────
        if _SIM_FRICTION:
            slip_mult = 1.0 + _SIM_SLIPPAGE_BPS / 10000.0
            adj_ladder = [round(p * slip_mult, 2) for p in bp.entry_ladder]
            first_fill_qty = max(1, int(bp.qty * _SIM_PARTIAL_FILL_PCT))
            remainder_qty = bp.qty - first_fill_qty
            avg_price = adj_ladder[len(adj_ladder) // 2] if adj_ladder else 0.0

            log.info(
                "sim friction applied  symbol=%s  slippage_bps=%.1f  "
                "delay_ms=%d  partial_fill=%d/%d  adj_ladder=%s",
                bp.symbol, _SIM_SLIPPAGE_BPS, _SIM_DELAY_MS,
                first_fill_qty, bp.qty,
                [f"{p:.2f}" for p in adj_ladder[:3]],
            )

            # Emit partial fill event
            _publish_event(OrderEvent(
                symbol=bp.symbol,
                event_type="PARTIAL",
                status="sim_friction",
                filled_qty=first_fill_qty,
                avg_fill_price=avg_price,
                message=(
                    f"sim_partial qty={first_fill_qty}/{bp.qty} "
                    f"slip={_SIM_SLIPPAGE_BPS}bps delay={_SIM_DELAY_MS}ms"
                ),
            ))

            # Emit remainder fill after simulated delay
            if remainder_qty > 0:
                import time as _time
                _time.sleep(_SIM_DELAY_MS / 1000.0)
                _publish_event(OrderEvent(
                    symbol=bp.symbol,
                    event_type="FILLED",
                    status="sim_friction",
                    filled_qty=bp.qty,
                    avg_fill_price=avg_price,
                    message=f"sim_fill_complete qty={bp.qty} slip={_SIM_SLIPPAGE_BPS}bps",
                ))
        else:
            log.info(
                "DRY RUN blueprint  symbol=%s  qty=%d  ladder=%s  "
                "stop=%.2f  trail=%.2f%%  timeout=%ds  max_spread=%.2f%%  "
                "risk=$%.2f  Q=%s",
                bp.symbol, bp.qty,
                [f"{p:.2f}" for p in bp.entry_ladder],
                bp.stop_price, bp.trail_pct, bp.timeout_s,
                bp.max_spread_pct, bp.risk_usd, bp.quality,
            )

        _publish_event(OrderEvent(
            symbol=bp.symbol,
            event_type="BLUEPRINT_READY",
            status="dry_run",
            message=(
                f"qty={bp.qty} ladder={len(bp.entry_ladder)} "
                f"stop={bp.stop_price:.2f} trail={bp.trail_pct}% "
                f"timeout={bp.timeout_s}s"
                + (f" sim_friction=true slip={_SIM_SLIPPAGE_BPS}bps" if _SIM_FRICTION else "")
            ),
        ))
        return

    # ── EXECUTION_ENABLED=true path (future: IB bracket orders) ────
    log.info(
        "LIVE blueprint  symbol=%s  qty=%d  (paper bracket submission TODO)",
        bp.symbol, bp.qty,
    )
    # TODO: map OrderBlueprint → IB bracket/OCO orders via _ib
    _publish_event(OrderEvent(
        symbol=bp.symbol,
        event_type="BLUEPRINT_READY",
        status="live_pending",
        message="IB bracket submission not yet implemented",
    ))


def _publish_event(event: OrderEvent) -> None:
    """Publish an OrderEvent to the bus (fire-and-forget)."""
    if _bus is not None:
        ok = _bus.publish(ORDER_EVENT, event)
        if ok:
            log.info(
                "Published OrderEvent  symbol=%s  type=%s  status=%s",
                event.symbol,
                event.event_type,
                event.status,
            )
        else:
            log.warning("Failed to publish OrderEvent for %s", event.symbol)
    else:
        log.warning("Bus unavailable — OrderEvent for %s not published", event.symbol)


# ── Bus connection ───────────────────────────────────────────────────

def _connect_bus():
    """Non-blocking attempt to connect to the event bus and subscribe."""
    try:
        from src.bus.bus_factory import get_bus
        bus = get_bus(max_retries=1)
        if not bus.is_connected:
            log.warning("Event bus unavailable — will retry next cycle")
            return None
        bus.subscribe(ORDER_PLAN_APPROVED, _on_order_plan, msg_type=OrderPlan)
        bus.subscribe(ORDER_BLUEPRINT, _on_order_blueprint, msg_type=OrderBlueprint)
        log.info("Subscribed to %s, %s", ORDER_PLAN_APPROVED, ORDER_BLUEPRINT)
        return bus
    except Exception:
        log.exception("Failed to initialise event bus — will retry")
        return None


# ── Main loop ────────────────────────────────────────────────────────

def main() -> None:
    """Entry-point for the execution arm."""
    global _bus

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    log.info(
        "Execution arm starting  mode=%s  allow_extended=%s  execution_enabled=%s  heartbeat=%ss",
        settings.trade_mode.value,
        _ALLOW_EXTENDED,
        _EXECUTION_ENABLED,
        settings.heartbeat_interval_s,
    )

    _bus = _connect_bus()

    tick = 0
    while _running:
        tick += 1

        # Lazy reconnect
        if _bus is None:
            _bus = _connect_bus()

        # Publish own heartbeat
        if _bus is not None:
            _bus.publish(HEARTBEAT, Heartbeat(arm="execution"))

        with _lock:
            count = _orders_processed
            bp_count = _blueprints_received
        log.info("heartbeat  tick=%d  orders_processed=%d  blueprints=%d", tick, count, bp_count)

        time.sleep(settings.heartbeat_interval_s)

    # Cleanup
    if _bus is not None:
        try:
            _bus.close()
        except Exception:
            pass
    log.info("Execution arm stopped.")


if __name__ == "__main__":
    main()
