"""Position sizing utilities."""


def size_position(portfolio_value: float, risk_fraction: float) -> float:
    """Return position size given portfolio value and risk fraction."""
    return portfolio_value * risk_fraction
