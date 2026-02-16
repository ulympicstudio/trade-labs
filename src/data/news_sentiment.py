"""
News Sentiment Analyzer
Analyzes sentiment of news articles to identify positive catalysts.
"""

import logging
from typing import List
from dataclasses import dataclass
import re

from src.data.news_fetcher import NewsArticle


logger = logging.getLogger(__name__)


@dataclass
class SentimentAnalysis:
    """Result of sentiment analysis."""
    sentiment_score: float  # -1 (very negative) to +1 (very positive)
    sentiment_label: str  # "very_positive", "positive", "neutral", "negative", "very_negative"
    confidence: float  # 0 to 1
    
    # Component scores
    title_sentiment: float
    body_sentiment: float
    
    # Keywords detected
    positive_keywords: List[str]
    negative_keywords: List[str]


class NewsSentimentAnalyzer:
    """
    Analyzes sentiment of news articles using keyword-based approach.
    Focuses on financial/stock-specific sentiment.
    """
    
    def __init__(self):
        # Positive keywords (bullish indicators)
        self.positive_keywords = {
            # Earnings/Financial
            'beat': 3.0, 'exceed': 2.5, 'strong': 2.0, 'record': 2.5,
            'surge': 3.0, 'soar': 3.0, 'rally': 2.5, 'gain': 2.0,
            'growth': 2.0, 'revenue': 1.5, 'profit': 2.0,
            
            # Analyst/Market
            'upgrade': 3.0, 'outperform': 2.5, 'buy': 2.0, 'bullish': 2.5,
            'raised': 2.0, 'increase': 1.5, 'positive': 2.0,
            'momentum': 1.5, 'breakout': 2.5,
            
            # Business
            'expansion': 2.0, 'launch': 1.5, 'innovation': 2.0,
            'partnership': 1.5, 'acquisition': 1.5, 'deal': 1.5,
            'contract': 1.5, 'win': 2.0, 'success': 2.0,
            
            # Superlatives
            'best': 2.0, 'top': 1.5, 'leading': 1.5, 'excellent': 2.0,
            'outstanding': 2.5, 'impressive': 2.0
        }
        
        # Negative keywords (bearish indicators)
        self.negative_keywords = {
            # Earnings/Financial
            'miss': -3.0, 'disappoint': -2.5, 'weak': -2.0, 'decline': -2.0,
            'fall': -2.0, 'drop': -2.5, 'plunge': -3.0, 'crash': -3.5,
            'loss': -2.5, 'losses': -2.5, 'deficit': -2.0,
            
            # Analyst/Market
            'downgrade': -3.0, 'underperform': -2.5, 'sell': -2.5,
            'bearish': -2.5, 'cut': -2.0, 'reduce': -2.0,
            'negative': -2.0, 'concern': -1.5, 'worry': -1.5,
            
            # Business
            'lawsuit': -2.0, 'investigation': -2.0, 'scandal': -3.0,
            'layoff': -2.5, 'bankruptcy': -3.5, 'delay': -1.5,
            'recall': -2.5, 'failure': -2.5, 'problem': -1.5,
            
            # Superlatives
            'worst': -2.5, 'poor': -2.0, 'bad': -1.5, 'terrible': -2.5
        }
        
        # Modifiers (amplify or reduce sentiment)
        self.amplifiers = {
            'very': 1.5, 'extremely': 1.8, 'significantly': 1.6,
            'substantially': 1.6, 'remarkably': 1.5, 'surprisingly': 1.4
        }
        
        self.reducers = {
            'slightly': 0.5, 'somewhat': 0.6, 'moderately': 0.7,
            'relatively': 0.7, 'fairly': 0.7
        }
    
    def analyze_article(self, article: NewsArticle) -> SentimentAnalysis:
        """
        Analyze sentiment of a news article.
        
        Args:
            article: NewsArticle object
        
        Returns:
            SentimentAnalysis with scores and labels
        """
        # Analyze title (weighted more heavily)
        title_score = self._analyze_text(article.title)
        
        # Analyze summary/body
        body_score = self._analyze_text(article.summary)
        
        # Weighted combination (title = 60%, body = 40%)
        combined_score = (title_score * 0.6) + (body_score * 0.4)
        
        # Clamp to [-1, 1]
        combined_score = max(-1.0, min(1.0, combined_score))
        
        # Determine label
        label = self._score_to_label(combined_score)
        
        # Calculate confidence (based on keyword density)
        confidence = self._calculate_confidence(article.title + " " + article.summary)
        
        # Find matched keywords
        positive_kws = self._find_keywords(article.title + " " + article.summary, self.positive_keywords)
        negative_kws = self._find_keywords(article.title + " " + article.summary, self.negative_keywords)
        
        return SentimentAnalysis(
            sentiment_score=combined_score,
            sentiment_label=label,
            confidence=confidence,
            title_sentiment=title_score,
            body_sentiment=body_score,
            positive_keywords=positive_kws,
            negative_keywords=negative_kws
        )
    
    def analyze_articles(self, articles: List[NewsArticle]) -> List[SentimentAnalysis]:
        """Analyze multiple articles."""
        results = []
        
        for article in articles:
            analysis = self.analyze_article(article)
            
            # Update article with sentiment
            article.sentiment_score = analysis.sentiment_score
            article.sentiment_label = analysis.sentiment_label
            
            results.append(analysis)
        
        return results
    
    def get_aggregate_sentiment(self, articles: List[NewsArticle]) -> dict:
        """
        Calculate aggregate sentiment for a list of articles (e.g., all articles about AAPL).
        
        Returns:
            Dictionary with aggregate metrics
        """
        if not articles:
            return {
                'avg_sentiment': 0.0,
                'median_sentiment': 0.0,
                'positive_count': 0,
                'negative_count': 0,
                'neutral_count': 0,
                'total_articles': 0,
                'positive_ratio': 0.0
            }
        
        sentiments = [a.sentiment_score for a in articles if a.sentiment_score is not None]
        
        if not sentiments:
            sentiments = [0.0]
        
        positive_count = sum(1 for s in sentiments if s > 0.2)
        negative_count = sum(1 for s in sentiments if s < -0.2)
        neutral_count = len(sentiments) - positive_count - negative_count
        
        return {
            'avg_sentiment': sum(sentiments) / len(sentiments),
            'median_sentiment': sorted(sentiments)[len(sentiments) // 2],
            'positive_count': positive_count,
            'negative_count': negative_count,
            'neutral_count': neutral_count,
            'total_articles': len(articles),
            'positive_ratio': positive_count / len(articles)
        }
    
    def _analyze_text(self, text: str) -> float:
        """Analyze sentiment of text string."""
        if not text:
            return 0.0
        
        text_lower = text.lower()
        words = text_lower.split()
        
        scores = []
        
        for i, word in enumerate(words):
            # Clean word
            word = re.sub(r'[^\w\s]', '', word)
            
            # Check for modifiers before this word
            modifier = 1.0
            if i > 0:
                prev_word = re.sub(r'[^\w\s]', '', words[i-1].lower())
                if prev_word in self.amplifiers:
                    modifier = self.amplifiers[prev_word]
                elif prev_word in self.reducers:
                    modifier = self.reducers[prev_word]
            
            # Check positive keywords
            if word in self.positive_keywords:
                scores.append(self.positive_keywords[word] * modifier)
            
            # Check negative keywords
            if word in self.negative_keywords:
                scores.append(self.negative_keywords[word] * modifier)
        
        # Average score, normalized
        if scores:
            avg_score = sum(scores) / len(scores)
            # Normalize to [-1, 1] range
            return max(-1.0, min(1.0, avg_score / 3.0))
        
        return 0.0
    
    def _score_to_label(self, score: float) -> str:
        """Convert numerical score to label."""
        if score >= 0.6:
            return "very_positive"
        elif score >= 0.2:
            return "positive"
        elif score <= -0.6:
            return "very_negative"
        elif score <= -0.2:
            return "negative"
        else:
            return "neutral"
    
    def _calculate_confidence(self, text: str) -> float:
        """Calculate confidence in sentiment analysis based on keyword density."""
        if not text:
            return 0.0
        
        words = text.lower().split()
        keyword_count = 0
        
        for word in words:
            word = re.sub(r'[^\w\s]', '', word)
            if word in self.positive_keywords or word in self.negative_keywords:
                keyword_count += 1
        
        # Confidence based on keyword density
        density = keyword_count / max(1, len(words))
        confidence = min(1.0, density * 10)  # Scale up, cap at 1.0
        
        return confidence
    
    def _find_keywords(self, text: str, keyword_dict: dict) -> List[str]:
        """Find which keywords from dictionary appear in text."""
        found = []
        text_lower = text.lower()
        
        for keyword in keyword_dict.keys():
            if keyword in text_lower:
                found.append(keyword)
        
        return found


def classify_news_catalyst(article: NewsArticle, sentiment: SentimentAnalysis) -> dict:
    """
    Classify news as a trading catalyst.
    
    Returns dict with:
    - is_catalyst: bool
    - catalyst_type: str
    - catalyst_strength: float (0-100)
    - expected_impact: str ("strong_positive", "positive", "neutral", "negative", "strong_negative")
    """
    is_catalyst = False
    catalyst_type = "none"
    catalyst_strength = 0.0
    expected_impact = "neutral"
    
    # Strong positive catalyst criteria
    if sentiment.sentiment_score > 0.5:
        is_catalyst = True
        expected_impact = "strong_positive"
        catalyst_strength = sentiment.sentiment_score * 100
        
        # Determine type
        if article.is_earnings_related:
            catalyst_type = "earnings_beat"
        elif article.is_analyst_upgrade:
            catalyst_type = "analyst_upgrade"
        elif article.is_product_news:
            catalyst_type = "product_launch"
        elif article.is_acquisition:
            catalyst_type = "ma_deal"
        else:
            catalyst_type = "positive_news"
    
    elif sentiment.sentiment_score > 0.2:
        is_catalyst = True
        expected_impact = "positive"
        catalyst_strength = sentiment.sentiment_score * 80
        catalyst_type = "positive_news"
    
    # Negative catalysts (to avoid)
    elif sentiment.sentiment_score < -0.3:
        is_catalyst = True
        expected_impact = "negative"
        catalyst_strength = abs(sentiment.sentiment_score) * 100
        catalyst_type = "negative_news"
    
    return {
        'is_catalyst': is_catalyst,
        'catalyst_type': catalyst_type,
        'catalyst_strength': catalyst_strength,
        'expected_impact': expected_impact,
        'sentiment_score': sentiment.sentiment_score,
        'confidence': sentiment.confidence
    }
