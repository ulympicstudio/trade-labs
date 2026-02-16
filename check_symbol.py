"""
Check Symbol Analysis
Simple script to analyze specific stocks for news and sentiment
"""

import os
from src.data.news_scorer import NewsScorer

def main():
    # Initialize news scorer with API key
    api_key = os.environ.get('FINNHUB_API_KEY')
    scorer = NewsScorer(earnings_api_key=api_key)
    
    # ========================================
    # CHANGE THESE SYMBOLS TO WHAT YOU WANT TO CHECK
    # ========================================
    symbols = ['AAPL', 'MSFT', 'NVDA', 'TSLA', 'GOOGL']
    days_back = 7  # Look back 7 days
    # ========================================
    
    print("\n" + "="*80)
    print(f"SYMBOL ANALYSIS REPORT - Last {days_back} Days")
    print("="*80 + "\n")
    
    for symbol in symbols:
        print(f"{'─'*80}")
        print(f"📊 {symbol}")
        print(f"{'─'*80}")
        
        score = scorer.score_symbol(symbol, days_back=days_back)
        
        if not score:
            print(f"❌ No news data available for {symbol}")
            print()
            continue
        
        # News Score
        news_score = score.total_news_score
        if news_score >= 70:
            score_emoji = "🟢"
            score_label = "EXCELLENT"
        elif news_score >= 60:
            score_emoji = "🟡"
            score_label = "GOOD"
        elif news_score >= 50:
            score_emoji = "⚪"
            score_label = "NEUTRAL"
        else:
            score_emoji = "🔴"
            score_label = "WEAK"
        
        print(f"News Score:     {score_emoji} {news_score:.1f}/100 ({score_label})")
        
        # Sentiment
        sentiment = score.avg_sentiment
        if sentiment > 0.2:
            sent_emoji = "🟢"
            sent_label = "VERY POSITIVE"
        elif sentiment > 0.05:
            sent_emoji = "🟡"
            sent_label = "POSITIVE"
        elif sentiment > -0.05:
            sent_emoji = "⚪"
            sent_label = "NEUTRAL"
        else:
            sent_emoji = "🔴"
            sent_label = "NEGATIVE"
        
        print(f"Sentiment:      {sent_emoji} {sentiment:+.3f} ({sent_label})")
        print(f"Signal:         {score.news_signal}")
        print(f"Articles:       {score.article_count} in last {days_back} days")
        
        # Catalyst
        if score.strongest_catalyst:
            print(f"Main Catalyst:  {score.strongest_catalyst}")
        
        # Earnings
        if score.has_upcoming_earnings:
            earnings_emoji = "📅"
            print(f"{earnings_emoji} Earnings:     In {score.days_until_earnings} days")
            if score.historical_beat_rate > 0:
                beat_emoji = "✅" if score.historical_beat_rate >= 70 else "⚪"
                print(f"{beat_emoji} Beat Rate:     {score.historical_beat_rate:.0f}% historically")
        
        # Sample headlines
        if hasattr(score, 'article_headlines') and score.article_headlines:
            print(f"\nRecent Headlines:")
            for i, headline in enumerate(score.article_headlines[:3], 1):
                print(f"  {i}. {headline[:75]}{'...' if len(headline) > 75 else ''}")
        
        print()
    
    print("="*80)
    print("💡 TIP: Use these insights to decide which stocks to scan with hybrid system")
    print("="*80 + "\n")

if __name__ == '__main__':
    main()
