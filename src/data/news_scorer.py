"""
News-Based Scoring System
Integrates news sentiment and earnings catalysts with quantitative scoring.
"""

import logging
from dataclasses import dataclass
from typing import List, Optional, Dict
from datetime import datetime

from src.data.news_fetcher import NewsArticle, NewsFetcher
from src.data.news_sentiment import NewsSentimentAnalyzer, classify_news_catalyst
from src.data.earnings_calendar import EarningsCalendar


logger = logging.getLogger(__name__)


@dataclass
class NewsScore:
    """News-based scoring for a symbol."""
    symbol: str
    timestamp: str
    
    # Composite news score (0-100)
    total_news_score: float
    
    # Component scores
    sentiment_score: float  # Based on article sentiment (0-100)
    catalyst_score: float  # Strength of catalysts (0-100)
    volume_score: float  # News volume/buzz (0-100)
    earnings_score: float  # Earnings potential (0-100)
    
    # News metrics
    article_count: int
    positive_article_count: int
    avg_sentiment: float  # -1 to +1
    
    # Catalysts identified
    has_catalyst: bool
    catalyst_types: List[str]
    strongest_catalyst: Optional[str] = None
    
    # Earnings info
    has_upcoming_earnings: bool = False
    days_until_earnings: Optional[int] = None
    historical_beat_rate: Optional[float] = None
    
    # Recommendation
    news_signal: str = "NEUTRAL"  # "STRONG_BUY", "BUY", "NEUTRAL", "AVOID"
    confidence: float = 0.0


class NewsScorer:
    """
    Scores stocks based on news sentiment and catalysts.
    Designed to work alongside the quant scoring system.
    """
    
    def __init__(self, earnings_api_key: Optional[str] = None):
        self.news_fetcher = NewsFetcher()
        self.sentiment_analyzer = NewsSentimentAnalyzer()
        self.earnings_calendar = EarningsCalendar(api_key=earnings_api_key)
        
        logger.info("NewsScorer initialized")
    
    def score_symbol(self, symbol: str, days_back: int = 7) -> Optional[NewsScore]:
        """
        Generate news-based score for a symbol.
        
        Args:
            symbol: Stock ticker
            days_back: Days of news to analyze
        
        Returns:
            NewsScore object or None
        """
        try:
            # Fetch news articles
            articles = self.news_fetcher.fetch_news_for_symbol(symbol, days_back)
            
            if not articles:
                logger.debug(f"{symbol}: No news articles found")
                return None
            
            # Analyze sentiment
            sentiments = self.sentiment_analyzer.analyze_articles(articles)
            
            # Aggregate sentiment metrics
            agg_sentiment = self.sentiment_analyzer.get_aggregate_sentiment(articles)
            
            # Identify catalysts
            catalysts = [
                classify_news_catalyst(article, sentiment)
                for article, sentiment in zip(articles, sentiments)
            ]
            
            positive_catalysts = [c for c in catalysts if c['is_catalyst'] and c['expected_impact'] in ['positive', 'strong_positive']]
            
            # Earnings analysis
            earnings_score, earnings_info = self._analyze_earnings(symbol)
            
            # Calculate component scores
            sentiment_score = self._calculate_sentiment_score(agg_sentiment)
            catalyst_score = self._calculate_catalyst_score(positive_catalysts)
            volume_score = self._calculate_volume_score(len(articles), days_back)
            
            # Weighted total score
            total_score = (
                sentiment_score * 0.35 +
                catalyst_score * 0.30 +
                volume_score * 0.15 +
                earnings_score * 0.20
            )
            
            # Determine signal
            signal = self._determine_signal(total_score, sentiment_score, catalyst_score)
            
            # Confidence based on article count and sentiment agreement
            confidence = self._calculate_confidence(articles, agg_sentiment)
            
            # Extract catalyst types
            catalyst_types = list(set([c['catalyst_type'] for c in positive_catalysts]))
            strongest = self._get_strongest_catalyst(positive_catalysts)
            
            return NewsScore(
                symbol=symbol,
                timestamp=datetime.now().isoformat(),
                total_news_score=round(total_score, 2),
                sentiment_score=round(sentiment_score, 2),
                catalyst_score=round(catalyst_score, 2),
                volume_score=round(volume_score, 2),
                earnings_score=round(earnings_score, 2),
                article_count=len(articles),
                positive_article_count=agg_sentiment['positive_count'],
                avg_sentiment=round(agg_sentiment['avg_sentiment'], 3),
                has_catalyst=len(positive_catalysts) > 0,
                catalyst_types=catalyst_types,
                strongest_catalyst=strongest,
                has_upcoming_earnings=earnings_info['has_upcoming'],
                days_until_earnings=earnings_info['days_until'],
                historical_beat_rate=earnings_info['beat_rate'],
                news_signal=signal,
                confidence=round(confidence, 2)
            )
            
        except Exception as e:
            logger.error(f"{symbol}: Failed to score news - {e}", exc_info=True)
            return None
    
    def score_symbols(self, symbols: List[str], days_back: int = 7) -> List[NewsScore]:
        """Score multiple symbols and return sorted by total score."""
        scores = []
        
        for symbol in symbols:
            score = self.score_symbol(symbol, days_back)
            if score and score.total_news_score > 50:  # Filter low scores
                scores.append(score)
        
        # Sort by total score (descending)
        scores.sort(key=lambda s: s.total_news_score, reverse=True)
        
        logger.info(f"Scored {len(scores)} symbols with news data")
        
        return scores
    
    def get_top_news_driven_opportunities(self, min_score: float = 65.0, days_back: int = 3) -> List[NewsScore]:
        """
        Get top opportunities driven by positive news.
        
        Args:
            min_score: Minimum total news score
            days_back: Days to look back for news
        
        Returns:
            List of NewsScore objects ranked by score
        """
        # Get most talked about stocks
        trending = self.news_fetcher.get_most_talked_about_stocks(min_articles=3, days_back=days_back)
        
        symbols = [t['symbol'] for t in trending[:50]]  # Top 50
        
        logger.info(f"Scoring {len(symbols)} trending symbols")
        
        scores = self.score_symbols(symbols, days_back)
        
        # Filter by minimum score
        top_scores = [s for s in scores if s.total_news_score >= min_score]
        
        logger.info(f"Found {len(top_scores)} opportunities with score >= {min_score}")
        
        return top_scores
    
    def get_earnings_winners(self, days_ahead: int = 14, min_beat_rate: float = 70.0) -> List[Dict]:
        """
        Get stocks with upcoming earnings and strong historical beat rates.
        
        Args:
            days_ahead: Look ahead this many days
            min_beat_rate: Minimum historical beat rate (%)
        
        Returns:
            List of earnings opportunities
        """
        opportunities = self.earnings_calendar.get_high_probability_earnings_plays(
            days_ahead=days_ahead,
            min_beat_rate=min_beat_rate
        )
        
        # Enhance with news scores
        for opp in opportunities:
            news_score = self.score_symbol(opp['symbol'], days_back=7)
            if news_score:
                opp['news_score'] = news_score.total_news_score
                opp['news_sentiment'] = news_score.avg_sentiment
            else:
                opp['news_score'] = 50.0  # Neutral
                opp['news_sentiment'] = 0.0
        
        # Sort by combination of beat rate and news score
        opportunities.sort(
            key=lambda x: (x['beat_rate'] * 0.6 + x.get('news_score', 50) * 0.4),
            reverse=True
        )
        
        logger.info(f"Found {len(opportunities)} high-probability earnings plays")
        
        return opportunities
    
    def _calculate_sentiment_score(self, agg_sentiment: dict) -> float:
        """Convert aggregate sentiment to 0-100 score."""
        avg_sentiment = agg_sentiment['avg_sentiment']
        positive_ratio = agg_sentiment['positive_ratio']
        
        # Base score from average sentiment (-1 to +1 → 0 to 100)
        base_score = (avg_sentiment + 1) * 50
        
        # Boost for high positive ratio
        ratio_boost = positive_ratio * 20
        
        score = base_score + ratio_boost
        
        return min(100, max(0, score))
    
    def _calculate_catalyst_score(self, positive_catalysts: List[dict]) -> float:
        """Score based on strength and count of positive catalysts."""
        if not positive_catalysts:
            return 0.0
        
        # Average catalyst strength
        avg_strength = sum([c['catalyst_strength'] for c in positive_catalysts]) / len(positive_catalysts)
        
        # Bonus for multiple catalysts
        count_bonus = min(30, len(positive_catalysts) * 10)
        
        score = avg_strength + count_bonus
        
        return min(100, score)
    
    def _calculate_volume_score(self, article_count: int, days_back: int) -> float:
        """Score based on news volume (buzz)."""
        # Normalize by time period
        articles_per_day = article_count / days_back
        
        # Score: 0-1 article/day = 0-30, 1-3 = 30-60, 3+ = 60-100
        if articles_per_day >= 3:
            score = 60 + min(40, (articles_per_day - 3) * 10)
        elif articles_per_day >= 1:
            score = 30 + (articles_per_day - 1) * 15
        else:
            score = articles_per_day * 30
        
        return min(100, score)
    
    def _analyze_earnings(self, symbol: str) -> tuple[float, dict]:
        """
        Analyze earnings potential for a symbol.
        
        Returns:
            (score, info_dict)
        """
        score = 50.0  # Neutral default
        info = {
            'has_upcoming': False,
            'days_until': None,
            'beat_rate': None
        }
        
        try:
            # Check upcoming earnings
            upcoming = self.earnings_calendar.get_upcoming_earnings(days_ahead=30)
            symbol_events = [e for e in upcoming if e.symbol == symbol]
            
            if symbol_events:
                event = symbol_events[0]  # Next earnings
                info['has_upcoming'] = True
                info['days_until'] = event.days_until
                
                # Get historical stats
                stats = self.earnings_calendar.calculate_earnings_statistics(symbol)
                info['beat_rate'] = stats['beat_rate']
                
                # Score based on beat rate and proximity
                if stats['beat_rate'] >= 75:
                    score = 80 + min(20, (100 - stats['beat_rate']) / 5)
                elif stats['beat_rate'] >= 60:
                    score = 60 + (stats['beat_rate'] - 60)
                else:
                    score = stats['beat_rate']
                
                # Boost if earnings imminent (within 7 days)
                if event.days_until <= 7:
                    score += 10
        
        except Exception as e:
            logger.debug(f"{symbol}: Earnings analysis failed - {e}")
        
        return score, info
    
    def _determine_signal(self, total_score: float, sentiment_score: float, catalyst_score: float) -> str:
        """Determine trading signal based on scores."""
        if total_score >= 80 and catalyst_score >= 60:
            return "STRONG_BUY"
        elif total_score >= 65 and sentiment_score >= 55:
            return "BUY"
        elif total_score <= 35 or sentiment_score <= 30:
            return "AVOID"
        else:
            return "NEUTRAL"
    
    def _calculate_confidence(self, articles: List[NewsArticle], agg_sentiment: dict) -> float:
        """Calculate confidence in news score."""
        # Based on:
        # 1. Article count (more is better)
        # 2. Sentiment agreement (high positive ratio = high confidence)
        
        article_count = len(articles)
        positive_ratio = agg_sentiment['positive_ratio']
        
        # Article count component (0-50)
        count_component = min(50, article_count * 10)
        
        # Sentiment agreement component (0-50)
        agreement_component = positive_ratio * 50
        
        confidence = count_component + agreement_component
        
        return min(100, confidence)
    
    def _get_strongest_catalyst(self, catalysts: List[dict]) -> Optional[str]:
        """Get the strongest catalyst type."""
        if not catalysts:
            return None
        
        strongest = max(catalysts, key=lambda c: c['catalyst_strength'])
        return strongest['catalyst_type']


def display_news_scores(scores: List[NewsScore], top_n: int = 20):
    """Pretty print news scores."""
    print(f"\n{'='*100}")
    print(f"TOP {min(top_n, len(scores))} NEWS-DRIVEN OPPORTUNITIES")
    print(f"{'='*100}\n")
    
    print(f"{'Rank':<6}{'Symbol':<8}{'Score':<8}{'Signal':<12}{'Sentiment':<11}"
          f"{'Catalyst':<10}{'Buzz':<7}{'Articles':<10}{'Strongest Catalyst':<25}")
    print("-" * 100)
    
    for i, score in enumerate(scores[:top_n], 1):
        catalyst_str = score.strongest_catalyst[:22] if score.strongest_catalyst else "N/A"
        
        print(f"{i:<6}{score.symbol:<8}{score.total_news_score:<8.1f}"
              f"{score.news_signal:<12}{score.avg_sentiment:>+6.3f}     "
              f"{score.catalyst_score:<10.1f}{score.volume_score:<7.1f}"
              f"{score.article_count:<10}{catalyst_str:<25}")
    
    print(f"\n{'='*100}\n")
