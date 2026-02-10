"""Risk guard helpers."""


def check_risk(position_size: float, max_size: float) -> bool:
    """Return True if position_size is within max_size."""
    return position_size <= max_size
