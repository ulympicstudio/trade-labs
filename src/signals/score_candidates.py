"""
Scoring module: Rank scanner results by quality metrics.

Current implementation uses:
- Rank (from scanner, already sorted by volume)
- Price level (prefer $10-$200 sweet spot)
- Spread quality (lower is better)

Future: Add momentum, volatility regime, correlation scoring.
"""

from dataclasses import dataclass
from typing import List, Optional
from math import log

from ib_insync import IB
from src.signals.market_scanner import ScanResult, get_quote, passes_quality_filters


@dataclass
class ScoredResult:
    symbol: str
    rank: int
    bid: Optional[float]
    ask: Optional[float]
    last: Optional[float]
    score: float  # 0.0 to 100.0
    reason: str


def score_candidate(
    result: ScanResult,
    bid: Optional[float],
    ask: Optional[float],
    last: Optional[float],
) -> ScoredResult:
    """
    Score a single candidate (0-100).
    
    Factors:
    - Passes quality filters (baseline)
    - Price level (prefer $20-$150)
    - Spread Quality (narrow spreads score higher)
    - Rank decay (top-ranked stocks score higher)
    """
    
    # Baseline: fails quality → score 0
    if not passes_quality_filters(result.symbol, bid, ask, last):
        return ScoredResult(
            symbol=result.symbol,
            rank=result.rank,
            bid=bid, ask=ask, last=last,
            score=0.0,
            reason="Failed quality filters"
        )
    
    # Use last if available, else midpoint
    if last is not None:
        price = last
    else:
        price = (bid + ask) / 2.0 if (bid and ask) else None
    
    if price is None:
        return ScoredResult(
            symbol=result.symbol,
            rank=result.rank,
            bid=bid, ask=ask, last=last,
            score=0.0,
            reason="No price available"
        )
    
    score = 100.0
    reason_parts = []
    
    # Factor 1: Price level (prefer $20-$150, decay outside)
    if 20 <= price <= 150:
        price_score = 100.0
    elif 10 <= price < 20:
        price_score = 80.0
    elif 150 < price <= 300:
        price_score = 80.0
    else:
        price_score = 50.0
    
    score = (score + price_score) / 2.0
    reason_parts.append(f"price={price:.2f} ({price_score:.0f})")
    
    # Factor 2: Spread quality (normalize by price, prefer <0.1%)
    if bid and ask and bid > 0:
        spread_pct = (ask - bid) / price
        if spread_pct < 0.001:  # <0.1%
            spread_score = 100.0
        elif spread_pct < 0.005:  # <0.5%
            spread_score = 90.0
        else:
            spread_score = 70.0
        score = (score + spread_score) / 2.0
        reason_parts.append(f"spread={spread_pct*100:.3f}% ({spread_score:.0f})")
    
    # Factor 3: Rank decay (top 10 = 100, top 50 = 80)
    if result.rank <= 10:
        rank_score = 100.0
    elif result.rank <= 30:
        rank_score = 90.0
    else:
        rank_score = 80.0 * (1.0 - (result.rank - 30) / 100.0)  # decay
    
    score = (score + rank_score) / 2.0
    reason_parts.append(f"rank={result.rank} ({rank_score:.0f})")
    
    return ScoredResult(
        symbol=result.symbol,
        rank=result.rank,
        bid=bid, ask=ask, last=last,
        score=score,
        reason=" | ".join(reason_parts)
    )


def score_scan_results(
    ib: IB,
    scan_results: List[ScanResult],
) -> List[ScoredResult]:
    """
    Score all scan results and return sorted by score (highest first).
    """
    scored = []
    
    for result in scan_results:
        try:
            bid, ask, last = get_quote(ib, result.symbol)
            scored_result = score_candidate(result, bid, ask, last)
            scored.append(scored_result)
        except Exception as e:
            # Skip candidates that fail on quote
            scored.append(ScoredResult(
                symbol=result.symbol,
                rank=result.rank,
                bid=None, ask=None, last=None,
                score=0.0,
                reason=f"Quote error: {str(e)[:50]}"
            ))
    
    # Sort by score descending, then by rank ascending
    scored.sort(key=lambda x: (-x.score, x.rank))
    return scored
