"""reentry_harvester — tracks recent exits and flags re-entry candidates."""
from __future__ import annotations
import os, time, logging
from dataclasses import dataclass
from typing import Dict

log = logging.getLogger("reentry_harvester")

REENTRY_ENABLED: bool = os.environ.get("TL_REENTRY_ENABLED", "true").lower() == "true"
_WINDOW_S:    float = float(os.environ.get("TL_REENTRY_WINDOW_S",  "300"))
_MIN_EXIT_R:  float = float(os.environ.get("TL_REENTRY_MIN_R",     "0.5"))
_BOOST:       float = float(os.environ.get("TL_REENTRY_BOOST",     "12.0"))
_MAX_WINDOWS: int   = int(os.environ.get("TL_REENTRY_MAX_WINDOWS", "20"))

@dataclass
class ReentryWindow:
    symbol:      str
    exit_ts:     float
    exit_r:      float
    exit_reason: str
    playbook:    str  = ""
    expires_ts:  float = 0.0
    triggered:   bool  = False
    trigger_count: int  = 0          # how many times boost was given
    last_trigger_ts: float = 0.0     # epoch of last trigger

_windows: Dict[str, ReentryWindow] = {}

def stamp_reentry(symbol: str, exit_r: float = 0.0,
                  exit_reason: str = "", playbook: str = "") -> None:
    if not REENTRY_ENABLED:
        return
    if exit_r < _MIN_EXIT_R:
        log.debug("reentry_skip_low_r sym=%s exit_r=%.2f", symbol, exit_r)
        return
    if len(_windows) >= _MAX_WINDOWS:
        oldest = min(_windows.values(), key=lambda w: w.exit_ts)
        _windows.pop(oldest.symbol, None)
    now = time.time()
    _windows[symbol] = ReentryWindow(
        symbol=symbol, exit_ts=now, exit_r=exit_r,
        exit_reason=exit_reason, playbook=playbook,
        expires_ts=now + _WINDOW_S,
    )
    log.info("reentry_window_open sym=%s exit_r=%.2f reason=%s window=%.0fs",
             symbol, exit_r, exit_reason, _WINDOW_S)

_INTER_BOOST_COOLDOWN_S: float = float(
    os.environ.get("TL_REENTRY_INTER_BOOST_COOLDOWN_S", "60")
)
_MAX_TRIGGERS: int = int(os.environ.get("TL_REENTRY_MAX_TRIGGERS", "3"))

def get_reentry_boost(symbol: str) -> float:
    """Returns priority boost if symbol is in active window.

    Allows up to TL_REENTRY_MAX_TRIGGERS re-triggers per window,
    with TL_REENTRY_INTER_BOOST_COOLDOWN_S seconds between each.
    Boost decays 20% per subsequent trigger to avoid over-weighting.
    """
    if not REENTRY_ENABLED:
        return 0.0
    w = _windows.get(symbol)
    if w is None:
        return 0.0
    now = time.time()
    if now > w.expires_ts:
        _windows.pop(symbol, None)
        return 0.0
    # Max triggers guard
    if w.trigger_count >= _MAX_TRIGGERS:
        return 0.0
    # Inter-boost cooldown
    if w.last_trigger_ts > 0 and (now - w.last_trigger_ts) < _INTER_BOOST_COOLDOWN_S:
        return 0.0
    # Decay boost 20% per subsequent trigger
    decayed_boost = round(_BOOST * (0.80 ** w.trigger_count), 2)
    w.triggered = True
    w.trigger_count += 1
    w.last_trigger_ts = now
    log.info(
        "reentry_boost sym=%s exit_r=%.2f boost=%.1f trigger=%d elapsed=%.0fs",
        symbol, w.exit_r, decayed_boost, w.trigger_count, now - w.exit_ts,
    )
    return decayed_boost

def is_in_window(symbol: str) -> bool:
    w = _windows.get(symbol)
    if w is None:
        return False
    if time.time() > w.expires_ts:
        _windows.pop(symbol, None)
        return False
    return w.trigger_count < _MAX_TRIGGERS

def active_windows() -> Dict[str, ReentryWindow]:
    now = time.time()
    for s in [s for s, w in _windows.items() if now > w.expires_ts]:
        _windows.pop(s)
    return dict(_windows)

def reset() -> None:
    _windows.clear()
