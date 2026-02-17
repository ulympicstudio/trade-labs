"""
RESEARCH ENGINE - Master orchestrator for catalyst discovery
Combines Hunter + Scorer + Technical analysis into actionable pipeline
Generates morning reports and real-time alerts
"""

import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from pathlib import Path
import json

logger = logging.getLogger(__name__)


class ResearchEngine:
    """
    Master research orchestrator.
    Discovers catalysts, scores them, validates with technical, generates reports.
    """
    
    def __init__(self, catalyst_hunter=None, catalyst_scorer=None, quant_scorer=None, db_manager=None):
        """
        Initialize research engine.
        
        Args:
            catalyst_hunter: CatalystHunter instance
            catalyst_scorer: CatalystScorer instance
            quant_scorer: Technical/quant scorer
            db_manager: Database manager for persistence
        """
        self.hunter = catalyst_hunter
        self.scorer = catalyst_scorer
        self.quant_scorer = quant_scorer
        self.db = db_manager
        
        # State
        self.last_hunt_time = None
        self.last_hunt_results = {}
        self.ranked_opportunities = []
    
    def hunt_all_sources(self) -> Dict:
        """Wrapper to hunt all catalyst sources."""
        if not self.hunter:
            logger.error("No catalyst hunter configured")
            return {}
        return self.hunter.hunt_all_sources()
    
    def run_morning_research(self, output_dir: Optional[str] = None) -> Dict:
        """
        Run comprehensive morning catalyst research.
        
        Returns:
            Research summary with top opportunities
        """
        logger.info("🌅 [RESEARCH ENGINE] Starting morning catalyst scan...")
        
        start_time = datetime.now()
        
        # 1. Hunt all sources
        logger.info("  Step 1/4: Hunting all catalyst sources...")
        if not self.hunter:
            logger.error("No catalyst hunter configured")
            return {}
        
        catalysts = self.hunt_all_sources()
        self.last_hunt_results = catalysts
        self.last_hunt_time = start_time
        
        logger.info(f"  ✓ Found {len(catalysts)} catalyst stocks")
        
        # 2. Score opportunities
        logger.info("  Step 2/4: Scoring opportunities...")
        if not self.scorer:
            logger.error("No catalyst scorer configured")
            return {}
        
        # Inject technical scores if available
        if self.quant_scorer:
            self.scorer.quant_scorer = self.quant_scorer
        
        self.ranked_opportunities = self.scorer.rank_opportunities(catalysts, max_results=30)
        logger.info(f"  ✓ Ranked top {len(self.ranked_opportunities)} opportunities")
        
        # 3. Validate against technical
        logger.info("  Step 3/4: Validating with technical analysis...")
        tradeable = []
        
        for opp in self.ranked_opportunities:
            should_trade, reason = self.scorer.should_trade_catalyst(opp, min_score=70.0)
            if should_trade:
                tradeable.append(opp)
            logger.debug(f"  {opp.symbol}: {reason}")
        
        logger.info(f"  ✓ {len(tradeable)} meet trading criteria")
        
        # 4. Generate report
        logger.info("  Step 4/4: Generating morning report...")
        report = self._generate_morning_report(tradeable)
        
        # Save to file
        if output_dir:
            report_path = self._save_report(report, output_dir)
            logger.info(f"  ✓ Report saved: {report_path}")
        
        elapsed = (datetime.now() - start_time).total_seconds()
        logger.info(f"✅ [RESEARCH ENGINE] Morning research complete ({elapsed:.1f}s)")
        
        return {
            "timestamp": start_time.isoformat(),
            "total_catalysts": len(catalysts),
            "ranked": len(self.ranked_opportunities),
            "tradeable": len(tradeable),
            "top_opportunities": tradeable[:10],
            "report": report,
        }
    
    def run_realtime_alert_loop(self, interval_seconds: int = 300, max_iterations: int = None):
        """
        Run continuous real-time alert loop.
        
        Checks for new catalyst signals periodically and alerts on high-quality ones.
        
        Args:
            interval_seconds: Check interval (default 5 minutes)
            max_iterations: Max iterations before stopping (None = infinite)
        """
        
        import time
        iteration = 0
        
        logger.info(f"🔔 [ALERT LOOP] Starting real-time monitoring (every {interval_seconds}s)...")
        
        while max_iterations is None or iteration < max_iterations:
            try:
                iteration += 1
                logger.info(f"\n[Iteration {iteration}] Checking for new catalysts...")
                
                # Hunt for new catalysts
                catalysts = self.hunter.hunt_all_sources()
                
                # Filter to new ones only
                new_catalysts = {
                    k: v for k, v in catalysts.items()
                    if k not in self.last_hunt_results
                }
                
                if new_catalysts:
                    logger.info(f"  🚨 NEW {len(new_catalysts)} new catalyst stocks detected!")
                    
                    # Score new catalysts
                    new_opportunities = self.scorer.rank_opportunities(new_catalysts, max_results=10)
                    
                    for opp in new_opportunities:
                        should_trade, reason = self.scorer.should_trade_catalyst(opp, min_score=75.0)
                        
                        if should_trade:
                            self._alert_opportunity(opp)
                else:
                    logger.info("  No new catalysts (existing only)")
                
                self.last_hunt_results = catalysts
                
                # Wait for next check
                logger.info(f"  Waiting {interval_seconds}s until next check...")
                time.sleep(interval_seconds)
                
            except KeyboardInterrupt:
                logger.info("🛑 Alert loop stopped by user")
                break
            except Exception as e:
                logger.error(f"Alert loop error: {e}", exc_info=True)
                time.sleep(interval_seconds)
    
    def _alert_opportunity(self, opportunity):
        """Alert user about high-quality opportunity."""
        
        logger.warning(
            f"\n{'='*80}\n"
            f"🎯 CATALYST ALERT: {opportunity.symbol}\n"
            f"{'='*80}\n"
            f"  Combined Score: {opportunity.combined_score:.1f}/100\n"
            f"  Catalyst Score: {opportunity.catalyst_score:.1f}/100\n"
            f"  Technical Score: {opportunity.technical_score:.1f}/100\n"
            f"  Signals: {opportunity.signal_count} ({', '.join(opportunity.best_catalyst_types)})\n"
            f"  Confidence: {opportunity.confidence:.0%} | Urgency: {opportunity.urgency:.0%}\n"
            f"  {opportunity.reasoning}\n"
            f"{'='*80}\n"
        )
    
    def _generate_morning_report(self, tradeable_opportunities: List) -> str:
        """Generate formatted morning research report."""
        
        report = []
        
        report.append("="*100)
        report.append("🌅 CATALYST RESEARCH - MORNING REPORT".center(100))
        report.append(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}".center(100))
        report.append("="*100)
        report.append("")
        
        # Summary
        report.append("📊 SUMMARY")
        report.append("-"*100)
        report.append(f"Total sources scanned: 6 (News, Earnings, Trending, Social, Insiders, Options)")
        report.append(f"Total catalyst stocks found: {len(self.last_hunt_results)}")
        report.append(f"Ranked opportunities: {len(self.ranked_opportunities)}")
        report.append(f"Tradeable (score > 70): {len(tradeable_opportunities)}")
        report.append("")
        
        if tradeable_opportunities:
            # Top opportunities
            report.append("🎯 TOP TRADE CANDIDATES")
            report.append("-"*100)
            report.append(f"{'Rank':<6} {'Symbol':<8} {'Catalyst Types':<30} {'Signals':<10} {'Score':<8} {'Confidence':<12} {'Urgency':<10}")
            report.append("-"*100)
            
            for i, opp in enumerate(tradeable_opportunities[:15], 1):
                types = ", ".join(opp.best_catalyst_types[:2])
                report.append(
                    f"{i:<6} "
                    f"{opp.symbol:<8} "
                    f"{types:<30} "
                    f"{opp.signal_count:<10} "
                    f"{opp.combined_score:<8.1f} "
                    f"{opp.confidence:<12.0%} "
                    f"{opp.urgency:<10.0%}"
                )
            
            report.append("")
            
            # Detailed analysis
            report.append("📋 DETAILED ANALYSIS (TOP 5)")
            report.append("-"*100)
            
            for i, opp in enumerate(tradeable_opportunities[:5], 1):
                report.append(f"\n{i}. {opp.symbol.upper()}")
                report.append(f"   {opp.reasoning}")
                report.append(f"   • Catalyst Score: {opp.catalyst_score:.1f}/100")
                report.append(f"   • Technical Score: {opp.technical_score:.1f}/100")
                report.append(f"   • Combined Score: {opp.combined_score:.1f}/100")
                report.append(f"   • Confidence: {opp.confidence:.0%}")
                report.append(f"   • Urgency: {opp.urgency:.0%}")
                report.append(f"   • Expected Move: {opp.magnitude:.1f}x typical ATR")
                report.append(f"   • Signal Types: {', '.join(opp.best_catalyst_types)}")
            
            report.append("")
        
        else:
            report.append("⚠️  NO HIGH-QUALITY CATALYSTS FOUND AT THIS TIME")
            report.append("    Consider widening search criteria or waiting for stronger signals")
            report.append("")
        
        # Sources methodology
        report.append("📡 SOURCES & METHODOLOGY")
        report.append("-"*100)
        report.append("• FINNHUB: News, earnings surprises, company events")
        report.append("• YAHOO: Trending stocks, volume anomalies")
        report.append("• REDDIT: Sentiment & social engagement (r/stocks, r/investing, r/wsb)")
        report.append("• SEC/INSIDER: Executive buying/selling activity")
        report.append("• OPTIONS: Unusual volume & volatility activity")
        report.append("• TECHNICAL: ATR, trend confirmation, volatility profiles")
        report.append("")
        
        report.append("⚙️  WEIGHTING")
        report.append("-"*100)
        report.append("• Catalyst Score: 60% weight (primary signal)")
        report.append("• Technical Score: 40% weight (validation)")
        report.append("")
        
        report.append("="*100)
        
        return "\n".join(report)
    
    def _save_report(self, report: str, output_dir: str) -> str:
        """Save report to file."""
        
        output_path = Path(output_dir)
        output_path.mkdir(parents=True, exist_ok=True)
        
        filename = f"catalyst_research_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
        filepath = output_path / filename
        
        with open(filepath, 'w') as f:
            f.write(report)
        
        return str(filepath)
    
    def get_top_candidates_for_trading(self, max_count: int = 10) -> List:
        """
        Get top ranked candidates ready to trade.
        
        Returns:
            List of CatalystScore objects, ready to pass to execution engine
        """
        
        if not self.ranked_opportunities:
            logger.warning("No opportunities ranked yet - run morning research first")
            return []
        
        tradeable = []
        for opp in self.ranked_opportunities:
            should_trade, _ = self.scorer.should_trade_catalyst(opp, min_score=70.0)
            if should_trade:
                tradeable.append(opp)
        
        return tradeable[:max_count]
    
    def print_trading_candidates(self, max_count: int = 15):
        """Pretty print trading candidates for morning briefing."""
        
        candidates = []
        for opp in self.ranked_opportunities:
            should_trade, _ = self.scorer.should_trade_catalyst(opp, min_score=70.0)
            if should_trade:
                candidates.append(opp)
        
        if not candidates:
            print("⚠️  No trading candidates identified")
            return
        
        print("\n" + "="*100)
        print("📈 TRADING CANDIDATES - READY TO RESEARCH & EXECUTE".center(100))
        print("="*100)
        
        for i, opp in enumerate(candidates[:max_count], 1):
            print(
                f"\n{i}. {opp.symbol.upper():<8} | "
                f"Score: {opp.combined_score:.1f} | "
                f"Catalyst: {opp.catalyst_score:.1f} | "
                f"Technical: {opp.technical_score:.1f} | "
                f"Confidence: {opp.confidence:.0%}"
            )
            print(f"   {opp.reasoning}")
            print(f"   Signals: {opp.signal_count} | Types: {', '.join(opp.best_catalyst_types)}")
        
        print("\n" + "="*100 + "\n")


# Quick integration helper
def create_research_engine(finnhub_key: Optional[str] = None, quant_scorer=None):
    """Factory to create a fully configured research engine."""
    
    from src.data.catalyst_hunter import CatalystHunter
    from src.data.catalyst_scorer import CatalystScorer
    
    hunter = CatalystHunter(finnhub_api_key=finnhub_key)
    scorer = CatalystScorer(quant_scorer=quant_scorer)
    
    engine = ResearchEngine(
        catalyst_hunter=hunter,
        catalyst_scorer=scorer,
        quant_scorer=quant_scorer,
    )
    
    return engine
