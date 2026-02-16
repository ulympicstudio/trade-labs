"""
Quant-News Integration System
Combines quantitative technical analysis with news sentiment for multi-factor scoring.
"""

import logging
from dataclasses import dataclass
from typing import List, Optional, Dict
from datetime import datetime

from src.quant.quant_scorer import QuantScorer, QuantScore
from src.data.news_scorer import NewsScorer, NewsScore


logger = logging.getLogger(__name__)


@dataclass
class UnifiedScore:
    """Combined quant + news scoring for a symbol."""
    symbol: str
    timestamp: str
    
    # Final combined score (0-100)
    total_score: float
    
    # Component scores
    quant_score: float  # Technical analysis (0-100)
    news_score: float  # News/sentiment (0-100)
    
    # Sub-scores from quant
    momentum_score: Optional[float] = None
    mean_reversion_score: Optional[float] = None
    volatility_score: Optional[float] = None
    
    # Sub-scores from news
    sentiment_score: Optional[float] = None
    catalyst_score: Optional[float] = None
    
    # Signal generation
    unified_signal: str = "NEUTRAL"  # "STRONG_BUY", "BUY", "NEUTRAL", "SELL", "STRONG_SELL"
    quant_signal: Optional[str] = None
    news_signal: Optional[str] = None
    
    # Metadata
    confidence: float = 0.0
    has_news: bool = False
    has_quant: bool = False
    
    # Trading parameters (if applicable)
    entry_price: Optional[float] = None
    stop_price: Optional[float] = None
    target_price: Optional[float] = None
    risk_reward_ratio: Optional[float] = None


class QuantNewsIntegrator:
    """
    Integrates quantitative technical analysis with news sentiment scoring.
    Produces unified rankings for trading opportunities.
    """
    
    def __init__(self, quant_weight: float = 0.60, news_weight: float = 0.40,
                 earnings_api_key: Optional[str] = None):
        """
        Initialize integrator.
        
        Args:
            quant_weight: Weight for quant score (default 60%)
            news_weight: Weight for news score (default 40%)
            earnings_api_key: Optional Finnhub API key
        """
        if not abs(quant_weight + news_weight - 1.0) < 0.01:
            raise ValueError("quant_weight + news_weight must equal 1.0")
        
        self.quant_weight = quant_weight
        self.news_weight = news_weight
        
        self.quant_scorer = QuantScorer()
        self.news_scorer = NewsScorer(earnings_api_key=earnings_api_key)
        
        logger.info(f"QuantNewsIntegrator initialized (quant:{quant_weight:.0%}, news:{news_weight:.0%})")
    
    def score_symbol(self, symbol: str, historical_data: dict,
                     news_days_back: int = 7) -> Optional[UnifiedScore]:
        """
        Generate unified score for a symbol.
        
        Args:
            symbol: Stock ticker
            historical_data: OHLCV data for quant analysis
            news_days_back: Days of news to analyze
        
        Returns:
            UnifiedScore object or None
        """
        try:
            # Get quant score
            quant_score_obj = self.quant_scorer.score_opportunity(symbol, historical_data)
            
            # Get news score
            news_score_obj = self.news_scorer.score_symbol(symbol, days_back=news_days_back)
            
            # Must have at least one score
            if not quant_score_obj and not news_score_obj:
                return None
            
            # Extract scores (use neutral 50 if missing)
            quant_score = quant_score_obj.total_score if quant_score_obj else 50.0
            news_score = news_score_obj.total_news_score if news_score_obj else 50.0
            
            # Calculate weighted combined score
            total_score = (quant_score * self.quant_weight) + (news_score * self.news_weight)
            
            # Determine unified signal
            unified_signal = self._determine_unified_signal(
                total_score, 
                quant_score_obj.signal if quant_score_obj else "NEUTRAL",
                news_score_obj.news_signal if news_score_obj else "NEUTRAL"
            )
            
            # Calculate confidence
            confidence = self._calculate_confidence(quant_score_obj, news_score_obj)
            
            return UnifiedScore(
                symbol=symbol,
                timestamp=datetime.now().isoformat(),
                total_score=round(total_score, 2),
                quant_score=round(quant_score, 2),
                news_score=round(news_score, 2),
                momentum_score=quant_score_obj.momentum_score if quant_score_obj else None,
                mean_reversion_score=quant_score_obj.mean_reversion_score if quant_score_obj else None,
                volatility_score=quant_score_obj.volatility_score if quant_score_obj else None,
                sentiment_score=news_score_obj.sentiment_score if news_score_obj else None,
                catalyst_score=news_score_obj.catalyst_score if news_score_obj else None,
                unified_signal=unified_signal,
                quant_signal=quant_score_obj.signal if quant_score_obj else None,
                news_signal=news_score_obj.news_signal if news_score_obj else None,
                confidence=round(confidence, 2),
                has_news=news_score_obj is not None,
                has_quant=quant_score_obj is not None,
                entry_price=quant_score_obj.entry_price if quant_score_obj else None,
                stop_price=quant_score_obj.stop_price if quant_score_obj else None,
                target_price=quant_score_obj.target_price if quant_score_obj else None,
                risk_reward_ratio=quant_score_obj.risk_reward_ratio if quant_score_obj else None
            )
            
        except Exception as e:
            logger.error(f"{symbol}: Failed to create unified score - {e}", exc_info=True)
            return None
    
    def scan_and_rank(self, symbols: List[str], historical_data_map: Dict[str, dict],
                      news_days_back: int = 7, min_score: float = 60.0,
                      top_n: int = 20) -> List[UnifiedScore]:
        """
        Score multiple symbols and return top opportunities.
        
        Args:
            symbols: List of tickers to analyze
            historical_data_map: Dict mapping symbol -> OHLCV data
            news_days_back: Days of news to analyze
            min_score: Minimum unified score to include
            top_n: Return top N opportunities
        
        Returns:
            List of UnifiedScore objects ranked by total score
        """
        scores = []
        
        logger.info(f"Scanning {len(symbols)} symbols...")
        
        for symbol in symbols:
            hist_data = historical_data_map.get(symbol)
            if not hist_data:
                continue
            
            score = self.score_symbol(symbol, hist_data, news_days_back)
            if score and score.total_score >= min_score:
                scores.append(score)
        
        # Sort by total score (descending)
        scores.sort(key=lambda s: s.total_score, reverse=True)
        
        logger.info(f"Found {len(scores)} opportunities with score >= {min_score}")
        
        return scores[:top_n]
    
    def get_best_opportunities(self, min_quant_score: float = 55.0,
                               min_news_score: float = 60.0,
                               news_days_back: int = 3,
                               top_n: int = 20) -> List[UnifiedScore]:
        """
        Get best trading opportunities combining quant + news signals.
        
        This method:
        1. Finds stocks with strong news (most talked about)
        2. Analyzes their technicals
        3. Returns top combined opportunities
        
        Args:
            min_quant_score: Minimum quant score
            min_news_score: Minimum news score
            news_days_back: Days to look back for news
            top_n: Return top N opportunities
        
        Returns:
            List of UnifiedScore objects
        """
        # Get news-driven candidates
        news_opportunities = self.news_scorer.get_top_news_driven_opportunities(
            min_score=min_news_score,
            days_back=news_days_back
        )
        
        logger.info(f"Found {len(news_opportunities)} news-driven candidates")
        
        if not news_opportunities:
            return []
        
        # Get historical data for these symbols
        from src.quant.quant_scanner import QuantMarketScanner
        scanner = QuantMarketScanner()
        
        symbols = [opp.symbol for opp in news_opportunities]
        historical_data_map = scanner.fetch_historical_data(symbols)
        
        # Score with combined system
        unified_scores = []
        
        for news_score_obj in news_opportunities:
            symbol = news_score_obj.symbol
            hist_data = historical_data_map.get(symbol)
            
            if not hist_data:
                continue
            
            # Get quant score
            quant_score_obj = self.quant_scorer.score_opportunity(symbol, hist_data)
            
            if not quant_score_obj or quant_score_obj.total_score < min_quant_score:
                continue
            
            # Create unified score
            total_score = (quant_score_obj.total_score * self.quant_weight +
                          news_score_obj.total_news_score * self.news_weight)
            
            unified_signal = self._determine_unified_signal(
                total_score,
                quant_score_obj.signal,
                news_score_obj.news_signal
            )
            
            confidence = self._calculate_confidence(quant_score_obj, news_score_obj)
            
            unified_scores.append(UnifiedScore(
                symbol=symbol,
                timestamp=datetime.now().isoformat(),
                total_score=round(total_score, 2),
                quant_score=round(quant_score_obj.total_score, 2),
                news_score=round(news_score_obj.total_news_score, 2),
                momentum_score=quant_score_obj.momentum_score,
                mean_reversion_score=quant_score_obj.mean_reversion_score,
                volatility_score=quant_score_obj.volatility_score,
                sentiment_score=news_score_obj.sentiment_score,
                catalyst_score=news_score_obj.catalyst_score,
                unified_signal=unified_signal,
                quant_signal=quant_score_obj.signal,
                news_signal=news_score_obj.news_signal,
                confidence=round(confidence, 2),
                has_news=True,
                has_quant=True,
                entry_price=quant_score_obj.entry_price,
                stop_price=quant_score_obj.stop_price,
                target_price=quant_score_obj.target_price,
                risk_reward_ratio=quant_score_obj.risk_reward_ratio
            ))
        
        # Sort by total score
        unified_scores.sort(key=lambda s: s.total_score, reverse=True)
        
        logger.info(f"Generated {len(unified_scores)} unified opportunities")
        
        return unified_scores[:top_n]
    
    def _determine_unified_signal(self, total_score: float, quant_signal: str, 
                                   news_signal: str) -> str:
        """
        Determine unified trading signal.
        
        Logic:
        - Both STRONG_BUY → STRONG_BUY
        - One STRONG_BUY, one BUY → STRONG_BUY
        - Both BUY → BUY
        - Mixed positive/neutral → BUY
        - One avoid → NEUTRAL
        - Both avoid → STRONG_SELL
        """
        # Map signals to numeric values
        signal_values = {
            "STRONG_BUY": 2,
            "BUY": 1,
            "NEUTRAL": 0,
            "AVOID": -1,
            "SELL": -1,
            "STRONG_SELL": -2
        }
        
        quant_val = signal_values.get(quant_signal, 0)
        news_val = signal_values.get(news_signal, 0)
        
        # Combined logic
        if quant_val >= 2 and news_val >= 1:
            return "STRONG_BUY"
        elif quant_val >= 1 and news_val >= 2:
            return "STRONG_BUY"
        elif quant_val >= 1 and news_val >= 1:
            return "BUY"
        elif total_score >= 70:
            return "BUY"
        elif total_score <= 35 or (quant_val < 0 and news_val < 0):
            return "STRONG_SELL"
        elif quant_val < 0 or news_val < 0:
            return "NEUTRAL"
        else:
            return "NEUTRAL"
    
    def _calculate_confidence(self, quant_score: Optional[QuantScore],
                              news_score: Optional[NewsScore]) -> float:
        """
        Calculate confidence in unified score.
        
        High confidence when:
        - Both quant and news signals agree
        - Both have high individual confidence
        - Both scores are extreme (very high or very low)
        """
        if not quant_score and not news_score:
            return 0.0
        
        # Individual confidences
        quant_conf = quant_score.total_score if quant_score else 50.0
        news_conf = news_score.confidence if news_score else 50.0
        
        # Agreement bonus
        if quant_score and news_score:
            # Check signal agreement
            if quant_score.signal == news_score.news_signal:
                agreement_bonus = 20.0
            elif self._signals_compatible(quant_score.signal, news_score.news_signal):
                agreement_bonus = 10.0
            else:
                agreement_bonus = 0.0
        else:
            agreement_bonus = 0.0
        
        # Base confidence is weighted average
        if quant_score and news_score:
            base_conf = (quant_conf * self.quant_weight + news_conf * self.news_weight)
        else:
            base_conf = quant_conf if quant_score else news_conf
        
        total_conf = min(100, base_conf + agreement_bonus)
        
        return total_conf
    
    def _signals_compatible(self, signal1: str, signal2: str) -> bool:
        """Check if two signals are compatible (both bullish or both bearish)."""
        bullish = {"STRONG_BUY", "BUY"}
        bearish = {"AVOID", "SELL", "STRONG_SELL"}
        
        if signal1 in bullish and signal2 in bullish:
            return True
        if signal1 in bearish and signal2 in bearish:
            return True
        
        return False


def display_unified_scores(scores: List[UnifiedScore], top_n: int = 20):
    """Pretty print unified scores."""
    print(f"\n{'='*120}")
    print(f"TOP {min(top_n, len(scores))} UNIFIED OPPORTUNITIES (Quant + News)")
    print(f"{'='*120}\n")
    
    print(f"{'Rank':<6}{'Symbol':<8}{'Total':<8}{'Quant':<8}{'News':<8}"
          f"{'Signal':<14}{'Conf':<7}{'Entry':<10}{'Stop':<10}{'Target':<10}{'R:R':<6}")
    print("-" * 120)
    
    for i, score in enumerate(scores[:top_n], 1):
        entry_str = f"${score.entry_price:.2f}" if score.entry_price else "N/A"
        stop_str = f"${score.stop_price:.2f}" if score.stop_price else "N/A"
        target_str = f"${score.target_price:.2f}" if score.target_price else "N/A"
        rr_str = f"{score.risk_reward_ratio:.1f}" if score.risk_reward_ratio else "N/A"
        
        print(f"{i:<6}{score.symbol:<8}{score.total_score:<8.1f}"
              f"{score.quant_score:<8.1f}{score.news_score:<8.1f}"
              f"{score.unified_signal:<14}{score.confidence:<7.0f}"
              f"{entry_str:<10}{stop_str:<10}{target_str:<10}{rr_str:<6}")
    
    print(f"\n{'='*120}\n")
