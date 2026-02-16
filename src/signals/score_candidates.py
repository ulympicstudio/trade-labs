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


LEVERAGED_OR_INVERSE_BLOCKLIST = {
    "TQQQ", "SQQQ", "SOXL", "SOXS", "TNA", "TZA",
    "SPXL", "SPXS", "UVXY", "SVXY", "ZSL", "UGL",
    "TSLL", "TSLS", "UVIX", "DUST"
}


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

    # ✅ Correct format: integer + SPACE + unit
    bars = ib.reqHistoricalData(
        c,
        endDateTime="",
        durationStr="2 H",
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

    # ✅ Correct format: integer + SPACE + unit
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


def score_scan_results(ib: IB, scan_results: List, top_n: int = 5) -> List[ScoredCandidate]:
    """
    Momentum hyper-swing scoring:
    - 60m momentum % (dominant)
    - ATR14 (swing potential boost)
    Uses historical bars only (avoids streaming quote entitlement problems).
    """
    scored: List[ScoredCandidate] = []

    for r in scan_results:
        sym = getattr(r, "symbol", None)
        rank = int(getattr(r, "rank", 9999))
        if not sym:
            continue

        # skip leveraged/inverse products by default
        if sym in LEVERAGED_OR_INVERSE_BLOCKLIST:
            continue

        try:
            df_1m = _get_intraday_1m(ib, sym)
            mom = _momentum_pct_60m(df_1m)
            if math.isnan(mom):
                continue

            df_d = _get_daily_30d(ib, sym)
            atr14 = _atr14_from_daily(df_d)
            if math.isnan(atr14):
                atr14 = 0.0

            # Score: momentum dominates, ATR adds swing preference
            score = (mom * 10.0) + (atr14 * 0.25)

            scored.append(ScoredCandidate(
                symbol=sym,
                rank=rank,
                momentum_pct_60m=mom,
                atr14=atr14,
                score=score
            ))

            # throttle to reduce IB cancellations
            ib.sleep(0.25)

        except Exception:
            # Skip any symbol that IB rejects (bad contracts, throttling, etc.)
            continue

    scored.sort(key=lambda x: x.score, reverse=True)
    return scored[:top_n]
