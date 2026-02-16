"""
Test script for News + Quant Integration System
Tests news fetching, sentiment analysis, and unified scoring.
"""

import sys
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent))

import logging
from datetime import datetime

from src.data.news_fetcher import NewsFetcher
from src.data.news_sentiment import NewsSentimentAnalyzer
from src.data.news_scorer import NewsScorer, display_news_scores
from src.data.quant_news_integrator import QuantNewsIntegrator, display_unified_scores
from src.quant.quant_scanner import QuantMarketScanner


# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

logger = logging.getLogger(__name__)


def test_news_fetcher():
    """Test 1: News fetching from Google News RSS."""
    print("\n" + "="*100)
    print("TEST 1: NEWS FETCHER")
    print("="*100)
    
    fetcher = NewsFetcher()
    
    # Test 1a: Fetch trending stocks
    print("\n1a. Fetching trending stocks...")
    trending = fetcher.get_most_talked_about_stocks(min_articles=2, days_back=1)
    
    print(f"\nFound {len(trending)} trending stocks:")
    for i, stock in enumerate(trending[:10], 1):
        print(f"  {i}. {stock['symbol']}: {stock['article_count']} articles")
    
    # Test 1b: Fetch news for specific symbol
    if trending:
        test_symbol = trending[0]['symbol']
        print(f"\n1b. Fetching news for {test_symbol}...")
        articles = fetcher.fetch_news_for_symbol(test_symbol, days_back=3)
        
        print(f"\nFound {len(articles)} articles for {test_symbol}:")
        for i, article in enumerate(articles[:5], 1):
            print(f"  {i}. {article.title[:80]}")
            print(f"     Source: {article.source}, Published: {article.published_date}")
    
    print("\n✅ News Fetcher test complete\n")


def test_sentiment_analyzer():
    """Test 2: Sentiment analysis."""
    print("\n" + "="*100)
    print("TEST 2: SENTIMENT ANALYZER")
    print("="*100)
    
    fetcher = NewsFetcher()
    analyzer = NewsSentimentAnalyzer()
    
    # Get some articles
    print("\n2a. Fetching articles for sentiment analysis...")
    trending = fetcher.get_most_talked_about_stocks(min_articles=2, days_back=1)
    
    if not trending:
        print("⚠️  No trending stocks found, skipping sentiment test")
        return
    
    test_symbol = trending[0]['symbol']
    articles = fetcher.fetch_news_for_symbol(test_symbol, days_back=3)
    
    if not articles:
        print(f"⚠️  No articles found for {test_symbol}, skipping sentiment test")
        return
    
    print(f"\n2b. Analyzing sentiment for {len(articles)} articles on {test_symbol}...")
    sentiments = analyzer.analyze_articles(articles)
    
    # Show individual sentiments
    print(f"\nIndividual article sentiments:")
    for i, (article, sentiment) in enumerate(zip(articles[:5], sentiments[:5]), 1):
        print(f"  {i}. Score: {sentiment.sentiment_score:+.3f}, Label: {sentiment.sentiment_label}, Confidence: {sentiment.confidence:.0%}")
        print(f"     Title: {article.title[:80]}")
    
    # Aggregate sentiment
    agg_sentiment = analyzer.get_aggregate_sentiment(articles)
    print(f"\nAggregate sentiment for {test_symbol}:")
    print(f"  Average sentiment: {agg_sentiment['avg_sentiment']:+.3f}")
    print(f"  Positive ratio: {agg_sentiment['positive_ratio']:.0%}")
    print(f"  Positive: {agg_sentiment['positive_count']}, "
          f"Neutral: {agg_sentiment['neutral_count']}, "
          f"Negative: {agg_sentiment['negative_count']}")
    
    print("\n✅ Sentiment Analyzer test complete\n")


def test_news_scorer():
    """Test 3: News scoring system."""
    print("\n" + "="*100)
    print("TEST 3: NEWS SCORER")
    print("="*100)
    
    scorer = NewsScorer()
    
    print("\n3a. Finding top news-driven opportunities...")
    opportunities = scorer.get_top_news_driven_opportunities(min_score=55.0, days_back=2)
    
    if opportunities:
        print(f"\nFound {len(opportunities)} opportunities with score >= 55")
        display_news_scores(opportunities, top_n=10)
    else:
        print("⚠️  No high-scoring opportunities found")
    
    print("\n✅ News Scorer test complete\n")


def test_unified_integration():
    """Test 4: Quant + News unified scoring."""
    print("\n" + "="*100)
    print("TEST 4: UNIFIED QUANT + NEWS INTEGRATION")
    print("="*100)
    
    # Initialize integrator (60% quant, 40% news)
    integrator = QuantNewsIntegrator(quant_weight=0.60, news_weight=0.40)
    
    print("\n4a. Finding best opportunities combining quant + news...")
    print("     (This may take 1-2 minutes as it analyzes technicals + news)\n")
    
    try:
        opportunities = integrator.get_best_opportunities(
            min_quant_score=50.0,
            min_news_score=55.0,
            news_days_back=2,
            top_n=15
        )
        
        if opportunities:
            print(f"\nFound {len(opportunities)} unified opportunities")
            display_unified_scores(opportunities, top_n=15)
            
            # Show detailed breakdown for top opportunity
            if opportunities:
                top = opportunities[0]
                print(f"\nDETAILED BREAKDOWN - Top Opportunity: {top.symbol}")
                print("-" * 80)
                print(f"  Total Score: {top.total_score:.1f}/100")
                print(f"  Quant Score: {top.quant_score:.1f}/100 (weight: 60%)")
                print(f"  News Score: {top.news_score:.1f}/100 (weight: 40%)")
                print(f"\n  Quant Components:")
                if top.momentum_score:
                    print(f"    - Momentum: {top.momentum_score:.1f}/100")
                if top.mean_reversion_score:
                    print(f"    - Mean Reversion: {top.mean_reversion_score:.1f}/100")
                if top.volatility_score:
                    print(f"    - Volatility: {top.volatility_score:.1f}/100")
                print(f"\n  News Components:")
                if top.sentiment_score:
                    print(f"    - Sentiment: {top.sentiment_score:.1f}/100")
                if top.catalyst_score:
                    print(f"    - Catalyst: {top.catalyst_score:.1f}/100")
                print(f"\n  Signal Analysis:")
                print(f"    - Unified Signal: {top.unified_signal}")
                print(f"    - Quant Signal: {top.quant_signal}")
                print(f"    - News Signal: {top.news_signal}")
                print(f"    - Confidence: {top.confidence:.0f}%")
                
                if top.entry_price:
                    print(f"\n  Trading Plan:")
                    print(f"    - Entry: ${top.entry_price:.2f}")
                    print(f"    - Stop: ${top.stop_price:.2f}")
                    print(f"    - Target: ${top.target_price:.2f}")
                    print(f"    - Risk:Reward = 1:{top.risk_reward_ratio:.1f}")
                
        else:
            print("⚠️  No unified opportunities found meeting criteria")
    
    except Exception as e:
        print(f"❌ Error during unified integration: {e}")
        import traceback
        traceback.print_exc()
    
    print("\n✅ Unified Integration test complete\n")


def test_manual_symbols():
    """Test 5: Test with specific symbols (AAPL, NVDA, TSLA)."""
    print("\n" + "="*100)
    print("TEST 5: MANUAL SYMBOL SCORING (AAPL, NVDA, TSLA)")
    print("="*100)
    
    symbols = ['AAPL', 'NVDA', 'TSLA']
    
    # News scoring
    news_scorer = NewsScorer()
    
    print("\n5a. News scores for manual symbols:")
    news_scores = []
    for symbol in symbols:
        score = news_scorer.score_symbol(symbol, days_back=7)
        if score:
            news_scores.append(score)
            print(f"\n  {symbol}:")
            print(f"    News Score: {score.total_news_score:.1f}/100")
            print(f"    Sentiment: {score.avg_sentiment:+.3f}")
            print(f"    Articles: {score.article_count}")
            print(f"    Signal: {score.news_signal}")
            if score.strongest_catalyst:
                print(f"    Catalyst: {score.strongest_catalyst}")
        else:
            print(f"\n  {symbol}: No news data")
    
    if news_scores:
        print("\n5b. News scores summary:")
        display_news_scores(news_scores, top_n=len(news_scores))
    
    print("\n✅ Manual Symbol test complete\n")


def main():
    """Run all tests."""
    print("\n" + "="*100)
    print("NEWS + QUANT INTEGRATION TEST SUITE")
    print(f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("="*100)
    
    try:
        # Test 1: News Fetcher
        test_news_fetcher()
        
        # Test 2: Sentiment Analysis
        test_sentiment_analyzer()
        
        # Test 3: News Scorer
        test_news_scorer()
        
        # Test 4: Unified Integration
        test_unified_integration()
        
        # Test 5: Manual symbols
        test_manual_symbols()
        
        print("\n" + "="*100)
        print("✅ ALL TESTS COMPLETE")
        print("="*100 + "\n")
        
    except KeyboardInterrupt:
        print("\n\n⚠️  Tests interrupted by user\n")
    except Exception as e:
        print(f"\n\n❌ Test suite failed: {e}\n")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    main()
