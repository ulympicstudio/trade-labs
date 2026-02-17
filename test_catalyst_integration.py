#!/usr/bin/env python3
"""
TEST CATALYST INTEGRATION
Validates catalyst engine working end-to-end
"""

import os
import sys
import logging

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)


def test_catalyst_hunter():
    """Test catalyst hunter with all sources."""
    print("\n" + "="*80)
    print("TEST 1: CATALYST HUNTER - Multi-source discovery".center(80))
    print("="*80)
    
    try:
        from src.data.catalyst_hunter import CatalystHunter
        
        finnhub_key = os.getenv("FINNHUB_API_KEY")
        hunter = CatalystHunter(finnhub_api_key=finnhub_key)
        
        print("\n🔍 Testing Finnhub news...")
        finnhub_catalysts = hunter.hunt_finnhub_news(limit=20)
        print(f"   ✓ Found {len(finnhub_catalysts)} from Finnhub")
        
        print("\n📰 Testing earnings surprises...")
        earnings_catalysts = hunter.hunt_earnings_surprises()
        print(f"   ✓ Found {len(earnings_catalysts)} earnings surprises")
        
        print("\n📈 Testing Yahoo trending...")
        yahoo_catalysts = hunter.hunt_yahoo_trending()
        print(f"   ✓ Found {len(yahoo_catalysts)} trending stocks")
        
        print("\n💬 Testing Reddit mentions...")
        reddit_catalysts = hunter.hunt_reddit_mentions()
        print(f"   ✓ Found {len(reddit_catalysts)} Reddit mentions")
        
        print("\n🤝 Testing insider activity...")
        insider_catalysts = hunter.hunt_insider_activity()
        print(f"   ✓ Found {len(insider_catalysts)} insider trades")
        
        print("\n📊 Testing options unusual...")
        options_catalysts = hunter.hunt_options_unusual()
        print(f"   ✓ Found {len(options_catalysts)} options unusual")
        
        print("\n🚀 Testing full hunt (all sources)...")
        all_catalysts = hunter.hunt_all_sources()
        print(f"   ✓ Found {len(all_catalysts)} total catalyst stocks")
        
        if all_catalysts:
            top_symbols = list(all_catalysts.keys())[:5]
            print(f"\n   Top symbols: {top_symbols}")
            for sym in top_symbols[:2]:
                stock = all_catalysts[sym]
                print(f"   - {sym}: {len(stock.signals)} signals, score={stock.combined_score:.1f}")
        
        return True
        
    except Exception as e:
        print(f"\n❌ Catalyst hunter failed: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_catalyst_scorer():
    """Test catalyst scorer and ranking."""
    print("\n" + "="*80)
    print("TEST 2: CATALYST SCORER - Ranking and validation".center(80))
    print("="*80)
    
    try:
        from src.data.catalyst_hunter import CatalystHunter, CatalystSignal, CatalystStock
        from src.data.catalyst_scorer import CatalystScorer
        
        scorer = CatalystScorer()
        
        # Create mock catalyst stock for testing
        test_stock = CatalystStock(symbol="TEST")
        test_stock.signals = [
            CatalystSignal(
                symbol="TEST",
                catalyst_type="earnings",
                source="finnhub",
                headline="TEST beats expectations",
                confidence=0.95,
                urgency=0.9,
                bullish=True,
                magnitude=1.5,
            ),
            CatalystSignal(
                symbol="TEST",
                catalyst_type="upgrade",
                source="finnhub",
                headline="Analyst upgrades TEST",
                confidence=0.85,
                urgency=0.8,
                bullish=True,
            ),
        ]
        
        # Score it
        score = scorer.score_catalyst_stock("TEST", test_stock)
        
        print(f"\n✓ Scored TEST stock:")
        print(f"  - Catalyst Score: {score.catalyst_score:.1f}/100")
        print(f"  - Technical Score: {score.technical_score:.1f}/100")
        print(f"  - Combined Score: {score.combined_score:.1f}/100")
        print(f"  - Confidence: {score.confidence:.0%}")
        print(f"  - Signals: {score.signal_count}")
        
        # Check if should trade
        should_trade, reason = scorer.should_trade_catalyst(score, min_score=70.0)
        print(f"  - Should trade: {should_trade} ({reason})")
        
        return True
        
    except Exception as e:
        print(f"\n❌ Catalyst scorer failed: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_research_engine():
    """Test research engine orchestration."""
    print("\n" + "="*80)
    print("TEST 3: RESEARCH ENGINE - Orchestration".center(80))
    print("="*80)
    
    try:
        from src.data.research_engine import create_research_engine
        
        print("\n🔧 Creating research engine...")
        finnhub_key = os.getenv("FINNHUB_API_KEY")
        engine = create_research_engine(finnhub_key=finnhub_key)
        
        print("✓ Engine created")
        
        # Run morning research
        print("\n🌅 Running morning research (this may take ~30-60 seconds)...")
        report_data = engine.run_morning_research(output_dir="data/research_reports_test")
        
        print(f"\n✓ Morning research complete:")
        print(f"  - Total catalysts found: {report_data.get('total_catalysts', 0)}")
        print(f"  - Ranked opportunities: {report_data.get('ranked', 0)}")
        print(f"  - Tradeable: {report_data.get('tradeable', 0)}")
        
        # Print candidates
        print("\n📊 Top trading candidates:")
        if engine.ranked_opportunities:
            for i, opp in enumerate(engine.ranked_opportunities[:5], 1):
                print(f"  {i}. {opp.symbol}: {opp.combined_score:.1f}/100 ({opp.signal_count} signals)")
        
        return True
        
    except Exception as e:
        print(f"\n❌ Research engine failed: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_live_loop_integration():
    """Test integration with live loop (without IB connection)."""
    print("\n" + "="*80)
    print("TEST 4: LIVE LOOP INTEGRATION - Syntax check".center(80))
    print("="*80)
    
    try:
        print("\n✓ Checking live_loop_10s imports...")
        # This will fail without IB connection, but we can check syntax
        with open("src/live_loop_10s.py", "r") as f:
            code = f.read()
        
        # Check for key integration points
        checks = [
            ("CATALYST_ENGINE_AVAILABLE", "Catalyst engine availability check"),
            ("ResearchEngine", "Research engine imported"),
            ("catalyst_hunt_interval", "Catalyst hunting loop"),
            ("CATALYST PRIMARY", "Catalyst-first mode indicator"),
            ("catalyst_candidates", "Catalyst candidate caching"),
        ]
        
        for check_str, desc in checks:
            if check_str in code:
                print(f"  ✓ {desc}")
            else:
                print(f"  ✗ {desc} - NOT FOUND")
                return False
        
        print("\n✅ All integration points present!")
        return True
        
    except Exception as e:
        print(f"\n❌ Live loop integration check failed: {e}")
        return False


def main():
    """Run all tests."""
    print("\n" + "="*80)
    print("CATALYST ENGINE - INTEGRATION TEST SUITE".center(80))
    print("="*80)
    
    results = []
    
    # Run tests
    results.append(("Hunter", test_catalyst_hunter()))
    results.append(("Scorer", test_catalyst_scorer()))
    results.append(("Research Engine", test_research_engine()))
    results.append(("Live Loop Integration", test_live_loop_integration()))
    
    # Summary
    print("\n" + "="*80)
    print("TEST SUMMARY".center(80))
    print("="*80)
    
    passed = sum(1 for _, result in results if result)
    total = len(results)
    
    for name, result in results:
        status = "✅ PASS" if result else "❌ FAIL"
        print(f"{name:<30} {status}")
    
    print(f"\nTotal: {passed}/{total} passed")
    
    if passed == total:
        print("\n🎉 ALL TESTS PASSED - Catalyst engine ready!")
        return 0
    else:
        print(f"\n⚠️  {total - passed} test(s) failed - check errors above")
        return 1


if __name__ == "__main__":
    sys.exit(main())
