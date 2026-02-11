from dataclasses import dataclass

@dataclass
class PositionSizeResult:
    entry_price: float
    stop_price: float
    risk_per_share: float
    shares: int
    total_risk: float


def calculate_position_size(
    account_equity: float,
    risk_percent: float,
    entry_price: float,
    stop_price: float = None,
    atr: float = None,
    atr_multiplier: float = 2.0,
):
    """
    If stop_price is provided → use it.
    If not → derive stop from ATR.
    """

    # Step 1: determine stop
    if stop_price is None:
        if atr is None:
            raise ValueError("Either stop_price or atr must be provided")

        stop_price = entry_price - (atr * atr_multiplier)

    # Step 2: risk per share
    risk_per_share = entry_price - stop_price

    # Step 3: max risk allowed
    max_risk = account_equity * risk_percent

    # Step 4: share calculation
    shares = int(max_risk // risk_per_share)

    total_risk = shares * risk_per_share

    return PositionSizeResult(
        entry_price=entry_price,
        stop_price=stop_price,
        risk_per_share=risk_per_share,
        shares=shares,
        total_risk=total_risk,
    )
