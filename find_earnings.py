"""
Find Earnings Winners
Simple script to find stocks with upcoming earnings and strong historical beat rates
"""

import os
from src.data.news_scorer import NewsScorer

def main():
    # Initialize news scorer with API key
    api_key = os.environ.get('FINNHUB_API_KEY')
    if not api_key:
        print("⚠️  FINNHUB_API_KEY not found in environment")
        print("Run this first:")
        print("  export FINNHUB_API_KEY='YOUR_FINNHUB_API_KEY'")
        return
    
    scorer = NewsScorer(earnings_api_key=api_key)
    
    print("\n" + "="*80)
    print("EARNINGS WINNERS - Next 2 Weeks")
    print("="*80)
    print("Finding stocks with strong earnings history...\n")
    
    # Find stocks with earnings in next 2 weeks + strong history
    winners = scorer.get_earnings_winners(
        days_ahead=14,
        min_beat_rate=70.0  # At least 70% beat rate
    )
    
    if not winners:
        print("No earnings winners found in the next 2 weeks.")
        print("Try adjusting days_ahead or lowering min_beat_rate.")
        return
    
    print(f"Found {len(winners)} stocks with 70%+ beat rate:\n")
    
    # Show top 25
    for i, winner in enumerate(winners[:25], 1):
        symbol = winner['symbol']
        beat_rate = winner['beat_rate']
        days_until = winner['days_until']
        earnings_date = winner['earnings_date']
        
        # Format beat rate with emoji
        if beat_rate >= 85:
            emoji = "🟢"
        elif beat_rate >= 75:
            emoji = "🟡"
        else:
            emoji = "⚪"
        
        print(f"{i:2d}. {emoji} {symbol:6s} | "
              f"Beat Rate: {beat_rate:5.1f}% | "
              f"Earnings in {days_until:2d} days ({earnings_date})")
    
    if len(winners) > 25:
        print(f"\n... and {len(winners) - 25} more")
    
    print("\n" + "="*80)
    print("💡 TIP: Run these symbols through hybrid scanner to see technicals")
    print("="*80 + "\n")

if __name__ == '__main__':
    main()
