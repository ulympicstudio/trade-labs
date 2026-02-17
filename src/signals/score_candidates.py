from dataclasses import dataclass
from typing import List
import math

from ib_insync import IB, Stock, util
import pandas as pd


@dataclass
class ScoredCandidate:
    symbol: str
    rank: int
    momentum_pct_60m: float
    atr14: float
    score: float
    reason: str


LEVERAGED_OR_INVERSE_BLOCKLIST = {
    "TQQQ", "SQQQ", "SOXL", "SOXS", "TNA", "TZA",
    "SPXL", "SPXS", "UVXY", "SVXY", "ZSL", "UGL",
    "TSLL", "TSLS", "UVIX", "DUST",
}

ETF_ALLOWLIST = {"SPY", "QQQ"}
ETF_BLOCKLIST = {"BITO"}


# ---- HARD FILTERS (tune anytime) ----
MIN_PRICE = 5.0
MIN_ATR14 = 0.25
MIN_AVG_DOLLAR_VOL_20D = 10_000_000  # $10M/day average dollar volume (was $25M, too restrictive)


def _contract(symbol: str) -> Stock:
    return Stock(symbol, "SMART", "USD")


def _atr14_from_daily(df: pd.DataFrame) -> float:
    high = df["high"]
    low = df["low"]
    close = df["close"]
    prev_close = close.shift(1)

    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

    atr = tr.rolling(14).mean().dropna()
    return float(atr.iloc[-1]) if not atr.empty else float("nan")


def _avg_dollar_volume_20d(df: pd.DataFrame) -> float:
    # IB daily bars include volume. Dollar volume ~ close * volume.
    if df is None or df.empty:
        return float("nan")
    d = df.tail(20).copy()
    if "volume" not in d.columns:
        return float("nan")
    return float((d["close"].astype(float) * d["volume"].astype(float)).mean())


def _last_close(df: pd.DataFrame) -> float:
    if df is None or df.empty:
        return float("nan")
    return float(df["close"].astype(float).iloc[-1])


def _momentum_pct_60m(df_1m: pd.DataFrame) -> float:
    if df_1m is None or df_1m.empty:
        return float("nan")
    closes = df_1m["close"].astype(float)
    if len(closes) < 61:
        return float("nan")
    last = float(closes.iloc[-1])
    past = float(closes.iloc[-61])
    if past <= 0:
        return float("nan")
    return (last / past - 1.0) * 100.0


def _get_intraday_1m(ib: IB, symbol: str) -> pd.DataFrame:
    c = _contract(symbol)
    ib.qualifyContracts(c)

    # 2 hours of 1-min bars = 7200 seconds (IB duration units must be S/D/W/M/Y)
    bars = ib.reqHistoricalData(
        c,
        endDateTime="",
        durationStr="7200 S",
        barSizeSetting="1 min",
        whatToShow="TRADES",
        useRTH=False,
        formatDate=1
    )
    df = util.df(bars)
    return df if df is not None else pd.DataFrame()


def _get_daily_30d(ib: IB, symbol: str) -> pd.DataFrame:
    c = _contract(symbol)
    ib.qualifyContracts(c)

    bars = ib.reqHistoricalData(
        c,
        endDateTime="",
        durationStr="30 D",
        barSizeSetting="1 day",
        whatToShow="TRADES",
        useRTH=True,
        formatDate=1
    )
    df = util.df(bars)
    return df if df is not None else pd.DataFrame()


def score_scan_results(
    ib: IB,
    scan_results: List,
    top_n: int = 5,
    max_scan: int = 15,
) -> List[ScoredCandidate]:
    """
    Hyper-swing momentum scoring with HARD liquidity filters.
    Uses historical bars only (stable, avoids streaming issues).
    """
    scored: List[ScoredCandidate] = []

    for r in scan_results[:max_scan]:
        sym = getattr(r, "symbol", None)
        rank = int(getattr(r, "rank", 9999))
        if not sym:
            continue

        if sym in LEVERAGED_OR_INVERSE_BLOCKLIST:
            print(f"  [SCORE] {sym} rejected: in LEVERAGED_OR_INVERSE_BLOCKLIST")
            continue

        if sym in ETF_BLOCKLIST and sym not in ETF_ALLOWLIST:
            print(f"  [SCORE] {sym} rejected: in ETF_BLOCKLIST")
            continue

        try:
            # Pull daily first for filters (cheaper than intraday sometimes)
            df_d = _get_daily_30d(ib, sym)
            px = _last_close(df_d)
            atr14 = _atr14_from_daily(df_d)
            adv20 = _avg_dollar_volume_20d(df_d)

            # HARD filters
            if math.isnan(px) or px < MIN_PRICE:
                print(f"  [SCORE] {sym} rejected: price ${px:.2f} < ${MIN_PRICE}")
                continue
            if math.isnan(adv20) or adv20 < MIN_AVG_DOLLAR_VOL_20D:
                print(f"  [SCORE] {sym} rejected: ADV20=${adv20/1e6:.1f}M < ${MIN_AVG_DOLLAR_VOL_20D/1e6:.0f}M")
                continue
            if math.isnan(atr14) or atr14 < MIN_ATR14:
                print(f"  [SCORE] {sym} rejected: ATR14={atr14:.2f} < {MIN_ATR14}")
                continue

            # Momentum (60 minutes)
            df_1m = _get_intraday_1m(ib, sym)
            mom = _momentum_pct_60m(df_1m)
            if math.isnan(mom):
                print(f"  [SCORE] {sym} rejected: no 60m momentum")
                continue

            # Score: momentum dominates; ATR gives swing preference
            score = (mom * 10.0) + (atr14 * 0.25)

            reason = (
                f"Momentum60m={mom:.2f}% | ATR14={atr14:.2f} | "
                f"LastClose=${px:.2f} | ADV20=${adv20/1e6:.1f}M | score={score:.2f}"
            )
            
            print(f"  [SCORE] {sym} ACCEPTED: {reason}")

            scored.append(ScoredCandidate(
                symbol=sym,
                rank=rank,
                momentum_pct_60m=mom,
                atr14=atr14,
                score=score,
                reason=reason
            ))

            # throttle to reduce IB cancellations
            ib.sleep(0.50)

        except Exception:
            continue

    scored.sort(key=lambda x: x.score, reverse=True)
    return scored[:top_n]
