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
from src.market.session import get_us_equity_session, PREMARKET, is_test_session_forced, get_test_force_session
from src.risk.session_gate import check_session_gate as _check_session_gate
from src.risk.kill_switch import (
    check_circuit_breakers as _check_breakers,
    record_trade as _record_breaker_trade,
    status_summary as _breaker_status,
)
from src.signals.regime import (
    get_regime as _get_regime,
    RISK_MULT as _REGIME_RISK_MULT,
    PANIC as _REGIME_PANIC,
    VOL_HIGH as _VOL_HIGH,
)
from src.risk.kill_switch import update_atr_spike as _ks_update_atr_spike
from src.risk.sector_limits import (
    check_sector_limit as _check_sector_limit,
    check_industry_limit as _check_industry_limit,
    record_draft as _record_sector_draft,
    record_fill as _record_sector_fill,
    record_industry_fill as _record_industry_fill,
    record_industry_close as _record_industry_close,
    get_sector_top as _get_sector_top,
    BLOCK as _SECTOR_BLOCK,
    REDUCE as _SECTOR_REDUCE,
)
from src.universe.sector_mapper import classify_symbol as _classify_symbol
from src.signals.sector_intel import get_sector_alignment as _get_sector_alignment
from src.signals.sector_rotation_selector import (
    get_last_rotation_decision as _get_rotation_decision,
    ROTATION_SEL_ENABLED as _ROTATION_SEL_ENABLED,
)
from src.universe.scan_scheduler import (
    get_last_schedule as _get_scan_schedule,
    SCAN_SCHEDULER_ENABLED as _SCAN_SCHED_ENABLED,
    HIGH as _PRI_HIGH,
    NORMAL as _PRI_NORMAL,
    LOW as _PRI_LOW,
)
from src.signals.volatility_leaders import (
    compute_leader as _vol_compute_leader,
    get_leader_summary as _vol_leader_summary,
)
from src.signals.industry_rotation import (
    compute_industry_rotation as _rotation_compute,
    get_rotation_summary as _rotation_summary,
    get_risk_qty_multiplier as _rotation_qty_mult,
    ROTATION_ENABLED as _ROTATION_ENABLED,
)
from src.signals.allocation_engine import (
    score_symbol_confluence as _alloc_confluence,
    check_bucket_capacity as _alloc_bucket_cap,
    get_confluence_qty_mult as _alloc_qty_mult,
    record_bucket_fill as _alloc_record_fill,
    get_allocation_summary as _alloc_summary,
    ALLOC_ENABLED as _ALLOC_ENABLED,
)
from src.signals.market_mode import (
    get_last_mode as _mm_get_last,
    get_market_mode_summary as _mm_summary,
    MODE_ENABLED as _MM_ENABLED,
)
from src.analysis.playbook_scorecard import (
    record_trade_open as _sc_record_open,
    get_risk_sizing_mult as _sc_risk_mult,
    get_playbook_scorecard as _sc_get_card,
    SCORECARD_ENABLED as _SC_ENABLED,
)
from src.risk.exit_intelligence import (
    get_position_count as _exit_pos_count,
    EXIT_ENABLED as _EXIT_ENABLED,
)
from src.analysis.self_tuning import (
    get_qty_mult_nudge as _tune_qty_mult,
    get_cap_mult_nudge as _tune_cap_mult,
    get_tuning_snapshot as _tune_snapshot,
    TUNING_ENABLED as _TUNING_ENABLED,
)
from src.analysis.pnl_attribution import (
    ATTRIB_ENABLED as _ATTRIB_ENABLED,
    get_open_count as _attrib_open_count,
)
from src.risk.open_risk_tracker import (
    record_fill as _ort_record_fill,
    record_close as _ort_record_close,
    get_total_open_risk as _ort_total_risk,
    get_position_count as _ort_pos_count,
)

log = get_logger("risk")

# ── Volatility-aware risk multipliers ────────────────────────────────
_VOL_LEADER_STOP_MULT = float(os.environ.get("TL_RISK_VOL_STOP_MULT", "1.3"))
_VOL_LEADER_QTY_MULT = float(os.environ.get("TL_RISK_VOL_QTY_MULT", "0.85"))

# ── Regime gate ─────────────────────────────────────────────────────
_BLOCK_LONGS_IN_PANIC = os.environ.get(
    "TL_RISK_BLOCK_LONGS_IN_PANIC", "true"
).lower() in ("1", "true", "yes")

# ── A: EventScore → position sizing ───────────────────────────────────
_EVENTSIZE_ENABLED = os.environ.get(
    "TL_RISK_EVENTSIZE_ENABLED", "true"
).lower() in ("1", "true", "yes")
_EVENTSIZE_BASE = int(os.environ.get("TL_RISK_EVENTSIZE_BASE", "50"))
_EVENTSIZE_MIN = float(os.environ.get("TL_RISK_EVENTSIZE_MIN", "0.6"))
_EVENTSIZE_MAX = float(os.environ.get("TL_RISK_EVENTSIZE_MAX", "1.8"))

# ── G: Volatility regime stop/qty multipliers ───────────────────────
_VOL_STOP_MULT = float(os.environ.get("TL_RISK_VOL_STOP_MULT", "1.3"))
_VOL_QTY_MULT = float(os.environ.get("TL_RISK_VOL_QTY_MULT", "0.8"))

# ── Runtime state ────────────────────────────────────────────────────
_running = True
_stop_event = threading.Event()  # cooperative shutdown
_bus = None           # type: Any
_intents_received = 0
_approved = 0
_rejected = 0
_drafts = 0
_draft_store: "dict[str, tuple]" = {}   # symbol → (PlanDraft, OpenPlanCandidate)
_DRAFT_TTL_S: float = float(__import__('os').environ.get("TL_DRAFT_TTL_S", "300"))  # 5-min TTL
_draft_store_ts: "dict[str, float]" = {}  # symbol → epoch timestamp
_blueprints = 0
_lock = threading.Lock()

# ── Configurable defaults (env-overridable) ──────────────────────────
_DEFAULT_EQUITY_USD = float(os.environ.get("TL_ACCOUNT_EQUITY", "100000"))
_DEFAULT_OPEN_RISK_USD = float(os.environ.get("TL_OPEN_RISK", "0"))
_DEFAULT_RISK_PCT = float(os.environ.get("TL_RISK_PER_TRADE_PCT", "0.005"))
_DEFAULT_ATR_MULT = float(os.environ.get("TL_ATR_MULTIPLIER", "2.0"))
_DEFAULT_TRAIL_PCT = float(os.environ.get("TL_TRAIL_PCT", "1.5"))

def _adaptive_trail_pct(bucket: str) -> float:
    """Scale trail_pct from scorecard avg_r_multiple per bucket.
    avg_r <1.0 → tight trail; avg_r >2.0 → wide trail; cold start → default."""
    _TRAIL_MIN, _TRAIL_MAX = 0.8, 3.0
    if not _SC_ENABLED or bucket == "none":
        return _DEFAULT_TRAIL_PCT
    card = _sc_get_card(bucket)
    if card is None or getattr(card, "sample_n", 0) < 10:
        return _DEFAULT_TRAIL_PCT
    scaled = _TRAIL_MIN + (card.avg_r_multiple - 1.0) * 1.7
    result = round(max(_TRAIL_MIN, min(_TRAIL_MAX, scaled)), 2)
    log.debug(
        "adaptive_trail bucket=%s avg_r=%.2f trail_pct=%.2f",
        bucket, card.avg_r_multiple, result,
    )
    return result

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
    _stop_event.set()


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

    # ── 2a. Session gate — time-of-day quality filter ────────────────
    _gate = _check_session_gate()
    if not _gate.allowed:
        log.info(
            "risk_session_gate BLOCK symbol=%s reason=%s",
            intent.symbol, _gate.reason,
        )
        _publish_rejected(intent, [f"session_gate:{_gate.reason}"])
        return

    final_qty = sizing.shares

    # Apply session quality score to position size
    if _gate.quality_score < 1.0:
        qty_before_gate = final_qty
        final_qty = max(1, int(final_qty * _gate.quality_score))
        log.info(
            "risk_session_gate symbol=%s window=%s quality=%.2f qty %d->%d",
            intent.symbol, _gate.reason, _gate.quality_score,
            qty_before_gate, final_qty,
        )

    # ── 2b. Enforce MAX_RISK_USD_PER_TRADE cap ───────────────────────
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

    # ── 2c. EventScore → position sizing (A) ──────────────────────
    if _EVENTSIZE_ENABLED:
        _event_score_val = 0
        for rc in intent.reason_codes:
            if rc.startswith("event_score="):
                try:
                    _event_score_val = int(rc.split("=", 1)[1])
                except (ValueError, IndexError):
                    pass
                break
        if _event_score_val > 0:
            _es_factor = max(_EVENTSIZE_MIN, min(_EVENTSIZE_MAX,
                             _event_score_val / max(_EVENTSIZE_BASE, 1)))
            qty_before_es = final_qty
            final_qty = max(1, int(final_qty * _es_factor))
            final_risk_usd = sizing.risk_per_share * final_qty
            log.info(
                "risk_eventsize symbol=%s event_score=%d factor=%.2f qty %d->%d",
                intent.symbol, _event_score_val, _es_factor,
                qty_before_es, final_qty,
            )

    # ── 2d. Regime risk multiplier ─────────────────────────────────
    _regime = _get_regime()
    _regime_mult = _REGIME_RISK_MULT.get(_regime.regime, 1.0)

    # Feed ATR spike state to kill_switch (J)
    _ks_update_atr_spike(_regime.atr_pct, _regime.atr_baseline_pct)

    if _regime.regime == _REGIME_PANIC and _BLOCK_LONGS_IN_PANIC:
        if intent.direction == "LONG":
            log.warning(
                "risk_regime BLOCK  symbol=%s  regime=PANIC  direction=LONG",
                intent.symbol,
            )
            _publish_rejected(intent, ["regime_panic_long_block"])
            return

    qty_before_regime = final_qty
    if _regime_mult != 1.0:
        final_qty = max(1, int(final_qty * _regime_mult))
        final_risk_usd = sizing.risk_per_share * final_qty

    if qty_before_regime != final_qty or _regime_mult != 1.0:
        log.info(
            "risk_regime symbol=%s regime=%s mult=%.2f qty_before=%d qty_after=%d",
            intent.symbol, _regime.regime, _regime_mult,
            qty_before_regime, final_qty,
        )

    # ── 2e. Volatility regime stop widening & qty reduction (G) ────
    if _regime.vol_regime == _VOL_HIGH:
        qty_before_vol = final_qty
        final_qty = max(1, int(final_qty * _VOL_QTY_MULT))
        final_risk_usd = sizing.risk_per_share * final_qty
        # Widen stop (adjust stop_price outward)
        stop_price = round(stop_price - abs(entry_price - stop_price) * (_VOL_STOP_MULT - 1.0), 2)
        log.info(
            "risk_vol_regime symbol=%s vol=%s stop_mult=%.1f qty_mult=%.1f qty %d->%d stop=%.2f",
            intent.symbol, _regime.vol_regime, _VOL_STOP_MULT, _VOL_QTY_MULT,
            qty_before_vol, final_qty, stop_price,
        )

    # ── 2f. Spread-adaptive qty penalty ─────────────────────────────
    # Penalise wide-spread entries: every bp above threshold costs size.
    # spread_pct=0.05% → mult=1.0  |  0.20% → 0.55  |  0.40%+ → floor
    _SPREAD_FREE_PCT  = float(os.environ.get("TL_SPREAD_FREE_PCT",  "0.0005"))  # 0.05%
    _SPREAD_SLOPE     = float(os.environ.get("TL_SPREAD_SLOPE",     "3.0"))     # steepness
    _SPREAD_FLOOR     = float(os.environ.get("TL_SPREAD_FLOOR",     "0.25"))    # min mult
    if _spread_pct > _SPREAD_FREE_PCT:
        _spread_mult = max(_SPREAD_FLOOR, 1.0 - (_spread_pct - _SPREAD_FREE_PCT) * _SPREAD_SLOPE * 100)
        qty_before_spread = final_qty
        final_qty = max(1, int(final_qty * _spread_mult))
        final_risk_usd = sizing.risk_per_share * final_qty
        if qty_before_spread != final_qty:
            log.info(
                "risk_spread_penalty symbol=%s spread_pct=%.4f mult=%.2f qty %d->%d",
                intent.symbol, _spread_pct, _spread_mult,
                qty_before_spread, final_qty,
            )

    # ── 2x. Consolidated risk path log ───────────────────────────────
    _reductions: list[str] = []
    if uncapped_risk > _MAX_RISK_USD:
        _reductions.append("risk_cap")
    _es_val_str = "n/a"
    for rc in intent.reason_codes:
        if rc.startswith("event_score="):
            _es_val_str = rc.split("=", 1)[1]
            break
    if _EVENTSIZE_ENABLED and _es_val_str != "n/a":
        _reductions.append(f"event_size(es={_es_val_str})")
    if _regime_mult != 1.0:
        _reductions.append(f"regime({_regime.regime}x{_regime_mult:.2f})")
    if _regime.vol_regime == _VOL_HIGH:
        _reductions.append(f"vol_high(x{_VOL_QTY_MULT:.1f})")

    # ── Sector evaluation for TradeIntent path ───────────────────────
    _ti_sp = _classify_symbol(intent.symbol)
    _ti_sa = _get_sector_alignment(intent.symbol)
    _proposed_notional = entry_price * final_qty
    _ti_slr = _check_sector_limit(
        intent.symbol, _ti_sa.sector_state,
        proposed_notional=_proposed_notional,
    )
    _exposure_before = _ti_slr.exposure
    _exposure_after_est = _exposure_before + (_proposed_notional / 100_000.0)
    if _ti_slr.verdict == _SECTOR_BLOCK:
        log.info(
            "sector_limit_block symbol=%s sector=%s exposure=%.2f "
            "soft=%.2f hard=%.2f reason=%s",
            intent.symbol, _ti_slr.sector, _exposure_after_est,
            _ti_slr.soft_limit, _ti_slr.hard_limit, _ti_slr.reason,
        )
        _publish_rejected(intent, ["sector_limit_block", _ti_slr.reason])
        return
    if _ti_slr.verdict == _SECTOR_REDUCE:
        _qty_before_sr = final_qty
        final_qty = max(1, int(final_qty * _ti_slr.qty_mult))
        final_risk_usd = sizing.risk_per_share * final_qty
        _reductions.append(f"sector_reduce(x{_ti_slr.qty_mult:.2f})")
        log.info(
            "sector_limit_reduce symbol=%s sector=%s exposure=%.2f "
            "soft=%.2f hard=%.2f mult=%.2f",
            intent.symbol, _ti_slr.sector, _exposure_after_est,
            _ti_slr.soft_limit, _ti_slr.hard_limit, _ti_slr.qty_mult,
        )
    elif _ti_slr.qty_mult != 1.0:
        _qty_before_sr = final_qty
        final_qty = max(1, int(final_qty * _ti_slr.qty_mult))
        final_risk_usd = sizing.risk_per_share * final_qty
        _reductions.append(f"sector_state(x{_ti_slr.qty_mult:.2f})")
    log.info(
        "risk_sector symbol=%s sector=%s state=%s "
        "exposure_before=%.2f exposure_after_est=%.2f "
        "soft=%.2f hard=%.2f mult=%.2f action=%s qty=%d",
        intent.symbol, _ti_sp.sector, _ti_sa.sector_state,
        _exposure_before, _exposure_after_est,
        _ti_slr.soft_limit, _ti_slr.hard_limit,
        _ti_slr.qty_mult, _ti_slr.verdict, final_qty,
    )

    # ── Volatility-aware qty/stop adjustment ──────────────────────────
    _vl = _vol_compute_leader(intent.symbol, _regime.regime, _ti_sa.sector_state)
    _vol_stop_mult = 1.0
    _vol_qty_mult = 1.0
    if _vl.leader_state == "TRIGGERED" and _vl.leader_score >= 65:
        _vol_stop_mult = _VOL_LEADER_STOP_MULT
        _vol_qty_mult = _VOL_LEADER_QTY_MULT
        qty_before_vl = final_qty
        # Widen stop for volatile leaders
        stop_price = round(
            stop_price - abs(entry_price - stop_price) * (_vol_stop_mult - 1.0), 2
        )
        final_qty = max(1, int(final_qty * _vol_qty_mult))
        final_risk_usd = sizing.risk_per_share * final_qty
        _reductions.append(f"vol_leader(score={_vl.leader_score},stop_x{_vol_stop_mult:.1f},qty_x{_vol_qty_mult:.2f})")
    log.info(
        "risk_volatility symbol=%s leader_score=%d state=%s atrx=%.1f "
        "rvol=%.1f stop_mult=%.2f qty_mult=%.2f",
        intent.symbol, _vl.leader_score, _vl.leader_state,
        _vl.atr_expansion_ratio, _vl.rvol_ratio,
        _vol_stop_mult, _vol_qty_mult,
    )

    # ── Industry rotation qty adjustment ──────────────────────────────
    _rot = _rotation_compute(intent.symbol)
    _rot_mult = _rotation_qty_mult(_rot.rotation_state) if _ROTATION_ENABLED else 1.0
    if _rot_mult != 1.0:
        final_qty = max(1, int(final_qty * _rot_mult))
        final_risk_usd = sizing.risk_per_share * final_qty
        _reductions.append(f"rotation({_rot.rotation_state},x{_rot_mult:.2f})")
    log.info(
        "risk_rotation symbol=%s industry=%s state=%s score=%d mult=%.2f",
        intent.symbol, _rot.industry, _rot.rotation_state,
        _rot.rotation_score, _rot_mult,
    )

    # ── Allocation-aware risk shaping ─────────────────────────────────
    _alloc_action = "PASS"
    _alloc_conf_mult = 1.0
    if _ALLOC_ENABLED:
        # Extract event_score from reason_codes
        _alloc_es = 0
        for rc in intent.reason_codes:
            if rc.startswith("event_score="):
                try:
                    _alloc_es = int(rc.split("=", 1)[1])
                except (ValueError, IndexError):
                    pass
                break

        _alloc_c = _alloc_confluence(
            symbol=intent.symbol,
            event_score=_alloc_es,
            sector_state=_ti_sa.sector_state,
            rotation_state=_rot.rotation_state,
            rotation_score=_rot.rotation_score,
            vol_state=_vl.leader_state,
            vol_score=_vl.leader_score,
            regime=_regime.regime,
        )
        _bucket_verdict = _alloc_bucket_cap(_alloc_c.bucket)
        _alloc_conf_mult = _alloc_qty_mult(_alloc_c.confluence_score, _bucket_verdict)
        if _bucket_verdict == "BLOCK":
            _alloc_action = "BLOCK"
            log.info(
                "risk_allocation symbol=%s bucket=%s priority=%.1f "
                "confluence=%.2f bucket_fill=FULL qty_mult=0.00 action=BLOCK",
                intent.symbol, _alloc_c.bucket, _alloc_c.priority_score,
                _alloc_c.confluence_score,
            )
            _publish_rejected(intent, ["allocation_bucket_full", _alloc_c.bucket])
            return
        elif _alloc_conf_mult != 1.0:
            _alloc_action = "REDUCE"
            final_qty = max(1, int(final_qty * _alloc_conf_mult))
            final_risk_usd = sizing.risk_per_share * final_qty
            _reductions.append(f"alloc(conf={_alloc_c.confluence_score:.2f},bucket={_bucket_verdict},x{_alloc_conf_mult:.2f})")
        log.info(
            "risk_allocation symbol=%s bucket=%s priority=%.1f "
            "confluence=%.2f bucket_fill=%s qty_mult=%.2f action=%s",
            intent.symbol, _alloc_c.bucket, _alloc_c.priority_score,
            _alloc_c.confluence_score, _bucket_verdict,
            _alloc_conf_mult, _alloc_action,
        )


# ── Time-of-day cap multiplier schedule ─────────────────────────────
def _get_tod_cap_mult() -> float:
    """Return a position-cap multiplier based on time of day (ET).
    Burst during open/close; throttle during lunch chop."""
    import os
    if os.environ.get("TL_TOD_CAP_MULT_ENABLED", "true").lower() != "true":
        return 1.0
    from datetime import datetime
    import zoneinfo
    t = datetime.now(tz=zoneinfo.ZoneInfo("America/New_York")).time()
    from datetime import time as dtime
    if   dtime(9, 30) <= t < dtime(10, 0):  return 1.00  # open burst
    elif dtime(10, 0) <= t < dtime(11, 30): return 0.85  # early session
    elif dtime(11,30) <= t < dtime(13,  0): return 0.65  # lunch chop
    elif dtime(13, 0) <= t < dtime(15,  0): return 0.80  # afternoon
    elif dtime(15, 0) <= t < dtime(16,  0): return 0.90  # closing push
    return 0.50  # off-hours (safety floor)

    # ── Market Mode risk adjustment ──────────────────────────────────
    _mm_action = "PASS"
    _mm_cap_mult = 1.0
    _mm_qty_mult = 1.0
    if _MM_ENABLED:
        _mm = _mm_get_last()
        if _mm is not None:
            _mm_cap_mult = _mm.position_cap_mult
            # ── Time-of-day burst multiplier ──────────────────────────
            _tod_mult = _get_tod_cap_mult()
            _mm_cap_mult = max(0.25, min(2.0, _mm_cap_mult * _tod_mult))
            if _mm.risk_posture == "MINIMAL":
                _mm_qty_mult = 0.60
            elif _mm.risk_posture == "DEFENSIVE":
                _mm_qty_mult = 0.80
            elif _mm.risk_posture == "AGGRESSIVE":
                _mm_qty_mult = 1.10
            # Apply mode qty multiplier
            if _mm_qty_mult != 1.0:
                _mm_action = "REDUCE" if _mm_qty_mult < 1.0 else "BOOST"
                final_qty = max(1, int(final_qty * _mm_qty_mult))
                final_risk_usd = sizing.risk_per_share * final_qty
                _reductions.append(f"mode({_mm.mode},x{_mm_qty_mult:.2f})")
            log.info(
                "risk_mode_adjustment symbol=%s mode=%s cap_mult=%.2f tod_mult=%.2f "
                "qty_mult=%.2f action=%s posture=%s",
                intent.symbol, _mm.mode, _mm_cap_mult, _tod_mult,
                _mm_qty_mult, _mm_action, _mm.risk_posture,
            )

    # ── Scorecard risk sizing ────────────────────────────────────────
    _sc_action = "PASS"
    _sc_mult = 1.0
    _sc_bucket = "none"
    if _SC_ENABLED and _ALLOC_ENABLED:
        _sc_bucket = _alloc_c.bucket
        _sc_mult = _sc_risk_mult(_sc_bucket)
        if _sc_mult != 1.0:
            _sc_action = "REDUCE" if _sc_mult < 1.0 else "BOOST"
            final_qty = max(1, int(final_qty * _sc_mult))
            final_risk_usd = sizing.risk_per_share * final_qty
            _reductions.append(f"scorecard({_sc_bucket},x{_sc_mult:.2f})")
        log.info(
            "risk_scorecard_adjustment symbol=%s bucket=%s sc_mult=%.3f "
            "action=%s conf_score=%.3f",
            intent.symbol, _sc_bucket, _sc_mult,
            _sc_action, _sc_get_card(_sc_bucket).confidence_score,
        )

    # ── Self-Tuning: qty multiplier + cap multiplier nudges ──────────
    _tune_action = "PASS"
    if _TUNING_ENABLED and _ALLOC_ENABLED:
        _t_bucket = _alloc_c.bucket
        _tq_nudge = _tune_qty_mult(_t_bucket)
        _tc_nudge = 0.0
        if _MM_ENABLED:
            _tmm = _mm_get_last()
            if _tmm is not None:
                _tc_nudge = _tune_cap_mult(_tmm.mode)
        if _tq_nudge != 0.0 or _tc_nudge != 0.0:
            _tuned_qty_mult = max(0.5, min(1.5, 1.0 + _tq_nudge))
            _tuned_cap_mult = max(0.5, min(2.0, 1.0 + _tc_nudge))
            if _tq_nudge != 0.0:
                _tune_action = "REDUCE" if _tq_nudge < 0 else "BOOST"
                final_qty = max(1, int(final_qty * _tuned_qty_mult))
                final_risk_usd = sizing.risk_per_share * final_qty
                _reductions.append(f"tuning({_t_bucket},qx{_tuned_qty_mult:.2f})")
            log.info(
                "risk_tuning_adjustment symbol=%s bucket=%s "
                "tuned_qty_mult=%.3f tuned_cap_mult=%.3f action=%s",
                intent.symbol, _t_bucket,
                _tuned_qty_mult, _tuned_cap_mult, _tune_action,
            )

    # ── Phase B: Rotation / scan priority bias ───────────────────────
    _rot_bias_mult = 1.0
    _rot_bias_action = "PASS"
    if _ROTATION_SEL_ENABLED or _SCAN_SCHED_ENABLED:
        _sp_class = _classify_symbol(intent.symbol)
        _sym_sector = _sp_class.sector
        _sym_industry = _sp_class.industry

        # Check scan priority
        _scan_pri = _PRI_NORMAL
        if _SCAN_SCHED_ENABLED:
            _scan_sched = _get_scan_schedule()
            _scan_pri = _scan_sched.get(intent.symbol, _PRI_NORMAL)

        # Check if sector is rotating out
        _is_rotating_out = False
        if _ROTATION_SEL_ENABLED:
            _rot_dec = _get_rotation_decision()
            if _rot_dec is not None:
                _is_rotating_out = _sym_sector in _rot_dec.rotating_out

        if _scan_pri == _PRI_LOW or _is_rotating_out:
            _rot_bias_mult = 0.70
            _rot_bias_action = "REDUCE"
            final_qty = max(1, int(final_qty * _rot_bias_mult))
            final_risk_usd = sizing.risk_per_share * final_qty
            _reductions.append(f"rotation_bias({_scan_pri})")
        elif _scan_pri == _PRI_HIGH and not _is_rotating_out:
            _rot_bias_mult = 1.0  # full size — no penalty
            _rot_bias_action = "PASS"

        log.info(
            "risk_rotation_bias symbol=%s sector=%s industry=%s "
            "priority=%s action=%s mult=%.2f rotating_out=%s",
            intent.symbol, _sym_sector, _sym_industry,
            _scan_pri, _rot_bias_action, _rot_bias_mult, _is_rotating_out,
        )

    log.info(
        "risk_path symbol=%s conf_raw=%.3f event_score=%s regime=%s "
        "regime_mult=%.2f vol=%s qty_raw=%d qty_final=%d risk_usd=%.2f "
        "reductions=%s",
        intent.symbol, intent.confidence, _es_val_str,
        _regime.regime, _regime_mult, _regime.vol_regime,
        sizing.shares, final_qty, final_risk_usd,
        _reductions or ["none"],
    )

    # ── 2f. Circuit breaker check ─────────────────────────────────
    _spread_pct = 0.0
    if entry_price > 0:
        _half_spread = abs(intent.entry_zone_high - intent.entry_zone_low) / 2.0
        _spread_pct = _half_spread / entry_price
    _breaker = _check_breakers(
        symbol=intent.symbol,
        risk_usd=final_risk_usd,
        spread_pct=_spread_pct,
    )
    if _breaker.blocked:
        log.warning(
            "CIRCUIT_BREAKER_BLOCK  symbol=%s  reasons=%s",
            intent.symbol, _breaker.reasons,
        )
        _publish_rejected(intent, ["circuit_breaker"] + _breaker.reasons)
        return
    if _breaker.action == "REDUCE":
        reduced_qty = max(1, int(final_qty * _breaker.size_mult))
        log.info(
            "CIRCUIT_BREAKER_REDUCE  symbol=%s  qty %d→%d  mult=%.2f  reasons=%s",
            intent.symbol, final_qty, reduced_qty, _breaker.size_mult, _breaker.reasons,
        )
        final_qty = reduced_qty
        final_risk_usd = sizing.risk_per_share * final_qty

    # ── 3. Risk approval (existing module) ───────────────────────────
    try:
        from src.risk.risk_guard import (
            get_risk_state,
            approve_new_trade,
            record_trade_taken,
        )

        state = get_risk_state()
        _current_open_risk = _ort_total_risk()
        log.info(
            "risk_guard_check symbol=%s open_risk=$%.2f proposed=$%.2f "
            "cap=$%.2f positions=%d",
            intent.symbol, _current_open_risk, final_risk_usd,
            _DEFAULT_EQUITY_USD * 0.02, _ort_pos_count(),
        )
        status = approve_new_trade(
            state=state,
            equity_usd=_DEFAULT_EQUITY_USD,
            open_risk_usd=_current_open_risk,
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
            "trail_pct": _adaptive_trail_pct(_sc_bucket),
            "atr_multiplier": _DEFAULT_ATR_MULT,
            "risk_per_share": round(sizing.risk_per_share, 4),
            "total_risk": round(final_risk_usd, 2),
            # Exit intelligence metadata
            "playbook": (_alloc_c.bucket if _ALLOC_ENABLED else (intent.setup_type or "unknown")),
            "sector": _ti_sp.sector if hasattr(_ti_sp, "sector") else "",
            "industry": getattr(_ti_sp, "industry", ""),
            "regime": _regime.regime,
            "market_mode": (_mm_get_last().mode if _MM_ENABLED and _mm_get_last() else ""),
            "volatility_state": _vl.leader_state,
            "scorecard_bias": (_sc_get_card(_sc_bucket).confidence_score if _SC_ENABLED and _sc_bucket != "none" else 1.0),
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

    # Record sector fill (notional exposure) for concentration tracking
    _record_sector_fill(intent.symbol, notional=entry_price * final_qty)

    # Record industry fill for concentration tracking
    _record_industry_fill(intent.symbol, notional=entry_price * final_qty)

    # Record allocation bucket fill
    if _ALLOC_ENABLED:
        _alloc_record_fill(_alloc_c.bucket)

    # Record trade for circuit breaker tracking
    _record_breaker_trade(intent.symbol, final_risk_usd)

    # Record fill in open-risk tracker so the 2% cap sees real exposure
    _ort_record_fill(intent.symbol, final_risk_usd)

    # Record trade count for daily limit persistence
    record_trade_taken()

    # TODO: subscribe to close events (ORDER_EVENT with fill_type=CLOSE)
    # and call _ort_record_close(symbol) to free up risk budget.

    # Record scorecard trade open
    if _SC_ENABLED:
        _sc_open_bucket = _alloc_c.bucket if _ALLOC_ENABLED else (intent.setup_type or "unknown")
        _sc_mm_mode = ""
        if _MM_ENABLED:
            _sc_mm = _mm_get_last()
            if _sc_mm is not None:
                _sc_mm_mode = _sc_mm.mode
        _sc_record_open(
            intent_id=intent.intent_id,
            symbol=intent.symbol,
            playbook=_sc_open_bucket,
            sector=_ti_sp.sector if hasattr(_ti_sp, "sector") else "",
            industry=getattr(_ti_sp, "industry", ""),
            regime=_regime.regime,
            market_mode=_sc_mm_mode,
            session_state=get_us_equity_session(),
            entry_price=entry_price,
            qty=final_qty,
            risk_usd=final_risk_usd,
        )

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
    Rejects candidates that did not pass the event gate.
    """
    global _drafts

    # ── Event gate enforcement ───────────────────────────────────────
    _gate_pass = getattr(cand, "event_gate_pass", False)
    _cand_es = getattr(cand, "event_score", 0)
    _cand_strategy = getattr(cand, "strategy", "unknown")
    _cand_regime = getattr(cand, "regime", "")

    if not _gate_pass:
        log.info(
            "open_plan_blocked symbol=%s score=%d strategy=%s reason=event_gate",
            cand.symbol, _cand_es, _cand_strategy,
        )
        return

    log.info(
        "open_plan_approved symbol=%s score=%d strategy=%s",
        cand.symbol, _cand_es, _cand_strategy,
    )

    # ── Sector concentration check ───────────────────────────────────
    _cand_sector_state = getattr(cand, "sector_state", "NEUTRAL") or "NEUTRAL"
    _opc_entry = cand.suggested_entry
    # Estimate notional for exposure check (rough: entry * typical qty)
    _opc_est_notional = _opc_entry * max(1, int((_DEFAULT_EQUITY_USD * _DEFAULT_RISK_PCT) / max(0.01, _opc_entry * 0.01)))
    _slr = _check_sector_limit(cand.symbol, _cand_sector_state, proposed_notional=_opc_est_notional)
    if _slr.verdict == _SECTOR_BLOCK:
        log.info(
            "sector_limit_block symbol=%s sector=%s reason=%s active=%d drafts=%d",
            cand.symbol, _slr.sector, _slr.reason, _slr.active_count, _slr.draft_count,
        )
        return
    if _slr.verdict == _SECTOR_REDUCE:
        log.info(
            "sector_limit_reduce symbol=%s sector=%s reason=%s qty_mult=%.2f",
            cand.symbol, _slr.sector, _slr.reason, _slr.qty_mult,
        )

    # ── Industry concentration check ─────────────────────────────────
    _ilr = _check_industry_limit(cand.symbol, proposed_notional=_opc_est_notional)
    if _ilr.verdict == _SECTOR_BLOCK:
        log.info(
            "industry_limit_block symbol=%s industry=%s reason=%s active=%d",
            cand.symbol, _ilr.industry, _ilr.reason, _ilr.active_count,
        )
        return
    if _ilr.verdict == _SECTOR_REDUCE:
        log.info(
            "industry_limit_reduce symbol=%s industry=%s reason=%s qty_mult=%.2f",
            cand.symbol, _ilr.industry, _ilr.reason, _ilr.qty_mult,
        )

    # ── Phase B: Rotation / scan priority gate for OPC ───────────────
    if _ROTATION_SEL_ENABLED or _SCAN_SCHED_ENABLED:
        _opc_sp = _classify_symbol(cand.symbol)
        _opc_scan_pri = _PRI_NORMAL
        _opc_rotating_out = False
        if _SCAN_SCHED_ENABLED:
            _opc_sched = _get_scan_schedule()
            _opc_scan_pri = _opc_sched.get(cand.symbol, _PRI_NORMAL)
        if _ROTATION_SEL_ENABLED:
            _opc_rot_d = _get_rotation_decision()
            if _opc_rot_d is not None:
                _opc_rotating_out = _opc_sp.sector in _opc_rot_d.rotating_out
        if _opc_scan_pri == _PRI_LOW and _opc_rotating_out:
            log.info(
                "risk_rotation_reject symbol=%s sector=%s priority=%s rotating_out=True",
                cand.symbol, _opc_sp.sector, _opc_scan_pri,
            )
            return
        log.info(
            "risk_rotation_bias symbol=%s sector=%s industry=%s "
            "priority=%s rotating_out=%s",
            cand.symbol, _opc_sp.sector, _opc_sp.industry,
            _opc_scan_pri, _opc_rotating_out,
        )

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

    # ── Sector qty multiplier (from concentration check) ─────────────
    if _slr.qty_mult != 1.0:
        _qty_before_sector = qty
        qty = max(1, int(qty * _slr.qty_mult))
        risk_usd = risk_per_share * qty

    # Always log sector evaluation for every approved candidate
    log.info(
        "risk_sector symbol=%s sector=%s state=%s exposure=%.2f "
        "limit=%.2f mult=%.2f action=%s qty=%d",
        cand.symbol, _slr.sector, _cand_sector_state,
        _slr.active_count / max(1, _slr.max_active),
        _slr.max_active, _slr.qty_mult, _slr.verdict, qty,
    )

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
        sector=getattr(cand, "sector", ""),
        industry=getattr(cand, "industry", ""),
        sector_state=getattr(cand, "sector_state", ""),
    )

    # Record draft in sector concentration tracker
    _record_sector_draft(cand.symbol)

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
        import time as _time_mod
        _draft_store[cand.symbol] = (draft, cand)
        _draft_store_ts[cand.symbol] = _time_mod.time()
        log.info("draft_stored symbol=%s entry=%.2f qty=%d total_drafts=%d",
                 cand.symbol, draft.suggested_entry, draft.qty, len(_draft_store))

    # ── Build OrderBlueprint if we are in PREMARKET (or test-forced) ─
    session = get_us_equity_session()
    if session == PREMARKET or is_test_session_forced():
        if is_test_session_forced() and session != PREMARKET:
            log.info(
                "forced_session_override blueprint_gate session=%s forced=%s",
                session, get_test_force_session(),
            )
        _build_and_publish_blueprint(draft, cand)


# ── OrderBlueprint builder ───────────────────────────────────────────

def _build_entry_ladder(entry_price: float, bias_mult: float = 1.0) -> list:
    """Build 3-5 price levels around entry: +/-step% in even steps.

    bias_mult < 1.0 shifts the entire ladder below mid (tighter open entry).
    bias_mult > 1.0 shifts above mid (aggressive chase — avoid).
    Default 1.0 = neutral (centered on entry_price).
    """
    biased_entry = round(entry_price * bias_mult, 2)
    step = biased_entry * (_BLUEPRINT_LADDER_STEP_PCT / 100.0)
    n = _BLUEPRINT_LADDER_LEVELS
    half = n // 2
    levels = []
    for i in range(-half, n - half):
        levels.append(round(biased_entry + i * step, 2))
    return levels


def _compute_trail_pct(stop_distance_pct: float, bucket: str = "none") -> float:
    """trail_pct = geometry floor blended with scorecard avg_r nudge.
    Geometry sets the floor (stop_distance * factor).
    Scorecard avg_r_multiple widens/tightens within blueprint bounds.
    """
    geo = stop_distance_pct * _BLUEPRINT_TRAIL_FACTOR
    geo_clamped = round(max(_BLUEPRINT_TRAIL_MIN_PCT, min(_BLUEPRINT_TRAIL_MAX_PCT, geo)), 3)
    if not _SC_ENABLED or bucket == "none":
        return geo_clamped
    card = _sc_get_card(bucket)
    if card is None or getattr(card, "sample_n", 0) < 10:
        return geo_clamped
    # Blend: 70% geometry, 30% scorecard signal
    sc_trail = round(_BLUEPRINT_TRAIL_MIN_PCT + (card.avg_r_multiple - 1.0) * 1.7, 3)
    sc_trail = max(_BLUEPRINT_TRAIL_MIN_PCT, min(_BLUEPRINT_TRAIL_MAX_PCT, sc_trail))
    blended = round(0.7 * geo_clamped + 0.3 * sc_trail, 3)
    log.debug(
        "blueprint_trail bucket=%s avg_r=%.2f geo=%.3f sc=%.3f blended=%.3f",
        bucket, card.avg_r_multiple, geo_clamped, sc_trail, blended,
    )
    return blended



def fire_open_plans(top_n: int = 10) -> int:
    """Fire buffered PlanDrafts at market open. Call once at RTH_OPEN (09:30 ET).

    Ranks drafts by composite_score → confidence → total_score.
    Purges stale drafts (> TL_DRAFT_TTL_S seconds old).
    Returns count of blueprints published.
    """
    import time as _tm
    global _draft_store, _draft_store_ts
    now = _tm.time()

    # Purge stale drafts
    stale = [s for s, ts in _draft_store_ts.items() if (now - ts) > _DRAFT_TTL_S]
    for s in stale:
        _draft_store.pop(s, None)
        _draft_store_ts.pop(s, None)
    if stale:
        log.info("draft_purge_stale count=%d symbols=%s", len(stale), stale)

    if not _draft_store:
        log.info("fire_open_plans: no drafts to fire")
        return 0

    # Rank by composite_score on the original candidate, then confidence
    ranked = sorted(
        _draft_store.values(),
        key=lambda t: (
            getattr(t[1], "composite_score", 0.0),
            t[0].confidence,
            t[0].total_score,
        ),
        reverse=True,
    )[:top_n]

    import os as _os2, time as _tm2
    from datetime import datetime as _dt2
    import zoneinfo as _zi2
    _et_now = _dt2.now(tz=_zi2.ZoneInfo("America/New_York"))
    from datetime import time as _dtime2
    # Within first 90s of open: bias ladder down to avoid paying open spread
    _open_bias = 1.0
    # Bid 0.15% below suggested_entry during first 90s to avoid paying open spread
    _OPEN_ENTRY_DISCOUNT_PCT = float(
        __import__('os').environ.get("TL_OPEN_ENTRY_DISCOUNT_PCT", "0.0015")
    )
    if _dtime2(9, 30) <= _et_now.time() < _dtime2(9, 31, 30):
        _open_bias = round(1.0 - _OPEN_ENTRY_DISCOUNT_PCT, 6)  # e.g. 0.9985

    fired = 0
    for draft, cand in ranked:
        try:
            if _open_bias != 1.0:
                import copy as _copy
                draft = _copy.copy(draft)
                draft.suggested_entry = round(draft.suggested_entry * _open_bias, 2)
                draft.suggested_stop  = round(draft.suggested_stop  * _open_bias, 2)
                draft.risk_usd = round(
                    abs(draft.suggested_entry - draft.suggested_stop) * draft.qty, 2
                )
            _build_and_publish_blueprint(draft, cand)
            fired += 1
            log.info(
                "fire_open_plans_fired symbol=%s entry=%.2f qty=%d "
                "composite=%.3f conf=%.2f",
                draft.symbol, draft.suggested_entry, draft.qty,
                getattr(cand, "composite_score", 0.0), draft.confidence,
            )
        except Exception as exc:
            log.error("fire_open_plans_error symbol=%s err=%s", draft.symbol, exc)

    # Clear store after firing
    _draft_store.clear()
    _draft_store_ts.clear()
    log.info("fire_open_plans_complete fired=%d/%d", fired, len(ranked))
    return fired

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
    trail = _compute_trail_pct(draft.stop_distance_pct, bucket=getattr(draft, 'bucket', 'none'))
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
                source="open_plan",
                sector=getattr(draft, "sector", ""),
                industry=getattr(draft, "industry", ""),
                sector_state=getattr(draft, "sector_state", ""),
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
        source="open_plan",
        sector=getattr(draft, "sector", ""),
        industry=getattr(draft, "industry", ""),
        sector_state=getattr(draft, "sector_state", ""),
    )

    log.info(
        "blueprint_source symbol=%s source=open_plan sector=%s",
        bp.symbol, bp.sector,
    )
    log.info(
        "OrderBlueprint  symbol=%s  qty=%d  ladder=%s  stop=%.2f  "
        "trail=%.2f%%  timeout=%ds  max_spread=%.2f%%  risk=$%.2f  Q=%s",
        bp.symbol, bp.qty,
        [f"{p:.2f}" for p in bp.entry_ladder],
        bp.stop_price, bp.trail_pct, bp.timeout_s,
        bp.max_spread_pct, bp.risk_usd, bp.quality,
    )

    # Regime log for every blueprint
    _bp_regime = _get_regime()
    _bp_mult = _REGIME_RISK_MULT.get(_bp_regime.regime, 1.0)
    log.info(
        "risk_regime symbol=%s regime=%s mult=%.2f qty_before=%d qty_after=%d",
        bp.symbol, _bp_regime.regime, _bp_mult, qty, bp.qty,
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

        breaker_str = ""
        try:
            bs = _breaker_status()
            breaker_str = (
                f"  breakers: trades_1h={bs.get('trades_1h', 0)}"
                f"  daily_pnl=${bs.get('daily_pnl_usd', 0):.0f}"
                f"  kill={'ON' if bs.get('master_kill') else 'off'}"
            )
        except Exception:
            pass

        _sector_top_str = ""
        try:
            _sector_top_str = f"  sector_top={_get_sector_top()}"
        except Exception:
            pass

        _vol_top_str = ""
        try:
            _vol_top_str = f"  vol_leaders={_vol_leader_summary()}"
        except Exception:
            pass

        _rot_top_str = ""
        try:
            if _ROTATION_ENABLED:
                _rot_top_str = f"  rotation_exposure={_rotation_summary()}"
        except Exception:
            pass

        _alloc_hb_str = ""
        try:
            if _ALLOC_ENABLED:
                _alloc_hb_str = f"  {_alloc_summary()}"
        except Exception:
            pass

        _mm_hb_str = ""
        try:
            if _MM_ENABLED:
                _mm_hb_str = f"  {_mm_summary()}"
        except Exception:
            pass

        _sc_hb_str = ""
        try:
            if _SC_ENABLED:
                _sc_card_sum = _sc_get_card("news")
                _sc_hb_str = f"  SC[news_conf={_sc_card_sum.confidence_score:.2f}]"
        except Exception:
            pass

        _exit_hb_str = ""
        try:
            if _EXIT_ENABLED:
                _exit_hb_str = f"  EXIT[pos={_exit_pos_count()}]"
        except Exception:
            pass

        _tune_hb_str = ""
        try:
            if _TUNING_ENABLED:
                _ts = _tune_snapshot()
                _tune_hb_str = f"  TUNE[ovr={_ts.active_overrides}]"
        except Exception:
            pass

        _ort_hb_str = f"  ORT[risk=${_ort_total_risk():.0f} pos={_ort_pos_count()}]"

        log.info(
            "heartbeat  tick=%d  intents=%d  approved=%d  rejected=%d  drafts=%d  blueprints=%d%s%s%s%s%s%s%s%s%s%s%s",
            tick, recv, appr, rej, drafts, bps, heat_str, breaker_str, _sector_top_str, _vol_top_str, _rot_top_str, _alloc_hb_str, _mm_hb_str, _sc_hb_str, _exit_hb_str, _tune_hb_str, _ort_hb_str,
        )

        _stop_event.wait(settings.heartbeat_interval_s)
        if _stop_event.is_set():
            break

    # Cleanup
    if _bus is not None:
        try:
            _bus.close()
        except Exception:
            pass
    log.info("Risk arm stopped.")


if __name__ == "__main__":
    main()
