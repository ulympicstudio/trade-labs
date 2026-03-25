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
from src.market.session import get_us_equity_session, RTH, is_test_session_forced, get_test_force_session
from src.risk.kill_switch import (
    record_fill as _ks_record_fill,
    record_failed_order as _ks_record_failed,
)
from src.analysis.playbook_scorecard import (
    _simulate_trade_close_for_dev as _sc_dev_close,
    record_trade_open as _sc_record_open,
    record_trade_close as _sc_record_close,
    SCORECARD_ENABLED as _SC_ENABLED,
)
from src.risk.exit_intelligence import (
    register_fill as _exit_register,
    update_position_state as _exit_update,
    unregister_position as _exit_unregister,
    get_exit_summary as _exit_summary,
    get_position_count as _exit_pos_count,
    EXIT_ENABLED as _EXIT_ENABLED,
    TRIM_25 as _TRIM_25,
    TRIM_50 as _TRIM_50,
    EXIT_FULL as _EXIT_FULL,
    TIGHTEN_STOP as _TIGHTEN_STOP,
)
from src.analysis.pnl_attribution import (
    record_open as _attrib_open,
    record_fill as _attrib_fill,
    record_mark as _attrib_mark,
    record_close as _attrib_close,
    get_open_count as _attrib_open_count,
    ATTRIB_ENABLED as _ATTRIB_ENABLED,
)
from src.analysis.self_tuning import (
    compute_tuning_decision as _tuning_compute,
    get_tuning_snapshot as _tuning_snapshot,
    TUNING_ENABLED as _TUNING_ENABLED,
)

log = get_logger("execution")

_ALLOW_EXTENDED = os.environ.get(
    "ALLOW_EXTENDED_HOURS", "false"
).lower() in ("1", "true", "yes")

_EXECUTION_ENABLED = os.environ.get(
    "EXECUTION_ENABLED", "false"
).lower() in ("1", "true", "yes")

# ── Sim Friction (PAPER mode execution realism) ─────────────────────
_SIM_FRICTION_DEFAULT = "true" if settings.trade_mode.value == "PAPER" else "false"
_SIM_FRICTION = os.environ.get(
    "TL_EXEC_SIM_FRICTION", _SIM_FRICTION_DEFAULT
).lower() in ("1", "true", "yes")
_SIM_DELAY_MS = int(os.environ.get("TL_EXEC_SIM_DELAY_MS", "250"))
_SIM_SLIPPAGE_BPS = float(os.environ.get("TL_EXEC_SIM_SLIPPAGE_BPS", "2"))
_SIM_PARTIAL_FILL_PCT = float(os.environ.get("TL_EXEC_SIM_PARTIAL_FILL_PCT", "0.6"))

_LIVE_TRADING_ENABLED = os.getenv("TL_LIVE_TRADING", "0") == "1"

# ── PAPER slippage model (H) ────────────────────────────────────────
_SLIPPAGE_MULT = float(os.environ.get("TL_EXEC_SLIPPAGE_MULT", "0.05"))

# ── Dev harness: synthetic fills for pipeline testing ────────────────
_FORCE_PAPER_FILL = os.environ.get(
    "TL_EXEC_FORCE_PAPER_FILL", "false"
).lower() in ("1", "true", "yes")
_DEV_HARNESS_ENABLED = _FORCE_PAPER_FILL and is_test_session_forced()
_DEV_HARNESS_INTERVAL_S = int(os.environ.get("TL_DEV_HARNESS_INTERVAL_S", "20"))
_DEV_HARNESS_SYMBOLS = ["AAPL", "MSFT", "NVDA"]

_running = True
_stop_event = threading.Event()  # cooperative shutdown
_bus = None          # type: Any  # RedisBus | None
_ib = None           # type: Any  # ib_insync.IB | None — set via connect_broker()
_orders_processed = 0
_blueprints_received = 0
_lock = threading.Lock()


def _handle_signal(signum, _frame):
    global _running
    log.info("Received shutdown signal (%s)", signum)
    _running = False
    _stop_event.set()


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
    _test_forced = is_test_session_forced()
    if session != RTH and not _ALLOW_EXTENDED and not _test_forced:
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
        _ks_record_failed()
        return
    if _test_forced and session != RTH:
        log.info(
            "forced_session_override session_gate session=%s forced=%s symbol=%s",
            session, get_test_force_session(), plan.symbol,
        )

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
        _ks_record_failed()
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
        _ks_record_failed()
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
        _ks_record_failed()
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
        _ks_record_failed()
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

    # ── 4b. Simulated fill when execution disabled + SIM_FRICTION ────
    if not _EXECUTION_ENABLED and _SIM_FRICTION and result.ok:
        _ref = plan.limit_prices[0] if plan.limit_prices else plan.stop_price
        _slip_bps = _SIM_SLIPPAGE_BPS
        _slip_mult = 1 + _slip_bps / 10_000
        _fill_px = round(_ref * _slip_mult, 2)
        log.info(
            "PAPER_FILL symbol=%s side=BUY ref=mid %.2f fill=%.2f slip=%.4f "
            "qty=%d risk=$%.2f",
            plan.symbol, _ref, _fill_px, _slip_bps / 10_000,
            plan.qty, plan.trail_params.get("total_risk", 0),
        )
        _ks_record_fill(plan.symbol, plan.qty, _fill_px, 0.0)
        log.info(
            "order_event symbol=%s type=PAPER_FILL qty=%d avg_px=%.2f source=order_plan",
            plan.symbol, plan.qty, _fill_px,
        )
        _publish_event(OrderEvent(
            symbol=plan.symbol,
            event_type="PAPER_FILL",
            status="sim_friction",
            filled_qty=plan.qty,
            avg_fill_price=_fill_px,
            message=f"SIM fill ref={_ref} slip={_slip_bps}bps",
        ))
        # Scorecard dev-close: record a near-zero-PnL close so stats flow
        if _SC_ENABLED:
            _sc_dev_close(plan.intent_id, _fill_px, plan.qty)
        # Exit intelligence: register position for exit tracking
        if _EXIT_ENABLED:
            _exit_register(
                symbol=plan.symbol,
                side=plan.trail_params.get("side", "BUY"),
                entry_price=_fill_px,
                qty=plan.qty,
                stop_price=plan.stop_price,
                trail_pct=plan.trail_params.get("trail_pct", 1.5),
                risk_usd=plan.trail_params.get("total_risk", 0.0),
                playbook=plan.trail_params.get("playbook", ""),
                sector=plan.trail_params.get("sector", ""),
                industry=plan.trail_params.get("industry", ""),
                regime=plan.trail_params.get("regime", ""),
                market_mode=plan.trail_params.get("market_mode", ""),
                volatility_state=plan.trail_params.get("volatility_state", ""),
                scorecard_bias=plan.trail_params.get("scorecard_bias", 1.0),
                intent_id=plan.intent_id,
            )
        # Attribution: register trade open
        if _ATTRIB_ENABLED:
            _attrib_open(
                symbol=plan.symbol,
                side=plan.trail_params.get("side", "BUY"),
                entry_price=_fill_px,
                qty=plan.qty,
                risk_usd=plan.trail_params.get("total_risk", 0.0),
                playbook=plan.trail_params.get("playbook", ""),
                sector=plan.trail_params.get("sector", ""),
                industry=plan.trail_params.get("industry", ""),
                regime=plan.trail_params.get("regime", ""),
                market_mode=plan.trail_params.get("market_mode", ""),
                volatility_state=plan.trail_params.get("volatility_state", ""),
                scorecard_bias=plan.trail_params.get("scorecard_bias", 1.0),
                intent_id=plan.intent_id,
            )
            _attrib_fill(plan.symbol, _fill_px, plan.qty)
            # Synthetic close for dev attribution stats
            _attrib_close(
                symbol=plan.symbol,
                exit_price=_fill_px,
                realized_pnl=0.0,
                exit_action="PAPER_CLOSE",
                exit_reason="dev_synthetic",
            )

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
            # Reference price = mid of entry ladder
            ref_price = bp.entry_ladder[len(bp.entry_ladder) // 2] if bp.entry_ladder else 0.0
            # Spread estimate from max_spread_pct (percent → absolute)
            spread_abs = ref_price * (bp.max_spread_pct / 100.0)
            # Slippage model (H): direction-aware fill price
            slip_amt = round(spread_abs * _SLIPPAGE_MULT, 4)
            if bp.direction == "LONG":
                avg_price = round(ref_price + slip_amt, 2)
            else:
                avg_price = round(ref_price - slip_amt, 2)

            # Legacy ladder adjustment (kept for partial-fill realism)
            slip_mult = 1.0 + _SIM_SLIPPAGE_BPS / 10000.0
            adj_ladder = [round(p * slip_mult, 2) for p in bp.entry_ladder]
            first_fill_qty = max(1, int(bp.qty * _SIM_PARTIAL_FILL_PCT))
            remainder_qty = bp.qty - first_fill_qty

            # ── Fill explainability metrics ──────────────────────────
            _actual_slip_bps = round(abs(avg_price - ref_price) / max(ref_price, 0.01) * 10000, 1) if ref_price > 0 else 0.0
            _spread_bps = round(bp.max_spread_pct * 100, 1)  # max_spread_pct is in %, convert to bps
            _bp_event_score = "n/a"
            for _rc in bp.reason_codes:
                if _rc.startswith("event_score="):
                    _bp_event_score = _rc.split("=", 1)[1]
                    break

            log.info(
                "PAPER_FILL symbol=%s side=%s ref=mid %.2f fill=%.2f slip=%.4f  "
                "slippage_bps=%.1f spread_bps=%.1f event_score=%s "
                "delay_ms=%d partial_fill=%d/%d adj_ladder=%s",
                bp.symbol, bp.direction, ref_price, avg_price, slip_amt,
                _actual_slip_bps, _spread_bps, _bp_event_score,
                _SIM_DELAY_MS,
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
            # PAPER fill → record synthetic outcome for kill_switch
            _sim_pnl = round((avg_price * 0.001) * bp.qty * (1 if bp.direction == "LONG" else -1), 2)  # tiny synthetic PnL
            _ks_record_fill(bp.symbol, bp.qty, avg_price, pnl=_sim_pnl)
            log.info(
                "killswitch PAPER_FILL symbol=%s qty=%d price=%.2f pnl=%.2f",
                bp.symbol, bp.qty, avg_price, _sim_pnl,
            )
            log.info(
                "order_event symbol=%s type=PAPER_FILL qty=%d avg_px=%.2f source=%s",
                bp.symbol, bp.qty, avg_price,
                getattr(bp, "source", "") or "trade_intent",
            )
            # Scorecard dev-close: feed stats during PAPER mode
            if _SC_ENABLED:
                _bp_intent = getattr(bp, "intent_id", "")
                if _bp_intent:
                    _sc_dev_close(_bp_intent, avg_price, bp.qty,
                                  slippage_bps=_actual_slip_bps)
            # Exit intelligence: register position for exit tracking
            if _EXIT_ENABLED:
                _exit_register(
                    symbol=bp.symbol,
                    side=bp.direction,
                    entry_price=avg_price,
                    qty=bp.qty,
                    stop_price=bp.stop_price,
                    trail_pct=bp.trail_pct,
                    risk_usd=bp.risk_usd,
                    playbook=getattr(bp, "source", "") or "",
                    intent_id=getattr(bp, "intent_id", "") or "",
                )
            # Attribution: register trade open + synthetic close
            if _ATTRIB_ENABLED:
                _attrib_open(
                    symbol=bp.symbol,
                    side=bp.direction,
                    entry_price=avg_price,
                    qty=bp.qty,
                    risk_usd=bp.risk_usd,
                    playbook=getattr(bp, "source", "") or "",
                    intent_id=getattr(bp, "intent_id", "") or "",
                )
                _attrib_fill(bp.symbol, avg_price, bp.qty)
                _attrib_close(
                    symbol=bp.symbol,
                    exit_price=avg_price,
                    realized_pnl=_sim_pnl,
                    exit_action="PAPER_CLOSE",
                    exit_reason="dev_synthetic",
                )
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
            log.info(
                "order_event symbol=%s type=DRY_RUN qty=%d avg_px=%.2f source=%s "
                "(sim_friction=off, enable TL_EXEC_SIM_FRICTION=true for PAPER_FILL)",
                bp.symbol, bp.qty,
                bp.entry_ladder[len(bp.entry_ladder) // 2] if bp.entry_ladder else 0.0,
                getattr(bp, "source", "") or "trade_intent",
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

    # ── EXECUTION_ENABLED=true path: IB bracket orders ─────────────
    if _ib is None:
        log.error(
            "EXECUTION_ENABLED but no broker connection — rejecting blueprint symbol=%s",
            bp.symbol,
        )
        _publish_event(OrderEvent(
            symbol=bp.symbol,
            event_type="REJECTED",
            status="no_broker",
            message="EXECUTION_ENABLED=true but no IB connection injected",
        ))
        _ks_record_failed()
        return

    from src.execution.bracket_orders import BracketParams, place_limit_tp_trail_bracket

    entry_price = bp.entry_ladder[len(bp.entry_ladder) // 2] if bp.entry_ladder else 0.0
    if entry_price <= 0:
        log.error("No valid entry price for %s — rejecting", bp.symbol)
        _publish_event(OrderEvent(
            symbol=bp.symbol,
            event_type="REJECTED",
            status="bad_entry_price",
            message="entry_ladder empty or mid-price <= 0",
        ))
        _ks_record_failed()
        return

    # Convert trail_pct to a dollar amount relative to entry
    trail_amount = round(entry_price * (bp.trail_pct / 100.0), 2)

    params = BracketParams(
        symbol=bp.symbol,
        qty=bp.qty,
        entry_limit=round(entry_price, 2),
        stop_loss=round(bp.stop_price, 2),
        trail_amount=trail_amount,
        tif="DAY",
    )

    log.info(
        "BRACKET_SUBMIT symbol=%s qty=%d entry=%.2f stop=%.2f trail=$%.2f direction=%s",
        bp.symbol, bp.qty, entry_price, bp.stop_price, trail_amount, bp.direction,
    )

    result = place_limit_tp_trail_bracket(_ib, params)

    log.info(
        "BRACKET_RESULT symbol=%s ok=%s parent=%s stop=%s trail=%s degraded=%s msg=%s",
        bp.symbol, result.ok, result.parent_id, result.stop_id,
        result.trail_id, result.degraded, result.message,
    )

    if result.ok:
        _ks_record_fill(bp.symbol, bp.qty, entry_price, pnl=0.0)
        if _EXIT_ENABLED:
            _exit_register(
                symbol=bp.symbol,
                side=bp.direction,
                entry_price=entry_price,
                qty=bp.qty,
                stop_price=bp.stop_price,
                trail_pct=bp.trail_pct,
                risk_usd=bp.risk_usd,
                playbook=getattr(bp, "source", "") or "",
                intent_id=getattr(bp, "intent_id", "") or "",
            )
        if _ATTRIB_ENABLED:
            _attrib_open(
                symbol=bp.symbol,
                side=bp.direction,
                entry_price=entry_price,
                qty=bp.qty,
                risk_usd=bp.risk_usd,
                playbook=getattr(bp, "source", "") or "",
                intent_id=getattr(bp, "intent_id", "") or "",
            )
            _attrib_fill(bp.symbol, entry_price, bp.qty)
        if _SC_ENABLED:
            _bp_intent = getattr(bp, "intent_id", "")
            if _bp_intent:
                _sc_record_open(
                    intent_id=_bp_intent,
                    symbol=bp.symbol,
                    playbook=getattr(bp, "source", "") or "",
                    sector=bp.sector,
                    industry=bp.industry,
                    session_state=get_us_equity_session(),
                    entry_price=entry_price,
                    qty=bp.qty,
                    risk_usd=bp.risk_usd,
                )
        _publish_event(OrderEvent(
            symbol=bp.symbol,
            event_type="BRACKET_READY",
            status="live_submitted",
            filled_qty=bp.qty,
            avg_fill_price=entry_price,
            order_id=str(result.parent_id or ""),
            message=(
                f"parent={result.parent_id} stop={result.stop_id} "
                f"trail={result.trail_id} degraded={result.degraded}"
            ),
        ))
    else:
        _ks_record_failed()
        _publish_event(OrderEvent(
            symbol=bp.symbol,
            event_type="REJECTED",
            status="bracket_failed",
            message=result.message[:200],
        ))


# ── Dev harness: generate synthetic fills for pipeline testing ───────

_dev_harness_idx = 0


def _dev_harness_tick() -> bool:
    """Generate one synthetic PAPER_FILL through the blueprint pipeline.

    Activates only when TL_EXEC_FORCE_PAPER_FILL=true AND
    TL_TEST_FORCE_SESSION is set.  Returns True if a fill was generated.
    """
    if not _DEV_HARNESS_ENABLED:
        return False

    global _dev_harness_idx
    sym = _DEV_HARNESS_SYMBOLS[_dev_harness_idx % len(_DEV_HARNESS_SYMBOLS)]
    _dev_harness_idx += 1

    # Build a realistic synthetic blueprint
    base_price = {"AAPL": 175.50, "MSFT": 412.30, "NVDA": 890.75}.get(sym, 200.0)
    step = round(base_price * 0.002, 2)
    ladder = [round(base_price + i * step, 2) for i in range(-1, 2)]
    stop = round(base_price * 0.99, 2)
    qty = 10
    risk = round((base_price - stop) * qty, 2)

    _harness_intent = f"dev_{sym}_{_dev_harness_idx}"

    bp = OrderBlueprint(
        symbol=sym,
        direction="LONG",
        qty=qty,
        entry_ladder=ladder,
        stop_price=stop,
        trail_pct=1.5,
        timeout_s=120,
        max_spread_pct=0.20,
        risk_usd=risk,
        confidence=0.75,
        total_score=55.0,
        quality="MED",
        stop_distance_pct=1.0,
        reason_codes=["dev_harness", "synthetic_fill"],
        notes="dev_harness_synthetic",
        source="dev_harness",
        sector="Technology",
        industry="Consumer Electronics" if sym == "AAPL" else "Software",
    )
    bp.intent_id = _harness_intent  # type: ignore[attr-defined]

    log.info(
        "dev_harness_inject symbol=%s qty=%d entry=%.2f stop=%.2f risk=$%.2f",
        sym, qty, base_price, stop, risk,
    )

    # Register with scorecard so _sc_dev_close can close it
    if _SC_ENABLED:
        _sc_record_open(
            intent_id=_harness_intent,
            symbol=sym,
            playbook="dev_harness",
            sector=bp.sector,
            industry=bp.industry,
            session_state=get_us_equity_session(),
            entry_price=base_price,
            qty=qty,
            risk_usd=risk,
        )

    _on_order_blueprint(bp)
    return True


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

    # ── Forced session override observability ────────────────────
    if is_test_session_forced():
        log.info(
            "forced_session_override requested=%s effective=%s source=env",
            get_test_force_session(), get_test_force_session(),
        )
    if _DEV_HARNESS_ENABLED:
        log.info(
            "dev_harness ENABLED  interval=%ds  symbols=%s  (synthetic PAPER_FILL pipeline)",
            _DEV_HARNESS_INTERVAL_S, _DEV_HARNESS_SYMBOLS,
        )

    log.info(
        "Execution arm starting  mode=%s  allow_extended=%s  execution_enabled=%s  live_flag=%s  heartbeat=%ss",
        settings.trade_mode.value,
        _ALLOW_EXTENDED,
        _EXECUTION_ENABLED,
        _LIVE_TRADING_ENABLED,
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

        # ── Exit intelligence: update tracked positions ──────────
        _exit_hb = ""
        _exit_closes_this_tick: list = []
        if _EXIT_ENABLED:
            from src.risk.exit_intelligence import _positions as _exit_positions
            # Pull real last prices from ingest cache
            try:
                from src.arms.ingest_main import _SYNTH_PREV_LAST as _real_prices
            except Exception:
                _real_prices = {}
            for _esym, _epos in list(_exit_positions.items()):
                # Use real ingest price; fall back to synthetic drift if unavailable
                _real_px = _real_prices.get(_esym.upper()) or _real_prices.get(_esym)
                _dev_px = _real_px if (_real_px and _real_px > 0) else                           _epos.entry_price * (1.0 + 0.005 * (tick % 10))
                _edecision = _exit_update(_esym, _dev_px)
                # Feed mark to attribution
                if _ATTRIB_ENABLED:
                    _attrib_mark(_esym, _epos.current_price,
                                 mfe=_epos.max_favorable_excursion,
                                 mae=_epos.max_adverse_excursion,
                                 r_multiple=_epos.r_multiple)
                # Process exit actions (TRIM / EXIT_FULL)
                if _edecision and _edecision.action in (_TRIM_25, _TRIM_50, _EXIT_FULL, _TIGHTEN_STOP):
                    _exit_closes_this_tick.append((_esym, _epos, _edecision))

            # Execute closes outside iteration to avoid dict-size-changed
            for _csym, _cpos, _cdec in _exit_closes_this_tick:
                _exit_pnl = _cpos.unrealized_pnl
                _exit_px = _cpos.current_price
                log.info(
                    "exit_action_executed symbol=%s action=%s pnl=%.2f exit_px=%.2f reasons=%s",
                    _csym, _cdec.action, _exit_pnl, _exit_px, _cdec.reason_codes,
                )
                if _ATTRIB_ENABLED:
                    _attrib_close(
                        symbol=_csym,
                        exit_price=_exit_px,
                        realized_pnl=_exit_pnl,
                        exit_action=_cdec.action,
                        exit_reason=",".join(_cdec.reason_codes),
                    )
                if _SC_ENABLED and _cpos.intent_id:
                    _sc_record_close(
                        intent_id=_cpos.intent_id,
                        exit_price=_exit_px,
                        pnl=_exit_pnl,
                        mfe=getattr(_cpos, "max_favorable_excursion", 0.0),
                        mae=getattr(_cpos, "max_adverse_excursion", 0.0),
                    )
                # iMessage exit alert
                try:
                    from src.arms.monitor_main import _alert_exit as _mx_exit
                    from src.signals.reentry_harvester import is_in_window as _reh_window
                    _mx_exit(
                        symbol=_csym,
                        qty=getattr(_cpos, "qty", 0),
                        pnl=_exit_pnl,
                        r_multiple=getattr(_cpos, "r_multiple", 0.0),
                        exit_action=_cdec.action,
                        bucket=getattr(_cpos, "playbook", ""),
                        reentry_open=_reh_window(_csym),
                    )
                except Exception:
                    pass
                # iMessage exit alert
                try:
                    from src.arms.monitor_main import _alert_exit as _mx_exit
                    from src.signals.reentry_harvester import is_in_window as _reh_window
                    _mx_exit(
                        symbol=_csym,
                        qty=getattr(_cpos, "qty", 0),
                        pnl=_exit_pnl,
                        r_multiple=getattr(_cpos, "r_multiple", 0.0),
                        exit_action=_cdec.action,
                        bucket=getattr(_cpos, "playbook", ""),
                        reentry_open=_reh_window(_csym),
                    )
                except Exception:
                    pass
                # Full exit: remove from exit tracking
                # ── TIGHTEN_STOP: modify trailing stop on IBKR ─────────
                if _cdec.action == _TIGHTEN_STOP:
                    _new_trail = _cdec.trail_pct
                    log.info(
                        "tighten_stop symbol=%s new_trail_pct=%.3f reasons=%s",
                        _csym, _new_trail, _cdec.reason_codes,
                    )
                    # Update position state so future tightens compound
                    _cpos.trail_pct = _new_trail
                    # Modify IBKR trailing stop if live connection exists
                    try:
                        from src.arms.execution_main import _ib
                        if _ib is not None and _ib.isConnected():
                            for _ord in _ib.trades():
                                if (hasattr(_ord, 'contract') and
                                        _ord.contract.symbol == _csym and
                                        hasattr(_ord, 'order') and
                                        getattr(_ord.order, 'orderType', '') == 'TRAIL'):
                                    _ord.order.trailingPercent = _new_trail
                                    _ib.placeOrder(_ord.contract, _ord.order)
                                    log.info(
                                        "ibkr_trail_modified symbol=%s trail_pct=%.3f orderId=%s",
                                        _csym, _new_trail,
                                        getattr(_ord.order, 'orderId', '?'),
                                    )
                                    break
                    except Exception as _te:
                        log.warning("tighten_stop_ibkr_err symbol=%s err=%s", _csym, _te)
                    # Skip attribution/scorecard close — position still open
                    continue

                if _cdec.action == _EXIT_FULL:
                    _exit_unregister(_csym)

            _exit_hb = f"  EXIT[pos={_exit_pos_count()}]"

        # ── Tuning: compute decisions periodically ───────────────
        _tune_hb = ""
        if _TUNING_ENABLED and tick % 6 == 0:  # every ~60s at 10s interval
            _tuning_compute()
        if _TUNING_ENABLED:
            _ts = _tuning_snapshot()
            _tune_hb = f"  TUNE[ovr={_ts.active_overrides}]"

        _attrib_hb = ""
        if _ATTRIB_ENABLED:
            _attrib_hb = f"  ATTRIB[open={_attrib_open_count()}]"
        log.info("heartbeat  tick=%d  orders_processed=%d  blueprints=%d%s%s%s", tick, count, bp_count, _exit_hb, _attrib_hb, _tune_hb)

        # ── Dev harness: inject synthetic fills periodically ─────
        if _DEV_HARNESS_ENABLED and tick % max(1, _DEV_HARNESS_INTERVAL_S // settings.heartbeat_interval_s) == 0:
            _dev_harness_tick()

        _stop_event.wait(settings.heartbeat_interval_s)
        if _stop_event.is_set():
            break

    # Cleanup
    if _bus is not None:
        try:
            _bus.close()
        except Exception:
            pass
    log.info("Execution arm stopped.")


if __name__ == "__main__":
    main()
