from typing import List

from ib_insync import IB

from src.contracts.trade_intent import TradeIntent
from src.signals.market_scanner import scan_us_most_active_stocks
from src.signals.score_candidates import score_scan_results


def get_trade_intents_from_scan(
    ib: IB,
    limit: int = 20,
    score_limit: int = 15,
    top_n: int = 5,
) -> List[TradeIntent]:
    """
    Market-scanning v2 (with scoring):
    - Scan MOST_ACTIVE US stocks
    - Score each by quality metrics
    - Return top N by score as TradeIntents
    """
    scan = scan_us_most_active_stocks(ib, limit=limit)
    scored = score_scan_results(ib, scan, top_n=top_n, max_scan=score_limit)
    
    # Take top N by score (only those with score > 0)
    top_candidates = [s for s in scored if s.score > 0][:top_n]
    
    intents: List[TradeIntent] = []
    for candidate in top_candidates:
        intents.append(
            TradeIntent(
                symbol=candidate.symbol,
                side="BUY",
                entry_type="MKT",
                quantity=None,
                stop_loss=None,
                trailing_percent=None,
                rationale=f"Scanner v2: score={candidate.score:.1f} | {candidate.reason}"
            )
        )
    return intents
