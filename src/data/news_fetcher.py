"""
News Fetcher
Retrieves news articles about stocks from multiple sources.
Primary provider: Benzinga (general news endpoint).
Fallback: RSS (Google News).
"""

import logging
import os
import requests
import threading
import time
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass
from typing import List, Optional, Dict
from urllib.parse import quote
import feedparser
from bs4 import BeautifulSoup
import re


logger = logging.getLogger(__name__)


# ── News Category Classification (Legend Phase 1) ────────────────────

# Category → (keywords, multiplier)
_CATEGORY_RULES: list[tuple[str, list[str], float]] = [
    ("FDA",      ["fda", "approval", "clinical trial", "phase 3", "phase 2", "drug", "biologic", "nda", "eua"], 4.0),
    ("MNA",      ["acquisition", "merger", "buyout", "takeover", "deal value", "acquires", "acquire", "merge"], 3.5),
    ("EARNINGS", ["earnings", "revenue", "eps", "beat", "miss", "guidance", "quarterly", "profit", "loss per share"], 3.0),
    ("MGMT",     ["ceo", "cfo", "cto", "appoint", "resign", "fired", "leadership", "board of directors"], 2.0),
    ("ANALYST",  ["upgrade", "downgrade", "price target", "maintains", "raises", "lowers", "buy rating", "sell rating", "overweight", "underweight"], 1.5),
    ("MACRO",    ["fed", "inflation", "rates", "cpi", "jobs", "fomc", "gdp", "tariff", "geopolitics", "sanctions"], 1.0),
]


def classify_news(title: str) -> tuple[str, float]:
    """Classify a news headline into a category with a score multiplier.

    Returns ``(category, multiplier)`` where *category* is one of
    ``FDA | MNA | EARNINGS | MGMT | ANALYST | MACRO | GENERAL``
    and *multiplier* scales news impact (1.0 – 4.0).

    Rules are evaluated in priority order; the first match wins.
    """
    hl = title.lower()
    for category, keywords, multiplier in _CATEGORY_RULES:
        if any(kw in hl for kw in keywords):
            return category, multiplier
    return "GENERAL", 1.0


# ── Benzinga configuration ───────────────────────────────────────────
# The ONLY supported env var is BENZINGA_API_KEY.  No aliases.

_BENZINGA_API_KEY: str = os.environ.get("BENZINGA_API_KEY", "").strip()
_BENZINGA_BASE_URL = "https://api.benzinga.com/api/v2/news"


class BenzingaNewsAPI:
    """Fetch general news from the Benzinga v2 news endpoint.

    One API call returns many articles, each with a ``stocks`` array
    containing ticker symbols.
    """

    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or _BENZINGA_API_KEY
        logger.info("BenzingaNewsAPI init  key_present=%s", bool(self.api_key))

    def fetch_general_news(self, max_items: int = 100) -> tuple[List[Dict], str]:
        """Fetch general news from Benzinga.

        Returns ``(items, reason)`` where *items* is a list of dicts::

            {"headline", "source", "url", "ts" (datetime UTC),
             "related_tickers" (list[str]), "summary"}

        *reason* is ``""`` on success, or a short string explaining the
        failure (HTTP status, network error, etc.).
        """
        if not self.api_key:
            return [], "no_api_key"

        items: List[Dict] = []
        try:
            resp = requests.get(
                _BENZINGA_BASE_URL,
                params={
                    "token": self.api_key,
                    "displayOutput": "json",
                    "items": str(min(max_items, 100)),
                },
                headers={"Accept": "application/json"},
                timeout=15,
            )
            if resp.status_code != 200:
                reason = f"HTTP {resp.status_code}"
                logger.warning("Benzinga fetch failed: %s", reason)
                return [], reason

            data = resp.json()

            # Benzinga returns a list of article objects
            if not isinstance(data, list):
                reason = f"unexpected_payload:{type(data).__name__}"
                logger.warning("Benzinga unexpected payload type: %s", type(data))
                return [], reason

            for raw in data[:max_items]:
                title = (raw.get("title") or "").strip()
                if not title:
                    continue

                # Extract ticker symbols from stocks[].name
                stocks = raw.get("stocks") or []
                related_tickers: List[str] = []
                for stock_entry in stocks:
                    name = stock_entry.get("name", "").strip().upper()
                    if name and 1 <= len(name) <= 6:
                        related_tickers.append(name)

                # Parse created timestamp (ISO-8601)
                created_str = raw.get("created", "")
                ts: datetime
                try:
                    ts = datetime.fromisoformat(created_str)
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                except (ValueError, TypeError):
                    ts = datetime.now(timezone.utc)

                items.append({
                    "headline": title,
                    "source": "Benzinga",
                    "url": raw.get("url", ""),
                    "ts": ts,
                    "related_tickers": related_tickers,
                    "summary": (raw.get("body") or raw.get("teaser") or "")[:500],
                })

            logger.info("Benzinga fetch ok items=%d", len(items))
            return items, ""

        except requests.exceptions.ConnectionError as exc:
            reason = f"connection_error:{exc}"
            logger.warning("Benzinga fetch failed: %s", reason)
            return [], reason
        except requests.exceptions.Timeout:
            reason = "timeout"
            logger.warning("Benzinga fetch failed: %s", reason)
            return [], reason
        except Exception as exc:
            reason = f"error:{exc}"
            logger.warning("Benzinga fetch failed: %s", reason)
            return [], reason


# ── Finnhub news fetcher (Legend Phase 2 – tertiary provider) ────────

_FINNHUB_API_KEY: str = os.environ.get("FINNHUB_API_KEY", "").strip()
_FINNHUB_NEWS_URL = "https://finnhub.io/api/v1/news"

# Rate-limit / cooldown state (module-level, thread-safe via GIL)
_finnhub_last_call_ts: float = 0.0
_finnhub_min_interval_s: float = 2.0          # ≥2 s between calls
_finnhub_cooldown_until: float = 0.0          # epoch – disabled until this time
_FINNHUB_COOLDOWN_S: float = 300.0            # 5 min cooldown on 429


def fetch_finnhub_news(max_items: int = 50) -> tuple[list[dict], str]:
    """Fetch general news from Finnhub.

    Returns ``(items, reason)`` in the same dict schema as BenzingaNewsAPI:
    ``{"headline", "source", "url", "ts", "related_tickers", "summary"}``.

    Rate-limits to one call per ``_finnhub_min_interval_s``.
    On HTTP 429, enters 5-min cooldown and returns empty.
    """
    global _finnhub_last_call_ts, _finnhub_cooldown_until

    key = _FINNHUB_API_KEY
    if not key:
        return [], "no_finnhub_key"

    now = time.time()
    if now < _finnhub_cooldown_until:
        remaining = int(_finnhub_cooldown_until - now)
        return [], f"cooldown_{remaining}s"

    # Rate-limit: enforce minimum interval
    elapsed = now - _finnhub_last_call_ts
    if elapsed < _finnhub_min_interval_s:
        time.sleep(_finnhub_min_interval_s - elapsed)
    _finnhub_last_call_ts = time.time()

    items: list[dict] = []
    try:
        resp = requests.get(
            _FINNHUB_NEWS_URL,
            params={"category": "general", "minId": "0", "token": key},
            timeout=10,
        )
        if resp.status_code == 429:
            _finnhub_cooldown_until = time.time() + _FINNHUB_COOLDOWN_S
            logger.warning(
                "Finnhub 429 — entering %.0fs cooldown", _FINNHUB_COOLDOWN_S,
            )
            return [], "rate_limited_429"
        if resp.status_code != 200:
            return [], f"HTTP {resp.status_code}"

        data = resp.json()
        if not isinstance(data, list):
            return [], f"unexpected_payload:{type(data).__name__}"

        for raw in data[:max_items]:
            title = (raw.get("headline") or "").strip()
            if not title:
                continue

            # Finnhub gives ``related`` as a comma-string, e.g. "AAPL,MSFT"
            related_str = raw.get("related") or ""
            related_tickers = [
                t.strip().upper()
                for t in related_str.split(",")
                if t.strip() and 1 <= len(t.strip()) <= 6
            ]

            ts: datetime
            epoch = raw.get("datetime")
            if epoch and isinstance(epoch, (int, float)):
                ts = datetime.fromtimestamp(epoch, tz=timezone.utc)
            else:
                ts = datetime.now(timezone.utc)

            items.append({
                "headline": title,
                "source": raw.get("source", "Finnhub"),
                "url": raw.get("url", ""),
                "ts": ts,
                "related_tickers": related_tickers,
                "summary": (raw.get("summary") or "")[:500],
            })

        logger.info("Finnhub fetch ok items=%d", len(items))
        return items, ""

    except requests.exceptions.Timeout:
        return [], "timeout"
    except requests.exceptions.ConnectionError as exc:
        return [], f"connection_error:{exc}"
    except Exception as exc:
        logger.warning("Finnhub fetch failed: %s", exc)
        return [], f"error:{exc}"


# ── Normalised title for dedupe (Legend Phase 2) ─────────────────────

_NORM_RE = re.compile(r"[^a-z0-9 ]+")


def normalise_title(title: str) -> str:
    """Lowercase, strip punctuation / extra spaces — for dedupe hashing."""
    return _NORM_RE.sub("", title.lower()).strip()


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
    related_tickers: Optional[List[str]] = None  # API-provided related symbols
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
