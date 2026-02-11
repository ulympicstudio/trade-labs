from dataclasses import dataclass
from typing import Optional

@dataclass
class TradeIntent:
    symbol: str
    side: str  # "BUY" or "SELL"
    entry_type: str  # "MKT" or "LMT"
    quantity: int
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    trailing_percent: Optional[float] = None
    rationale: Optional[str] = None
