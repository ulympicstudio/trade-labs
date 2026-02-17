"""
CATALYST SCORER - Comprehensive catalyst signal ranking and validation
Scores catalysts by strength, urgency, magnitude, cross-validation
"""

import logging
from dataclasses import dataclass
from typing import Dict, List, Tuple, Optional
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)


@dataclass
class CatalystScore:
    """Comprehensive score for a catalyst-triggered trade."""
    symbol: str
    catalyst_score: float  # 0-100: Quality of catalyst signals
    technical_score: float  # 0-100: Technical setup goodness
    combined_score: float  # 0-100: Overall trade score
    
    signal_count: int
    best_catalyst_types: List[str]
    
    urgency: float  # 0-1: How fast to act
    confidence: float  # 0-1: How sure we are
    magnitude: float  # Expected move size
    
    reasoning: str  # Why we're interested
    
    rank: int = 0


class CatalystScorer:
    """Score and rank catalyst-driven trading opportunities."""
    
    def __init__(self, quant_scorer=None):
        """
        Initialize scorer.
        
        Args:
            quant_scorer: Optional technical/quant scorer for combined scoring
        """
        self.quant_scorer = quant_scorer
        
        # Weighting by signal type effectiveness
        self.catalyst_weights = {
            "earnings": 2.5,  # Biggest moves typically
            "upgrade": 2.0,
            "acquisition": 2.5,
            "product": 1.8,
            "insider_buy": 1.9,
            "options_unusual": 1.6,
            "volume_spike": 1.2,
            "social_buzz": 0.8,
        }
        
        # How much each source is trusted
        self.source_credibility = {
            "finnhub": 0.95,
            "finnhub_earnings": 0.98,
            "insider_trading": 0.92,
            "options_market": 0.85,
            "reddit_wallstreetbets": 0.60,
            "reddit_stocks": 0.70,
            "reddit_investing": 0.68,
            "yahoo_trending": 0.65,
        }
    
    def score_catalyst_stock(self, symbol: str, catalyst_stock) -> CatalystScore:
        """
        Score a stock with multiple catalyst signals.
        
        Args:
            symbol: Stock ticker
            catalyst_stock: CatalystStock object with signals list
        
        Returns:
            CatalystScore with composite ranking
        """
        
        signals = catalyst_stock.signals
        if not signals:
            return CatalystScore(
                symbol=symbol,
                catalyst_score=0.0,
                technical_score=0.0,
                combined_score=0.0,
                signal_count=0,
                best_catalyst_types=[],
                urgency=0.0,
                confidence=0.0,
                magnitude=0.0,
                reasoning="No catalyst signals found",
            )
        
        # 1. Calculate weighted catalyst score
        catalyst_score = self._calculate_catalyst_score(signals)
        
        # 2. Get technical score (if available)
        technical_score = 0.0
        if self.quant_scorer:
            try:
                technical_score = self.quant_scorer.score_symbol(symbol).final_score
            except:
                technical_score = 50.0  # Neutral default
        
        # 3. Calculate composite
        # 60% catalyst, 40% technical (catalyst-first mode)
        combined = (catalyst_score * 0.60) + (technical_score * 0.40)
        
        # 4. Extract best catalyst types
        catalyst_types = list(set(s.catalyst_type for s in signals))
        best_types = sorted(
            catalyst_types,
            key=lambda t: self.catalyst_weights.get(t, 1.0),
            reverse=True
        )[:3]
        
        # 5. Aggregate urgency/confidence/magnitude
        avg_urgency = sum(s.urgency for s in signals) / len(signals)
        avg_confidence = sum(s.confidence for s in signals) / len(signals)
        avg_magnitude = sum(s.magnitude for s in signals) / len(signals)
        
        # Boost confidence if multiple independent sources agree
        if len(signals) > 2:
            independent_sources = len(set(s.source for s in signals))
            avg_confidence = min(0.98, avg_confidence * (1 + (independent_sources * 0.1)))
        
        # 6. Build reasoning
        reasoning = self._build_reasoning(symbol, signals, catalyst_score, technical_score)
        
        return CatalystScore(
            symbol=symbol,
            catalyst_score=catalyst_score,
            technical_score=technical_score,
            combined_score=combined,
            signal_count=len(signals),
            best_catalyst_types=best_types,
            urgency=min(1.0, avg_urgency),
            confidence=min(1.0, avg_confidence),
            magnitude=avg_magnitude,
            reasoning=reasoning,
        )
    
    def _calculate_catalyst_score(self, signals: List) -> float:
        """
        Calculate weighted score from multiple signals.
        
        Higher scores = better trading opportunity
        - Multiple signals reinforce each other
        - High credibility sources weighted more
        - Bullish + bearish can cancel out
        """
        
        if not signals:
            return 0.0
        
        total_weight = 0.0
        total_score = 0.0
        
        for signal in signals:
            # Base weight from catalyst type
            base_weight = self.catalyst_weights.get(signal.catalyst_type, 1.0)
            
            # Adjust by source credibility
            credibility = self.source_credibility.get(signal.source, 0.7)
            
            # Final weight
            weight = base_weight * credibility * signal.confidence
            
            # Direction: bullish = positive, bearish = negative
            direction = 1.0 if signal.bullish else -1.0
            
            # Contribution: weight × direction × signal quality
            contribution = weight * direction * signal.magnitude
            
            total_weight += weight
            total_score += contribution
        
        # Normalize to 0-100
        if total_weight == 0:
            return 50.0
        
        normalized = (total_score / total_weight) * 25 + 50  # Center at 50
        return max(0, min(100, normalized))
    
    def _build_reasoning(self, symbol: str, signals: List, catalyst_score: float, technical_score: float) -> str:
        """Build human-readable reasoning for the score."""
        
        signal_types = list(set(s.catalyst_type for s in signals))
        main_catalyst = signal_types[0] if signal_types else "unknown"
        
        # Format: "{symbol}: {best_catalysts} ({signal_count} signals)"
        catalyst_str = ", ".join([s.upper() for s in signal_types[:2]])
        
        reasoning = f"{symbol}: {catalyst_str}"
        
        if len(signals) > 1:
            reasoning += f" + {len(signals)} signals"
        
        # Add assessment
        if catalyst_score > 75:
            reasoning += " ⭐ STRONG catalyst"
        elif catalyst_score > 60:
            reasoning += " ✓ Good catalyst"
        elif catalyst_score > 50:
            reasoning += " ○ Moderate catalyst"
        else:
            reasoning += " ✗ Weak catalyst"
        
        if technical_score > 70:
            reasoning += " | TECHNICAL bullish"
        elif technical_score < 40:
            reasoning += " | TECHNICAL bearish"
        
        return reasoning
    
    def rank_opportunities(self, catalyst_stocks: Dict[str, object], max_results: int = 20) -> List[CatalystScore]:
        """
        Rank all catalyst stocks by opportunity quality.
        
        Args:
            catalyst_stocks: Dict of symbol -> CatalystStock
            max_results: Max stocks to return
        
        Returns:
            Sorted list of top CatalystScores
        """
        
        scores = []
        
        for symbol, stock in catalyst_stocks.items():
            score = self.score_catalyst_stock(symbol, stock)
            scores.append(score)
        
        # Sort by combined score (descending)
        scores = sorted(scores, key=lambda s: s.combined_score, reverse=True)
        
        # Add rank
        for i, score in enumerate(scores, 1):
            score.rank = i
        
        return scores[:max_results]
    
    def print_opportunity_report(self, opportunities: List[CatalystScore]):
        """Pretty print ranked opportunities."""
        
        print("\n" + "="*100)
        print("🎯 CATALYST OPPORTUNITY REPORT".center(100))
        print("="*100)
        print(f"{'Rank':<6} {'Symbol':<8} {'Catalyst':<20} {'Signals':<10} {'Catalyst':<12} {'Technical':<12} {'Combined':<10}")
        print("-"*100)
        
        for opp in opportunities:
            catalyst_types = ", ".join(opp.best_catalyst_types[:2])
            
            print(
                f"{opp.rank:<6} "
                f"{opp.symbol:<8} "
                f"{catalyst_types:<20} "
                f"{opp.signal_count:<10} "
                f"{opp.catalyst_score:<12.1f} "
                f"{opp.technical_score:<12.1f} "
                f"{opp.combined_score:<10.1f}"
            )
        
        print("="*100)
        print("")
        
        # Detailed reasoning
        print("📋 DETAILED ANALYSIS".center(100))
        print("-"*100)
        
        for opp in opportunities[:5]:  # Top 5 detail
            print(f"\n{opp.rank}. {opp.reasoning}")
            print(f"   Urgency: {opp.urgency:.2%} | Confidence: {opp.confidence:.2%} | Expected move: {opp.magnitude:.1f}x")
            print(f"   Signal types: {', '.join(opp.best_catalyst_types)}")
    
    def should_trade_catalyst(self, score: CatalystScore, min_score: float = 70.0) -> Tuple[bool, str]:
        """
        Determine if catalyst score warrants a trade.
        
        Args:
            score: CatalystScore for a stock
            min_score: Minimum combined score to trade
        
        Returns:
            (should_trade: bool, reason: str)
        """
        
        # Must meet minimum combined score
        if score.combined_score < min_score:
            return False, f"Score {score.combined_score:.1f} < threshold {min_score}"
        
        # Must have confidence
        if score.confidence < 0.55:
            return False, f"Confidence {score.confidence:.2%} too low"
        
        # Must be somewhat urgent (don't chase stale news)
        if score.urgency < 0.5:
            return False, f"Urgency {score.urgency:.2%} too low (stale signal)"
        
        return True, "✅ Catalyst meets trading criteria"
