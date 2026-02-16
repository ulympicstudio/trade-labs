#!/usr/bin/env python3
"""
Test Finnhub API Key
Verifies that your Finnhub API key is valid and working.
"""

import os
import sys
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent))


def test_finnhub_connection():
    """Test Finnhub API key is valid."""
    print("\n" + "="*80)
    print("FINNHUB API KEY TEST")
    print("="*80 + "\n")
    
    # Check environment variable
    api_key = os.environ.get('FINNHUB_API_KEY')
    
    if not api_key:
        print("❌ FINNHUB_API_KEY not set")
        print("\nSet it with:")
        print("  export FINNHUB_API_KEY='your_key_here'")
        print("\nOr add to ~/.zshrc for persistence:")
        print("  echo 'export FINNHUB_API_KEY=\"your_key\"' >> ~/.zshrc")
        return False
    
    print(f"✅ API Key found: {api_key[:8]}...{api_key[-8:]}")
    
    # Test API connection
    print("\nTesting API connection...")
    
    try:
        from src.data.earnings_calendar import EarningsCalendar
        
        calendar = EarningsCalendar(api_key=api_key)
        
        # Try to fetch upcoming earnings
        print("   Fetching upcoming earnings...")
        upcoming = calendar.get_upcoming_earnings(days_ahead=14)
        
        if upcoming:
            print(f"   ✅ Successfully fetched {len(upcoming)} earnings events")
            print(f"\n   Sample earnings (next 14 days):")
            for event in upcoming[:5]:
                print(f"      • {event.symbol} - {event.company_name} - {event.report_date}")
        else:
            print("   ⚠️  No upcoming earnings in next 14 days (may be normal)")
        
        # Test historical earnings
        print("\n   Testing historical earnings for AAPL...")
        historical = calendar.get_historical_earnings('AAPL', limit=4)
        
        if historical:
            print(f"   ✅ Successfully fetched {len(historical)} historical quarters")
            
            # Calculate statistics
            stats = calendar.calculate_earnings_statistics('AAPL')
            print(f"\n   AAPL Earnings Statistics:")
            print(f"      Beat Rate: {stats['beat_rate']:.0f}%")
            print(f"      Avg Surprise: {stats['avg_surprise_pct']:+.2f}%")
            print(f"      Last 4 Quarters: {stats['last_4_beat_rate']:.0f}% beat rate")
        else:
            print("   ⚠️  Could not fetch historical earnings")
        
        print("\n" + "="*80)
        print("✅ FINNHUB API KEY IS VALID AND WORKING")
        print("="*80)
        print("\nEarnings features are now enabled:")
        print("   • Earnings calendar with upcoming reports")
        print("   • Historical beat rate analysis")
        print("   • Consistent beater identification")
        print("   • High-probability earnings plays")
        print("\n")
        
        return True
        
    except Exception as e:
        print(f"\n❌ API test failed: {e}")
        print("\nPossible issues:")
        print("   1. Invalid API key")
        print("   2. API rate limit exceeded (60 calls/minute on free tier)")
        print("   3. Network connectivity issue")
        print("\nGet a valid key at: https://finnhub.io/register")
        return False


def show_usage_examples():
    """Show how to use earnings features."""
    print("\n" + "="*80)
    print("USAGE EXAMPLES")
    print("="*80)
    
    print("\n1. Get Earnings Winners (high beat rate stocks):")
    print("""
from src.data.news_scorer import NewsScorer

scorer = NewsScorer(earnings_api_key=os.environ.get('FINNHUB_API_KEY'))
winners = scorer.get_earnings_winners(
    days_ahead=14,
    min_beat_rate=70.0
)

for winner in winners[:10]:
    print(f"{winner['symbol']}: {winner['beat_rate']:.0f}% beat rate")
    """)
    
    print("\n2. Check Earnings in Hybrid System:")
    print("""
from run_hybrid_trading import HybridTradingSystem

# API key is automatically read from environment
hybrid = HybridTradingSystem(
    ib_connection=ib,
    quant_weight=0.60,
    news_weight=0.40
)

# Earnings data will be included in news scoring
positions = hybrid.run_full_scan()
    """)
    
    print("\n3. Direct Earnings Calendar Access:")
    print("""
from src.data.earnings_calendar import EarningsCalendar
import os

calendar = EarningsCalendar(api_key=os.environ.get('FINNHUB_API_KEY'))

# Upcoming earnings
upcoming = calendar.get_upcoming_earnings(days_ahead=30)

# Find consistent beaters
beaters = calendar.identify_consistent_beaters(upcoming, min_beat_rate=70)

for event in beaters:
    print(f"{event.symbol}: {event.historical_beat_rate:.0f}% beat rate")
    """)


if __name__ == "__main__":
    success = test_finnhub_connection()
    
    if success:
        show_usage_examples()
        sys.exit(0)
    else:
        sys.exit(1)
