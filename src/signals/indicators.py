"""
Pure-Python technical indicators.

No external dependencies — uses only the standard library.
"""

from __future__ import annotations

from typing import Optional


def compute_rsi(closes: list[float], period: int = 14) -> Optional[float]:
    """Compute the Relative Strength Index for a series of closing prices.

    Parameters
    ----------
    closes:
        Ordered list of closing prices (oldest first).  At least
        ``period + 1`` values are required so that *period* price
        changes can be computed.
    period:
        Look-back window (default 14).

    Returns
    -------
    float | None
        RSI in the range ``[0, 100]``, or ``None`` if there are
        insufficient data points.

    Algorithm
    ---------
    Uses the classic Wilder smoothing (exponential moving average of
    gains / losses):

    1. Compute price changes ``Δ = close[i] - close[i-1]``.
    2. Seed average gain / loss from the first *period* changes.
    3. Smooth subsequent changes with ``avg = (prev_avg * (period-1) + current) / period``.
    4. ``RS = avg_gain / avg_loss``; ``RSI = 100 - 100 / (1 + RS)``.
    """
    if len(closes) < period + 1:
        return None

    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]

    # Seed from the first `period` deltas
    gains = [d if d > 0 else 0.0 for d in deltas[:period]]
    losses = [-d if d < 0 else 0.0 for d in deltas[:period]]
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period

    # Smooth over the remaining deltas (Wilder's method)
    for d in deltas[period:]:
        avg_gain = (avg_gain * (period - 1) + max(d, 0.0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-d, 0.0)) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100.0 - 100.0 / (1.0 + rs), 2)
