from ib_insync import IB, Stock, util
import pandas as pd
import math
from config.ib_config import HOST, PORT, CLIENT_ID
def get_history_bars(
    ib: IB,
    contract,
    duration: str = "30 D",
    bar_size: str = "1 day"
) -> pd.DataFrame:
    bars = ib.reqHistoricalData(
        contract,
        endDateTime="",
        durationStr=duration,
        barSizeSetting=bar_size,
        whatToShow="TRADES",
        useRTH=True,
        formatDate=1
    )
    df = util.df(bars)
    if df is None or df.empty:
        raise RuntimeError("No historical bars returned.")
    return df

def connect_ib() -> IB:
    ib = IB()
    ib.connect(HOST, PORT, clientId=CLIENT_ID, timeout=10)
    return ib

def get_spy_contract():
    # Always use SMART routing
    return Stock("SPY", "SMART", "USD")

def get_account_equity_usd(ib: IB) -> float:
    summary = ib.accountSummary()
    for item in summary:
        if item.tag == "NetLiquidation" and item.currency == "USD":
            return float(item.value)
    raise RuntimeError("NetLiquidation (USD) not found.")

def _is_valid_number(x) -> bool:
    if x is None:
        return False
    try:
        return not math.isnan(float(x))
    except Exception:
        return False

def get_last_price(ib: IB, contract) -> float:
    ticker = ib.reqMktData(contract, "", True, False)
    ib.sleep(1.0)
    if _is_valid_number(ticker.last):
        return float(ticker.last)
    if _is_valid_number(ticker.close):
        return float(ticker.close)
    if _is_valid_number(ticker.bid) and _is_valid_number(ticker.ask):
        return float((float(ticker.bid) + float(ticker.ask)) / 2.0)
    bars = ib.reqHistoricalData(
        contract,
        endDateTime="",
        durationStr="5 D",
        barSizeSetting="1 day",
        whatToShow="TRADES",
        useRTH=True,
        formatDate=1
    )
    df = util.df(bars)
    if df is None or df.empty:
        raise RuntimeError("No price available (snapshot + history both failed).")
    return float(df["close"].iloc[-1])

def get_recent_price_from_history(ib: IB, contract) -> float:
    """
    Alias for get_last_price for backward compatibility.
    """
    return get_last_price(ib, contract)


