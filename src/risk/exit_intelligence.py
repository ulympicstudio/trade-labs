"""Exit Intelligence Engine — adaptive exit management for open positions.

Manages stops, targets, trims, trailing behaviour, and time-based exits
using playbook, market mode, regime, sector/industry state, volatility
state, and scorecard feedback.  **Stocks only — no options logic.**

Data flow
---------
1.  ``register_fill()`` is called from execution_main on PAPER_FILL / real fill.
2.  ``update_position()`` is called periodically (heartbeat) with latest price.
3.  ``compute_exit_decision()`` returns an ``ExitDecision`` per position.
4.  monitor_main calls ``get_exit_summary()`` / ``get_open_positions_snapshot()``
    for the observability table.

All thresholds are overridable via ``TL_EXIT_*`` env vars.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from src.monitoring.logger import get_logger

log = get_logger("exit_intel")

# ── Tunables ────────────────────────────────────────────────────────

EXIT_ENABLED: bool = os.environ.get(
    "TL_EXIT_ENABLED", "true"
).lower() in ("1", "true", "yes")

MONITOR_ENABLED: bool = os.environ.get(
    "TL_EXIT_MONITOR_ENABLED", "true"
).lower() in ("1", "true", "yes")

# Force-path env vars for testing
_FORCE_PLAYBOOK: str = os.environ.get("TL_EXIT_FORCE_PLAYBOOK", "").strip().lower()
_FORCE_MODE: str = os.environ.get("TL_EXIT_FORCE_MODE", "").strip().upper()
_FORCE_ACTION: str = os.environ.get("TL_EXIT_FORCE_ACTION", "").strip().upper()
_FORCE_MFE: float = float(os.environ.get("TL_EXIT_FORCE_MFE", "0"))
_FORCE_MAE: float = float(os.environ.get("TL_EXIT_FORCE_MAE", "0"))
_FORCE_PNL: float = float(os.environ.get("TL_EXIT_FORCE_PNL", "0"))

# Time-stop defaults (seconds)
_TIME_STOP_MEANREVERT_S: int = int(os.environ.get("TL_EXIT_TIME_STOP_MR_S", "1800"))   # 30 min
_TIME_STOP_DEFAULT_S: int = int(os.environ.get("TL_EXIT_TIME_STOP_DEFAULT_S", "7200"))  # 2 h
_TIME_STOP_CHOP_S: int = int(os.environ.get("TL_EXIT_TIME_STOP_CHOP_S", "2700"))       # 45 min
_TIME_STOP_DEFENSIVE_S: int = int(os.environ.get("TL_EXIT_TIME_STOP_DEFENSIVE_S", "1200"))  # 20 min

# Trail defaults
_TRAIL_DEFAULT_PCT: float = float(os.environ.get("TL_EXIT_TRAIL_DEFAULT_PCT", "1.5"))
_TRAIL_TIGHT_PCT: float = float(os.environ.get("TL_EXIT_TRAIL_TIGHT_PCT", "0.8"))
_TRAIL_LOOSE_PCT: float = float(os.environ.get("TL_EXIT_TRAIL_LOOSE_PCT", "2.5"))

# Trim thresholds (multiples of risk)
_TRIM_25_R: float = float(os.environ.get("TL_EXIT_TRIM_25_R", "2.0"))
_TRIM_50_R: float = float(os.environ.get("TL_EXIT_TRIM_50_R", "3.5"))

# Scorecard confidence thresholds
_SC_STRONG_THRESH: float = float(os.environ.get("TL_EXIT_SC_STRONG", "1.10"))
_SC_WEAK_THRESH: float = float(os.environ.get("TL_EXIT_SC_WEAK", "0.90"))


# ── Exit action enum (string constants) ─────────────────────────────

HOLD = "HOLD"
TIGHTEN_STOP = "TIGHTEN_STOP"
WIDEN_STOP = "WIDEN_STOP"
TRAIL = "TRAIL"
TRIM_25 = "TRIM_25"
TRIM_50 = "TRIM_50"
EXIT_FULL = "EXIT_FULL"

_ALL_ACTIONS = {HOLD, TIGHTEN_STOP, WIDEN_STOP, TRAIL, TRIM_25, TRIM_50, EXIT_FULL}


# ── Data structures ─────────────────────────────────────────────────

@dataclass
class PositionState:
    """Tracked state for one open position."""
    symbol: str = ""
    side: str = "LONG"              # LONG | SHORT
    entry_price: float = 0.0
    current_price: float = 0.0
    qty: int = 0
    remaining_qty: int = 0          # may decrease after trims
    unrealized_pnl: float = 0.0
    unrealized_pnl_pct: float = 0.0
    max_favorable_excursion: float = 0.0   # best PnL seen
    max_adverse_excursion: float = 0.0     # worst PnL seen
    r_multiple: float = 0.0
    risk_usd: float = 0.0
    risk_per_share: float = 0.0
    open_ts: float = 0.0
    elapsed_s: float = 0.0
    bars_in_trade: int = 0          # incremented per heartbeat
    playbook: str = ""
    sector: str = ""
    industry: str = ""
    regime: str = ""
    market_mode: str = ""
    volatility_state: str = ""
    scorecard_bias: float = 1.0     # from scorecard confidence
    stop_price: float = 0.0
    target_price: float = 0.0
    trail_pct: float = 0.0
    intent_id: str = ""
    trims_done: int = 0             # how many trims executed


@dataclass
class ExitDecision:
    """Result of compute_exit_decision() for one position."""
    symbol: str = ""
    action: str = HOLD
    stop_price: float = 0.0
    target_price: float = 0.0
    trail_pct: float = 0.0
    trail_amount: float = 0.0
    trim_pct: float = 0.0
    confidence: float = 0.0
    reason_codes: List[str] = field(default_factory=list)
    r_multiple: float = 0.0
    unrealized_pnl: float = 0.0


# ── Module state ────────────────────────────────────────────────────

_positions: Dict[str, PositionState] = {}   # symbol → PositionState
_decisions: Dict[str, ExitDecision] = {}    # symbol → last ExitDecision
_trims_total: int = 0
_exits_total: int = 0
_time_stops_total: int = 0


# ── Position lifecycle ──────────────────────────────────────────────

def register_fill(
    symbol: str,
    side: str,
    entry_price: float,
    qty: int,
    stop_price: float = 0.0,
    trail_pct: float = 0.0,
    risk_usd: float = 0.0,
    playbook: str = "",
    sector: str = "",
    industry: str = "",
    regime: str = "",
    market_mode: str = "",
    volatility_state: str = "",
    scorecard_bias: float = 1.0,
    intent_id: str = "",
) -> None:
    """Register a new filled position for exit tracking."""
    if not EXIT_ENABLED:
        return

    risk_per_share = risk_usd / max(qty, 1)
    if trail_pct <= 0:
        trail_pct = _TRAIL_DEFAULT_PCT

    # Compute initial target (2R by default)
    target_price = 0.0
    if risk_per_share > 0:
        if side == "LONG":
            target_price = round(entry_price + risk_per_share * 2.0, 2)
        else:
            target_price = round(entry_price - risk_per_share * 2.0, 2)

    pos = PositionState(
        symbol=symbol,
        side=side,
        entry_price=entry_price,
        current_price=entry_price,
        qty=qty,
        remaining_qty=qty,
        risk_usd=risk_usd,
        risk_per_share=risk_per_share,
        open_ts=time.time(),
        playbook=_FORCE_PLAYBOOK or playbook.lower(),
        sector=sector,
        industry=industry,
        regime=regime,
        market_mode=_FORCE_MODE or market_mode,
        volatility_state=volatility_state,
        scorecard_bias=scorecard_bias,
        stop_price=stop_price,
        target_price=target_price,
        trail_pct=trail_pct,
        intent_id=intent_id,
    )
    _positions[symbol] = pos
    log.info(
        "exit_register symbol=%s side=%s entry=%.2f qty=%d stop=%.2f "
        "target=%.2f trail=%.2f%% playbook=%s sector=%s mode=%s risk=$%.2f",
        symbol, side, entry_price, qty, stop_price,
        target_price, trail_pct, pos.playbook, sector,
        pos.market_mode, risk_usd,
    )


def unregister_position(symbol: str) -> None:
    """Remove a position from exit tracking (after full exit)."""
    pos = _positions.pop(symbol, None)
    _decisions.pop(symbol, None)
    # Re-entry harvester hook — stamp window if exit was profitable
    try:
        from src.signals.reentry_harvester import stamp_reentry as _stamp
        if pos is not None:
            _stamp(
                symbol=symbol,
                exit_r=getattr(pos, "r_multiple", 0.0),
                exit_reason="trailing_stop",
                playbook=getattr(pos, "playbook", ""),
            )
    except Exception:
        pass  # harvester is optional — never block exit logic


def update_position_state(
    symbol: str,
    current_price: float,
    regime: str = "",
    market_mode: str = "",
    volatility_state: str = "",
    scorecard_bias: float = 1.0,
) -> Optional[ExitDecision]:
    """Update a tracked position with latest market state and compute exit.

    Returns the ExitDecision (also stored internally), or None if
    the symbol is not tracked.
    """
    if not EXIT_ENABLED:
        return None

    pos = _positions.get(symbol)
    if pos is None:
        return None

    # ── Update price & excursions ────────────────────────────────────
    pos.current_price = current_price
    pos.elapsed_s = time.time() - pos.open_ts
    pos.bars_in_trade += 1

    if pos.side == "LONG":
        pos.unrealized_pnl = (current_price - pos.entry_price) * pos.remaining_qty
        pos.unrealized_pnl_pct = (current_price - pos.entry_price) / max(pos.entry_price, 0.01)
    else:
        pos.unrealized_pnl = (pos.entry_price - current_price) * pos.remaining_qty
        pos.unrealized_pnl_pct = (pos.entry_price - current_price) / max(pos.entry_price, 0.01)

    if pos.risk_per_share > 0:
        pos.r_multiple = pos.unrealized_pnl / (pos.risk_per_share * pos.remaining_qty)
    else:
        pos.r_multiple = 0.0

    # MFE / MAE
    if _FORCE_MFE > 0:
        pos.max_favorable_excursion = _FORCE_MFE
    else:
        if pos.unrealized_pnl > pos.max_favorable_excursion:
            pos.max_favorable_excursion = pos.unrealized_pnl
    if _FORCE_MAE > 0:
        pos.max_adverse_excursion = _FORCE_MAE
    else:
        if pos.unrealized_pnl < pos.max_adverse_excursion:
            pos.max_adverse_excursion = pos.unrealized_pnl

    if _FORCE_PNL != 0:
        pos.unrealized_pnl = _FORCE_PNL

    # ── Update environment state ─────────────────────────────────────
    if regime:
        pos.regime = regime
    if market_mode:
        pos.market_mode = _FORCE_MODE or market_mode
    if volatility_state:
        pos.volatility_state = volatility_state
    pos.scorecard_bias = scorecard_bias

    # ── Compute exit decision ────────────────────────────────────────
    decision = compute_exit_decision(pos)
    _decisions[symbol] = decision
    return decision


# ── Core exit logic ─────────────────────────────────────────────────

def compute_exit_decision(pos: PositionState) -> ExitDecision:
    """Determine the best exit action for the given position state.

    Decision layers (in priority order):
    1. Force-path override
    2. Time-stop check
    3. Playbook-specific rules
    4. Market-mode adjustments
    5. Scorecard feedback
    6. Trail / trim thresholds
    """
    global _trims_total, _exits_total, _time_stops_total

    reasons: List[str] = []
    action = HOLD
    stop_px = pos.stop_price
    target_px = pos.target_price
    trail_pct = pos.trail_pct
    trim_pct = 0.0
    confidence = 0.5

    # ── 1. Force-path override ───────────────────────────────────────
    if _FORCE_ACTION and _FORCE_ACTION in _ALL_ACTIONS:
        # Compute trim_pct for forced trims so downstream sees correct qty
        if _FORCE_ACTION == TRIM_25:
            trim_pct = 0.25
            _trims_total += 1
            pos.trims_done += 1
            log.info(
                "exit_trim symbol=%s action=%s trim_pct=%.0f%% R=%.2f pnl=%.2f reasons=%s",
                pos.symbol, _FORCE_ACTION, trim_pct * 100, pos.r_multiple,
                pos.unrealized_pnl, ["force_action"],
            )
        elif _FORCE_ACTION == TRIM_50:
            trim_pct = 0.50
            _trims_total += 1
            pos.trims_done += 1
            log.info(
                "exit_trim symbol=%s action=%s trim_pct=%.0f%% R=%.2f pnl=%.2f reasons=%s",
                pos.symbol, _FORCE_ACTION, trim_pct * 100, pos.r_multiple,
                pos.unrealized_pnl, ["force_action"],
            )
        elif _FORCE_ACTION == EXIT_FULL:
            _exits_total += 1
            log.info(
                "exit_full symbol=%s R=%.2f pnl=%.2f elapsed=%ds reasons=%s",
                pos.symbol, pos.r_multiple, pos.unrealized_pnl,
                int(pos.elapsed_s), ["force_action"],
            )
        decision = ExitDecision(
            symbol=pos.symbol,
            action=_FORCE_ACTION,
            stop_price=stop_px,
            target_price=target_px,
            trail_pct=trail_pct,
            trim_pct=trim_pct,
            confidence=0.99,
            reason_codes=["force_action"],
            r_multiple=pos.r_multiple,
            unrealized_pnl=pos.unrealized_pnl,
        )
        _log_decision(pos, decision)
        return decision

    # ── 2. Time-stop check ───────────────────────────────────────────
    time_limit_s = _get_time_limit(pos)
    if pos.elapsed_s >= time_limit_s and pos.unrealized_pnl <= 0:
        action = EXIT_FULL
        reasons.append(f"time_stop({int(pos.elapsed_s)}s>={time_limit_s}s)")
        confidence = 0.85
        _time_stops_total += 1
        log.info(
            "exit_time_stop symbol=%s elapsed=%ds limit=%ds pnl=%.2f action=%s",
            pos.symbol, int(pos.elapsed_s), time_limit_s,
            pos.unrealized_pnl, action,
        )
        decision = ExitDecision(
            symbol=pos.symbol, action=action,
            stop_price=stop_px, target_price=target_px,
            trail_pct=trail_pct, confidence=confidence,
            reason_codes=reasons, r_multiple=pos.r_multiple,
            unrealized_pnl=pos.unrealized_pnl,
        )
        _log_decision(pos, decision)
        return decision

    # ── 3. Playbook-specific rules ───────────────────────────────────
    pb = pos.playbook
    if pb in ("news", "breakout"):
        action, stop_px, trail_pct, trim_pct, reasons = _rules_news_breakout(pos, reasons)
    elif pb == "rotation":
        action, stop_px, trail_pct, trim_pct, reasons = _rules_rotation(pos, reasons)
    elif pb == "volatility":
        action, stop_px, trail_pct, trim_pct, reasons = _rules_volatility(pos, reasons)
    elif pb == "meanrevert":
        action, stop_px, trail_pct, trim_pct, reasons = _rules_meanrevert(pos, reasons)
    else:
        # Generic / consensus / unknown
        action, stop_px, trail_pct, trim_pct, reasons = _rules_generic(pos, reasons)

    # ── 4. Market-mode adjustments ───────────────────────────────────
    action, stop_px, trail_pct, reasons = _apply_mode_adjustment(
        pos, action, stop_px, trail_pct, reasons,
    )

    # ── 5. Scorecard feedback ────────────────────────────────────────
    action, stop_px, trail_pct, reasons = _apply_scorecard_adjustment(
        pos, action, stop_px, trail_pct, reasons,
    )

    # ── 6. Trim / full exit thresholds from R-multiple ───────────────
    if action == HOLD and pos.r_multiple >= _TRIM_50_R and pos.trims_done < 2:
        action = TRIM_50
        trim_pct = 0.50
        reasons.append(f"r_mult_trim50(R={pos.r_multiple:.1f}>={_TRIM_50_R})")
    elif action == HOLD and pos.r_multiple >= _TRIM_25_R and pos.trims_done < 1:
        action = TRIM_25
        trim_pct = 0.25
        reasons.append(f"r_mult_trim25(R={pos.r_multiple:.1f}>={_TRIM_25_R})")

    # Confidence from R-multiple + environment
    confidence = _compute_confidence(pos, action, reasons)

    # Record trims / exits
    if action in (TRIM_25, TRIM_50):
        _trims_total += 1
        pos.trims_done += 1
        log.info(
            "exit_trim symbol=%s action=%s trim_pct=%.0f%% R=%.2f pnl=%.2f reasons=%s",
            pos.symbol, action, trim_pct * 100, pos.r_multiple,
            pos.unrealized_pnl, reasons,
        )
    elif action == EXIT_FULL:
        _exits_total += 1
        log.info(
            "exit_full symbol=%s R=%.2f pnl=%.2f elapsed=%ds reasons=%s",
            pos.symbol, pos.r_multiple, pos.unrealized_pnl,
            int(pos.elapsed_s), reasons,
        )

    decision = ExitDecision(
        symbol=pos.symbol,
        action=action,
        stop_price=round(stop_px, 2),
        target_price=round(target_px, 2),
        trail_pct=round(trail_pct, 3),
        trail_amount=round(pos.current_price * trail_pct / 100.0, 2) if trail_pct > 0 else 0.0,
        trim_pct=trim_pct,
        confidence=round(confidence, 3),
        reason_codes=reasons,
        r_multiple=round(pos.r_multiple, 3),
        unrealized_pnl=round(pos.unrealized_pnl, 2),
    )
    _log_decision(pos, decision)
    return decision


# ── Playbook rule sets ──────────────────────────────────────────────

def _rules_news_breakout(
    pos: PositionState, reasons: List[str],
) -> Tuple[str, float, float, float, List[str]]:
    """News / breakout: let runners run if supportive, tighten if fading."""
    action = HOLD
    stop_px = pos.stop_price
    trail_pct = pos.trail_pct
    trim_pct = 0.0

    supportive = (
        pos.market_mode in ("TREND_EXPANSION", "ROTATION_TAPE")
        and pos.scorecard_bias >= _SC_STRONG_THRESH
    )

    if pos.r_multiple >= 1.5 and supportive:
        # Let it run with a trailing stop
        action = TRAIL
        trail_pct = _TRAIL_LOOSE_PCT
        reasons.append(f"news_runner(R={pos.r_multiple:.1f},mode={pos.market_mode})")
    elif pos.r_multiple >= 1.0:
        # Tighten stop to breakeven + buffer
        action = TIGHTEN_STOP
        if pos.side == "LONG":
            stop_px = max(stop_px, pos.entry_price + pos.risk_per_share * 0.25)
        else:
            stop_px = min(stop_px, pos.entry_price - pos.risk_per_share * 0.25)
        trail_pct = _TRAIL_DEFAULT_PCT
        reasons.append("news_tighten_breakeven")
    elif pos.r_multiple <= -0.75:
        # Momentum fading — tighten hard
        action = TIGHTEN_STOP
        trail_pct = _TRAIL_TIGHT_PCT
        reasons.append(f"news_momentum_fade(R={pos.r_multiple:.1f})")

    return action, stop_px, trail_pct, trim_pct, reasons


def _rules_rotation(
    pos: PositionState, reasons: List[str],
) -> Tuple[str, float, float, float, List[str]]:
    """Rotation: hold while industry leading, tighten if weakening."""
    action = HOLD
    stop_px = pos.stop_price
    trail_pct = pos.trail_pct
    trim_pct = 0.0

    # Industry rotation state lookup
    try:
        from src.signals.industry_rotation import compute_industry_rotation
        rot = compute_industry_rotation(pos.symbol)
        rot_state = rot.rotation_state
    except Exception:
        rot_state = "NEUTRAL"

    if rot_state in ("LEADING", "ROTATING_IN"):
        # Stay in — trail loosely
        if pos.r_multiple >= 1.0:
            action = TRAIL
            trail_pct = _TRAIL_LOOSE_PCT
            reasons.append(f"rotation_hold_leader(state={rot_state},R={pos.r_multiple:.1f})")
    elif rot_state in ("ROTATING_OUT", "LAGGING"):
        # Leadership weakening — tighten or trim
        if pos.r_multiple >= 1.5:
            action = TRIM_25
            trim_pct = 0.25
            reasons.append(f"rotation_weakening_trim(state={rot_state})")
        else:
            action = TIGHTEN_STOP
            trail_pct = _TRAIL_TIGHT_PCT
            reasons.append(f"rotation_weakening_tighten(state={rot_state})")

    return action, stop_px, trail_pct, trim_pct, reasons


def _rules_volatility(
    pos: PositionState, reasons: List[str],
) -> Tuple[str, float, float, float, List[str]]:
    """Volatility: wider initial stop, aggressive trail after extension."""
    action = HOLD
    stop_px = pos.stop_price
    trail_pct = pos.trail_pct
    trim_pct = 0.0

    if pos.volatility_state == "TRIGGERED":
        # Wider stop initially
        if pos.bars_in_trade <= 3:
            action = WIDEN_STOP
            if pos.side == "LONG":
                stop_px = min(stop_px, pos.entry_price - pos.risk_per_share * 1.5)
            else:
                stop_px = max(stop_px, pos.entry_price + pos.risk_per_share * 1.5)
            reasons.append("vol_triggered_wide_stop")
        elif pos.r_multiple >= 2.0:
            # Move extension — aggressive trail
            action = TRAIL
            trail_pct = _TRAIL_TIGHT_PCT
            reasons.append(f"vol_extension_trail(R={pos.r_multiple:.1f})")
        elif pos.r_multiple >= 1.0:
            action = TIGHTEN_STOP
            if pos.side == "LONG":
                stop_px = max(stop_px, pos.entry_price)
            else:
                stop_px = min(stop_px, pos.entry_price)
            reasons.append("vol_breakeven_lock")
    else:
        # Normal vol — standard trail after 1R
        if pos.r_multiple >= 1.0:
            action = TRAIL
            trail_pct = _TRAIL_DEFAULT_PCT
            reasons.append("vol_standard_trail")

    return action, stop_px, trail_pct, trim_pct, reasons


def _rules_meanrevert(
    pos: PositionState, reasons: List[str],
) -> Tuple[str, float, float, float, List[str]]:
    """Mean-revert: faster take-profit, tighter time-stop."""
    action = HOLD
    stop_px = pos.stop_price
    trail_pct = _TRAIL_TIGHT_PCT  # always tight for mean-revert
    trim_pct = 0.0

    # Quick take-profit at 1R
    if pos.r_multiple >= 1.0:
        action = TRIM_50
        trim_pct = 0.50
        reasons.append(f"mr_quick_profit(R={pos.r_multiple:.1f})")
    elif pos.r_multiple >= 0.5:
        action = TIGHTEN_STOP
        if pos.side == "LONG":
            stop_px = max(stop_px, pos.entry_price)
        else:
            stop_px = min(stop_px, pos.entry_price)
        reasons.append("mr_breakeven_lock")
    elif pos.r_multiple <= -0.5:
        # Mean-revert failed fast — cut
        action = EXIT_FULL
        reasons.append(f"mr_failed(R={pos.r_multiple:.1f})")

    return action, stop_px, trail_pct, trim_pct, reasons


def _rules_generic(
    pos: PositionState, reasons: List[str],
) -> Tuple[str, float, float, float, List[str]]:
    """Generic / consensus / unknown playbook: standard trail + trim."""
    action = HOLD
    stop_px = pos.stop_price
    trail_pct = pos.trail_pct
    trim_pct = 0.0

    if pos.r_multiple >= 1.5:
        action = TRAIL
        trail_pct = _TRAIL_DEFAULT_PCT
        reasons.append(f"generic_trail(R={pos.r_multiple:.1f})")
    elif pos.r_multiple >= 1.0:
        action = TIGHTEN_STOP
        if pos.side == "LONG":
            stop_px = max(stop_px, pos.entry_price)
        else:
            stop_px = min(stop_px, pos.entry_price)
        reasons.append("generic_breakeven")

    return action, stop_px, trail_pct, trim_pct, reasons


# ── Mode adjustments ────────────────────────────────────────────────

def _apply_mode_adjustment(
    pos: PositionState,
    action: str,
    stop_px: float,
    trail_pct: float,
    reasons: List[str],
) -> Tuple[str, float, float, List[str]]:
    """Adjust exit parameters based on market mode."""
    mode = pos.market_mode

    if mode == "TREND_EXPANSION":
        # Looser trails, let winners run
        if action == TRAIL or action == HOLD:
            trail_pct = max(trail_pct, _TRAIL_LOOSE_PCT)
            reasons.append("mode_trend_loose_trail")
    elif mode == "ROTATION_TAPE":
        # Prioritise leaders — no change for leaders, tighten laggards
        pass  # rotation rules already handle this
    elif mode == "VOLATILITY_SHOCK":
        # Faster trim, tighter post-expansion trail
        if pos.r_multiple >= 1.5 and action in (HOLD, TRAIL):
            action = TRIM_25
            reasons.append("mode_vol_shock_fast_trim")
        if trail_pct > _TRAIL_TIGHT_PCT:
            trail_pct = _TRAIL_TIGHT_PCT
            reasons.append("mode_vol_shock_tight_trail")
    elif mode == "CHOP_RANGE":
        # Quick targets, tighter stops
        if pos.r_multiple >= 1.0 and action == HOLD:
            action = TRIM_25
            reasons.append("mode_chop_quick_exit")
        trail_pct = min(trail_pct, _TRAIL_TIGHT_PCT)
    elif mode == "DEFENSIVE_RISK_OFF":
        # Reduce hold time, faster exits
        if pos.elapsed_s > _TIME_STOP_DEFENSIVE_S and action == HOLD:
            action = EXIT_FULL
            reasons.append(f"mode_defensive_time({int(pos.elapsed_s)}s)")
        trail_pct = min(trail_pct, _TRAIL_TIGHT_PCT)

    if mode and mode != pos.market_mode:
        log.info(
            "exit_mode_adjustment symbol=%s mode=%s action=%s trail=%.2f%%",
            pos.symbol, mode, action, trail_pct,
        )
    return action, stop_px, trail_pct, reasons


# ── Scorecard adjustments ───────────────────────────────────────────

def _apply_scorecard_adjustment(
    pos: PositionState,
    action: str,
    stop_px: float,
    trail_pct: float,
    reasons: List[str],
) -> Tuple[str, float, float, List[str]]:
    """Adjust exit parameters based on scorecard confidence."""
    sc = pos.scorecard_bias

    if sc >= _SC_STRONG_THRESH:
        # Strong playbook → slightly looser hold policy
        if action in (TIGHTEN_STOP,) and pos.r_multiple > 0:
            trail_pct = max(trail_pct, _TRAIL_DEFAULT_PCT)
            reasons.append(f"sc_strong_loosen(bias={sc:.2f})")
    elif sc <= _SC_WEAK_THRESH:
        # Weak playbook → tighter stops, earlier exits
        if action == HOLD and pos.r_multiple >= 0.75:
            action = TIGHTEN_STOP
            trail_pct = min(trail_pct, _TRAIL_TIGHT_PCT)
            reasons.append(f"sc_weak_tighten(bias={sc:.2f})")
        elif action == HOLD and pos.r_multiple <= -0.5:
            action = EXIT_FULL
            reasons.append(f"sc_weak_exit(bias={sc:.2f},R={pos.r_multiple:.1f})")

    if sc != 1.0:
        log.info(
            "exit_scorecard_adjustment symbol=%s sc_bias=%.2f action=%s trail=%.2f%%",
            pos.symbol, sc, action, trail_pct,
        )
    return action, stop_px, trail_pct, reasons


# ── Helpers ─────────────────────────────────────────────────────────

def _get_time_limit(pos: PositionState) -> int:
    """Return time-stop in seconds based on playbook + mode."""
    pb = pos.playbook
    mode = pos.market_mode

    if pb == "meanrevert":
        base = _TIME_STOP_MEANREVERT_S
    elif mode == "CHOP_RANGE":
        base = _TIME_STOP_CHOP_S
    elif mode == "DEFENSIVE_RISK_OFF":
        base = _TIME_STOP_DEFENSIVE_S
    else:
        base = _TIME_STOP_DEFAULT_S

    # Extend for strong scorecard
    if pos.scorecard_bias >= _SC_STRONG_THRESH:
        base = int(base * 1.25)

    return base


def _compute_confidence(
    pos: PositionState, action: str, reasons: List[str],
) -> float:
    """Derive confidence for the exit decision."""
    conf = 0.5

    # Higher confidence for clear actions
    if action == EXIT_FULL:
        conf = 0.90
    elif action in (TRIM_25, TRIM_50):
        conf = 0.75
    elif action in (TRAIL, TIGHTEN_STOP):
        conf = 0.65
    elif action == WIDEN_STOP:
        conf = 0.55

    # Boost if R-multiple strongly supports the action
    if pos.r_multiple >= 2.0 and action in (TRAIL, TRIM_25, TRIM_50):
        conf = min(conf + 0.10, 0.99)
    elif pos.r_multiple <= -1.0 and action == EXIT_FULL:
        conf = min(conf + 0.10, 0.99)

    return round(conf, 3)


def _log_decision(pos: PositionState, dec: ExitDecision) -> None:
    """Log the exit decision."""
    log.info(
        "exit_decision symbol=%s action=%s stop=%.2f target=%.2f "
        "trail=%.2f%% trim=%.0f%% conf=%.3f R=%.2f pnl=%.2f "
        "mfe=%.2f mae=%.2f elapsed=%ds playbook=%s mode=%s "
        "sc_bias=%.2f reasons=%s",
        dec.symbol, dec.action, dec.stop_price, dec.target_price,
        dec.trail_pct, dec.trim_pct * 100, dec.confidence,
        dec.r_multiple, dec.unrealized_pnl,
        pos.max_favorable_excursion, pos.max_adverse_excursion,
        int(pos.elapsed_s), pos.playbook, pos.market_mode,
        pos.scorecard_bias, dec.reason_codes,
    )


# ── Public query API ────────────────────────────────────────────────

def get_open_positions_snapshot() -> List[Dict]:
    """Return a list of dicts for all open tracked positions."""
    result = []
    for sym, pos in sorted(_positions.items()):
        dec = _decisions.get(sym)
        result.append({
            "symbol": sym,
            "side": pos.side,
            "entry": pos.entry_price,
            "current": pos.current_price,
            "qty": pos.remaining_qty,
            "pnl": round(pos.unrealized_pnl, 2),
            "pnl_pct": round(pos.unrealized_pnl_pct * 100, 2),
            "r_mult": round(pos.r_multiple, 2),
            "mfe": round(pos.max_favorable_excursion, 2),
            "mae": round(pos.max_adverse_excursion, 2),
            "elapsed_s": int(pos.elapsed_s),
            "playbook": pos.playbook,
            "sector": pos.sector,
            "mode": pos.market_mode,
            "stop": pos.stop_price,
            "target": pos.target_price,
            "trail_pct": pos.trail_pct,
            "action": dec.action if dec else HOLD,
            "confidence": dec.confidence if dec else 0.0,
            "reasons": dec.reason_codes if dec else [],
            "trims_done": pos.trims_done,
        })
    return result


def get_exit_summary() -> Dict:
    """Return a summary dict for monitoring."""
    positions = get_open_positions_snapshot()
    total_pnl = sum(p["pnl"] for p in positions)
    runners = [p for p in positions if p["r_mult"] >= 1.5]
    weakest = [p for p in positions if p["r_mult"] <= -0.5]
    near_exit = [p for p in positions if p["action"] in (EXIT_FULL, TRIM_25, TRIM_50)]

    return {
        "open_count": len(positions),
        "total_unrealized_pnl": round(total_pnl, 2),
        "avg_r_mult": round(
            sum(p["r_mult"] for p in positions) / max(len(positions), 1), 2
        ),
        "runners": len(runners),
        "weakest": len(weakest),
        "near_exit": len(near_exit),
        "trims_total": _trims_total,
        "exits_total": _exits_total,
        "time_stops_total": _time_stops_total,
        "positions": positions,
        "runner_symbols": [p["symbol"] for p in runners],
        "weak_symbols": [p["symbol"] for p in weakest],
        "exit_watchlist": [p["symbol"] for p in near_exit],
    }


def get_decision(symbol: str) -> Optional[ExitDecision]:
    """Return the last ExitDecision for a tracked symbol, or None."""
    return _decisions.get(symbol)


def get_position_count() -> int:
    """Return count of tracked open positions."""
    return len(_positions)
