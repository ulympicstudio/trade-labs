import pandas as pd

def compute_atr(df: pd.DataFrame, period: int = 14) -> float:
    """
    ATR using daily bars.
    df must have columns: high, low, close
    """
    high = df["high"]
    low = df["low"]
    close = df["close"]

    prev_close = close.shift(1)

    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()

    true_range = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    atr = true_range.rolling(period).mean()

    latest = atr.dropna().iloc[-1]
    return float(latest)
