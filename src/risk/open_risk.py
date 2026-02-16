from ib_insync import IB
from src.data.ib_market_data import get_last_price

def estimate_open_risk_usd(ib: IB, atr: float, atr_multiplier: float = 2.0) -> float:
    """
    MVP estimate:
    open risk = sum over positions of (shares * ATR*multiplier)

    This assumes stops roughly 2x ATR away.
    Later we’ll track actual stops per position.
    """
    positions = ib.positions()
    total = 0.0

    for p in positions:
        if p.position == 0:
            continue

        # For long positions, risk approximated by ATR stop distance
        shares = abs(int(p.position))
        risk_per_share = atr * atr_multiplier
        total += shares * risk_per_share

    return float(total)
