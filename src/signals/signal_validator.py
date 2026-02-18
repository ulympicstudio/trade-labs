"""
Signal Validator — Phase 2
Bridges IB market data to quant filters.
Fetches intraday/daily bars, computes candidate metrics, enforces hyper-swing gates.

All IB requests use aggressive caching to stay within pacing limits.
"""

import math
import time
import os
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple, List

import pandas as pd
from ib_insync import IB, Stock, util

from src.quant.hyper_swing_filters import (
    calc_vwap,
    calc_momentum,
    calc_volume_accel,
    calc_atr_expansion,
    calc_relative_strength,
    calc_trend_structure,
    estimate_atr14_20d_avg,
    quant_score,
)
from src.patterns.playbook_miner import (
    PlaybookConfig,
    compute_playbook_score,
    get_playbook_stats_cached,
)


# ── cache store ──────────────────────────────────────────────────────────────
_cache: Dict[str, Tuple[float, object]] = {}   # key → (ts, value)

# Cache TTLs (seconds)
_TTL_5M = 60       # 5-min bars: refresh every 60s
_TTL_DAILY = 600   # daily bars:  10 min
_TTL_SPY = 60      # SPY 5-min


def _get_cached(key: str, ttl: float):
    """Return cached value or None if stale / missing."""
    entry = _cache.get(key)
    if entry is None:
        return None
    ts, val = entry
    if time.time() - ts > ttl:
        return None
    return val


def _put_cache(key: str, val):
    _cache[key] = (time.time(), val)


# ── IB data helpers ──────────────────────────────────────────────────────────

def _contract(symbol: str) -> Stock:
    return Stock(symbol, "SMART", "USD")


def fetch_intraday_5m(ib: IB, symbol: str) -> Optional[pd.DataFrame]:
    """Fetch 1-day of 5-minute bars.  Cached for 60 s."""
    key = f"5m:{symbol}"
    cached = _get_cached(key, _TTL_5M)
    if cached is not None:
        return cached

    try:
        c = _contract(symbol)
        ib.qualifyContracts(c)
        bars = ib.reqHistoricalData(
            c,
            endDateTime="",
            durationStr="1 D",
            barSizeSetting="5 mins",
            whatToShow="TRADES",
            useRTH=False,
            formatDate=1,
        )
        df = util.df(bars)
        if df is not None and not df.empty:
            _put_cache(key, df)
            return df
    except Exception:
        pass
    return None


def fetch_spy_5m(ib: IB) -> Optional[pd.DataFrame]:
    """Fetch SPY 5-min bars.  Cached for 60 s."""
    return fetch_intraday_5m(ib, "SPY")


def fetch_daily_30d(ib: IB, symbol: str) -> Optional[pd.DataFrame]:
    """Fetch 30 calendar days of daily bars.  Cached 10 min."""
    key = f"daily:{symbol}"
    cached = _get_cached(key, _TTL_DAILY)
    if cached is not None:
        return cached

    try:
        c = _contract(symbol)
        ib.qualifyContracts(c)
        bars = ib.reqHistoricalData(
            c,
            endDateTime="",
            durationStr="30 D",
            barSizeSetting="1 day",
            whatToShow="TRADES",
            useRTH=True,
            formatDate=1,
        )
        df = util.df(bars)
        if df is not None and not df.empty:
            _put_cache(key, df)
            return df
    except Exception:
        pass
    return None


# ── ATR helper ─────────────────────────────────────────────────────────────

def _atr14_from_daily(df: pd.DataFrame) -> float:
    high = df["high"]
    low = df["low"]
    close = df["close"]
    prev = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev).abs(),
        (low - prev).abs(),
    ], axis=1).max(axis=1)
    atr = tr.rolling(14).mean().dropna()
    if atr.empty:
        return 0.0
    return float(atr.iloc[-1])


def _adv20_dollars(df_daily: pd.DataFrame) -> float:
    """Average daily dollar volume over most recent 20 trading days."""
    if df_daily is None or df_daily.empty:
        return 0.0
    if "volume" not in df_daily.columns or "close" not in df_daily.columns:
        return 0.0
    dv = df_daily["close"] * df_daily["volume"]
    last20 = dv.tail(20)
    if last20.empty:
        return 0.0
    return float(last20.mean())


# ── Main metrics computation ───────────────────────────────────────────────

@dataclass
class CandidateMetrics:
    """All quant metrics for one candidate symbol."""
    symbol: str = ""
    price: float = 0.0
    atr14: float = 0.0
    atr_percent: float = 0.0
    atr14_20d_avg: float = 0.0
    atr_expansion: float = 1.0
    adv20_dollars: float = 0.0
    momentum_30m: float = 0.0
    volume_accel: float = 1.0
    rel_strength_vs_spy: float = 0.0
    vwap: float = 0.0
    price_above_vwap: bool = False
    trend_structure_score: float = 0.0
    playbook_win_rate_5d: float = 0.0
    playbook_expectancy_5d: float = 0.0
    playbook_mae_5d: float = 0.0
    playbook_sample_size_5d: int = 0
    playbook_score: float = 50.0
    quant_score: float = 0.0
    ok: bool = True
    error: str = ""


def compute_candidate_metrics(
    ib: IB,
    symbol: str,
    spy_mom_30m: Optional[float] = None,
) -> CandidateMetrics:
    """
    Compute all quant metrics for *symbol*.

    If *spy_mom_30m* is provided it will be reused; otherwise SPY bars are fetched.
    """
    m = CandidateMetrics(symbol=symbol)

    # ---------- daily bars → ATR, ADV ----------
    df_d = fetch_daily_30d(ib, symbol)
    if df_d is None or df_d.empty or len(df_d) < 15:
        m.ok = False
        m.error = "insufficient daily data"
        return m

    m.atr14 = _atr14_from_daily(df_d)
    m.price = float(df_d["close"].iloc[-1])

    if m.price > 0 and m.atr14 > 0:
        m.atr_percent = m.atr14 / m.price
    m.adv20_dollars = _adv20_dollars(df_d)

    atr_avg = estimate_atr14_20d_avg(df_d)
    m.atr14_20d_avg = atr_avg if atr_avg else m.atr14
    m.atr_expansion = calc_atr_expansion(m.atr14, atr_avg)

    # ---------- playbook mining (cached daily per symbol) ----------
    try:
        playbook_stats = get_playbook_stats_cached(
            symbol=symbol,
            df_daily=df_d,
            cfg=PlaybookConfig(
                name="support_bounce_5d",
                method=(os.getenv("TRADE_LABS_PLAYBOOK_METHOD") or "quantile").lower(),
                horizon_days=5,
            ),
        )
        m.playbook_win_rate_5d = float(playbook_stats.get("win_rate", 0.0) or 0.0)
        m.playbook_expectancy_5d = float(playbook_stats.get("expectancy", 0.0) or 0.0)
        m.playbook_mae_5d = float((playbook_stats.get("mae_percentiles") or {}).get("p50", 0.0) or 0.0)
        m.playbook_sample_size_5d = int(playbook_stats.get("sample_size", 0) or 0)
        m.playbook_score = compute_playbook_score(playbook_stats)
    except Exception:
        m.playbook_win_rate_5d = 0.0
        m.playbook_expectancy_5d = 0.0
        m.playbook_mae_5d = 0.0
        m.playbook_sample_size_5d = 0
        m.playbook_score = 50.0

    # ---------- intraday 5-min → momentum, volume, VWAP, trend ----------
    df_5 = fetch_intraday_5m(ib, symbol)
    if df_5 is not None and not df_5.empty:
        m.momentum_30m = calc_momentum(df_5, minutes=30)
        m.volume_accel = calc_volume_accel(df_5, window=3, baseline=8)
        m.vwap = calc_vwap(df_5)
        m.price_above_vwap = m.price > m.vwap if math.isfinite(m.vwap) else False
        m.trend_structure_score = calc_trend_structure(df_5, m.vwap)
    else:
        m.momentum_30m = 0.0
        m.volume_accel = 1.0
        m.vwap = float("nan")
        m.trend_structure_score = 0.0

    # ---------- relative strength vs SPY ----------
    if spy_mom_30m is None:
        spy_df = fetch_spy_5m(ib)
        spy_mom_30m = calc_momentum(spy_df, 30) if spy_df is not None else 0.0

    m.rel_strength_vs_spy = calc_relative_strength(m.momentum_30m, spy_mom_30m)

    # ---------- composite quant score ----------
    m.quant_score = quant_score({
        "momentum_30m":         m.momentum_30m,
        "volume_accel":         m.volume_accel,
        "rel_strength_vs_spy":  m.rel_strength_vs_spy,
        "atr_expansion":        m.atr_expansion,
        "trend_structure_score": m.trend_structure_score,
        "playbook_score":       m.playbook_score,
    })

    return m


# ── Hyper-swing gate ───────────────────────────────────────────────────────

# Default thresholds (overridable via config dict)
_DEFAULTS = dict(
    PRICE_MIN=2.0,
    PRICE_MAX=250.0,
    MIN_ATR_PCT=0.008,
    MIN_ADV20_DOLLARS=25_000_000,
    MIN_VOLUME_ACCEL=1.3,
    MIN_RS_VS_SPY=0.0025,
    TIER2_MIN_VOLUME_ACCEL=1.15,
    TIER2_MAX_VOLUME_ACCEL=1.30,
    TIER2_MIN_RS_VS_SPY=0.0045,
    TIER2_MIN_MOMENTUM_30M=0.004,
    TIER2_MIN_TREND_STRUCTURE=75.0,
    REQUIRE_ABOVE_VWAP=True,
    PRICE_MAX_ALLOWLIST={"NVDA", "META", "AAPL", "TSLA", "PLTR", "PANW", "MSFT", "AMZN"},
)


def passes_hyper_swing_filters(
    metrics: CandidateMetrics,
    config: Optional[dict] = None,
) -> Tuple[bool, str]:
    """
    Enforce hyper-swing gates on *metrics*.

    Returns (pass: bool, reason: str).
    """
    cfg = {**_DEFAULTS, **(config or {})}

    if metrics.price < cfg["PRICE_MIN"]:
        return False, f"[HYPER_REJECT] price ${metrics.price:.2f} < min ${cfg['PRICE_MIN']}"

    if metrics.price > cfg["PRICE_MAX"] and metrics.symbol not in cfg["PRICE_MAX_ALLOWLIST"]:
        return False, f"[HYPER_REJECT] price ${metrics.price:.2f} > max ${cfg['PRICE_MAX']}"

    if metrics.atr_percent < cfg["MIN_ATR_PCT"]:
        return False, f"[HYPER_REJECT] atr% {metrics.atr_percent:.3f} < {cfg['MIN_ATR_PCT']}"

    if metrics.adv20_dollars < cfg["MIN_ADV20_DOLLARS"]:
        return False, f"[HYPER_REJECT] adv20 ${metrics.adv20_dollars/1e6:.1f}M < ${cfg['MIN_ADV20_DOLLARS']/1e6:.0f}M"

    if cfg["REQUIRE_ABOVE_VWAP"] and not metrics.price_above_vwap:
        return False, "[HYPER_REJECT] price below VWAP"

    # Tier 1 (standard)
    if metrics.volume_accel >= cfg["MIN_VOLUME_ACCEL"] and metrics.rel_strength_vs_spy >= cfg["MIN_RS_VS_SPY"]:
        return True, (
            f"[HYPER_PASS:TIER1] {metrics.symbol}: "
            f"vol_accel={metrics.volume_accel:.2f}>={cfg['MIN_VOLUME_ACCEL']:.2f} "
            f"RS_30m={metrics.rel_strength_vs_spy*100:+.2f}%>={cfg['MIN_RS_VS_SPY']*100:+.2f}% "
            f"mom30={metrics.momentum_30m*100:+.2f}% trend={metrics.trend_structure_score:.0f} "
            f"atr%={metrics.atr_percent*100:.2f}% adv20=${metrics.adv20_dollars/1e6:.1f}M"
        )

    # Tier 2 (relaxed volume, stricter confirmation)
    tier2_band = cfg["TIER2_MIN_VOLUME_ACCEL"] <= metrics.volume_accel < cfg["TIER2_MAX_VOLUME_ACCEL"]
    tier2_rs = metrics.rel_strength_vs_spy >= cfg["TIER2_MIN_RS_VS_SPY"]
    tier2_mom = metrics.momentum_30m >= cfg["TIER2_MIN_MOMENTUM_30M"]
    tier2_trend = metrics.trend_structure_score >= cfg["TIER2_MIN_TREND_STRUCTURE"]
    if tier2_band and tier2_rs and tier2_mom and tier2_trend:
        return True, (
            f"[HYPER_PASS:TIER2] {metrics.symbol}: "
            f"vol_accel={metrics.volume_accel:.2f} in [{cfg['TIER2_MIN_VOLUME_ACCEL']:.2f},{cfg['TIER2_MAX_VOLUME_ACCEL']:.2f}) "
            f"RS_30m={metrics.rel_strength_vs_spy*100:+.2f}%>={cfg['TIER2_MIN_RS_VS_SPY']*100:+.2f}% "
            f"mom30={metrics.momentum_30m*100:+.2f}%>={cfg['TIER2_MIN_MOMENTUM_30M']*100:+.2f}% "
            f"trend={metrics.trend_structure_score:.0f}>={cfg['TIER2_MIN_TREND_STRUCTURE']:.0f} "
            f"atr%={metrics.atr_percent*100:.2f}% adv20=${metrics.adv20_dollars/1e6:.1f}M"
        )

    reject_reasons: List[str] = []
    if metrics.volume_accel < cfg["TIER2_MIN_VOLUME_ACCEL"]:
        reject_reasons.append(
            f"vol_accel {metrics.volume_accel:.2f} < {cfg['TIER2_MIN_VOLUME_ACCEL']:.2f}"
        )
    elif metrics.volume_accel < cfg["MIN_VOLUME_ACCEL"]:
        if not tier2_rs:
            reject_reasons.append(
                f"RS_30m {metrics.rel_strength_vs_spy*100:+.2f}% < {cfg['TIER2_MIN_RS_VS_SPY']*100:+.2f}% (Tier2)"
            )
        if not tier2_mom:
            reject_reasons.append(
                f"mom30 {metrics.momentum_30m*100:+.2f}% < {cfg['TIER2_MIN_MOMENTUM_30M']*100:+.2f}% (Tier2)"
            )
        if not tier2_trend:
            reject_reasons.append(
                f"trend {metrics.trend_structure_score:.0f} < {cfg['TIER2_MIN_TREND_STRUCTURE']:.0f} (Tier2)"
            )
    else:
        reject_reasons.append(
            f"RS_30m {metrics.rel_strength_vs_spy*100:+.2f}% < {cfg['MIN_RS_VS_SPY']*100:+.2f}% (Tier1)"
        )

    if not reject_reasons:
        reject_reasons.append("no tier conditions met")

    return False, f"[HYPER_REJECT] {' | '.join(reject_reasons)}"
