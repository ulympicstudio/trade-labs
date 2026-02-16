import math
import os
import uuid
from typing import Any, Dict, List, Optional

from ib_insync import IB, Stock, util
import pandas as pd

from config.identity import SYSTEM_NAME, HUMAN_NAME
from src.data.ib_market_data import connect_ib, get_account_equity_usd
from src.signals.signal_engine import get_trade_intents_from_scan
from src.utils.trade_history_db import TradeHistoryDB


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
        run_id = str(uuid.uuid4())[:8]
        backend = os.getenv("TRADE_LABS_EXECUTION_BACKEND", "SIM")
        armed = os.getenv("TRADE_LABS_ARMED", "0") == "1"
        db = TradeHistoryDB("data/trade_history")

        if logger and hasattr(logger, "scan_started"):
            logger.scan_started(run_id)

        scan_limit = int(kwargs.get("scan_limit", 20))
        score_limit = int(kwargs.get("score_limit", 15))
        top_n = int(kwargs.get("top_n", 5))
        intents = get_trade_intents_from_scan(
            ib,
            limit=scan_limit,
            score_limit=score_limit,
            top_n=top_n,
        )

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
                stop_price = entry_price - (atr14 * atr_mult)

                print(f"Entry Price (1m bars): ${entry_price:.2f}")
                print(f"ATR(14): {atr14:.4f}")

                if shares <= 0:
                    print("✗ Skipped: shares computed as 0")
                    if logger and hasattr(logger, "execution_completed"):
                        logger.execution_completed(
                            run_id=run_id,
                            symbol=intent.symbol,
                            shares=0,
                            entry_price=entry_price,
                            stop_loss=stop_price,
                            order_id=None,
                            ok=False,
                            reason="shares computed as 0",
                        )
                    continue

                print(f"✓ Suggested: {shares} shares @ ${entry_price:.2f}")
                successful += 1
                db.record_candidate(
                    run_id=run_id,
                    symbol=intent.symbol,
                    side=intent.side,
                    entry_price=entry_price,
                    quantity=shares,
                    stop_loss=stop_price,
                    rationale=intent.rationale,
                    backend=backend,
                    armed=armed,
                )

            except Exception as e:
                print(f"✗ Error: {e}")

                if logger and hasattr(logger, "execution_completed"):
                    logger.execution_completed(
                        run_id=run_id,
                        symbol=intent.symbol,
                        shares=0,
                        entry_price=0.0,
                        stop_loss=0.0,
                        order_id=None,
                        ok=False,
                        reason=str(e)[:80],
                    )
                continue

            if logger and hasattr(logger, "execution_completed"):
                logger.execution_completed(
                    run_id=run_id,
                    symbol=intent.symbol,
                    shares=shares,
                    entry_price=entry_price,
                    stop_loss=stop_price,
                    order_id=None,
                    ok=True,
                    reason="suggested",
                )

            ib.sleep(0.15)

        if logger and hasattr(logger, "pipeline_completed"):
            logger.pipeline_completed(run_id, executed, successful)

        db.record_pipeline_run(
            run_id=run_id,
            backend=backend,
            armed=armed,
            num_candidates_scanned=scan_limit,
            num_candidates_executed=executed,
            num_successful=successful,
            details={
                "scan_limit": scan_limit,
                "score_limit": score_limit,
                "use_spy_only": use_spy_only,
            },
        )

        print("\n" + "=" * 60)
        print("PIPELINE COMPLETE")
        print("=" * 60)
        print(f"Executed: {executed} candidates  |  Successful: {successful}")
        print("=" * 60 + "\n")

        return {
            "ok": True,
            "run_id": run_id,
            "candidates": [i.symbol for i in intents],
            "executed": executed,
            "successful": successful,
        }

    finally:
        if created_ib and ib is not None:
            ib.disconnect()
