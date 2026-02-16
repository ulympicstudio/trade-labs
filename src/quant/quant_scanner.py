"""
Quantitative Market Scanner
Integrates hundreds of metrics to generate high-probability swing trade signals.
Designed to handle 100s of symbols and trades per day.
"""

import logging
from datetime import datetime
from typing import List, Optional
from ib_insync import IB, Stock
from dataclasses import asdict

from src.quant.technical_indicators import TechnicalIndicators, IndicatorResponse
from src.quant.quant_scorer import QuantScorer, QuantScore
from src.signals.market_scanner import scan_us_most_active, get_quote, passes_quality_filters


logger = logging.getLogger(__name__)


class QuantMarketScanner:
    """
    High-frequency quantitative scanner that:
    1. Scans market for liquid symbols
    2. Fetches historical data for each
    3. Calculates hundreds of technical indicators
    4. Generates probability-based scores
    5. Ranks opportunities by total score
    6  Suggests optimal entry/exit/stop levels
    """
    
    def __init__(self, ib: IB, lookback_days: int = 252):
        self.ib = ib
        self.lookback_days = lookback_days
        
        self.tech_indicators = TechnicalIndicators(lookback=lookback_days)
        self.quant_scorer = QuantScorer()
        
        logger.info(f"QuantMarketScanner initialized with {lookback_days}-day lookback")
    
    def fetch_historical_bars(self, symbol: str, duration: str = "1 Y", 
                             bar_size: str = "1 day") -> Optional[dict]:
        """
        Fetch historical OHLCV data from Interactive Brokers.
        Returns dict with lists of highs, lows, closes, volumes.
        """
        try:
            contract = Stock(symbol, "SMART", "USD")
            self.ib.qualifyContracts(contract)
            
            bars = self.ib.reqHistoricalData(
                contract,
                endDateTime="",
                durationStr=duration,
                barSizeSetting=bar_size,
                whatToShow="TRADES",
                useRTH=True,  # Regular trading hours only
                formatDate=1
            )
            
            if not bars:
                logger.warning(f"{symbol}: No historical data available")
                return None
            
            # Convert to lists
            data = {
                "highs": [float(bar.high) for bar in bars],
                "lows": [float(bar.low) for bar in bars],
                "closes": [float(bar.close) for bar in bars],
                "volumes": [float(bar.volume) for bar in bars],
                "dates": [bar.date for bar in bars]
            }
            
            logger.debug(f"{symbol}: Fetched {len(bars)} bars")
            return data
            
        except Exception as e:
            logger.error(f"{symbol}: Failed to fetch historical data - {e}")
            return None
    
    def calculate_indicators_for_symbol(self, symbol: str, 
                                       historical_data: dict,
                                       bid: Optional[float] = None,
                                       ask: Optional[float] = None) -> Optional[IndicatorResponse]:
        """
        Calculate all technical indicators for a symbol using historical data.
        """
        try:
            timestamp = datetime.now().isoformat()
            
            indicators = self.tech_indicators.calculate_all_indicators(
                symbol=symbol,
                timestamp=timestamp,
                highs=historical_data["highs"],
                lows=historical_data["lows"],
                closes=historical_data["closes"],
                volumes=historical_data["volumes"],
                bid=bid,
                ask=ask
            )
            
            logger.debug(f"{symbol}: Calculated {len([f for f in asdict(indicators).values() if f is not None])} indicators")
            return indicators
            
        except Exception as e:
            logger.error(f"{symbol}: Failed to calculate indicators - {e}")
            return None
    
    def score_symbol(self, indicators: IndicatorResponse, current_price: float) -> Optional[QuantScore]:
        """
        Generate quantitative score for a symbol based on all indicators.
        """
        try:
            score = self.quant_scorer.calculate_score(indicators, current_price)
            
            logger.debug(f"{indicators.symbol}: Score={score.total_score:.1f}, "
                        f"Direction={score.direction}, Confidence={score.confidence:.1f}")
            
            return score
            
        except Exception as e:
            logger.error(f"{indicators.symbol}: Failed to calculate score - {e}")
            return None
    
    def scan_and_score(self, candidate_limit: int = 100, 
                      min_score: float = 60.0,
                      min_confidence: float = 50.0) -> List[QuantScore]:
        """
        Full quantitative scan pipeline:
        1. Scan market for active stocks
        2. Filter by quality (price, spread, etc.)
        3. Fetch historical data
        4. Calculate hundreds of indicators
        5. Generate probability scores
        6. Filter and rank opportunities
        
        Returns list of QuantScore objects sorted by total score.
        """
        logger.info(f"Starting quantitative scan (limit={candidate_limit})")
        
        # Step 1: Scan market
        scan_results = scan_us_most_active(self.ib, limit=candidate_limit)
        logger.info(f"Scanned {len(scan_results)} symbols from market")
        
        all_scores = []
        
        # Step 2-6: Process each symbol
        for result in scan_results:
            symbol = result.symbol
            
            try:
                # Get current quote
                bid, ask, last = get_quote(self.ib, symbol)
                
                # Quality filters
                if not passes_quality_filters(symbol, bid, ask, last):
                    logger.debug(f"{symbol}: Failed quality filters")
                    continue
                
                current_price = last if last else (bid + ask) / 2
                
                # Fetch historical data
                historical_data = self.fetch_historical_bars(symbol)
                if not historical_data:
                    continue
                
                # Calculate indicators
                indicators = self.calculate_indicators_for_symbol(
                    symbol, historical_data, bid, ask
                )
                if not indicators:
                    continue
                
                # Generate score
                score = self.score_symbol(indicators, current_price)
                if not score:
                    continue
                
                # Filter by minimum thresholds
                if score.total_score >= min_score and score.confidence >= min_confidence:
                    all_scores.append(score)
                    logger.info(f"{symbol}: ✓ Score={score.total_score:.1f}, "
                              f"Confidence={score.confidence:.1f}, "
                              f"Direction={score.direction}, "
                              f"R:R={score.risk_reward_ratio:.2f}")
                else:
                    logger.debug(f"{symbol}: ✗ Score={score.total_score:.1f} "
                               f"(below threshold)")
                
            except Exception as e:
                logger.error(f"{symbol}: Error during processing - {e}", exc_info=True)
                continue
        
        # Rank by composite score
        ranked_scores = self.quant_scorer.rank_opportunities(all_scores, top_n=50)
        
        logger.info(f"Scan complete: {len(all_scores)} opportunities found, "
                   f"top {len(ranked_scores)} ranked")
        
        return ranked_scores
    
    def display_top_opportunities(self, scores: List[QuantScore], top_n: int = 20):
        """Pretty print top trading opportunities."""
        print(f"\n{'='*100}")
        print(f"TOP {min(top_n, len(scores))} QUANTITATIVE SWING TRADE OPPORTUNITIES")
        print(f"{'='*100}\n")
        
        print(f"{'Rank':<6}{'Symbol':<8}{'Score':<8}{'Conf':<7}{'Dir':<6}{'Entry':<10}"
              f"{'Stop':<10}{'Target':<10}{'R:R':<6}{'Exp%':<8}{'Signals':<30}")
        print("-" * 100)
        
        for i, score in enumerate(scores[:top_n], 1):
            # Truncate signals for display
            signals_str = ", ".join(score.key_signals[:2]) if score.key_signals else "N/A"
            if len(signals_str) > 28:
                signals_str = signals_str[:25] + "..."
            
            print(f"{i:<6}{score.symbol:<8}{score.total_score:<8.1f}"
                  f"{score.confidence:<7.1f}{score.direction:<6}"
                  f"${score.suggested_entry:<9.2f}${score.suggested_stop:<9.2f}"
                  f"${score.suggested_target:<9.2f}{score.risk_reward_ratio:<6.2f}"
                  f"{score.expected_return_pct:>6.2f}%  {signals_str}")
        
        print(f"\n{'='*100}\n")
    
    def get_detailed_report(self, score: QuantScore) -> str:
        """Generate detailed report for a single opportunity."""
        report = []
        report.append(f"\n{'='*80}")
        report.append(f"QUANTITATIVE ANALYSIS: {score.symbol}")
        report.append(f"{'='*80}")
        report.append(f"Timestamp: {score.timestamp}")
        report.append(f"\n--- COMPOSITE SCORES ---")
        report.append(f"Total Score:         {score.total_score:>6.2f} / 100")
        report.append(f"Confidence:          {score.confidence:>6.2f} / 100")
        report.append(f"\n--- COMPONENT BREAKDOWN ---")
        report.append(f"Momentum:            {score.momentum_score:>6.2f} / 100")
        report.append(f"Mean Reversion:      {score.mean_reversion_score:>6.2f} / 100")
        report.append(f"Volatility:          {score.volatility_score:>6.2f} / 100")
        report.append(f"Volume:              {score.volume_score:>6.2f} / 100")
        report.append(f"Microstructure:      {score.microstructure_score:>6.2f} / 100")
        report.append(f"\n--- TRADE RECOMMENDATION ---")
        report.append(f"Direction:           {score.direction}")
        report.append(f"Suggested Entry:     ${score.suggested_entry:>8.2f}")
        report.append(f"Stop Loss:           ${score.suggested_stop:>8.2f}")
        report.append(f"Profit Target:       ${score.suggested_target:>8.2f}")
        report.append(f"Risk:Reward Ratio:   {score.risk_reward_ratio:>8.2f}:1")
        report.append(f"Expected Return:     {score.expected_return_pct:>7.2f}%")
        report.append(f"\n--- KEY SIGNALS ({len(score.key_signals)}) ---")
        for i, signal in enumerate(score.key_signals, 1):
            report.append(f"{i:2}. {signal}")
        report.append(f"{'='*80}\n")
        
        return "\n".join(report)


def run_quant_scan(ib: IB, candidate_limit: int = 100, 
                  min_score: float = 60.0,
                  min_confidence: float = 50.0,
                  display_top_n: int = 20) -> List[QuantScore]:
    """
    Convenience function to run full quantitative scan.
    
    Args:
        ib: Connected IB instance
        candidate_limit: Max symbols to scan
        min_score: Minimum total score threshold (0-100)
        min_confidence: Minimum confidence threshold (0-100)
        display_top_n: Number of top opportunities to display
    
    Returns:
        List of ranked QuantScore objects
    """
    scanner = QuantMarketScanner(ib)
    
    scores = scanner.scan_and_score(
        candidate_limit=candidate_limit,
        min_score=min_score,
        min_confidence=min_confidence
    )
    
    if scores:
        scanner.display_top_opportunities(scores, top_n=display_top_n)
    else:
        logger.warning("No opportunities found meeting criteria")
    
    return scores
