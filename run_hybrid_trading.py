#!/usr/bin/env python3
"""
Trade Labs - Hybrid Trading System
Combines News + Quantitative Analysis for high-probability swing trades.

Workflow:
1. NEWS ARM: Discovers trending stocks with positive catalysts
2. QUANT ARM: Validates technicals (50+ indicators)
3. UNIFIED SCORING: Combines news (40%) + quant (60%)
4. PORTFOLIO MANAGER: Allocates capital across approved positions
5. EXECUTION: Ready to submit orders (manual review recommended)

Human Operator: Ulympic
Machine: Ulympic Studio (Mac Studio)
AI System: Studio
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

import logging
from datetime import datetime
from typing import List, Dict
from ib_insync import IB, util

# Import system components
from src.data.news_scorer import NewsScorer, display_news_scores
from src.data.quant_news_integrator import QuantNewsIntegrator, display_unified_scores
from src.quant.quant_scorer import QuantScorer
from src.quant.quant_scanner import QuantMarketScanner
from src.quant.portfolio_risk_manager import PortfolioRiskManager


# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

logger = logging.getLogger(__name__)


class HybridTradingSystem:
    """
    Hybrid trading system combining news sentiment + quantitative analysis.
    
    Architecture:
    - News Arm (40%): Discovers trending stocks with positive catalysts
    - Quant Arm (60%): Validates with 50+ technical indicators
    - Portfolio Manager: Risk controls for 100 simultaneous positions
    """
    
    def __init__(self, ib_connection: IB,
                 quant_weight: float = 0.60,
                 news_weight: float = 0.40,
                 total_capital: float = 100000.0,
                 finnhub_api_key: str = None):
        """
        Initialize hybrid system.
        
        Args:
            ib_connection: Connected IB instance
            quant_weight: Weight for technical analysis (default 60%)
            news_weight: Weight for news sentiment (default 40%)
            total_capital: Total trading capital
            finnhub_api_key: Optional Finnhub API key (reads from env if not provided)
        """
        self.ib = ib_connection
        
        # Get Finnhub API key from environment if not provided
        if finnhub_api_key is None:
            import os
            finnhub_api_key = os.environ.get('FINNHUB_API_KEY')
        
        # Initialize components
        self.news_scorer = NewsScorer(earnings_api_key=finnhub_api_key)
        self.integrator = QuantNewsIntegrator(
            quant_weight=quant_weight,
            news_weight=news_weight,
            earnings_api_key=finnhub_api_key
        )
        self.quant_scanner = QuantMarketScanner(ib=self.ib)
        self.portfolio_manager = PortfolioRiskManager(
            total_capital=total_capital,
            max_positions=100,
            max_risk_per_trade_pct=1.0,
            max_total_risk_pct=20.0
        )
        
        logger.info(f"HybridTradingSystem initialized (Capital: ${total_capital:,.0f})")
        logger.info(f"Scoring weights: Quant {quant_weight:.0%}, News {news_weight:.0%}")
    
    def discover_news_candidates(self, min_news_score: float = 60.0,
                                  days_back: int = 3,
                                  max_candidates: int = 50) -> List[str]:
        """
        Phase 1: News arm discovers trending stocks with positive catalysts.
        
        Args:
            min_news_score: Minimum news score (0-100)
            days_back: Days to look back for news
            max_candidates: Maximum symbols to return
        
        Returns:
            List of ticker symbols with strong news
        """
        print("\n" + "="*100)
        print("PHASE 1: NEWS DISCOVERY")
        print("="*100)
        print(f"Scanning for trending stocks with positive news (min score: {min_news_score})")
        print(f"Looking back: {days_back} days\n")
        
        # Get top news-driven opportunities
        news_opportunities = self.news_scorer.get_top_news_driven_opportunities(
            min_score=min_news_score,
            days_back=days_back
        )
        
        if not news_opportunities:
            logger.warning("No news-driven candidates found")
            return []
        
        # Display news scores
        display_news_scores(news_opportunities, top_n=min(20, len(news_opportunities)))
        
        # Extract symbols
        symbols = [opp.symbol for opp in news_opportunities[:max_candidates]]
        
        print(f"\n✅ Discovered {len(symbols)} news-driven candidates")
        print(f"Top symbols: {', '.join(symbols[:10])}\n")
        
        return symbols
    
    def validate_with_quant(self, symbols: List[str],
                            min_quant_score: float = 55.0) -> Dict:
        """
        Phase 2: Quant arm validates technical analysis for news candidates.
        
        Args:
            symbols: List of symbols to analyze
            min_quant_score: Minimum quant score (0-100)
        
        Returns:
            Dict mapping symbol -> unified score object
        """
        print("\n" + "="*100)
        print("PHASE 2: QUANTITATIVE VALIDATION")
        print("="*100)
        print(f"Validating {len(symbols)} symbols with technical analysis")
        print(f"Min quant score: {min_quant_score}\n")
        
        # Fetch historical data
        print("Fetching historical data from IB...")
        historical_data_map = self.quant_scanner.fetch_historical_data(symbols)
        
        print(f"Retrieved data for {len(historical_data_map)} symbols\n")
        
        # Score with unified system
        validated = {}
        
        for symbol in symbols:
            hist_data = historical_data_map.get(symbol)
            if not hist_data:
                continue
            
            # Get unified score (news + quant)
            score = self.integrator.score_symbol(
                symbol,
                hist_data,
                news_days_back=7
            )
            
            if score and score.quant_score >= min_quant_score:
                validated[symbol] = score
        
        print(f"✅ {len(validated)} symbols passed quant validation\n")
        
        return validated
    
    def create_unified_opportunities(self, validated_scores: Dict) -> List:
        """
        Phase 3: Create unified opportunity list ranked by combined score.
        
        Args:
            validated_scores: Dict of symbol -> UnifiedScore
        
        Returns:
            List of UnifiedScore objects, sorted by total score
        """
        print("\n" + "="*100)
        print("PHASE 3: UNIFIED SCORING & RANKING")
        print("="*100)
        
        opportunities = list(validated_scores.values())
        
        # Sort by total score
        opportunities.sort(key=lambda s: s.total_score, reverse=True)
        
        # Display unified scores
        display_unified_scores(opportunities, top_n=min(25, len(opportunities)))
        
        # Show breakdown of top opportunity
        if opportunities:
            top = opportunities[0]
            print(f"\n{'='*100}")
            print(f"TOP OPPORTUNITY BREAKDOWN: {top.symbol}")
            print(f"{'='*100}")
            print(f"  Total Score: {top.total_score:.1f}/100")
            print(f"  └─ Quant Score: {top.quant_score:.1f}/100 (weight: 60%)")
            print(f"  └─ News Score: {top.news_score:.1f}/100 (weight: 40%)")
            print(f"\n  Quant Components:")
            if top.momentum_score:
                print(f"    • Momentum: {top.momentum_score:.1f}/100")
            if top.mean_reversion_score:
                print(f"    • Mean Reversion: {top.mean_reversion_score:.1f}/100")
            if top.volatility_score:
                print(f"    • Volatility: {top.volatility_score:.1f}/100")
            print(f"\n  News Components:")
            if top.sentiment_score:
                print(f"    • Sentiment: {top.sentiment_score:.1f}/100")
            if top.catalyst_score:
                print(f"    • Catalyst: {top.catalyst_score:.1f}/100")
            print(f"\n  Signal Analysis:")
            print(f"    • Unified Signal: {top.unified_signal}")
            print(f"    • Quant Signal: {top.quant_signal}")
            print(f"    • News Signal: {top.news_signal}")
            print(f"    • Confidence: {top.confidence:.0f}%")
            
            if top.entry_price:
                risk = top.entry_price - top.stop_price
                reward = top.target_price - top.entry_price
                print(f"\n  Trading Plan:")
                print(f"    • Entry: ${top.entry_price:.2f}")
                print(f"    • Stop: ${top.stop_price:.2f} (risk: ${risk:.2f})")
                print(f"    • Target: ${top.target_price:.2f} (reward: ${reward:.2f})")
                print(f"    • Risk:Reward = 1:{top.risk_reward_ratio:.1f}")
            print(f"{'='*100}\n")
        
        return opportunities
    
    def allocate_portfolio(self, opportunities: List,
                           min_unified_score: float = 65.0,
                           min_confidence: float = 60.0,
                           max_positions: int = 50) -> List:
        """
        Phase 4: Portfolio manager allocates capital with risk controls.
        
        Args:
            opportunities: List of UnifiedScore objects
            min_unified_score: Minimum combined score
            min_confidence: Minimum confidence level
            max_positions: Maximum concurrent positions
        
        Returns:
            List of approved positions with allocated capital
        """
        print("\n" + "="*100)
        print("PHASE 4: PORTFOLIO ALLOCATION")
        print("="*100)
        print(f"Applying risk controls (min score: {min_unified_score}, min confidence: {min_confidence})")
        print(f"Max positions: {max_positions}\n")
        
        # Filter by score and confidence
        filtered = [
            opp for opp in opportunities
            if opp.total_score >= min_unified_score
            and opp.confidence >= min_confidence
        ]
        
        print(f"Filtered to {len(filtered)} high-quality opportunities\n")
        
        if not filtered:
            logger.warning("No opportunities met filtering criteria")
            return []
        
        # Convert to format for portfolio manager
        opportunity_dicts = []
        for opp in filtered[:max_positions]:
            opportunity_dicts.append({
                'symbol': opp.symbol,
                'score': opp.total_score,
                'confidence': opp.confidence,
                'entry_price': opp.entry_price,
                'stop_price': opp.stop_price,
                'target_price': opp.target_price,
                'signal': opp.unified_signal
            })
        
        # Allocate with portfolio manager
        approved_positions = self.portfolio_manager.prioritize_opportunities(opportunity_dicts)
        
        # Display allocation summary
        self._display_allocation_summary(approved_positions)
        
        return approved_positions
    
    def _display_allocation_summary(self, positions: List[Dict]):
        """Display portfolio allocation summary."""
        if not positions:
            print("⚠️  No positions approved by portfolio manager\n")
            return
        
        total_capital_used = sum(p['position_size_dollars'] for p in positions)
        total_risk = sum(p['risk_dollars'] for p in positions)
        
        print(f"\n{'='*100}")
        print(f"PORTFOLIO ALLOCATION SUMMARY")
        print(f"{'='*100}\n")
        print(f"Total Capital: ${self.portfolio_manager.total_capital:,.0f}")
        print(f"Capital Allocated: ${total_capital_used:,.0f} ({total_capital_used/self.portfolio_manager.total_capital*100:.1f}%)")
        print(f"Total Risk: ${total_risk:,.0f} ({total_risk/self.portfolio_manager.total_capital*100:.1f}%)")
        print(f"Approved Positions: {len(positions)}\n")
        
        print(f"{'Rank':<6}{'Symbol':<8}{'Score':<8}{'Conf':<7}{'Signal':<14}"
              f"{'Shares':<8}{'Capital':<12}{'Risk':<10}{'R:R':<6}")
        print("-" * 100)
        
        for i, pos in enumerate(positions, 1):
            signal = pos.get('signal', 'N/A')
            rr = pos.get('risk_reward_ratio', 0)
            
            print(f"{i:<6}{pos['symbol']:<8}{pos['score']:<8.1f}{pos['confidence']:<7.0f}"
                  f"{signal:<14}{pos['shares']:<8}"
                  f"${pos['position_size_dollars']:<11,.0f}"
                  f"${pos['risk_dollars']:<9,.0f}{rr:<6.1f}")
        
        print(f"\n{'='*100}\n")
    
    def run_full_scan(self,
                     min_news_score: float = 60.0,
                     min_quant_score: float = 55.0,
                     min_unified_score: float = 65.0,
                     min_confidence: float = 60.0,
                     news_days_back: int = 3,
                     max_positions: int = 50) -> List[Dict]:
        """
        Execute complete hybrid trading workflow.
        
        Args:
            min_news_score: Minimum news score for discovery
            min_quant_score: Minimum quant score for validation
            min_unified_score: Minimum combined score for portfolio
            min_confidence: Minimum confidence for portfolio
            news_days_back: Days to look back for news
            max_positions: Maximum concurrent positions
        
        Returns:
            List of approved positions ready for execution
        """
        print("\n" + "="*100)
        print("TRADE LABS - HYBRID TRADING SYSTEM")
        print(f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"Operator: Ulympic | Machine: Ulympic Studio | AI: Studio")
        print("="*100)
        
        try:
            # Phase 1: News Discovery
            candidates = self.discover_news_candidates(
                min_news_score=min_news_score,
                days_back=news_days_back,
                max_candidates=50
            )
            
            if not candidates:
                print("\n❌ No news-driven candidates found. Try lowering min_news_score or extending days_back.\n")
                return []
            
            # Phase 2: Quant Validation
            validated = self.validate_with_quant(
                candidates,
                min_quant_score=min_quant_score
            )
            
            if not validated:
                print("\n❌ No candidates passed quant validation. Try lowering min_quant_score.\n")
                return []
            
            # Phase 3: Unified Scoring
            opportunities = self.create_unified_opportunities(validated)
            
            # Phase 4: Portfolio Allocation
            approved_positions = self.allocate_portfolio(
                opportunities,
                min_unified_score=min_unified_score,
                min_confidence=min_confidence,
                max_positions=max_positions
            )
            
            # Final Summary
            print("\n" + "="*100)
            print("EXECUTION READY")
            print("="*100)
            
            if approved_positions:
                print(f"\n✅ {len(approved_positions)} positions approved and ready for execution")
                print(f"\nNext steps:")
                print(f"  1. Review positions above")
                print(f"  2. Adjust parameters if needed")
                print(f"  3. Execute via: src/execution/pipeline.py")
                print(f"  4. Monitor positions with real-time P&L tracking")
            else:
                print(f"\n⚠️  No positions approved by portfolio manager")
                print(f"\nTry adjusting parameters:")
                print(f"  - Lower min_unified_score (currently {min_unified_score})")
                print(f"  - Lower min_confidence (currently {min_confidence})")
                print(f"  - Increase news_days_back (currently {news_days_back})")
            
            print(f"\n{'='*100}\n")
            
            return approved_positions
            
        except Exception as e:
            logger.error(f"Hybrid scan failed: {e}", exc_info=True)
            print(f"\n❌ Error during hybrid scan: {e}\n")
            return []


def main():
    """Main entry point for hybrid trading system."""
    
    # Configuration
    IB_HOST = "127.0.0.1"
    IB_PORT = 7497  # TWS paper trading
    CLIENT_ID = 1
    TOTAL_CAPITAL = 100000.0
    
    # Hybrid system parameters
    QUANT_WEIGHT = 0.60  # 60% technical analysis
    NEWS_WEIGHT = 0.40   # 40% news sentiment
    
    # Filtering parameters
    MIN_NEWS_SCORE = 60.0      # News discovery threshold
    MIN_QUANT_SCORE = 55.0     # Quant validation threshold
    MIN_UNIFIED_SCORE = 65.0   # Portfolio allocation threshold
    MIN_CONFIDENCE = 60.0      # Minimum confidence level
    NEWS_DAYS_BACK = 3         # Look back 3 days for news
    MAX_POSITIONS = 50         # Maximum concurrent positions
    
    print("\n" + "="*100)
    print("INITIALIZING HYBRID TRADING SYSTEM")
    print("="*100)
    print(f"\nConnecting to Interactive Brokers...")
    print(f"  Host: {IB_HOST}:{IB_PORT}")
    print(f"  Client ID: {CLIENT_ID}\n")
    
    # Connect to IB
    ib = IB()
    
    try:
        ib.connect(IB_HOST, IB_PORT, clientId=CLIENT_ID)
        print("✅ Connected to IB successfully\n")
        
        # Initialize hybrid system
        hybrid_system = HybridTradingSystem(
            ib_connection=ib,
            quant_weight=QUANT_WEIGHT,
            news_weight=NEWS_WEIGHT,
            total_capital=TOTAL_CAPITAL
        )
        
        # Run full hybrid scan
        approved_positions = hybrid_system.run_full_scan(
            min_news_score=MIN_NEWS_SCORE,
            min_quant_score=MIN_QUANT_SCORE,
            min_unified_score=MIN_UNIFIED_SCORE,
            min_confidence=MIN_CONFIDENCE,
            news_days_back=NEWS_DAYS_BACK,
            max_positions=MAX_POSITIONS
        )
        
        # Optionally save results
        if approved_positions:
            import json
            output_file = f"hybrid_scan_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
            with open(output_file, 'w') as f:
                json.dump(approved_positions, f, indent=2)
            print(f"💾 Results saved to: {output_file}\n")
        
    except ConnectionRefusedError:
        print("\n❌ Unable to connect to IB. Please ensure TWS/IB Gateway is running.\n")
    except Exception as e:
        print(f"\n❌ Error: {e}\n")
        import traceback
        traceback.print_exc()
    finally:
        if ib.isConnected():
            ib.disconnect()
            print("Disconnected from IB\n")


if __name__ == "__main__":
    main()
