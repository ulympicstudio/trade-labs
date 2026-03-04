"""Risk Arm — pre-trade risk checks & position sizing.

Subscribes to ``TRADE_INTENT`` on the Redis bus.  For every intent:

1. Calls :func:`approve_new_trade` from the existing risk guard.
2. Calls :func:`calculate_position_size` to size the order.
3. Publishes an :class:`OrderPlan` on ``ORDER_PLAN_APPROVED``
   (or ``ORDER_PLAN_REJECTED`` with reasons).

Run::

    python -m src.arms.risk_main
"""

from __future__ import annotations

import os
import signal
import threading
import time
from datetime import date
from typing import Any, Dict, Optional

from src.config.settings import settings
from src.monitoring.logger import get_logger
from src.bus.topics import (
    TRADE_INTENT,
    ORDER_PLAN_APPROVED,
    ORDER_PLAN_REJECTED,
    OPEN_PLAN_CANDIDATE,
    PLAN_DRAFT,
    ORDER_BLUEPRINT,
    HEARTBEAT,
)
from src.schemas.messages import (
    TradeIntent, OrderPlan, Heartbeat,
    OpenPlanCandidate, PlanDraft, OrderBlueprint,
)
from src.market.session import get_us_equity_session, PREMARKET

log = get_logger("risk")

# ── Runtime state ────────────────────────────────────────────────────
_running = True
_bus = None           # type: Any
_intents_received = 0
_approved = 0
_rejected = 0
_drafts = 0
_blueprints = 0
_lock = threading.Lock()

# ── Configurable defaults (env-overridable) ──────────────────────────
_DEFAULT_EQUITY_USD = float(os.environ.get("TL_ACCOUNT_EQUITY", "100000"))
_DEFAULT_OPEN_RISK_USD = float(os.environ.get("TL_OPEN_RISK", "0"))
_DEFAULT_RISK_PCT = float(os.environ.get("TL_RISK_PER_TRADE_PCT", "0.005"))
_DEFAULT_ATR_MULT = float(os.environ.get("TL_ATR_MULTIPLIER", "2.0"))
_DEFAULT_TRAIL_PCT = float(os.environ.get("TL_TRAIL_PCT", "1.5"))

# Volatility-aware stop defaults for OFF_HOURS PlanDrafts
_VOL_STOP_MIN_PCT = 0.004   # 0.4 %
_VOL_STOP_MAX_PCT = 0.012   # 1.2 %
_VOL_STOP_DEFAULT_PCT = 0.006  # 0.6 % when vol_pct missing

# OrderBlueprint defaults (configurable via env)
_BLUEPRINT_TRAIL_MIN_PCT = float(os.environ.get("BLUEPRINT_TRAIL_MIN_PCT", "0.3"))
_BLUEPRINT_TRAIL_MAX_PCT = float(os.environ.get("BLUEPRINT_TRAIL_MAX_PCT", "1.0"))
_BLUEPRINT_TRAIL_FACTOR = float(os.environ.get("BLUEPRINT_TRAIL_FACTOR", "0.8"))
_BLUEPRINT_TIMEOUT_S = int(os.environ.get("BLUEPRINT_TIMEOUT_S", "120"))
_BLUEPRINT_MAX_SPREAD_PCT = float(os.environ.get("BLUEPRINT_MAX_SPREAD_PCT", "0.25"))
_BLUEPRINT_LADDER_STEP_PCT = float(os.environ.get("BLUEPRINT_LADDER_STEP_PCT", "0.05"))
_BLUEPRINT_LADDER_LEVELS = int(os.environ.get("BLUEPRINT_LADDER_LEVELS", "5"))

# Max dollar risk per trade.  Defaults to $50 in PAPER mode, $500 otherwise.
_PAPER_DEFAULT_MAX_RISK = "50"
_LIVE_DEFAULT_MAX_RISK = "500"
_MAX_RISK_USD = float(
    os.environ.get(
        "MAX_RISK_USD_PER_TRADE",
        _PAPER_DEFAULT_MAX_RISK if settings.trade_mode.value == "PAPER" else _LIVE_DEFAULT_MAX_RISK,
    )
)

# ── Escalation Mode (News Shock Engine v1) ─────────────────────────
_ESCALATION_ENABLED = os.environ.get("TL_ESCALATION_ENABLED", "false").lower() in ("1", "true", "yes")
_ESCALATION_IMPACT_MIN = int(os.environ.get("TL_ESCALATION_IMPACT_MIN", "6"))
_ESCALATION_VOL_RISING_MIN = float(os.environ.get("TL_ESCALATION_VOL_RISING_MIN", "0.05"))
_ESCALATION_SPREAD_MAX = float(os.environ.get("TL_ESCALATION_SPREAD_MAX", "0.0025"))
_ESCALATION_SIZE_MULT = float(os.environ.get("TL_ESCALATION_SIZE_MULT", "1.5"))
_ESCALATION_LADDER_WIDEN_BP = float(os.environ.get("TL_ESCALATION_LADDER_WIDEN_BP", "5"))
_ESCALATION_TRAIL_TIGHTEN_MULT = float(os.environ.get("TL_ESCALATION_TRAIL_TIGHTEN_MULT", "0.85"))

# Per-symbol vol tracking for escalation vol-rising detection
_last_vol_pct_by_symbol: Dict[str, float] = {}

# ── Heat Cap (Legend Phase 1) ────────────────────────────────────────
_HEAT_CAP_ENABLED = os.environ.get("TL_HEAT_CAP_ENABLED", "false").lower() in ("1", "true", "yes")
_HEAT_MAX_OPEN_POS = int(os.environ.get("TL_HEAT_MAX_OPEN_POS", "5"))
_HEAT_MAX_TOTAL_RISK_PCT = float(os.environ.get("TL_HEAT_MAX_TOTAL_RISK_PCT", "0.02"))

# Heat tracking state
_heat_open_positions: Dict[str, float] = {}   # symbol → risk_usd for active blueprints
_heat_blocked: int = 0                         # count of heat-blocked blueprints


def _handle_signal(signum, _frame):
    global _running
    log.info("Received shutdown signal (%s)", signum)
    _running = False


# ── Intent handler ───────────────────────────────────────────────────

def _on_trade_intent(intent: TradeIntent) -> None:
    """Process one TradeIntent from the bus."""
    global _intents_received, _approved, _rejected

    with _lock:
        _intents_received += 1

    log.info(
        "Received TradeIntent  id=%s  symbol=%s  dir=%s  conf=%.2f",
        intent.intent_id,
        intent.symbol,
        intent.direction,
        intent.confidence,
    )

    # ── 1. Compute entry & stop prices from the intent ───────────────
    entry_price = (intent.entry_zone_low + intent.entry_zone_high) / 2.0
    if entry_price <= 0:
        _publish_rejected(intent, ["invalid_entry_zone"])
        return

    stop_price = intent.invalidation
    if stop_price <= 0 or stop_price >= entry_price:
        _publish_rejected(intent, ["invalid_invalidation_price"])
        return

    risk_per_share = abs(entry_price - stop_price)
    if risk_per_share <= 0:
        _publish_rejected(intent, ["zero_risk_per_share"])
        return

    # ── 2. Position sizing (existing module) ─────────────────────────
    try:
        from src.risk.position_sizing import calculate_position_size

        sizing = calculate_position_size(
            account_equity=_DEFAULT_EQUITY_USD,
            risk_percent=_DEFAULT_RISK_PCT,
            entry_price=entry_price,
            stop_price=stop_price,
        )
    except Exception:
        log.exception("Position sizing failed for %s", intent.symbol)
        _publish_rejected(intent, ["sizing_error"])
        return

    if sizing.shares <= 0:
        _publish_rejected(intent, ["zero_shares"])
        return

    # ── 2b. Enforce MAX_RISK_USD_PER_TRADE cap ───────────────────────
    final_qty = sizing.shares
    uncapped_risk = sizing.risk_per_share * final_qty

    if uncapped_risk > _MAX_RISK_USD:
        final_qty = max(1, int(_MAX_RISK_USD / sizing.risk_per_share))
        capped_risk = sizing.risk_per_share * final_qty
        if capped_risk > _MAX_RISK_USD:
            # Even 1 share exceeds the cap — reject
            log.warning(
                "Risk cap reject  symbol=%s  1-share risk=$%.2f > cap=$%.2f",
                intent.symbol, capped_risk, _MAX_RISK_USD,
            )
            _publish_rejected(intent, ["risk_cap_exceeded"])
            return
        log.info(
            "Qty reduced for risk cap  symbol=%s  %d→%d  risk $%.2f→$%.2f  cap=$%.2f",
            intent.symbol, sizing.shares, final_qty,
            uncapped_risk, capped_risk, _MAX_RISK_USD,
        )

    final_risk_usd = sizing.risk_per_share * final_qty

    # ── 3. Risk approval (existing module) ───────────────────────────
    try:
        from src.risk.risk_guard import (
            RiskState,
            approve_new_trade,
        )

        state = RiskState(day=date.today())
        status = approve_new_trade(
            state=state,
            equity_usd=_DEFAULT_EQUITY_USD,
            open_risk_usd=_DEFAULT_OPEN_RISK_USD,
            proposed_trade_risk_usd=final_risk_usd,
        )
    except Exception:
        log.exception("Risk approval failed for %s", intent.symbol)
        _publish_rejected(intent, ["risk_guard_error"])
        return

    if not status.allowed:
        log.info(
            "Trade REJECTED  symbol=%s  reason=%s",
            intent.symbol,
            status.reason,
        )
        _publish_rejected(intent, ["risk_guard", status.reason])
        return

    # ── 4. Build OrderPlan with bracket/trailing fields ──────────────
    side = "BUY" if intent.direction == "LONG" else "SELL"

    plan = OrderPlan(
        symbol=intent.symbol,
        intent_id=intent.intent_id,
        qty=final_qty,
        entry_type="LMT",
        limit_prices=[round(entry_price, 2)],
        stop_price=round(stop_price, 2),
        trail_params={
            "side": side,
            "trail_pct": _DEFAULT_TRAIL_PCT,
            "atr_multiplier": _DEFAULT_ATR_MULT,
            "risk_per_share": round(sizing.risk_per_share, 4),
            "total_risk": round(final_risk_usd, 2),
        },
        tif="DAY",
        timeout_s=60.0,
    )

    log.info(
        "Trade APPROVED  symbol=%s  qty=%d  entry=%.2f  stop=%.2f"
        "  risk=$%.2f  cap=$%.2f",
        plan.symbol,
        plan.qty,
        entry_price,
        stop_price,
        final_risk_usd,
        _MAX_RISK_USD,
    )

    if _bus is not None:
        _bus.publish(ORDER_PLAN_APPROVED, plan)
    else:
        log.warning("Bus unavailable — approved plan for %s not published", plan.symbol)

    with _lock:
        _approved += 1


def _publish_rejected(intent: TradeIntent, reasons: list) -> None:
    """Publish a rejected OrderPlan with reason codes."""
    global _rejected

    plan = OrderPlan(
        symbol=intent.symbol,
        intent_id=intent.intent_id,
        qty=0,
        trail_params={"rejected": True, "reasons": reasons},
    )

    log.info(
        "Trade REJECTED  symbol=%s  intent=%s  reasons=%s",
        intent.symbol,
        intent.intent_id,
        reasons,
    )

    if _bus is not None:
        _bus.publish(ORDER_PLAN_REJECTED, plan)
    else:
        log.warning("Bus unavailable — rejected plan for %s not published", intent.symbol)

    with _lock:
        _rejected += 1


# ── Open plan candidate handler ───────────────────────────────────

def _on_open_plan_candidate(cand: OpenPlanCandidate) -> None:
    """Convert an off-hours OpenPlanCandidate into a PlanDraft (no execution).

    v4 blended stop distance:
        base = clamp(vol_pct * 2, 0.4%, 1.2%)
        adj  = 0.05% per momentum point
        stop_distance_pct = clamp(base + adj, 0.4%, 1.2%)
    Applies MAX_RISK_USD_PER_TRADE cap by reducing qty.
    """
    global _drafts

    entry_price = cand.suggested_entry

    if entry_price <= 0:
        log.warning(
            "Invalid OpenPlanCandidate  symbol=%s  entry=%.2f — skipping",
            cand.symbol, entry_price,
        )
        return

    # ── v4 blended stop distance (vol + momentum) ─────────────────────
    vol_pct = getattr(cand, "vol_pct", 0.0) or 0.0
    momentum_pts = getattr(cand, "momentum_pts", 0.0) or 0.0
    if vol_pct > 0:
        base_stop = max(_VOL_STOP_MIN_PCT, min(_VOL_STOP_MAX_PCT, vol_pct / 100.0 * 2.0))
    else:
        base_stop = _VOL_STOP_DEFAULT_PCT
    mom_adj = 0.0005 * momentum_pts  # 0.05% per momentum point
    stop_distance_pct = max(_VOL_STOP_MIN_PCT, min(_VOL_STOP_MAX_PCT, base_stop + mom_adj))

    stop_price = round(entry_price * (1.0 - stop_distance_pct), 2)

    if stop_price <= 0 or stop_price >= entry_price:
        log.warning(
            "Invalid computed stop  symbol=%s  entry=%.2f  stop=%.2f — skipping",
            cand.symbol, entry_price, stop_price,
        )
        return

    risk_per_share = abs(entry_price - stop_price)
    # Size using the same equity/risk-pct defaults
    dollar_risk = _DEFAULT_EQUITY_USD * _DEFAULT_RISK_PCT
    qty = max(1, int(dollar_risk / risk_per_share))
    risk_usd = risk_per_share * qty

    # Apply risk cap
    risk_cap_applied = False
    if risk_usd > _MAX_RISK_USD:
        qty = max(1, int(_MAX_RISK_USD / risk_per_share))
        risk_usd = risk_per_share * qty
        risk_cap_applied = True
        if risk_usd > _MAX_RISK_USD:
            log.warning(
                "PlanDraft risk cap reject  symbol=%s  1-share risk=$%.2f > cap=$%.2f",
                cand.symbol, risk_usd, _MAX_RISK_USD,
            )
            return

    # ── Build reason codes with stop & risk info ─────────────────────
    reason_codes = list(getattr(cand, "reason_codes", [])[:6])
    reason_codes.append(f"stop_dist={stop_distance_pct * 100:.2f}%")
    if risk_cap_applied:
        reason_codes.append(f"risk_cap_reduced=${_MAX_RISK_USD:.0f}")

    draft = PlanDraft(
        symbol=cand.symbol,
        suggested_entry=round(entry_price, 2),
        suggested_stop=stop_price,
        qty=qty,
        risk_usd=round(risk_usd, 2),
        confidence=cand.confidence,
        notes=f"off_hours_session={cand.session}",
        reason_codes=reason_codes[:8],
        news_count_2h=getattr(cand, "news_count_2h", 0),
        latest_headline=getattr(cand, "latest_headline", "")[:120],
        vol_pct=vol_pct,
        stop_distance_pct=round(stop_distance_pct * 100, 3),
        news_points=getattr(cand, "news_points", 0.0),
        momentum_pts=getattr(cand, "momentum_pts", 0.0),
        vol_points=getattr(cand, "vol_points", 0.0),
        spread_points=getattr(cand, "spread_points", 0.0),
        rsi_points=getattr(cand, "rsi_points", 0.0),
        liq_points=getattr(cand, "liq_points", 0.0),
        total_score=getattr(cand, "total_score", 0.0),
        quality=getattr(cand, "quality", ""),
        impact_score=getattr(cand, "impact_score", 0),
        burst_flag=getattr(cand, "burst_flag", False),
    )

    log.info(
        "PlanDraft  symbol=%s  qty=%d  entry=%.2f  stop=%.2f  risk=$%.2f  "
        "conf=%.2f  news_2h=%d  stop_dist=%.2f%%  vol=%.3f%%  "
        "mom_pts=%.0f  mom_adj=%.3f%%  reasons=%s",
        draft.symbol, draft.qty, draft.suggested_entry,
        draft.suggested_stop, draft.risk_usd, draft.confidence,
        draft.news_count_2h, stop_distance_pct * 100, vol_pct,
        momentum_pts, mom_adj * 100,
        draft.reason_codes[:3],
    )

    if _bus is not None:
        _bus.publish(PLAN_DRAFT, draft)
    else:
        log.warning("Bus unavailable — PlanDraft for %s not published", draft.symbol)

    with _lock:
        _drafts += 1

    # ── Build OrderBlueprint if we are in PREMARKET ──────────────────
    session = get_us_equity_session()
    if session == PREMARKET:
        _build_and_publish_blueprint(draft, cand)


# ── OrderBlueprint builder ───────────────────────────────────────────

def _build_entry_ladder(entry_price: float) -> list:
    """Build 3-5 price levels around entry: +/-step% in even steps."""
    step = entry_price * (_BLUEPRINT_LADDER_STEP_PCT / 100.0)
    n = _BLUEPRINT_LADDER_LEVELS
    half = n // 2
    levels = []
    for i in range(-half, n - half):
        levels.append(round(entry_price + i * step, 2))
    return levels


def _compute_trail_pct(stop_distance_pct: float) -> float:
    """trail_pct = clamp(stop_distance_pct * factor, min, max)."""
    raw = stop_distance_pct * _BLUEPRINT_TRAIL_FACTOR
    return round(max(_BLUEPRINT_TRAIL_MIN_PCT, min(_BLUEPRINT_TRAIL_MAX_PCT, raw)), 3)


def _build_and_publish_blueprint(draft: PlanDraft, cand: OpenPlanCandidate) -> None:
    """Convert a PlanDraft into an OrderBlueprint and publish.

    When escalation mode is enabled and conditions are met,
    adjusts qty, ladder width, and trailing stop.
    When heat cap is enabled, blocks new blueprints if portfolio
    would exceed max open positions or total risk %.
    """
    global _blueprints, _heat_blocked

    entry = draft.suggested_entry
    ladder = _build_entry_ladder(entry)
    trail = _compute_trail_pct(draft.stop_distance_pct)
    qty = draft.qty
    risk_usd = draft.risk_usd

    # ── Heat Cap gate (Legend Phase 1) ───────────────────────────────
    heat_blocked = False
    if _HEAT_CAP_ENABLED:
        n_open = len(_heat_open_positions)
        total_risk = sum(_heat_open_positions.values())
        max_risk_usd = _DEFAULT_EQUITY_USD * _HEAT_MAX_TOTAL_RISK_PCT

        pos_ok = n_open < _HEAT_MAX_OPEN_POS
        risk_ok = (total_risk + risk_usd) <= max_risk_usd

        if not pos_ok or not risk_ok:
            heat_blocked = True
            _heat_blocked += 1
            log.warning(
                "HEAT_BLOCK  symbol=%s  open_pos=%d/%d  total_risk=$%.2f/$%.2f  "
                "proposed_risk=$%.2f  reason=%s",
                draft.symbol, n_open, _HEAT_MAX_OPEN_POS,
                total_risk, max_risk_usd, risk_usd,
                "max_positions" if not pos_ok else "max_risk",
            )
            # Publish a zero-qty blueprint with escalation="HEAT_BLOCK"
            bp = OrderBlueprint(
                symbol=draft.symbol,
                direction="LONG",
                qty=0,
                entry_ladder=[],
                stop_price=draft.suggested_stop,
                trail_pct=trail,
                risk_usd=risk_usd,
                confidence=draft.confidence,
                total_score=draft.total_score,
                quality=draft.quality,
                stop_distance_pct=draft.stop_distance_pct,
                reason_codes=list(draft.reason_codes)[:6] + ["HEAT_BLOCK"],
                notes=f"heat_blocked open={n_open}/{_HEAT_MAX_OPEN_POS} risk=${total_risk:.0f}/${max_risk_usd:.0f}",
                impact_score=draft.impact_score,
                burst_flag=draft.burst_flag,
                escalation=True,
            )
            if _bus is not None:
                _bus.publish(ORDER_BLUEPRINT, bp)
            with _lock:
                _blueprints += 1
            return

    # ── Escalation Mode ────────────────────────────────────────────────
    escalation = False
    if _ESCALATION_ENABLED:
        impact_ok = draft.impact_score >= _ESCALATION_IMPACT_MIN or draft.burst_flag
        # Spread check (use spread_points as proxy: 1=tight)
        spread_ok = draft.spread_points >= 1.0
        # Vol-rising detection
        vol_pct_now = draft.vol_pct
        last_vol = _last_vol_pct_by_symbol.get(draft.symbol, 0.0)
        vol_rising = (vol_pct_now - last_vol) >= _ESCALATION_VOL_RISING_MIN
        _last_vol_pct_by_symbol[draft.symbol] = vol_pct_now

        escalation = impact_ok and spread_ok and vol_rising

        if escalation:
            # Size up (capped by risk)
            esc_qty = min(int(qty * _ESCALATION_SIZE_MULT), qty * 3)  # safety cap
            risk_per_share = abs(entry - draft.suggested_stop) if entry > draft.suggested_stop else 1.0
            max_qty_by_risk = max(1, int(_MAX_RISK_USD / risk_per_share)) if risk_per_share > 0 else qty
            qty = min(esc_qty, max_qty_by_risk)
            risk_usd = round(risk_per_share * qty, 2)

            # Widen ladder by escalation bps
            widen_pct = _ESCALATION_LADDER_WIDEN_BP / 10000.0
            half_widen = entry * widen_pct / 2.0
            ladder = [round(p - half_widen + (i / max(1, len(ladder) - 1)) * entry * widen_pct, 2)
                      for i, p in enumerate(ladder)]

            # Tighten trailing stop
            trail = round(max(_BLUEPRINT_TRAIL_MIN_PCT,
                              min(_BLUEPRINT_TRAIL_MAX_PCT,
                                  trail * _ESCALATION_TRAIL_TIGHTEN_MULT)), 3)

            log.info(
                "escalation applied  symbol=%s  qty=%d  trail=%.2f%%  "
                "impact=%d  burst=%s  vol_rising=%.3f",
                draft.symbol, qty, trail,
                draft.impact_score, draft.burst_flag,
                vol_pct_now - last_vol,
            )

    bp = OrderBlueprint(
        symbol=draft.symbol,
        direction="LONG",
        qty=qty,
        entry_ladder=ladder,
        stop_price=draft.suggested_stop,
        trail_pct=trail,
        take_profit_levels=[],  # future: computed from R-multiples
        timeout_s=_BLUEPRINT_TIMEOUT_S,
        max_spread_pct=_BLUEPRINT_MAX_SPREAD_PCT,
        risk_usd=risk_usd,
        confidence=draft.confidence,
        total_score=draft.total_score,
        quality=draft.quality,
        stop_distance_pct=draft.stop_distance_pct,
        reason_codes=list(draft.reason_codes)[:8],
        notes=f"premarket_blueprint entry_ladder={len(ladder)}"
              + (" escalation=true" if escalation else ""),
        impact_score=draft.impact_score,
        burst_flag=draft.burst_flag,
        escalation=escalation,
    )

    log.info(
        "OrderBlueprint  symbol=%s  qty=%d  ladder=%s  stop=%.2f  "
        "trail=%.2f%%  timeout=%ds  max_spread=%.2f%%  risk=$%.2f  Q=%s",
        bp.symbol, bp.qty,
        [f"{p:.2f}" for p in bp.entry_ladder],
        bp.stop_price, bp.trail_pct, bp.timeout_s,
        bp.max_spread_pct, bp.risk_usd, bp.quality,
    )

    if _bus is not None:
        _bus.publish(ORDER_BLUEPRINT, bp)
    else:
        log.warning("Bus unavailable — OrderBlueprint for %s not published", bp.symbol)

    # ── Heat tracking: register this blueprint ───────────────────────
    if _HEAT_CAP_ENABLED:
        _heat_open_positions[draft.symbol] = risk_usd

    with _lock:
        _blueprints += 1


# ── Bus connection ───────────────────────────────────────────────────

def _connect_bus():
    """Non-blocking attempt to connect to the event bus and subscribe."""
    try:
        from src.bus.bus_factory import get_bus
        bus = get_bus(max_retries=1)
        if not bus.is_connected:
            log.warning("Event bus unavailable — will retry next cycle")
            return None
        bus.subscribe(TRADE_INTENT, _on_trade_intent, msg_type=TradeIntent)
        bus.subscribe(OPEN_PLAN_CANDIDATE, _on_open_plan_candidate, msg_type=OpenPlanCandidate)
        log.info("Subscribed to %s, %s", TRADE_INTENT, OPEN_PLAN_CANDIDATE)
        return bus
    except Exception:
        log.exception("Failed to initialise event bus — will retry")
        return None


# ── Main loop ────────────────────────────────────────────────────────

def main() -> None:
    """Entry-point for the risk arm."""
    global _bus

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    log.info(
        "Risk arm starting  mode=%s  equity=$%.0f  risk_pct=%.3f"
        "  max_risk_usd=$%.0f  heartbeat=%ss",
        settings.trade_mode.value,
        _DEFAULT_EQUITY_USD,
        _DEFAULT_RISK_PCT,
        _MAX_RISK_USD,
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
            _bus.publish(HEARTBEAT, Heartbeat(arm="risk"))

        with _lock:
            recv, appr, rej, drafts, bps = (
                _intents_received, _approved, _rejected, _drafts, _blueprints,
            )

        heat_str = ""
        if _HEAT_CAP_ENABLED:
            heat_str = (
                f"  heat: open={len(_heat_open_positions)}/{_HEAT_MAX_OPEN_POS}"
                f"  risk=${sum(_heat_open_positions.values()):.0f}"
                f"  blocked={_heat_blocked}"
            )

        log.info(
            "heartbeat  tick=%d  intents=%d  approved=%d  rejected=%d  drafts=%d  blueprints=%d%s",
            tick, recv, appr, rej, drafts, bps, heat_str,
        )

        time.sleep(settings.heartbeat_interval_s)

    # Cleanup
    if _bus is not None:
        try:
            _bus.close()
        except Exception:
            pass
    log.info("Risk arm stopped.")


if __name__ == "__main__":
    main()
