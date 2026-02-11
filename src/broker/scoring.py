from dataclasses import dataclass

@dataclass
class Score:
    value: float          # -1.0 to +1.0 (example)
    confidence: float     # 0.0 to 1.0
    notes: str = ""

def score_symbol(symbol: str, features: dict) -> Score:
    """
    MVP stub: returns a neutral score.
    Later: plug in momentum, volume, catalysts, RSI, etc.
    """
    return Score(value=0.0, confidence=0.0, notes="Stub scoring (MVP)")
