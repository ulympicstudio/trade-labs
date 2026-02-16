"""
News Fetcher
Retrieves news articles about stocks from multiple sources.
Focuses on identifying trending stocks with positive news sentiment.
"""

import logging
import requests
from datetime import datetime, timedelta
from dataclasses import dataclass
from typing import List, Optional, Dict
from urllib.parse import quote
import feedparser
from bs4 import BeautifulSoup
import re


logger = logging.getLogger(__name__)


@dataclass
class NewsArticle:
    """Container for a news article about a stock."""
    symbol: str
    title: str
    source: str
    url: str
    published_date: str
    summary: str
    sentiment_score: Optional[float] = None  # -1 to +1
    sentiment_label: Optional[str] = None  # "positive", "neutral", "negative"
    relevance_score: Optional[float] = None  # 0 to 1
    
    # Categorization
    is_earnings_related: bool = False
    is_analyst_upgrade: bool = False
    is_product_news: bool = False
    is_acquisition: bool = False
    
    # Volume metrics
    mentions_count: int = 1
    social_buzz: Optional[int] = None  # Social media mentions


class NewsFetcher:
    """
    Fetches news from multiple sources and identifies trending stocks.
    Focuses on positive catalysts like earnings beats, upgrades, product launches.
    """
    
    def __init__(self):
        self.user_agent = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"
        
    def fetch_google_news_rss(self, query: str = "stock market", max_articles: int = 50) -> List[NewsArticle]:
        """
        Fetch news from Google News RSS feed.
        
        Args:
            query: Search query (e.g., "AAPL stock", "technology stocks")
            max_articles: Maximum articles to retrieve
        
        Returns:
            List of NewsArticle objects
        """
        articles = []
        
        try:
            # Google News RSS feed - URL encode the query
            encoded_query = quote(query)
            rss_url = f"https://news.google.com/rss/search?q={encoded_query}&hl=en-US&gl=US&ceid=US:en"
            
            feed = feedparser.parse(rss_url)
            
            for entry in feed.entries[:max_articles]:
                # Try to extract stock symbol from title
                symbol = self._extract_symbol_from_text(entry.title)
                
                if not symbol:
                    continue
                
                article = NewsArticle(
                    symbol=symbol,
                    title=entry.title,
                    source=entry.get('source', {}).get('title', 'Google News'),
                    url=entry.link,
                    published_date=entry.get('published', datetime.now().isoformat()),
                    summary=entry.get('summary', entry.title),
                )
                
                # Categorize article
                self._categorize_article(article)
                
                articles.append(article)
                
            logger.info(f"Fetched {len(articles)} articles from Google News RSS")
            
        except Exception as e:
            logger.error(f"Failed to fetch Google News RSS: {e}")
        
        return articles
    
    def fetch_trending_stocks(self) -> List[str]:
        """
        Fetch list of trending stock symbols from various sources.
        
        Returns:
            List of trending stock symbols
        """
        trending = []
        
        # Method 1: Google Trends for stocks
        queries = [
            "stock earnings",
            "stock upgrade",
            "stock rally",
            "stock breakout",
            "stock announcement"
        ]
        
        for query in queries:
            articles = self.fetch_google_news_rss(query, max_articles=20)
            for article in articles:
                if article.symbol and article.symbol not in trending:
                    trending.append(article.symbol)
        
        logger.info(f"Found {len(trending)} trending stocks: {trending[:10]}")
        
        return trending
    
    def fetch_news_for_symbol(self, symbol: str, days_back: int = 7) -> List[NewsArticle]:
        """
        Fetch recent news articles for a specific symbol.
        
        Args:
            symbol: Stock ticker symbol
            days_back: Number of days to look back
        
        Returns:
            List of NewsArticle objects
        """
        articles = []
        
        # Google News search for symbol
        query = f"{symbol} stock"
        articles.extend(self.fetch_google_news_rss(query, max_articles=20))
        
        # Filter by date
        cutoff_date = datetime.now() - timedelta(days=days_back)
        articles = [
            a for a in articles
            if self._parse_date(a.published_date) >= cutoff_date
        ]
        
        logger.info(f"{symbol}: Found {len(articles)} articles in last {days_back} days")
        
        return articles
    
    def fetch_news_for_symbols(self, symbols: List[str], days_back: int = 7) -> Dict[str, List[NewsArticle]]:
        """
        Fetch news for multiple symbols.
        
        Args:
            symbols: List of stock symbols
            days_back: Number of days to look back
        
        Returns:
            Dictionary mapping symbol -> list of articles
        """
        news_by_symbol = {}
        
        for symbol in symbols:
            articles = self.fetch_news_for_symbol(symbol, days_back)
            if articles:
                news_by_symbol[symbol] = articles
        
        logger.info(f"Fetched news for {len(news_by_symbol)} symbols")
        
        return news_by_symbol
    
    def get_most_talked_about_stocks(self, min_articles: int = 3, days_back: int = 1) -> List[Dict]:
        """
        Identify stocks with highest news volume (most talked about).
        
        Args:
            min_articles: Minimum number of articles required
            days_back: Days to look back
        
        Returns:
            List of dicts with symbol, article_count, articles
        """
        # Get trending stocks
        trending = self.fetch_trending_stocks()
        
        # Count articles per symbol
        symbol_counts = {}
        symbol_articles = {}
        
        cutoff_date = datetime.now() - timedelta(days=days_back)
        
        for symbol in trending:
            articles = self.fetch_news_for_symbol(symbol, days_back)
            
            recent_articles = [
                a for a in articles
                if self._parse_date(a.published_date) >= cutoff_date
            ]
            
            if len(recent_articles) >= min_articles:
                symbol_counts[symbol] = len(recent_articles)
                symbol_articles[symbol] = recent_articles
        
        # Sort by article count
        sorted_symbols = sorted(
            symbol_counts.items(),
            key=lambda x: x[1],
            reverse=True
        )
        
        results = [
            {
                'symbol': symbol,
                'article_count': count,
                'articles': symbol_articles[symbol]
            }
            for symbol, count in sorted_symbols
        ]
        
        logger.info(f"Most talked about: {[r['symbol'] for r in results[:10]]}")
        
        return results
    
    def _extract_symbol_from_text(self, text: str) -> Optional[str]:
        """Extract stock symbol from text (e.g., 'AAPL', 'TSLA')."""
        # Common patterns: (AAPL), [TSLA], NASDAQ:AAPL, NYSE:MSFT
        patterns = [
            r'\(([A-Z]{1,5})\)',  # (AAPL)
            r'\[([A-Z]{1,5})\]',  # [AAPL]
            r'NASDAQ:([A-Z]{1,5})',  # NASDAQ:AAPL
            r'NYSE:([A-Z]{1,5})',  # NYSE:MSFT
            r'\b([A-Z]{2,5})\b(?:\s+stock|\s+shares)',  # AAPL stock
        ]
        
        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                symbol = match.group(1)
                # Filter out common false positives
                if symbol not in ['US', 'CEO', 'CFO', 'USD', 'USA', 'IPO', 'ETF']:
                    return symbol
        
        return None
    
    def _categorize_article(self, article: NewsArticle):
        """Categorize article based on keywords in title/summary."""
        text = (article.title + " " + article.summary).lower()
        
        # Earnings related
        earnings_keywords = ['earnings', 'revenue', 'eps', 'quarterly', 'beat', 'miss', 'guidance']
        if any(kw in text for kw in earnings_keywords):
            article.is_earnings_related = True
        
        # Analyst upgrades
        upgrade_keywords = ['upgrade', 'raised', 'bullish', 'buy rating', 'price target']
        if any(kw in text for kw in upgrade_keywords):
            article.is_analyst_upgrade = True
        
        # Product news
        product_keywords = ['launch', 'product', 'release', 'unveil', 'announce']
        if any(kw in text for kw in product_keywords):
            article.is_product_news = True
        
        # M&A
        ma_keywords = ['acquisition', 'merger', 'buyout', 'deal', 'acquire']
        if any(kw in text for kw in ma_keywords):
            article.is_acquisition = True
    
    def _parse_date(self, date_str: str) -> datetime:
        """Parse date string to datetime."""
        try:
            # Try common formats
            for fmt in ['%Y-%m-%d', '%Y-%m-%dT%H:%M:%S', '%a, %d %b %Y %H:%M:%S %Z']:
                try:
                    return datetime.strptime(date_str, fmt)
                except:
                    continue
            
            # If all fail, return now
            return datetime.now()
        except:
            return datetime.now()


class FinancialNewsAPI:
    """
    Alternative news sources using APIs.
    Requires API keys - configure in environment.
    """
    
    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or "demo"  # Use demo key if not provided
        self.base_url = "https://finnhub.io/api/v1"
    
    def fetch_company_news(self, symbol: str, days_back: int = 7) -> List[NewsArticle]:
        """
        Fetch news from Finnhub API.
        Sign up at https://finnhub.io for free API key.
        """
        articles = []
        
        try:
            from_date = (datetime.now() - timedelta(days=days_back)).strftime('%Y-%m-%d')
            to_date = datetime.now().strftime('%Y-%m-%d')
            
            url = f"{self.base_url}/company-news"
            params = {
                'symbol': symbol,
                'from': from_date,
                'to': to_date,
                'token': self.api_key
            }
            
            response = requests.get(url, params=params, timeout=10)
            
            if response.status_code == 200:
                data = response.json()
                
                for item in data:
                    article = NewsArticle(
                        symbol=symbol,
                        title=item.get('headline', ''),
                        source=item.get('source', 'Finnhub'),
                        url=item.get('url', ''),
                        published_date=datetime.fromtimestamp(item.get('datetime', 0)).isoformat(),
                        summary=item.get('summary', '')
                    )
                    articles.append(article)
                
                logger.info(f"{symbol}: Fetched {len(articles)} articles from Finnhub")
            else:
                logger.warning(f"Finnhub API returned status {response.status_code}")
        
        except Exception as e:
            logger.error(f"Failed to fetch from Finnhub: {e}")
        
        return articles
    
    def fetch_market_news(self, category: str = "general") -> List[NewsArticle]:
        """
        Fetch general market news.
        Categories: general, forex, crypto, merger
        """
        articles = []
        
        try:
            url = f"{self.base_url}/news"
            params = {
                'category': category,
                'token': self.api_key
            }
            
            response = requests.get(url, params=params, timeout=10)
            
            if response.status_code == 200:
                data = response.json()
                
                for item in data[:50]:  # Limit to 50
                    # Try to extract symbol
                    symbol = self._extract_symbol(item.get('headline', ''))
                    
                    if symbol:
                        article = NewsArticle(
                            symbol=symbol,
                            title=item.get('headline', ''),
                            source=item.get('source', 'Finnhub'),
                            url=item.get('url', ''),
                            published_date=datetime.fromtimestamp(item.get('datetime', 0)).isoformat(),
                            summary=item.get('summary', '')
                        )
                        articles.append(article)
                
                logger.info(f"Fetched {len(articles)} market news articles")
        
        except Exception as e:
            logger.error(f"Failed to fetch market news: {e}")
        
        return articles
    
    def _extract_symbol(self, text: str) -> Optional[str]:
        """Extract first stock symbol from text."""
        match = re.search(r'\b([A-Z]{1,5})\b', text)
        if match:
            symbol = match.group(1)
            if symbol not in ['US', 'CEO', 'CFO', 'USD', 'IPO']:
                return symbol
        return None
