"""
Hyper-Swing Quant Filters — Phase 2 Signal Quality
Pure functions; no IB dependency. Operate on DataFrames / scalars.
"""

import math
import numpy as np
import pandas as pd
from typing import Optional


# ---------------------------------------------------------------------------
# VWAP
# ---------------------------------------------------------------------------

def calc_vwap(df_5m: pd.DataFrame) -> float:
    """
    Volume-Weighted Average Price from intraday 5-minute bars.
    Falls back to EMA-20 of close if volume column is missing/zero.

    Expects columns: close, high, low, volume (optional).
    """
    if df_5m is None or df_5m.empty:
        return float("nan")

    if "volume" in df_5m.columns and df_5m["volume"].sum() > 0:
        typical = (df_5m["high"] + df_5m["low"] + df_5m["close"]) / 3.0
        cum_tp_vol = (typical * df_5m["volume"]).cumsum()
        cum_vol = df_5m["volume"].cumsum()
        vwap_series = cum_tp_vol / cum_vol
        val = float(vwap_series.iloc[-1])
        if math.isfinite(val):
            return val

    # Fallback: EMA-20 of close
    ema = df_5m["close"].ewm(span=20, adjust=False).mean()
    return float(ema.iloc[-1])


# ---------------------------------------------------------------------------
# Momentum (30-minute return from 5-min bars)
# ---------------------------------------------------------------------------

def calc_momentum(df_5m: pd.DataFrame, minutes: int = 30) -> float:
    """
    Percent return over the last *minutes* of 5-minute bars.
    Returns e.g. 0.015 for +1.5%.
    """
    if df_5m is None or df_5m.empty:
        return 0.0

    bars_needed = max(1, minutes // 5)
    if len(df_5m) < bars_needed + 1:
        return 0.0

    cur = float(df_5m["close"].iloc[-1])
    prev = float(df_5m["close"].iloc[-1 - bars_needed])
    if prev <= 0:
        return 0.0
    return (cur - prev) / prev


# ---------------------------------------------------------------------------
# Volume acceleration
# ---------------------------------------------------------------------------

def calc_volume_accel(df_5m: pd.DataFrame, window: int = 3, baseline: int = 8) -> float:
    """
    Intraday burst detector using 15-minute buckets.

    recent_vol = sum(last *window* bars volume)  (default: 15m)
    baseline   = mean(volume of prior *baseline* buckets)
    Return recent / baseline  (>1 = acceleration).
    """
    if df_5m is None or df_5m.empty or "volume" not in df_5m.columns:
        return 1.0

    vol = df_5m["volume"].to_numpy(dtype=float)
    bars_per_bucket = max(1, int(window))
    baseline_buckets = max(1, int(baseline))
    total_bars_needed = bars_per_bucket * (baseline_buckets + 1)
    if len(vol) < total_bars_needed:
        return 1.0

    recent = float(vol[-bars_per_bucket:].sum())

    history = vol[-total_bars_needed:-bars_per_bucket]
    buckets = history.reshape(baseline_buckets, bars_per_bucket).sum(axis=1)
    base_mean = float(buckets.mean())
    if base_mean <= 0:
        return 1.0
    return recent / base_mean


# ---------------------------------------------------------------------------
# ATR expansion  (current ATR vs 20-day rolling mean ATR)
# ---------------------------------------------------------------------------

def calc_atr_expansion(atr14: float, atr14_20d_avg: Optional[float] = None) -> float:
    """
    atr_expansion = atr14 / atr14_20d_avg.
    If 20-day average not supplied, return 1.0 (neutral).
    """
    if atr14_20d_avg is None or atr14_20d_avg <= 0:
        return 1.0
    if atr14 <= 0:
        return 0.0
    return atr14 / atr14_20d_avg


def estimate_atr14_20d_avg(daily_df: pd.DataFrame) -> Optional[float]:
    """
    Compute the 20-day rolling mean of ATR-14 from a daily DataFrame.
    Expects columns: high, low, close.
    """
    if daily_df is None or daily_df.empty or len(daily_df) < 20:
        return None

    high = daily_df["high"]
    low = daily_df["low"]
    close = daily_df["close"]
    prev_close = close.shift(1)

    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)

    atr14 = tr.rolling(14).mean()
    avg_20 = atr14.rolling(20).mean().dropna()
    if avg_20.empty:
        return None
    return float(avg_20.iloc[-1])


# ---------------------------------------------------------------------------
# Relative strength vs SPY
# ---------------------------------------------------------------------------

def calc_relative_strength(stock_return_30m: float, spy_return_30m: float) -> float:
    """
    Relative strength delta-return over 30m.
    Positive means stock outperformed SPY over the same window.

    Example: stock +0.8% and SPY +0.3% => RS_30m = +0.5% (= 0.005).
    """
    return float(stock_return_30m) - float(spy_return_30m)


# ---------------------------------------------------------------------------
# Trend structure (higher-highs / higher-lows + above VWAP)
# ---------------------------------------------------------------------------

def calc_trend_structure(df_5m: pd.DataFrame, vwap: float) -> float:
    """
    Score 0-100 for intraday trend structure.
      +40: price above VWAP
      +30: higher-high pattern (last 3 swing highs ascending)
      +30: higher-low pattern  (last 3 swing lows ascending)
    """
    if df_5m is None or df_5m.empty:
        return 0.0

    score = 0.0
    cur_px = float(df_5m["close"].iloc[-1])

    # Above VWAP
    if math.isfinite(vwap) and cur_px > vwap:
        score += 40.0

    # Simple higher-high / higher-low over recent 5-bar chunks
    closes = df_5m["close"].values
    if len(closes) >= 15:
        chunks = np.array_split(closes[-15:], 3)
        highs = [float(c.max()) for c in chunks]
        lows = [float(c.min()) for c in chunks]

        if highs[2] > highs[1] > highs[0]:
            score += 30.0
        elif highs[2] > highs[1]:
            score += 15.0

        if lows[2] > lows[1] > lows[0]:
            score += 30.0
        elif lows[2] > lows[1]:
            score += 15.0

    return min(score, 100.0)


# ---------------------------------------------------------------------------
# Composite Quant Score  (0–100)
# ---------------------------------------------------------------------------

_COMPONENT_WEIGHTS = {
    "momentum_30m":      0.255,
    "volume_accel":      0.17,
    "rel_strength":      0.17,
    "atr_expansion":     0.1275,
    "trend_structure":   0.1275,
    "playbook_score":    0.15,
}


def _normalise(value: float, lo: float, hi: float) -> float:
    """Clamp *value* into [lo, hi] then scale to 0-100."""
    if hi <= lo:
        return 50.0
    clamped = max(lo, min(value, hi))
    return ((clamped - lo) / (hi - lo)) * 100.0


def quant_score_components(metrics: dict) -> dict:
    """
    Return normalized sub-scores plus composite score.

    Expects keys: momentum_30m, volume_accel, rel_strength_vs_spy,
                  atr_expansion, trend_structure_score, playbook_score.
    """
    mom = _normalise(metrics.get("momentum_30m", 0.0), -0.005, 0.0075)
    vol = _normalise(metrics.get("volume_accel", 1.0), 0.2, 1.7)
    rs = _normalise(metrics.get("rel_strength_vs_spy", 0.0), -0.004, 0.00475)
    atr_e = _normalise(metrics.get("atr_expansion", 1.0), 0.7, 1.3)
    trend = max(0.0, min(float(metrics.get("trend_structure_score", 0.0)), 100.0))
    playbook = max(0.0, min(float(metrics.get("playbook_score", 50.0)), 100.0))

    composite = (
        _COMPONENT_WEIGHTS["momentum_30m"] * mom
        + _COMPONENT_WEIGHTS["volume_accel"] * vol
        + _COMPONENT_WEIGHTS["rel_strength"] * rs
        + _COMPONENT_WEIGHTS["atr_expansion"] * atr_e
        + _COMPONENT_WEIGHTS["trend_structure"] * trend
        + _COMPONENT_WEIGHTS["playbook_score"] * playbook
    )

    return {
        "momentum_norm": round(mom, 1),
        "vol_norm": round(vol, 1),
        "rs_norm": round(rs, 1),
        "atr_exp_norm": round(atr_e, 1),
        "trend_norm": round(trend, 1),
        "playbook_norm": round(playbook, 1),
        "composite": round(max(0.0, min(100.0, composite)), 1),
    }


def quant_score(metrics: dict) -> float:
    """
    Combine components into a single 0-100 score.

    Expects keys: momentum_30m, volume_accel, rel_strength_vs_spy,
                  atr_expansion, trend_structure_score, playbook_score.
    """
    components = quant_score_components(metrics)
    return components["composite"]
