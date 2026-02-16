import math
from typing import Any, Dict, List, Optional

from ib_insync import IB, Stock, util
import pandas as pd

from config.identity import SYSTEM_NAME, HUMAN_NAME
from src.data.ib_market_data import connect_ib, get_account_equity_usd
from src.signals.signal_engine import get_trade_intents_from_scan


# ---------- Helpers: Historical-only pricing (avoids 10089 spam) ----------

def _contract(symbol: str) -> Stock:
    return Stock(symbol, "SMART", "USD")


def get_recent_price_1m(ib: IB, symbol: str) -> float:
    """
    Entry price from historical 1-min bars (last close). Avoids reqMktData().
    """
    c = _contract(symbol)
    ib.qualifyContracts(c)

    bars = ib.reqHistoricalData(
        c,
        endDateTime="",
        durationStr="1 D",
        barSizeSetting="1 min",
        whatToShow="TRADES",
        useRTH=False,
        formatDate=1
    )
    df = util.df(bars)
    if df is None or df.empty:
        raise RuntimeError(f"No 1-min bars for {symbol}")
    return float(df["close"].iloc[-1])


def get_daily_30d(ib: IB, symbol: str) -> pd.DataFrame:
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
    if df is None or df.empty:
        raise RuntimeError(f"No daily bars for {symbol}")
    return df


def atr14_from_daily(df: pd.DataFrame) -> float:
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    close = df["close"].astype(float)
    prev_close = close.shift(1)

    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

    atr = tr.rolling(14).mean().dropna()
    if atr.empty:
        return float("nan")
    return float(atr.iloc[-1])


# ---------- Simple sizing (paper-safe, deterministic) ----------

def size_shares(
    equity: float,
    risk_pct: float,
    atr14: float,
    atr_mult: float = 2.0,
) -> int:
    """
    Shares = risk_dollars / stop_distance
    stop_distance = ATR * atr_mult
    """
    risk_dollars = equity * risk_pct
    stop_distance = max(atr14 * atr_mult, 0.01)
    shares = int(risk_dollars // stop_distance)
    return max(shares, 0)


# ---------- Main pipeline ----------

def run_full_pipeline(
    num_candidates: int = 5,
    use_spy_only: bool = False,
    logger: Optional[Any] = None,
    ib: Optional[IB] = None,
    **kwargs
) -> Dict[str, Any]:
    """
    Orchestrator entrypoint.

    Compatibility:
    - Orchestrator calls run_full_pipeline(...) WITHOUT passing ib
    - So we create an IB connection here if not provided.

    Behavior:
    - Scan + score candidates via signal_engine
    - Execution uses historical bars for entry/ATR (no streaming quotes)
    - Does NOT place orders here; it computes and prints sizes only
    """
    print("\n" + "=" * 60)
    print(f"{SYSTEM_NAME} → {HUMAN_NAME}: FULL PIPELINE v1 (historical-bars execution)")
    print("=" * 60 + "\n")

    created_ib = False
    if ib is None:
        ib = connect_ib()
        created_ib = True

    try:
        if logger and hasattr(logger, "scan_started"):
            logger.scan_started()

        intents = get_trade_intents_from_scan(ib, limit=50)

        if use_spy_only:
            intents = [i for i in intents if i.symbol == "SPY"]
            intents = intents[:1]

        intents = intents[:num_candidates]

        if not intents:
            print("No tradeable candidates found.")
            return {"ok": True, "candidates": [], "executed": 0, "successful": 0}

        print(f"Selected {len(intents)} candidates:\n")
        for idx, intent in enumerate(intents, start=1):
            print(f"  {idx}. {intent.symbol} — {intent.rationale}")

        # pull real equity from IB (paper)
        equity = float(kwargs.get("account_equity", get_account_equity_usd(ib)))
        risk_pct = float(kwargs.get("risk_pct", 0.005))   # 0.5% per trade
        atr_mult = float(kwargs.get("atr_mult", 2.0))

        executed = 0
        successful = 0

        for intent in intents:
            executed += 1
            print("\n" + "─" * 52)
            print(f"Executing: {intent.symbol}")
            print("─" * 52)

            try:
                entry_price = get_recent_price_1m(ib, intent.symbol)
                df_d = get_daily_30d(ib, intent.symbol)
                atr14 = atr14_from_daily(df_d)

                if math.isnan(atr14) or atr14 <= 0:
                    raise RuntimeError("ATR unavailable")

                shares = size_shares(equity, risk_pct, atr14, atr_mult=atr_mult)

                print(f"Entry Price (1m bars): ${entry_price:.2f}")
                print(f"ATR(14): {atr14:.4f}")

                if shares <= 0:
                    print("✗ Skipped: shares computed as 0")
                    if logger and hasattr(logger, "execution_completed"):
                        logger.execution_completed()
                    continue

                print(f"✓ Success: {shares} shares @ ${entry_price:.2f}")
                successful += 1

            except Exception as e:
                print(f"✗ Error: {e}")

            if logger and hasattr(logger, "execution_completed"):
                logger.execution_completed()

            ib.sleep(0.15)

        if logger and hasattr(logger, "pipeline_completed"):
            logger.pipeline_completed()

        print("\n" + "=" * 60)
        print("PIPELINE COMPLETE")
        print("=" * 60)
        print(f"Executed: {executed} candidates  |  Successful: {successful}")
        print("=" * 60 + "\n")

        return {"ok": True, "candidates": [i.symbol for i in intents], "executed": executed, "successful": successful}

    finally:
        if created_ib and ib is not None:
            ib.disconnect()
