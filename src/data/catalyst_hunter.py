"""
CATALYST HUNTER - Multi-Source Catalyst Discovery Engine
Scours the web (news, earnings, options, social, insiders) for swing trading catalysts.
"""

import logging
import requests
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Set, Tuple
from collections import defaultdict
import feedparser
from urllib.parse import quote
import json

logger = logging.getLogger(__name__)


@dataclass
class CatalystSignal:
    """A single catalyst event for a stock."""
    symbol: str
    catalyst_type: str  # "earnings", "upgrade", "product", "acquisition", "volume_spike", "social_buzz", "insider_buy", "options_unusual"
    source: str  # "finnhub", "seeking_alpha", "yahoo", "reddit", "twitter", "options", "sec"
    headline: str
    description: Optional[str] = None
    url: Optional[str] = None
    published_date: Optional[str] = None
    
    # Signal strength
    confidence: float = 0.7  # 0-1.0
    urgency: float = 0.5  # 0-1.0 (how soon this matters)
    
    # Additional context
    bullish: bool = True  # True = upside catalyst, False = downside
    magnitude: float = 1.0  # 0-2.0 (how big is the move likely to be)
    mentions_count: int = 1
    
    # Metadata
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())


@dataclass
class CatalystStock:
    """A stock with multiple catalyst signals."""
    symbol: str
    signals: List[CatalystSignal] = field(default_factory=list)
    
    @property
    def combined_score(self) -> float:
        """Aggregate score from all signals."""
        if not self.signals:
            return 0.0
        weights = {
            "earnings": 2.0,
            "upgrade": 1.8,
            "product": 1.5,
            "acquisition": 2.0,
            "volume_spike": 1.2,
            "social_buzz": 0.8,
            "insider_buy": 1.3,
            "options_unusual": 1.4,
        }
        total = sum(
            (1.0 if s.bullish else -1.0) * s.confidence * s.urgency * weights.get(s.catalyst_type, 1.0)
            for s in self.signals
        )
        # Normalize to 0-100
        return max(0, min(100, (total / len(self.signals)) * 25 + 50))
    
    @property
    def signal_types(self) -> Set[str]:
        """Unique catalyst types present."""
        return {s.catalyst_type for s in self.signals}


class CatalystHunter:
    """
    Multi-source catalyst discovery engine.
    Aggregates signals from news, earnings, options, social, insiders.
    """
    
    def __init__(self, finnhub_api_key: Optional[str] = None):
        """
        Initialize hunter with API credentials.
        
        Args:
            finnhub_api_key: Finnhub API key (get from env if not provided)
        """
        import os
        self.finnhub_key = finnhub_api_key or os.getenv("FINNHUB_API_KEY")
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (trading-labs catalyst-hunter)"
        })
        
        # Cache for deduplication
        self.seen_signals = set()
        self.catalyst_cache = {}
        self.cache_time = {}
        
    # ============================================================
    # SOURCE 1: FINNHUB NEWS & EARNINGS
    # ============================================================
    
    def hunt_finnhub_news(self, limit: int = 50) -> Dict[str, CatalystStock]:
        """Fetch news from Finnhub (earnings, upgrades, press releases)."""
        if not self.finnhub_key:
            logger.warning("No Finnhub API key - skipping news source")
            return {}
        
        catalysts = {}
        
        try:
            # Get company news
            url = f"https://finnhub.io/api/v1/news?category=general&minId=0&token={self.finnhub_key}"
            resp = self.session.get(url, timeout=5)
            resp.raise_for_status()
            
            for article in resp.json()[:limit]:
                symbols = self._extract_symbols_from_text(article.get("headline", ""))
                
                for symbol in symbols:
                    headline = article.get("headline", "")
                    catalyst_type = self._classify_news(headline)
                    
                    signal = CatalystSignal(
                        symbol=symbol,
                        catalyst_type=catalyst_type,
                        source="finnhub",
                        headline=headline,
                        description=article.get("summary", ""),
                        url=article.get("url", ""),
                        published_date=article.get("datetime"),
                        confidence=self._confidence_for_type(catalyst_type),
                        urgency=0.9,  # News is urgent
                        bullish=self._is_bullish(headline),
                    )
                    
                    if self._is_new_signal(signal):
                        if symbol not in catalysts:
                            catalysts[symbol] = CatalystStock(symbol)
                        catalysts[symbol].signals.append(signal)
            
            logger.info(f"[FINNHUB] Found {len(catalysts)} catalyst stocks")
            
        except Exception as e:
            logger.error(f"Finnhub news fetch failed: {e}")
        
        return catalysts
    
    def hunt_earnings_surprises(self) -> Dict[str, CatalystStock]:
        """Track earnings beats/misses (high volatility catalysts)."""
        catalysts = {}
        
        try:
            if not self.finnhub_key:
                return catalysts
            
            # Get earnings calendar with surprises
            url = f"https://finnhub.io/api/v1/calendar/earnings?token={self.finnhub_key}"
            resp = self.session.get(url, timeout=5)
            resp.raise_for_status()
            
            earnings_data = resp.json()
            if not isinstance(earnings_data, list):
                logger.debug(f"Earnings data not list: {type(earnings_data)}")
                return catalysts
            
            for earning in earnings_data[0:30]:
                symbol = earning.get("symbol", "")
                if not symbol:
                    continue
                
                # Look for surprise indicator
                estimate = earning.get("epsEstimate", 0)
                actual = earning.get("epsActual")
                
                if actual and estimate:
                    surprise_pct = ((actual - estimate) / abs(estimate)) * 100 if estimate else 0
                    
                    if abs(surprise_pct) > 5:  # >5% surprise threshold
                        catalyst_type = "earnings_beat" if surprise_pct > 0 else "earnings_miss"
                        
                        signal = CatalystSignal(
                            symbol=symbol,
                            catalyst_type="earnings",
                            source="finnhub_earnings",
                            headline=f"Earnings surprise: {surprise_pct:+.1f}% ({actual} vs {estimate})",
                            confidence=0.95,  # Very high confidence
                            urgency=0.95,  # Immediate market impact
                            bullish=surprise_pct > 0,
                            magnitude=min(2.0, abs(surprise_pct) / 10),  # Bigger surprise = bigger move
                        )
                        
                        if self._is_new_signal(signal):
                            if symbol not in catalysts:
                                catalysts[symbol] = CatalystStock(symbol)
                            catalysts[symbol].signals.append(signal)
            
            logger.info(f"[EARNINGS] Found {len(catalysts)} earnings catalyst stocks")
            
        except Exception as e:
            logger.error(f"Earnings surprise fetch failed: {e}")
        
        return catalysts
    
    # ============================================================
    # SOURCE 2: YAHOO FINANCE TRENDING & VOLUME SPIKES
    # ============================================================
    
    def hunt_yahoo_trending(self) -> Dict[str, CatalystStock]:
        """Get trending symbols from Yahoo Finance."""
        catalysts = {}
        
        try:
            # Yahoo trending stocks
            url = "https://finance.yahoo.com"
            resp = self.session.get(url, timeout=5)
            
            # Parse for trending tickers (simplified - in production use YF API)
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(resp.text, 'html.parser')
            
            # Look for trending section (simplified extraction)
            trending_text = resp.text
            
            # Extract symbols from trending mentions - use existing method for consistency
            symbols = self._extract_symbols_from_text(trending_text)
            
            for symbol in set(symbols[:15]):  # Top 15 trending
                if len(symbol) >= 2 and len(symbol) <= 5:
                    signal = CatalystSignal(
                        symbol=symbol,
                        catalyst_type="volume_spike",
                        source="yahoo_trending",
                        headline=f"{symbol} trending on Yahoo Finance",
                        confidence=0.6,
                        urgency=0.8,
                        bullish=True,
                    )
                    
                    if self._is_new_signal(signal):
                        if symbol not in catalysts:
                            catalysts[symbol] = CatalystStock(symbol)
                        catalysts[symbol].signals.append(signal)
            
            logger.info(f"[YAHOO] Found {len(catalysts)} trending stocks")
            
        except Exception as e:
            logger.warning(f"Yahoo trending fetch failed (non-critical): {e}")
        
        return catalysts
    
    # ============================================================
    # SOURCE 3: REDDIT SOCIAL SENTIMENT
    # ============================================================
    
    def hunt_reddit_mentions(self) -> Dict[str, CatalystStock]:
        """Monitor r/stocks, r/investing, r/wallstreetbets for buzz."""
        catalysts = {}
        
        try:
            subreddits = ["stocks", "investing", "wallstreetbets"]
            found_any = False
            
            for subreddit in subreddits:
                try:
                    url = f"https://www.reddit.com/r/{subreddit}/hot.json"
                    # Add realistic user agent to avoid 403
                    headers = {
                        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
                    }
                    resp = self.session.get(url, headers=headers, timeout=5)
                    
                    if resp.status_code == 403:
                        logger.debug(f"Reddit {subreddit} blocked (403) - API may require auth")
                        continue
                    
                    resp.raise_for_status()
                    
                    data = resp.json()
                    posts = data.get("data", {}).get("children", [])
                    
                    for post in posts[:20]:  # Top 20 posts
                        title = post.get("data", {}).get("title", "")
                        symbols = self._extract_symbols_from_text(title)
                        score = post.get("data", {}).get("score", 0)
                        
                        for symbol in symbols:
                            signal = CatalystSignal(
                                symbol=symbol,
                                catalyst_type="social_buzz",
                                source=f"reddit_{subreddit}",
                                headline=title,
                                confidence=0.5 + (min(score, 1000) / 2000),  # Score affects confidence
                                urgency=0.7,
                                bullish=self._is_bullish(title),
                                mentions_count=score,
                            )
                            
                            if self._is_new_signal(signal):
                                if symbol not in catalysts:
                                    catalysts[symbol] = CatalystStock(symbol)
                                catalysts[symbol].signals.append(signal)
                
                except Exception as e:
                    logger.warning(f"Reddit {subreddit} fetch failed: {e}")
            
            logger.info(f"[REDDIT] Found {len(catalysts)} social buzz stocks")
            
        except Exception as e:
            logger.warning(f"Reddit social sentiment failed (non-critical): {e}")
        
        return catalysts
    
    # ============================================================
    # SOURCE 4: INSIDER BUYING/SELLING
    # ============================================================
    
    def hunt_insider_activity(self) -> Dict[str, CatalystStock]:
        """Detect significant insider buying (very bullish signal)."""
        catalysts = {}
        
        try:
            # In production, use SEC EDGAR API or insider.com data
            # This is a placeholder for integration
            
            # Example: We could fetch from finviz or similar
            url = "https://www.finviz.com/insidertrading.ashx"
            resp = self.session.get(url, timeout=5)
            resp.raise_for_status()
            
            # Parse insider transactions
            try:
                from bs4 import BeautifulSoup
            except ImportError:
                logger.debug("BeautifulSoup not available, skipping insider parsing")
                return catalysts
            
            soup = BeautifulSoup(resp.text, 'html.parser')
            
            # Extract insider buying signals
            rows = soup.find_all('tr')
            
            for row in rows[:50]:  # Top 50 transactions
                cells = row.find_all('td')
                if len(cells) >= 6:
                    try:
                        symbol = cells[1].text.strip()
                        insider_name = cells[2].text.strip()
                        relationship = cells[3].text.strip()
                        transaction = cells[4].text.strip()
                        
                        # High confidence if CEO/Director buying
                        is_executive = any(x in relationship.upper() for x in ["CEO", "DIRECTOR", "CFO"])
                        is_buying = "BUY" in transaction.upper()
                        
                        if is_buying and is_executive:
                            signal = CatalystSignal(
                                symbol=symbol,
                                catalyst_type="insider_buy",
                                source="insider_trading",
                                headline=f"Insider buying: {insider_name} ({relationship})",
                                confidence=0.9,
                                urgency=0.85,
                                bullish=True,
                                magnitude=1.8,
                            )
                            
                            if self._is_new_signal(signal):
                                if symbol not in catalysts:
                                    catalysts[symbol] = CatalystStock(symbol)
                                catalysts[symbol].signals.append(signal)
                    except:
                        pass
            
            logger.info(f"[INSIDER] Found {len(catalysts)} insider activity stocks")
            
        except Exception as e:
            logger.warning(f"Insider activity fetch failed (non-critical): {e}")
        
        return catalysts
    
    # ============================================================
    # SOURCE 5: OPTIONS UNUSUAL ACTIVITY
    # ============================================================
    
    def hunt_options_unusual(self) -> Dict[str, CatalystStock]:
        """Detect unusual options volume/volatility (smart money signal)."""
        catalysts = {}
        
        try:
            # In production, use options data provider
            # Check for unusual call/put activity
            
            url = "https://www.barchart.com/options/unusual-activity"
            resp = self.session.get(url, timeout=5)
            resp.raise_for_status()
            
            try:
                from bs4 import BeautifulSoup
            except ImportError:
                logger.debug("BeautifulSoup not available, skipping options parsing")
                return catalysts
            
            soup = BeautifulSoup(resp.text, 'html.parser')
            
            rows = soup.find_all('tr')
            
            for row in rows[:30]:
                cells = row.find_all('td')
                if len(cells) >= 4:
                    try:
                        symbol = cells[0].text.strip()
                        volume_ratio = float(cells[2].text.strip().replace('x', ''))
                        
                        if volume_ratio > 3.0:  # 3x normal volume
                            signal = CatalystSignal(
                                symbol=symbol,
                                catalyst_type="options_unusual",
                                source="options_market",
                                headline=f"Unusual options activity: {volume_ratio:.1f}x volume",
                                confidence=0.75,
                                urgency=0.9,
                                bullish=True,  # Usually bullish for calls
                                magnitude=min(2.0, volume_ratio / 2),
                            )
                            
                            if self._is_new_signal(signal):
                                if symbol not in catalysts:
                                    catalysts[symbol] = CatalystStock(symbol)
                                catalysts[symbol].signals.append(signal)
                    except:
                        pass
            
            logger.info(f"[OPTIONS] Found {len(catalysts)} options unusual activity stocks")
            
        except Exception as e:
            logger.warning(f"Options unusual activity fetch failed (non-critical): {e}")
        
        return catalysts
    # ============================================================
    
    def _extract_symbols_from_text(self, text: str) -> List[str]:
        """Extract stock tickers from text - with validation."""
        import re
        
        # Known invalid symbols that commonly appear in Reddit
        invalid = {
            "THE", "AND", "FOR", "ARE", "BUT", "NOT", "CAN", "HAS", "HIS", "ITS",
            "OUR", "OUT", "WHO", "WHY", "HOW", "WAY", "DAY", "END", "GET", "GOT",
            "MAY", "OLD", "ONE", "PUT", "RAN", "SIT", "TON", "TOO", "TWO", "USE",
            "WAS", "WAY", "WHO", "WIN", "YES", "YET", "YOU", "ALL", "BAD", "BIG",
            "NEW", "ODD", "RED", "SAD", "TOP", "TRY", "BUY", "NOW", "PAY", "RUN",
            "SAY", "SET", "SHE", "TRY", "GET", "HAS", "AGE", "LAY", "SAW", "BET",
            "BOX", "BOY", "CAR", "CUT", "DOG", "EAR", "EAT", "EYE", "FUN", "GAS",
            "MAN", "RAN", "SEE", "SUN", "LET", "LOT", "RUB", "WIN", "UP", "OR",
            "IT", "DO", "SO", "AT", "NO", "GO", "BY", "BE", "ME", "HE", "WE", "MY",
            "LI", "LA", "YES", "OK", "Y", "I", "A", "OK", "HI", "BO", "CO", "CR",
            "DI", "DR", "FI", "FO", "GO", "GU", "HI", "HO", "JR", "LO", "MI", "MO",
            "NI", "OI", "PI", "RE", "SH", "SI", "SO", "ST", "TE", "TI", "TO", "UN",
            "VI", "VO", "WI", "WO", "XI", "YO", "ZA",
            # Additional invalid
            "YAHOO", "RBI", "BTC", "ETH", "BACK", "CCC", "EMAT", "HDD", "IRON",
            # Country codes
            "US", "UK", "IN", "RU", "CN", "BR", "DE", "FR", "JP", "AU", "CA", "MX",
            # Time periods (YTD=Year-To-Date, QTD=Quarter-To-Date, MTD=Month-To-Date, etc.)
            "YTD", "QTD", "MTD", "WTD", "TD",
        }
        
        # Look for patterns like: NVDA, $NVDA, TSLA
        # Prioritize: $SYMBOL patterns (most reliable)
        dollar_symbols = re.findall(r'\$([A-Z]{2,5})\b', text)
        if dollar_symbols:
            return [s for s in dollar_symbols if s not in invalid]
        
        # Fallback: SYMBOL in parentheses like (NVDA)
        paren_symbols = re.findall(r'\(([A-Z]{2,5})\)', text)
        if paren_symbols:
            return [s for s in paren_symbols if s not in invalid]
        
        # Last resort: ANY uppercase 2-5 letter word (less reliable)
        # But filter to known likely stock patterns with strong keywords
        all_symbols = re.findall(r'\b([A-Z]{2,5})\b', text)
        
        # Very strict filtering: only return if we're somewhat confident
        candidates = []
        text_lower = text.lower()
        
        for s in all_symbols:
            if s not in invalid and len(s) >= 2 and len(s) <= 5:
                # Additional heuristics: symbols should be near strong trading/investment keywords
                # Check if symbol is mentioned near strong trading verbs
                strong_keywords = ["buy", "sell", "long", "short", "position", "holding", "bought", "sold", "bullish", "bearish", "upgrade", "downgrade", "rating"]
                
                has_strong_keyword = any(keyword in text_lower for keyword in strong_keywords)
                
                if "$" in text and s in text:
                    # $ prefix is very reliable
                    candidates.append(s)
                elif "(" in text and f"({s})" in text:
                    # Already handled above, but just in case
                    candidates.append(s)
                elif has_strong_keyword:
                    # Only extract if we have strong trading context
                    candidates.append(s)
        
        return list(set(candidates))  # Deduplicate
    
    def _classify_news(self, headline: str) -> str:
        """Classify news headline into catalyst type."""
        headline_lower = headline.lower()
        
        keywords = {
            "earnings": ["earnings", "profit", "revenue", "guidance"],
            "upgrade": ["upgrade", "rating increase", "outperform", "buy"],
            "product": ["product", "launch", "announced", "new", "fda approval"],
            "acquisition": ["acquisition", "acquire", "merger", "merged", "buyout"],
        }
        
        for cat, words in keywords.items():
            if any(word in headline_lower for word in words):
                return cat
        
        return "news"
    
    def _is_bullish(self, text: str) -> bool:
        """Estimate if text is bullish or bearish."""
        bullish_words = ["beat", "surge", "soar", "gains", "upgrade", "buy", "positive", "record", "growth"]
        bearish_words = ["miss", "plunge", "falls", "downgrade", "sell", "negative", "loss", "decline"]
        
        text_lower = text.lower()
        bullish_count = sum(1 for word in bullish_words if word in text_lower)
        bearish_count = sum(1 for word in bearish_words if word in text_lower)
        
        return bullish_count >= bearish_count
    
    def _confidence_for_type(self, catalyst_type: str) -> float:
        """Base confidence by catalyst type."""
        confidence_map = {
            "earnings": 0.95,
            "upgrade": 0.85,
            "product": 0.7,
            "acquisition": 0.8,
            "volume_spike": 0.6,
            "social_buzz": 0.5,
            "insider_buy": 0.9,
            "options_unusual": 0.75,
        }
        return confidence_map.get(catalyst_type, 0.6)
    
    def _is_new_signal(self, signal: CatalystSignal) -> bool:
        """Check if signal is new (deduplication)."""
        sig_key = f"{signal.symbol}_{signal.catalyst_type}_{signal.headline[:30]}"
        if sig_key in self.seen_signals:
            return False
        self.seen_signals.add(sig_key)
        return True
    
    def hunt_all_sources(self) -> Dict[str, CatalystStock]:
        """Run full catalyst hunt across all sources."""
        logger.info("🔍 [CATALYST HUNTER] Starting multi-source scan...")
        
        all_catalysts = {}
        
        sources = [
            ("Finnhub News", self.hunt_finnhub_news),
            ("Earnings Surprises", self.hunt_earnings_surprises),
            ("Yahoo Trending", self.hunt_yahoo_trending),
            ("Reddit Social", self.hunt_reddit_mentions),
            ("Insider Activity", self.hunt_insider_activity),
            ("Options Unusual", self.hunt_options_unusual),
        ]
        
        for source_name, source_fn in sources:
            try:
                logger.info(f"  Hunting {source_name}...")
                results = source_fn()
                
                # Merge results
                for symbol, stock in results.items():
                    if symbol not in all_catalysts:
                        all_catalysts[symbol] = stock
                    else:
                        all_catalysts[symbol].signals.extend(stock.signals)
                
            except Exception as e:
                logger.error(f"Error in {source_name}: {e}")
        
        # Sort by combined score
        ranked = sorted(
            all_catalysts.items(),
            key=lambda x: x[1].combined_score,
            reverse=True
        )
        
        logger.info(f"✅ [CATALYST HUNTER] Found {len(ranked)} catalyst stocks")
        for symbol, stock in ranked[:10]:
            types = ", ".join(stock.signal_types)
            logger.info(f"  {symbol}: score={stock.combined_score:.1f} | signals={types}")
        
        return dict(ranked)
