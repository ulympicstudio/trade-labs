                                                                                                                        # --- Cluster-key consensus tracking ---
                                                                                                            # --- Cluster-key consensus tracking ---
                                                                                                # --- Cluster-key consensus tracking ---
                                                                                    # --- Cluster-key consensus tracking ---
                                                                        # --- Cluster-key consensus tracking ---
                                                            # --- Cluster-key consensus tracking ---
                                                # --- Cluster-key consensus tracking ---
                                    # --- Cluster-key consensus tracking ---
                        # --- Cluster-key consensus tracking ---
            # --- Cluster-key consensus tracking ---
# --- Cluster-key consensus helpers ---
import re
_STOPWORDS = set([
    'the','a','an','and','or','but','if','in','on','at','to','for','of','by','with','as','is','are','was','were','be','been','has','had','have','from','that','this','it','its','he','she','they','them','his','her','their','will','would','can','could','should','may','might','do','does','did','so','such','not','no','yes','up','down','out','over','under','again','more','most','some','any','each','other','than','then','now','only','own','same','too','very','s','t','just','don','should','ll','d','re','ve','m','o'
])
def _extract_story_tokens(headline: str) -> tuple[str, ...]:
    # Strip publisher attribution (e.g. " - Reuters") before tokenising
    headline = re.sub(r'\s+-\s+[A-Z][A-Za-z .]+$', '', headline).strip()
    tokens = re.findall(r"\b\w+\b", headline.lower())
    def _stem(w):
        if w.endswith('ing') and len(w) > 4: return w[:-3]
        if w.endswith('ed') and len(w) > 3: return w[:-2]
        if w.endswith('s') and len(w) > 3: return w[:-1]
        return w
    return tuple(sorted(_stem(t) for t in tokens if t not in _STOPWORDS and len(t) > 2))
def _primary_symbol(art):
    if 'symbol' in art and art['symbol']:
        return art['symbol']
    rel = art.get('related', [])
    if rel:
        return rel[0]
    return ''
def _cluster_key(art):
    ts = art.get('ts')
    if hasattr(ts, 'timestamp'):
        ts = ts.timestamp()
    elif isinstance(ts, (int, float)):
        pass
    else:
        ts = time.time()
    bucket = int(ts // (15*60))
    sym = _primary_symbol(art)
    tokens = sorted(_extract_story_tokens(art.get('headline','')))[:3]
    return f"{bucket}:{sym}:{','.join(tokens)}"

import os
import json
import random
import re
import signal
import threading
import time
import logging
import requests
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional, Set, Tuple

_NEWS_ENABLE_FINNHUB = os.environ.get("TL_NEWS_ENABLE_FINNHUB", "false").lower() in ("1", "true", "yes")
_SYMBOLS = ["SPY", "QQQ", "AAPL", "MSFT", "NVDA"]
_BASE_SYMBOLS: list[str] = [
    s.strip().upper()
    for s in os.environ.get("BASE_SYMBOLS", ",".join(_SYMBOLS)).split(",")
    if s.strip()
]
_NEWS_DEDUPE_WINDOW_S = float(
    os.environ.get("TL_NEWS_DEDUPE_WINDOW_S",
                   os.environ.get("NEWS_DEDUPE_WINDOW_S", "3600"))
)
_USE_IB = os.environ.get("TL_INGEST_USE_IB", "0").lower() in ("1", "true", "yes")
_NEWS_PROVIDER = os.environ.get("NEWS_PROVIDER_PRIMARY", "benzinga").lower()
_BENZINGA_NEWS_MAX_ITEMS = int(os.environ.get("BENZINGA_NEWS_MAX_ITEMS", "50"))
_NEWS_DAYS = int(os.environ.get("TL_INGEST_NEWS_DAYS", "1"))
_NEWS_PROVIDERS = [s.strip().lower() for s in os.environ.get("NEWS_PROVIDERS", "benzinga,gnews").split(",") if s.strip()]
def _fetch_rss_news():
    return [], "not_implemented"
_NEWS_INTERVAL_S = float(os.environ.get("TL_INGEST_NEWS_INTERVAL_S", "20"))
_NEWS_CANONICALIZE_GNEWS = os.environ.get("TL_NEWS_CANONICALIZE_GNEWS", "true").lower() in ("1", "true", "yes")
_NEWS_CANONICALIZE_MAX_PER_POLL = int(os.environ.get("TL_NEWS_CANONICALIZE_MAX_PER_POLL", "50"))
_NEWS_CANONICALIZE_TIMEOUT_S = float(os.environ.get("TL_NEWS_CANONICALIZE_TIMEOUT_S", "3.0"))
_NEWS_CANONICALIZE_DEBUG = os.environ.get("TL_NEWS_CANONICALIZE_DEBUG", "false").lower() in ("1", "true", "yes")
_NEWS_RESOLVE_REDIRECTS = os.environ.get("TL_NEWS_RESOLVE_REDIRECTS", "false").lower() in ("1", "true", "yes")
_NEWS_RESOLVE_TIMEOUT_S = float(os.environ.get("TL_NEWS_RESOLVE_TIMEOUT_S", "2.0"))
_NEWS_RESOLVE_MAX = int(os.environ.get("TL_NEWS_RESOLVE_MAX", "20"))
_NEWS_CONSENSUS_BOOST_ENABLED = os.environ.get("TL_NEWS_CONSENSUS_BOOST_ENABLED", "true").lower() in ("1", "true", "yes")
_NEWS_CONSENSUS_BOOST = int(os.environ.get("TL_NEWS_CONSENSUS_BOOST", "2"))
_NEWS_MAX_PUBLISHED_PER_POLL = int(os.environ.get("TL_NEWS_MAX_PUBLISHED_PER_POLL", "100"))
_POLL_INTERVAL_S = float(os.environ.get("TL_INGEST_INTERVAL_S", "10"))



"""
Ingest Arm — market-data and news ingestion.

Responsibilities
----------------
* Poll market data (via IB when connected, or stub prices) at a
    configurable interval and publish **MarketSnapshot** messages.
* Poll news (via Benzinga general-news endpoint) and publish
    **NewsEvent** messages, deduplicating by *(symbol, headline)*.
* Emit periodic heartbeats so the monitor arm can track liveness.

Configuration (env vars)
------------------------
``TL_INGEST_SYMBOLS``      — comma-separated watchlist (default ``SPY,QQQ,AAPL,MSFT,NVDA``)
``TL_INGEST_INTERVAL_S``   — seconds between market-data polls (default ``10``)
``TL_INGEST_NEWS_INTERVAL_S`` — seconds between news polls (default ``20``)
``TL_INGEST_NEWS_DAYS``    — look-back days for news fetch (default ``1``)
``TL_INGEST_USE_IB``       — ``1`` to attempt IB market data (default ``0``)
``BENZINGA_API_KEY``       — required for Benzinga news
``NEWS_PROVIDER_PRIMARY``  — ``benzinga`` (default) or ``rss``

Run::

        python -m src.arms.ingest_main
"""

import json
import os
import random
import re
import signal
import threading
import time
import logging
import requests
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional, Set, Tuple

from src.bus.bus_factory import get_bus
from src.bus.topics import HEARTBEAT, MARKET_SNAPSHOT, NEWS_EVENT, UNIVERSE_CANDIDATES
from src.config.settings import settings
from src.market.session import get_us_equity_session, PREMARKET
from src.monitoring.logger import get_logger
from src.schemas.messages import Heartbeat, MarketSnapshot, NewsEvent, UniverseCandidates
from src.utils.playbook_io import load_playbook_symbols
from src.utils.price_cache import load_prices, save_prices
from src.signals.squeeze import get_watchlist as _squeeze_watchlist
from src.signals.agent_intel import get_all_active_intel as _get_agent_intel

log = get_logger("ingest")
log.info("ingest_main module loaded (v2026-03-05a)")  # version marker for confirming code reload

# ── Tunables from environment ────────────────────────────────────────

## All legacy/duplicate helpers removed. Use the standardized helpers defined above.
# Optional static liquid universe (merged into base at init)
_LIQUID_UNIVERSE: list[str] = [
    s.strip().upper()
    for s in os.environ.get("LIQUID_UNIVERSE", "").split(",")
    if s.strip()
]
# File-based liquid universe seed list
_LIQUID_UNIVERSE_PATH: str = os.environ.get(
    "LIQUID_UNIVERSE_PATH", "data/liquid_universe.txt"
)
_UNIVERSE_MAX: int = int(os.environ.get("UNIVERSE_MAX", "500"))
_UNIVERSE_REFRESH_S: float = float(os.environ.get("UNIVERSE_REFRESH_S", "900"))
_UNIVERSE_SYMBOLS_PER_POLL: int = int(os.environ.get("TL_SYMBOLS_PER_POLL", "40"))
_UNIVERSE_LOG_INTERVAL_S: float = 300.0  # log universe size every 5 min
_SQUEEZE_UNIVERSE_TOP_N: int = int(os.environ.get("TL_SQUEEZE_UNIVERSE_TOP_N", "25"))
_SQUEEZE_MIN_SCORE: int = int(os.environ.get("TL_SQUEEZE_MIN_SCORE", "30"))
_TICKER_RE = re.compile(r"\b([A-Z]{1,5})\b")  # parse uppercase tickers from headlines
# Common false-positive words to exclude from ticker extraction
_TICKER_BLACKLIST = frozenset({
    # Titles / acronyms
    "CEO", "CFO", "CTO", "COO", "IPO", "SEC", "FDA", "GDP", "CPI", "ETF",
    "NYSE", "DOJ", "FBI", "EPA", "FTC", "IRS", "IMF", "WHO", "CDC",
    "OPEC", "NATO", "NASA", "FDIC", "FOMC", "SPAC",
    # Media / network names
    "CNBC", "CNN", "BBC", "NBC", "CBS", "ABC", "PBS", "NPR", "FOX",
    "WSJ", "AP", "AFP", "MSNBC",
    # Common English words
    "THE", "FOR", "AND", "BUT", "NOT", "ARE", "WAS", "HAS",
    "ITS", "NEW", "NOW", "ALL", "OUT", "DAY", "BIG", "TOP", "LOW",
    "HIGH", "SAY", "MAY", "CAN", "SET", "RUN", "HIT", "CUT", "BUY",
    "PUT", "GET", "HOW", "WHY", "JUST", "WILL", "OVER",
    "FROM", "MORE", "MOST", "YEAR", "THAT", "THIS", "WITH", "WHAT",
    "THAN", "BEEN", "DOWN", "NEXT", "AFTER", "BACK", "ALSO",
    "SELL", "HOLD", "MOVE", "JULY", "JUNE", "SEPT", "SAYS", "COULD",
    "MAKE", "TAKE", "SEEN", "SOME", "MUCH", "ONLY", "VERY",
    "DEAL", "PLAN", "NEWS", "DATA", "RISE", "FALL", "GAIN", "LOSS",
    "CORP", "INC", "LTD", "LLC",
    # Tech / jargon false positives
    "AI", "API", "CPU", "GPU", "RAM", "USB", "HTML", "HTTP",
    "COBOL", "COVID", "VIRUS",
    # Short noise
    "I", "A", "UP", "ON", "AT", "TO", "IN", "BY", "OR", "SO", "IF",
    "AN", "DO", "GO", "NO", "US", "IT", "MY", "WE", "HE",
})

# ── Global state ─────────────────────────────────────────────────────

_running = True
_stop_event = threading.Event()                 # cooperative shutdown
_stopping = False                               # set once — skip all network I/O
_news_seen: Dict[Tuple[str, str], float] = {}  # (symbol, headline) → expiry epoch
_NEWS_DEDUPE_TTL_S = 86_400.0                   # 24 h TTL for dedupe entries
_MAX_DEDUPE_SIZE = 50_000                       # cap memory
_NEWS_FETCH_TIMEOUT_S = float(os.environ.get("NEWS_FETCH_TIMEOUT_S", "60"))  # hard cap for news API calls
_NEWS_BURST_CAP = int(os.environ.get("NEWS_BURST_CAP", "200"))  # max events published per poll cycle
_SYMBOL_SET: set[str] = {s.upper() for s in _SYMBOLS}  # O(1) universe filter

# ── Legend Phase 2: Cross-provider dedupe/consensus state ────────────
import hashlib as _hashlib
import re as _re_mod
from urllib.parse import urlparse, urlencode, parse_qs

_NEWS_CONSENSUS_DEBUG: bool = os.environ.get(
    "TL_NEWS_CONSENSUS_DEBUG", "false"
).lower() in ("1", "true", "yes")

# Story-level fingerprint → {ts, providers, tokens, bucket}
# Story fingerprint cache (legacy, still used for some dedupe)
_story_fp_cache: Dict[str, dict] = {}
# Bucket index: bucket_int → list[fingerprint]  (for fuzzy scan)
_story_bucket_index: Dict[int, list] = {}
# --- Cluster-key consensus state ---
_story_cluster_cache: Dict[str, dict] = {}  # cluster_key → {ts, providers, example, symbols}
def _extract_canonical_domain(url: str) -> str:
    try:
        if not url:
            return "unknown"
        p = urlparse(url)
        host = p.netloc.lower()
        if host:
            # Remove www.
            if host.startswith("www."):
                host = host[4:]
            return host
    except Exception:
        pass
    return "unknown"

def _publisher_hint_domain_from_headline(headline: str) -> str:
    # Try to extract a publisher domain from the headline (very basic)
    # e.g. "Reuters: ..." or "(Bloomberg) ..."
    import re
    m = re.match(r"([A-Za-z0-9\-\.]+)[:\)]", headline)
    if m:
        return m.group(1).lower()
    return "unknown"

def _compute_cluster_key(art: dict) -> str:
    # bucket15m
    bucket = str(_article_bucket(art))
    # top_tokens(headline) — attribution already stripped inside _extract_story_tokens
    tokens = sorted(_extract_story_tokens(art.get("headline", "")))[:3]
    # symbols
    symbols = sorted(_article_symbols(art))
    # Domain is intentionally EXCLUDED so cross-publisher stories cluster.
    key_str = bucket + "|" + ",".join(tokens) + "|" + ",".join(symbols)
    return _hashlib.md5(key_str.encode("utf-8")).hexdigest()

def _prune_story_cluster_cache():
    now = time.time()
    cutoff = now - _NEWS_DEDUPE_WINDOW_S
    expired = [k for k, v in _story_cluster_cache.items() if v["ts"] < cutoff]
    for k in expired:
        del _story_cluster_cache[k]
# Per-(symbol, fingerprint) publish-dedupe
_mp_dedupe_cache: Dict[Tuple[str, str], dict] = {}  # (symbol, fp) → {ts, providers}
_MP_DEDUPE_MAX = 100_000
_STORY_FP_MAX = 50_000
_FUZZY_JACCARD_THRESHOLD = float(os.environ.get("TL_FUZZY_JACCARD_THRESHOLD", "0.20"))
_FUZZY_MAX_CANDIDATES = 50

# Tracking params stripped during URL normalisation
_URL_STRIP_PARAMS = frozenset({
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "fbclid", "gclid", "msclkid", "ref", "source", "si",
})


# --- Standardized helpers ---
def _canonicalize_url(url: str) -> str:
    """Strips tracking params and normalizes to scheme://netloc/path. Never raises."""
    try:
        p = urlparse(url)
        netloc = p.netloc.lower()
        if netloc.startswith("www."):
            netloc = netloc[4:]
        return f"{p.scheme}://{netloc}{p.path}" if netloc else url
    except Exception:
        return url

def _resolve_redirect_url(url: str, timeout_s: float) -> str | None:
    """HEAD allow_redirects=True, fallback GET. Never raises."""
    import requests
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,*/*",
    }
    try:
        resp = requests.head(url, allow_redirects=True, timeout=timeout_s, headers=headers)
        if resp.url and resp.url != url:
            return resp.url
    except Exception:
        pass
    try:
        resp = requests.get(url, allow_redirects=True, timeout=timeout_s, headers=headers, stream=True)
        if resp.url and resp.url != url:
            return resp.url
    except Exception:
        pass
    return None


# Domains to reject when extracting publisher URLs from Google News blobs
_GOOGLE_DOMAIN_BLOCKLIST = frozenset({
    "news.google.com", "google.com", "www.google.com",
    "accounts.google.com", "consent.google.com",
    "play.google.com", "support.google.com",
    "googleusercontent.com", "googlesyndication.com",
    "googleapis.com", "gstatic.com",
    "youtube.com", "www.youtube.com",
    "doubleclick.net",
    "google-analytics.com", "www.google-analytics.com",
    "googletagmanager.com", "angular.dev",
})


def _is_google_domain(candidate_url: str) -> bool:
    """Check if a URL's domain belongs to Google/blocked infrastructure."""
    try:
        p = urlparse(candidate_url)
        dom = p.netloc.lower()
        if dom.startswith("www."):
            dom = dom[4:]
        if dom in _GOOGLE_DOMAIN_BLOCKLIST:
            return True
        # Catch any remaining *.google.com subdomains
        if dom.endswith(".google.com") or dom == "google.com":
            return True
        # Catch *.googleapis.com, *.googleusercontent.com, etc.
        for suffix in (".googleapis.com", ".googleusercontent.com",
                        ".googlesyndication.com", ".gstatic.com",
                        ".doubleclick.net"):
            if dom.endswith(suffix):
                return True
        return False
    except Exception:
        return True  # if we can't parse, reject it


def _extract_gnews_rss_target(url: str) -> str | None:
    """Extract the real publisher URL from a Google News RSS article link.

    GNews RSS links look like:
        https://news.google.com/rss/articles/<base64url-token>?...
    The token is typically URL-safe base64 WITHOUT padding.
    The decoded payload is a small protobuf blob that embeds the target URL.

    Collects ALL http(s) URLs from decoded bytes, returns the first one
    whose domain is NOT in the Google blocklist.  Returns None if only
    Google/infra URLs are found (so the HTML-fetch fallback can run).
    """
    import base64 as _b64
    try:
        p = urlparse(url)
        path = p.path  # e.g. /rss/articles/CBMi...
        if "/articles/" not in path:
            return None
        token = path.split("/articles/")[-1]
        # Strip trailing segments and query params
        token = token.split("/")[0].split("?")[0]
        if not token or len(token) < 20:
            return None

        # Terminator bytes for URL extraction from protobuf blobs
        _TERM = set(b' \t\n\r"\'\\\x3c\x3e\x00\x01\x02\x03\x04\x05\x06\x07\x08')

        def _scan_all_urls(data: bytes) -> list[str]:
            """Extract ALL http(s) URLs from raw bytes."""
            found: list[str] = []
            for prefix in (b"https://", b"http://"):
                start = 0
                while True:
                    idx = data.find(prefix, start)
                    if idx < 0:
                        break
                    end = idx + len(prefix)
                    while end < len(data) and data[end] not in _TERM:
                        end += 1
                    candidate = data[idx:end].decode("ascii", errors="ignore")
                    if len(candidate) > 12:
                        found.append(candidate)
                    start = idx + 1
            return found

        def _pick_publisher(urls: list[str]) -> str | None:
            """Return the first URL whose domain is not Google infra."""
            for u in urls:
                if not _is_google_domain(u):
                    return u
            return None

        # Attempt 1: URL-safe base64 decode (no padding)
        padded = token + "=" * (-len(token) % 4)
        try:
            decoded = _b64.urlsafe_b64decode(padded)
            hit = _pick_publisher(_scan_all_urls(decoded))
            if hit:
                return hit
        except Exception:
            pass

        # Attempt 2: standard base64 decode (validate=False tolerates junk)
        try:
            decoded2 = _b64.b64decode(padded, validate=False)
            hit2 = _pick_publisher(_scan_all_urls(decoded2))
            if hit2:
                return hit2
        except Exception:
            pass
    except Exception:
        pass
    return None


def _gnews_articles_url_from_rss(rss_url: str) -> str:
    """Convert ``/rss/articles/<token>`` → ``/articles/<token>``.

    The non-RSS variant of Google News article pages exposes the
    outbound publisher URL more reliably than the ``/rss/`` SPA page.
    If the URL doesn't match the expected pattern it is returned unchanged.
    """
    from urllib.parse import urlparse, urlunparse
    p = urlparse(rss_url)
    if p.path.startswith("/rss/articles/"):
        new_path = p.path.replace("/rss/articles/", "/articles/", 1)
        return urlunparse(p._replace(path=new_path))
    return rss_url


def _fetch_gnews_rss_article_page_target(url: str, timeout_s: float) -> str | None:
    """GET a Google News article page and extract the real publisher URL.

    Google News article pages typically embed the publisher URL in one of:
      - HTTP redirect (rare for RSS article links)
      - <link rel="canonical"> / <meta og:url>
      - <meta http-equiv="refresh">
      - Google redirect URLs: google.com/url?url=<percent-encoded-publisher-URL>
      - data-url / JSON "url":"..." attributes in inline JS/data
      - First bare non-google http(s) URL in body
    Never raises.
    """
    import requests
    import re as _re
    from urllib.parse import unquote as _unquote, urlparse as _urlparse, parse_qs as _parse_qs
    _BROWSER_UA = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
    try:
        resp = requests.get(
            url,
            timeout=timeout_s,
            headers={"User-Agent": _BROWSER_UA, "Accept": "text/html,*/*"},
            allow_redirects=True,
        )
        # If we redirected to a non-google page, that's the answer
        if resp.url and resp.url != url and not _is_google_domain(resp.url):
            return resp.url

        ct = resp.headers.get("content-type", "")
        if "html" not in ct.lower() and "xml" not in ct.lower():
            return None
        html = resp.text[:80_000]  # cap to avoid huge pages

        # Helper: validate a candidate URL is a real publisher link
        def _valid_publisher(candidate: str) -> bool:
            if not candidate or not candidate.startswith("http"):
                return False
            return not _is_google_domain(candidate)

        # Helper: extract url= param from a Google redirect URL
        def _extract_google_redirect_target(gurl: str) -> str | None:
            try:
                p = _urlparse(gurl)
                qs = _parse_qs(p.query)
                # Google uses ?url=... or ?q=...
                for key in ("url", "q"):
                    vals = qs.get(key, [])
                    if vals:
                        decoded = _unquote(vals[0])
                        if decoded.startswith("http") and _valid_publisher(decoded):
                            return decoded
            except Exception:
                pass
            return None

        # 1) <link rel="canonical" href="...">
        m = _re.search(r'<link[^>]+rel=["\']canonical["\'][^>]+href=["\']([^"\'>]+)', html, _re.I)
        if m:
            href = m.group(1)
            if _valid_publisher(href):
                return href
            # canonical might be a google redirect URL itself
            redir = _extract_google_redirect_target(href)
            if redir:
                return redir

        # 1b) <meta property="og:url" content="...">
        m = _re.search(r'<meta[^>]+property=["\']og:url["\'][^>]+content=["\']([^"\'>]+)', html, _re.I)
        if m:
            href = m.group(1)
            if _valid_publisher(href):
                return href
            redir = _extract_google_redirect_target(href)
            if redir:
                return redir

        # 2) <meta http-equiv="refresh" content="...;url=...">
        m = _re.search(r'<meta[^>]+http-equiv=["\']refresh["\'][^>]+content=["\'][^"\'>]*url=([^"\'>\s]+)', html, _re.I)
        if m:
            href = _unquote(m.group(1))
            if _valid_publisher(href):
                return href
            redir = _extract_google_redirect_target(href)
            if redir:
                return redir

        # 3) Google redirect URLs in the page:
        #    https://www.google.com/url?url=https%3A%2F%2Fpublisher.com%2Farticle&...
        for gm in _re.finditer(
            r'https?://(?:www\.)?google\.com/url\?[^\s"<>\']{10,1000}', html
        ):
            redir = _extract_google_redirect_target(gm.group(0))
            if redir:
                return redir

        # 4) JSON/data-attribute url fields:
        #    "url":"https://publisher.com/..."  or  data-url="https://..."
        for jm in _re.finditer(
            r'(?:"url"\s*:\s*"|data-url=["\'])([^"\'\\]{12,600})', html
        ):
            candidate = _unquote(jm.group(1))
            if candidate.startswith("http") and _valid_publisher(candidate):
                return candidate

        # 5) Percent-encoded URLs in href/src attributes (url=https%3A%2F%2F...)
        for pm in _re.finditer(
            r'[?&]url=(https?%3A%2F%2F[^\s"<>&]{10,600})', html
        ):
            decoded_url = _unquote(pm.group(1))
            if _valid_publisher(decoded_url):
                return decoded_url

        # 6) First bare non-google http(s) URL in body (last resort)
        for match in _re.finditer(r'https?://[^\s"<>\']{12,500}', html):
            candidate = match.group(0)
            if _valid_publisher(candidate):
                return candidate
    except Exception:
        pass
    return None

def _extract_domain(url: str) -> str:
    """Lower netloc, strip leading www., return 'unknown' if invalid."""
    try:
        p = urlparse(url)
        netloc = p.netloc.lower()
        if netloc.startswith("www."):
            netloc = netloc[4:]
        return netloc if netloc else "unknown"
    except Exception:
        return "unknown"


def _is_bad_canonical(url: str) -> bool:
    """Return True when *url* is empty, domain-only, or still a Google wrapper."""
    if not url:
        return True
    try:
        p = urlparse(url)
        if not p.netloc:
            return True
        # Domain-only: path is empty or just "/"
        if p.path in ("", "/"):
            return True
        # Still a Google News RSS wrapper
        if "news.google.com" in p.netloc.lower() and "/rss/articles/" in (p.path or ""):
            return True
    except Exception:
        return True
    return False


# --- Minimal local RSS fetcher for gnews ---
import xml.etree.ElementTree as ET
def _fetch_rss_urls(urls: list[str], timeout_s: float = 5.0, max_items_total: int = 30) -> tuple[list[dict], str]:
    import requests, time
    items = []
    for url in urls:
        try:
            resp = requests.get(url, timeout=timeout_s)
            if resp.status_code != 200:
                return [], f"http_{resp.status_code}"
            root = ET.fromstring(resp.content)
            for item in root.findall('.//item'):
                title = item.findtext('title') or ''
                link = item.findtext('link') or ''
                pubdate = item.findtext('pubDate') or ''
                # Try to parse pubDate to epoch seconds
                ts = time.time()
                try:
                    import email.utils
                    dt = email.utils.parsedate_to_datetime(pubdate)
                    ts = dt.timestamp()
                except Exception:
                    pass
                # Extract <source url="..."> for publisher domain
                source_el = item.find('source')
                source_url = ''
                source_name = ''
                if source_el is not None:
                    source_url = source_el.get('url', '')
                    source_name = (source_el.text or '').strip()
                items.append({
                    "headline": title,
                    "url": link,
                    "ts": ts,
                    "source_url": source_url,
                    "source_name": source_name,
                    "_provider": "gnews",
                })
                if len(items) >= max_items_total:
                    return items, "ok"
        except Exception as exc:
            return [], f"rss_parse_error:{exc}"
    return items, "ok"

# --- Google News Canonicalization Helpers ---
def _is_gnews_url(url: str) -> bool:
    if not url:
        return False
    try:
        u = url.lower()
        if "news.google.com" in u:
            return True
        if u.startswith("https://www.google.com/url") or u.startswith("http://www.google.com/url"):
            return True
        return False
    except Exception:
        return False

def _strip_tracking_params(url: str) -> str:
    try:
        from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
        p = urlparse(url)
        qs = parse_qs(p.query, keep_blank_values=False)
        clean_qs = {k: v for k, v in qs.items() if not (k.lower().startswith("utm_") or k.lower() in ("gclid", "fbclid", "msclkid", "ref", "source", "si"))}
        query = urlencode(clean_qs, doseq=True) if clean_qs else ""
        return urlunparse((p.scheme, p.netloc, p.path, '', query, ''))
    except Exception:
        return url

# Built-in stopwords for headline token extraction (kept small)
_STOPWORDS = frozenset({
    "the", "a", "an", "and", "or", "but", "in", "on", "at", "to", "for",
    "of", "with", "by", "from", "is", "are", "was", "were", "be", "been",
    "has", "have", "had", "do", "does", "did", "will", "would", "could",
    "should", "may", "might", "can", "shall", "not", "no", "its", "it",
    "this", "that", "these", "those", "he", "she", "they", "we", "you",
    "his", "her", "their", "our", "your", "who", "what", "which", "how",
    "when", "where", "why", "if", "then", "so", "as", "than", "into",
    "also", "just", "about", "up", "out", "new", "more", "after", "over",
    "says", "said", "per", "via", "all", "any", "some",
})
_TOKEN_RE = _re_mod.compile(r"[a-z0-9]+")


def _strip_gnews_attribution(headline: str) -> str:
    """Strip trailing publisher attribution from GNews headlines.

    Google News RSS headlines end with " - Publisher Name", e.g.
    ``'Apple stock surges on earnings beat - Reuters'``.
    Removing the attribution improves token overlap with Benzinga/other
    provider headlines about the same story.
    """
    # Match " - <One or two capitalised words>" at end of string
    m = _re_mod.search(r'\s+-\s+[A-Z][A-Za-z .]+$', headline)
    if m and len(headline) - m.start() < 40:  # sanity: attribution < 40 chars
        return headline[:m.start()].strip()
    return headline

# ── Premarket playbook priority ──────────────────────────────────────
_PREMARKET_POLL_INTERVAL_S: float = float(os.environ.get("TL_PREMARKET_POLL_S", "5"))
_playbook_symbols: list[str] = []              # loaded at start of PREMARKET session
_playbook_loaded: bool = False

# ── Dynamic universe state ───────────────────────────────────────────
_universe: Dict[str, float] = {}  # symbol → expiry epoch (TTL)
_UNIVERSE_SYMBOL_TTL_S = 86_400.0  # 24 h before auto-expire
_universe_lock = threading.Lock()
_round_robin_idx: int = 0  # current position in sorted universe for polling
_last_universe_log_ts: float = 0.0
_last_universe_refresh_ts: float = 0.0

# ── Priority queue: consensus/news-driven symbols for aggressive polling ──
_PRIORITY_TTL_S = float(os.environ.get("TL_PRIORITY_TTL_S", "600"))  # 10 min
_priority_queue: Dict[str, float] = {}  # symbol → expiry epoch
_priority_lock = threading.Lock()


def _promote_to_priority(symbol: str, reason: str = "") -> bool:
    """Add *symbol* to the priority queue for aggressive polling.

    Returns True if newly promoted.
    """
    sym = symbol.upper()
    now = time.time()
    with _priority_lock:
        is_new = sym not in _priority_queue or _priority_queue[sym] < now
        _priority_queue[sym] = now + _PRIORITY_TTL_S
    if is_new:
        log.info("priority_promote sym=%s reason=%s ttl=%ds", sym, reason, int(_PRIORITY_TTL_S))
    return is_new


def _get_priority_symbols() -> list[str]:
    """Return currently active priority symbols (not expired)."""
    now = time.time()
    with _priority_lock:
        active = [s for s, exp in _priority_queue.items() if exp > now]
        # Prune expired
        expired = [s for s, exp in _priority_queue.items() if exp <= now]
        for s in expired:
            del _priority_queue[s]
    return active

# ── Synthetic-quote state (PAPER mode only) ──────────────────────────
_SYNTH_RNGS: Dict[str, random.Random] = {}    # per-symbol independent RNG
_SYNTH_PREV_LAST: Dict[str, float] = {}      # symbol → last synthetic price
_SYNTH_VOLUME_CTR: Dict[str, int] = {}       # symbol → running volume counter
_SYNTH_SEED_PRICES: Dict[str, float] = {     # reasonable starting prices
    "SPY": 520.0, "QQQ": 440.0, "AAPL": 195.0,
    "MSFT": 420.0, "NVDA": 135.0,
}
_SYNTH_DEFAULT_SEED = 100.0
_cached_prices: Dict[str, float] = {}           # loaded from disk at startup
_SYNTH_INFO_LOG_INTERVAL = 3600.0                # log INFO once per symbol per hour
_synth_last_info_ts: Dict[str, float] = {}       # symbol → last INFO log epoch

# ── Per-symbol drift / vol params (created once per symbol) ──────────
#  drift: random uniform(-0.00002, 0.00002)   per tick
#  vol:   random uniform(0.0006,  0.0020)     0.06 % – 0.20 % per tick
_SYNTH_PARAMS: Dict[str, tuple] = {}  # symbol → (drift, vol)

# ── US symbol whitelist ───────────────────────────────────────────
_US_SYMBOLS_PATH = Path(settings.data_dir) / "us_symbols.json"
_valid_symbols: Set[str] = set()  # populated at startup by _load_valid_symbols()


def _load_valid_symbols() -> None:
    """Populate *_valid_symbols* from local cache ``data/us_symbols.json``.

    The cache is expected to already exist (pre-generated).  No live API
    call is made.
    """
    global _valid_symbols

    if _US_SYMBOLS_PATH.exists():
        try:
            data = json.loads(_US_SYMBOLS_PATH.read_text())
            _valid_symbols = {s.upper() for s in data if isinstance(s, str) and s}
            log.info(
                "Loaded %d valid US symbols from %s",
                len(_valid_symbols), _US_SYMBOLS_PATH,
            )
            return
        except Exception as exc:
            log.warning("Failed to read %s: %s", _US_SYMBOLS_PATH, exc)

    log.warning(
        "Symbol whitelist file not found at %s — universe filtering will "
        "accept all tickers (may include junk)", _US_SYMBOLS_PATH,
    )


# ── Strict symbol validator (pollution prevention) ──────────────────

# Hard denylist: country/geo codes, common false-positives that pass _TICKER_BLACKLIST
_SYMBOL_DENYLIST: frozenset = frozenset({
    # Country / geographic codes
    "UAE", "UK", "USA", "EU", "UK", "HK", "JP", "CN", "CA", "AU", "NZ",
    "DE", "FR", "IN", "BR", "SA", "KR", "TW", "SG", "MX", "IL", "TR",
    # Indices / non-tradable references
    "SPX", "NDX", "DJI", "VIX", "TNX", "DXY", "SOX",
    # Media / noise that slip past blacklist
    "EST", "PST", "UTC", "GMT", "PM", "AM", "EOD", "YTD", "QOQ", "MOM",
    "PE", "EPS", "CEO", "IPO", "SEC", "FED", "GDP", "CPI", "PPI",
    # Crypto tickers (not US equities)
    "BTC", "ETH", "SOL", "XRP", "DOGE", "ADA", "DOT", "AVAX",
})


def _validate_symbol(sym: str, source: str = "unknown") -> bool:
    """Return True if *sym* is a valid tradeable US equity symbol.

    Must pass all of:
    1. 1-6 uppercase alphanumeric characters
    2. Not in hard denylist
    3. Not in _TICKER_BLACKLIST
    4. In _valid_symbols whitelist (if loaded)

    Logs rejected symbols for observability.
    """
    s = sym.strip().upper()
    if not s or len(s) > 6 or not s.isalpha():
        return False
    if s in _SYMBOL_DENYLIST:
        _symbol_reject_counts[s] = _symbol_reject_counts.get(s, 0) + 1
        log.debug("symbol_rejected sym=%s reason=denylist source=%s", s, source)
        return False
    if s in _TICKER_BLACKLIST:
        return False  # silent — these are too noisy
    if _valid_symbols and s not in _valid_symbols:
        _symbol_reject_counts[s] = _symbol_reject_counts.get(s, 0) + 1
        log.debug("symbol_rejected sym=%s reason=not_in_whitelist source=%s", s, source)
        return False
    return True


_symbol_reject_counts: Dict[str, int] = {}
_last_symbol_reject_log_ts: float = 0.0


def _log_symbol_rejections() -> None:
    """Periodically log aggregate rejection counts (every 5 min)."""
    global _last_symbol_reject_log_ts
    now = time.time()
    if now - _last_symbol_reject_log_ts < 300.0:
        return
    _last_symbol_reject_log_ts = now
    if _symbol_reject_counts:
        # Top 10 most rejected
        top = sorted(_symbol_reject_counts.items(), key=lambda x: -x[1])[:10]
        total = sum(_symbol_reject_counts.values())
        log.info(
            "symbol_rejected summary total=%d top=%s",
            total, top,
        )
        _symbol_reject_counts.clear()


# ── RSI candle cache ───────────────────────────────────────────────
_RSI_PERIOD = 14
_RSI_MAX_CACHE = 200                             # keep at most N closes per symbol
_close_cache: Dict[str, list[float]] = {}        # symbol → [close_0, close_1, …]
_RSI_LOG_INTERVAL_S = 60.0                       # log RSI summary once per minute
_last_rsi_log_ts: float = 0.0
_rsi_warmup_logged: Set[str] = set()             # symbols that have logged warmup_complete

# Tracks how many REAL (non-synthetic) closes each symbol has received.
# RSI is not trustworthy until real_bar_count >= _RSI_WARMUP_THRESHOLD.
_real_bar_count: Dict[str, int] = {}
_RSI_WARMUP_THRESHOLD = int(os.environ.get('TL_RSI_WARMUP_BARS', str(_RSI_PERIOD)))  # env-overridable


# ── RVOL computation state ───────────────────────────────────────────
_RVOL_LOOKBACK: int = int(os.environ.get("TL_RVOL_LOOKBACK", "20"))
_rvol_vol_cache: Dict[str, list[int]] = {}       # symbol → [volume_0, volume_1, …]


def _compute_rvol(symbol: str, current_volume: int) -> Optional[float]:
    """Compute relative volume for *symbol*.  Returns None if insufficient data."""
    vols = _rvol_vol_cache.setdefault(symbol, [])
    vols.append(current_volume)
    if len(vols) > _RSI_MAX_CACHE:
        _rvol_vol_cache[symbol] = vols[-_RSI_MAX_CACHE:]
        vols = _rvol_vol_cache[symbol]
    if len(vols) < _RVOL_LOOKBACK + 1:
        return None  # need baseline
    baseline_vols = vols[-(_RVOL_LOOKBACK + 1):-1]
    avg = sum(baseline_vols) / len(baseline_vols) if baseline_vols else 0
    if avg <= 0:
        return None
    return round(current_volume / avg, 2)


def _seed_rsi_warmup(symbols: list[str]) -> None:
    """Pre-fill close cache with micro-jittered seed prices so RSI can
    produce a value after just 1-2 real bars instead of 15.

    This uses the known seed prices (for PAPER) or a neutral 100.0,
    generating ``_RSI_PERIOD`` synthetic closes with tiny random noise.
    The initial RSI will be ~50 (neutral) and will converge to real
    values quickly as real prices arrive.
    """
    import random
    seeded = 0
    for sym in symbols:
        if sym in _close_cache and len(_close_cache[sym]) >= _RSI_PERIOD + 1:
            continue  # already warm
        seed_price = _SYNTH_SEED_PRICES.get(sym, _cached_prices.get(sym, _SYNTH_DEFAULT_SEED))
        if seed_price == _SYNTH_DEFAULT_SEED and sym not in _SYNTH_SEED_PRICES:
            log.warning("rsi_warmup using $%.0f fallback for %s — no cached price", _SYNTH_DEFAULT_SEED, sym)
        rng = random.Random(hash(sym))  # deterministic per symbol
        # Generate RSI_PERIOD + 1 closes with tiny random walk
        closes = []
        price = seed_price
        for _ in range(_RSI_PERIOD + 1):
            price *= (1.0 + rng.gauss(0, 0.0003))  # ±0.03% noise
            closes.append(round(price, 2))
        existing = _close_cache.get(sym, [])
        _close_cache[sym] = closes + existing  # prepend seed data
        seeded += 1
        _real_bar_count[sym] = 0  # explicit: warmup starts at zero real bars
    if seeded > 0:
        log.info("rsi_warmup_seed seeded=%d symbols (pre-filled %d closes each)",
                 seeded, _RSI_PERIOD + 1)


def _synthetic_snapshot(symbol: str) -> MarketSnapshot:
    """Generate a random-walk synthetic quote for *symbol* (PAPER only).

    Each symbol gets its own seeded RNG so price paths are independent
    (no cross-symbol correlation).  Runs are still reproducible because
    the seed is derived from the symbol name.
    """
    # Get or create per-symbol RNG (seeded by symbol hash for reproducibility)
    rng = _SYNTH_RNGS.get(symbol)
    if rng is None:
        rng = random.Random(hash(symbol) & 0xFFFF_FFFF)
        _SYNTH_RNGS[symbol] = rng

    # Per-symbol drift / vol params (created once)
    if symbol not in _SYNTH_PARAMS:
        drift = rng.uniform(-0.00002, 0.00002)
        vol = rng.uniform(0.0006, 0.0020)  # 0.06 % – 0.20 % per tick
        _SYNTH_PARAMS[symbol] = (drift, vol)
    drift, vol = _SYNTH_PARAMS[symbol]

    prev = _SYNTH_PREV_LAST.get(
        symbol,
        _SYNTH_SEED_PRICES.get(symbol.upper(),
            _cached_prices.get(symbol.upper(), _SYNTH_DEFAULT_SEED)),
    )
    if prev == _SYNTH_DEFAULT_SEED and symbol.upper() not in _SYNTH_SEED_PRICES and symbol.upper() not in _cached_prices:
        log.warning("synth_snapshot using $%.0f fallback for %s — no cached price", _SYNTH_DEFAULT_SEED, symbol)
    ret = rng.normalvariate(drift, vol)
    last = round(prev * (1.0 + ret), 2)
    if last <= 0:
        last = prev  # safety: never go negative
    _SYNTH_PREV_LAST[symbol] = last

    vol = _SYNTH_VOLUME_CTR.get(symbol, 0) + rng.randint(500, 5000)
    _SYNTH_VOLUME_CTR[symbol] = vol

    return MarketSnapshot(
        symbol=symbol,
        last=last,
        bid=round(last * 0.999, 2),
        ask=round(last * 1.001, 2),
        vwap=last,
        volume=vol,
        cum_volume=vol,   # synthetic: volume IS cumulative
        session="SYNTH",
    )


def _handle_signal(signum, _frame):
    global _running, _stopping
    log.info("Received shutdown signal (%s)", signum)
    _running = False
    _stopping = True
    _stop_event.set()


def _interruptible_sleep(seconds: float, *, resolution: float = 1.0) -> None:
    """Sleep for *seconds* but wake early if ``_stop_event`` is set.

    Checks the stop event every *resolution* seconds (default 1.0 s,
    **never** exceeds 1 s) so the arm can exit promptly on Ctrl-C.
    """
    resolution = min(resolution, 1.0)  # enforce <=1 s upper bound
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if _stop_event.is_set():
            return
        remaining = deadline - time.monotonic()
        _stop_event.wait(min(resolution, max(remaining, 0)))


# ── IB market-data helpers (optional) ────────────────────────────────

def _try_connect_ib():
    """Attempt to connect to IB Gateway / TWS.  Returns IB instance or None."""
    if not _USE_IB or _stopping:
        return None
    try:
        from src.data.ib_market_data import connect_ib  # type: ignore[import-untyped]
        ib = connect_ib()
        log.info("IB connected for market data")
        return ib
    except Exception as exc:
        log.warning("IB connection failed — falling back to stub prices: %s", exc)
        return None


def _fetch_ib_snapshot(ib, symbol: str) -> Optional[MarketSnapshot]:
    """Build a MarketSnapshot from IB live data."""
    if _stopping:
        return None
    try:
        from ib_insync import Stock
        from src.data.ib_market_data import get_last_price

        contract = Stock(symbol, "SMART", "USD")
        ib.qualifyContracts(contract)
        price = get_last_price(ib, contract)
        return MarketSnapshot(
            symbol=symbol,
            last=price,
            bid=price,   # best-effort; real depth requires streaming
            ask=price,
        )
    except Exception as exc:
        log.warning("IB snapshot for %s failed: %s", symbol, exc)
        return None


def _stub_snapshot(symbol: str) -> MarketSnapshot:
    """Return a zero-price placeholder when no live data source is available."""
    return MarketSnapshot(symbol=symbol, session="STUB")


# ── News helpers ─────────────────────────────────────────────────────

def _fetch_benzinga_news() -> tuple[list[dict], str]:
    """Fetch news from Benzinga general-news endpoint.

    Returns ``(items, skip_reason)`` where *items* is a list of dicts
    ``{"headline", "source", "url", "ts", "related_tickers", "summary"}``
    and *skip_reason* is empty on success or explains why fetch was skipped.
    """
    if _stopping:
        return [], "stopping"

    if _NEWS_PROVIDER == "rss":
        return [], "provider=rss"

    benzinga_key = os.environ.get("BENZINGA_API_KEY", "").strip()
    if not benzinga_key:
        return [], "no_benzinga_key"

    try:
        from src.data.news_fetcher import BenzingaNewsAPI

        api = BenzingaNewsAPI(api_key=benzinga_key)
        items, reason = api.fetch_general_news(max_items=_BENZINGA_NEWS_MAX_ITEMS)
        if reason:
            # Non-empty reason = Benzinga returned non-200 or errored
            return [], reason
        if not items:
            return [], "benzinga_empty"
        return items, ""
    except Exception as exc:
        log.warning("Benzinga news fetch failed: %s", exc)
        return [], f"error:{exc}"


def _fetch_rss_fallback(symbols: list[str]) -> list[dict]:
    """RSS-only fallback when Benzinga is unavailable."""
    if _stopping:
        return []
    results: list[dict] = []
    try:
        from src.data.news_fetcher import NewsFetcher

        fetcher = NewsFetcher()
        news_map = fetcher.fetch_news_for_symbols(symbols[:10], days_back=_NEWS_DAYS)
        for sym, articles in news_map.items():
            if _stopping:
                break
            for article in articles:
                results.append({
                    "headline": article.title,
                    "source": article.source,
                    "url": article.url,
                    "ts": None,
                    "related_tickers": [],
                    "summary": "",
                    "_primary_symbol": sym,
                })
        log.info("RSS fallback returned %d articles", len(results))
    except Exception as exc:
        log.warning("RSS fallback failed: %s", exc)
    return results


def _fetch_news_with_timeout() -> tuple[list[dict], str]:
    """Run Benzinga news fetch inside a worker thread with a hard timeout.

    Returns ``(items, skip_reason)``.
    """
    if _stopping:
        return [], "stopping"
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="news-fetch")
    future = pool.submit(_fetch_benzinga_news)
    try:
        return future.result(timeout=max(_NEWS_FETCH_TIMEOUT_S, 10.0))
    except FuturesTimeout:
        log.warning(
            "News fetch exceeded %ss timeout — skipping this cycle",
            _NEWS_FETCH_TIMEOUT_S,
        )
        future.cancel()
        return [], "timeout"
    except Exception as exc:
        log.warning("News fetch failed: %s", exc)
        return [], f"error:{exc}"
    finally:
        pool.shutdown(wait=False)


# ── Legend Phase 2: Multi-provider fetch ─────────────────────────────

def _fetch_finnhub_news() -> tuple[list[dict], str]:
    """Fetch from Finnhub tertiary provider, protected by rate-limit/cooldown."""
    if _stopping:
        return [], "stopping"
    try:
        from src.data.news_fetcher import fetch_finnhub_news
        return fetch_finnhub_news(max_items=50)
    except Exception as exc:
        log.warning("Finnhub news fetch failed: %s", exc)
        return [], f"error:{exc}"


def _fetch_multi_provider_news() -> dict[str, list[dict]]:
    """
    Fetch from all enabled providers.
    Returns: {provider: [article_dict, ...]}
    """
    results: dict[str, list[dict]] = {}
    log.info("news_providers_active=%s", _NEWS_PROVIDERS)
    for provider in _NEWS_PROVIDERS:
        try:
            # Ensure normalization cannot remap 'gnews'
            prov = provider.strip().lower()
            if prov == "gnews":
                items, reason = _fetch_gnews_news()
                log.info("provider=gnews fetched=%d reason=%s", len(items), reason)
                results["gnews"] = items
            elif prov == "benzinga":
                items, reason = _fetch_benzinga_news()
                log.info("provider=benzinga fetched=%d reason=%s", len(items), reason)
                results["benzinga"] = items
            elif prov == "rss":
                items, reason = _fetch_rss_news()
                log.info("provider=rss fetched=%d reason=%s", len(items), reason)
                results["rss"] = items
        except Exception as exc:
            log.warning("provider=%s error=%s", provider, exc)
            results[provider] = []
    # Google News Redirect Resolution is handled in the main ingest loop, not here.
    return results
# --- GNews fetcher ---
def _fetch_gnews_news() -> tuple[list[dict], str]:
    if _stopping:
        return [], "stopping"
    url = "https://news.google.com/rss/search?q=stock%20market&hl=en-US&gl=US&ceid=US:en"
    try:
        import requests, time
        import xml.etree.ElementTree as ET
        resp = requests.get(url, timeout=5.0)
        if resp.status_code != 200:
            return [], f"http_{resp.status_code}"
        root = ET.fromstring(resp.content)
        items = []
        for item in root.findall('.//item'):
            title = item.findtext('title') or ''
            link = item.findtext('link') or ''
            pubdate = item.findtext('pubDate') or ''
            ts = time.time()
            try:
                import email.utils
                dt = email.utils.parsedate_to_datetime(pubdate)
                ts = dt.timestamp()
            except Exception:
                pass
            # Extract <source url="..."> for publisher domain
            source_el = item.find('source')
            source_url = ''
            source_name = ''
            if source_el is not None:
                source_url = source_el.get('url', '')
                source_name = (source_el.text or '').strip()
            items.append({
                "headline": title,
                "url": link,
                "ts": ts,
                "source_url": source_url,
                "source_name": source_name,
                "provider": "gnews",
                "related_tickers": [],
            })
            if len(items) >= 30:
                break
        if items:
            return items, "ok"
        else:
            return [], "no_items"
    except Exception as exc:
        return [], f"error:{exc}"


# ── Story token extraction (for Tier B + C) ─────────────────────────

def _extract_story_tokens(headline: str) -> set[str]:
    """Extract normalised token set from headline for signature/fuzzy matching.

    Steps: strip publisher attribution → lowercase → extract alphanumeric
    tokens → drop stopwords → drop tokens < 3 chars → stem → keep top-10
    by sorted order (stable).
    """
    def _stem(w: str) -> str:
        if w.endswith('ing') and len(w) > 4: return w[:-3]
        if w.endswith('ed') and len(w) > 3: return w[:-2]
        if w.endswith('s') and len(w) > 3: return w[:-1]
        return w
    headline = _strip_gnews_attribution(headline)
    tokens = _TOKEN_RE.findall(headline.lower())
    tokens = [_stem(t) for t in tokens if t not in _STOPWORDS and len(t) >= 3]
    # Dedupe, sort, keep top 10 for a compact signature
    unique = sorted(set(tokens))
    return set(unique[:10])


def _article_symbols(article: dict) -> list[str]:
    """Collect sorted unique symbol list from article metadata."""
    syms: list[str] = []
    related = article.get("related_tickers") or []
    for t in related:
        t_up = t.strip().upper()
        if t_up and 1 <= len(t_up) <= 6:
            syms.append(t_up)
    primary = (article.get("_primary_symbol") or "").upper()
    if primary and primary not in syms:
        syms.append(primary)
    # Also check fan-out symbol
    sym = (article.get("symbol") or "").upper()
    if sym and sym not in syms:
        syms.append(sym)
    return sorted(set(syms))


def _article_bucket(article: dict) -> int:
    """15-minute time bucket from article timestamp."""
    ts = article.get("ts")
    if ts and hasattr(ts, "timestamp"):
        return int(ts.timestamp()) // 900
    return int(time.time()) // 900


def _jaccard(a: set, b: set) -> float:
    """Jaccard similarity between two sets."""
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _fuzzy_find_fp(token_set: set[str], bucket: int, title_hint: str) -> str | None:
    """Tier C: scan existing fingerprints in the same 15-min bucket.

    Returns the first fingerprint with Jaccard >= threshold, or None.
    Limited to _FUZZY_MAX_CANDIDATES comparisons to stay lightweight.
    """
    fps_in_bucket = _story_bucket_index.get(bucket)
    if not fps_in_bucket:
        return None
    now = time.time()
    checked = 0
    for fp in fps_in_bucket:
        if checked >= _FUZZY_MAX_CANDIDATES:
            break
        entry = _story_fp_cache.get(fp)
        if entry is None or now - entry["ts"] >= _NEWS_DEDUPE_WINDOW_S:
            continue
        other_tokens = entry.get("tokens")
        if not other_tokens:
            continue
        checked += 1
        sim = _jaccard(token_set, other_tokens)
        if sim >= _FUZZY_JACCARD_THRESHOLD:
            if _NEWS_CONSENSUS_DEBUG:
                log.debug(
                    "consensus_fuzzy_match -> fp=%s matched_fp=%s "
                    "jaccard=%.2f title='%s'",
                    "<new>", fp[:12], sim, title_hint[:80],
                )
            return fp
    return None


def _story_fingerprint(article: dict) -> str:
    """Produce a stable story-level fingerprint for cross-provider matching.

    3-Tier strategy (Phase 2.2):
      Tier A: normalised URL hash (same URL from different providers matches).
      Tier B: token-signature hash (stopword-filtered top-10 tokens + symbols + 15-min bucket).
      Tier C: fuzzy Jaccard scan — if a cached fingerprint in the same bucket
              shares >= 75 % tokens, re-use it (merge providers).
    """
    headline = article.get("headline", "")
    token_set = _extract_story_tokens(headline)
    sym_list = _article_symbols(article)
    bucket = _article_bucket(article)

    # ── Tier A: Canonical URL-based ──
    url = (article.get("canonical_url") or article.get("url") or "").strip()
    if url:
        norm_url = _canonicalize_url(url)
        if norm_url:
            fp_url = _hashlib.md5(
                norm_url.encode(), usedforsecurity=False
            ).hexdigest()[:20]
            if fp_url in _story_fp_cache:
                entry = _story_fp_cache[fp_url]
                if not entry.get("tokens"):
                    entry["tokens"] = token_set
                    entry["bucket"] = bucket
                return fp_url
            fuzzy_fp = _fuzzy_find_fp(token_set, bucket, headline) if token_set else None
            if fuzzy_fp:
                return fuzzy_fp
            return fp_url

    # ── Tier B: token-signature hash ─────────────────────────────────
    sym_key = ",".join(sym_list) if sym_list else "_"
    token_key = ",".join(sorted(token_set)) if token_set else "_"
    raw = f"{sym_key}|{token_key}|{bucket}"
    fp_sig = _hashlib.md5(raw.encode(), usedforsecurity=False).hexdigest()[:20]

    if fp_sig in _story_fp_cache:
        entry = _story_fp_cache[fp_sig]
        if not entry.get("tokens"):
            entry["tokens"] = token_set
            entry["bucket"] = bucket
        return fp_sig

    # ── Tier C: fuzzy Jaccard scan ───────────────────────────────────
    if token_set:
        fuzzy_fp = _fuzzy_find_fp(token_set, bucket, headline)
        if fuzzy_fp:
            return fuzzy_fp

    return fp_sig


def _story_register_provider(fp: str, provider: str,
                             token_set: set[str] | None = None,
                             bucket: int | None = None) -> int:
    """Register *provider* against story fingerprint.  Returns provider-set size.

    Also stores *token_set* and *bucket* so Tier C fuzzy matching can scan them.
    """
    now = time.time()
    entry = _story_fp_cache.get(fp)
    if entry is not None:
        if now - entry["ts"] < _NEWS_DEDUPE_WINDOW_S:
            entry["providers"].add(provider)
            # Backfill tokens/bucket if they weren't set
            if token_set and not entry.get("tokens"):
                entry["tokens"] = token_set
            if bucket is not None and entry.get("bucket") is None:
                entry["bucket"] = bucket
            return len(entry["providers"])
        # Expired — remove from bucket index too
        old_bucket = entry.get("bucket")
        if old_bucket is not None and old_bucket in _story_bucket_index:
            try:
                _story_bucket_index[old_bucket].remove(fp)
            except ValueError:
                pass
        del _story_fp_cache[fp]

    # Prune if too big
    if len(_story_fp_cache) >= _STORY_FP_MAX:
        cutoff = now - _NEWS_DEDUPE_WINDOW_S
        expired = [k for k, v in _story_fp_cache.items() if v["ts"] < cutoff]
        for k in expired:
            exp_bucket = _story_fp_cache[k].get("bucket")
            if exp_bucket is not None and exp_bucket in _story_bucket_index:
                try:
                    _story_bucket_index[exp_bucket].remove(k)
                except ValueError:
                    pass
            del _story_fp_cache[k]

    _story_fp_cache[fp] = {
        "ts": now,
        "providers": {provider},
        "tokens": token_set or set(),
        "bucket": bucket,
    }
    # Register in bucket index for Tier C scanning
    if bucket is not None:
        bucket_list = _story_bucket_index.setdefault(bucket, [])
        if fp not in bucket_list:
            bucket_list.append(fp)
    return 1


def _story_consensus_count(fp: str) -> int:
    """How many distinct providers have reported this story fingerprint."""
    entry = _story_fp_cache.get(fp)
    return len(entry["providers"]) if entry else 0


def _mp_dedupe_check(symbol: str, fp: str, provider: str, **kwargs) -> tuple[bool, int]:
    """Per-(symbol, fingerprint) publish-dedupe.

    Returns ``(is_new, n_providers_on_story)``.
    If the (symbol, fp) was already published, returns False so caller skips.
    The story-level provider set is always updated regardless.
    """
    now = time.time()

    # Always register at story level (even if symbol-level is a dupe)
    story_n = _story_register_provider(fp, provider,
                                       token_set=kwargs.get("token_set"),
                                       bucket=kwargs.get("bucket"))

    key = (symbol.upper(), fp)
    entry = _mp_dedupe_cache.get(key)
    if entry is not None:
        if now - entry["ts"] < _NEWS_DEDUPE_WINDOW_S:
            entry["providers"].add(provider)
            return False, story_n
        del _mp_dedupe_cache[key]

    # Prune if too big
    if len(_mp_dedupe_cache) >= _MP_DEDUPE_MAX:
        cutoff = now - _NEWS_DEDUPE_WINDOW_S
        expired = [k for k, v in _mp_dedupe_cache.items() if v["ts"] < cutoff]
        for k in expired:
            del _mp_dedupe_cache[k]
        if len(_mp_dedupe_cache) >= _MP_DEDUPE_MAX:
            by_age = sorted(_mp_dedupe_cache, key=lambda k: _mp_dedupe_cache[k]["ts"])
            for k in by_age[: len(by_age) // 2]:
                del _mp_dedupe_cache[k]

    _mp_dedupe_cache[key] = {"ts": now, "providers": {provider}}
    return True, story_n


def _dedupe_key(symbol: str, headline: str) -> Tuple[str, str]:
    """Canonical key for news deduplication."""
    return (symbol.upper(), headline.strip().lower())


def _is_new_news(symbol: str, headline: str) -> bool:
    """Return True if (symbol, headline) has not been seen within the TTL window."""
    now = time.time()
    key = _dedupe_key(symbol, headline)

    expiry = _news_seen.get(key)
    if expiry is not None and now < expiry:
        return False  # still within TTL — duplicate

    # Evict expired entries when the cache grows too large
    if len(_news_seen) >= _MAX_DEDUPE_SIZE:
        before = len(_news_seen)
        _news_seen.update(
            {k: v for k, v in _news_seen.items() if v > now}
        )  # keep only live entries
        # If still too big after eviction, drop oldest half
        if len(_news_seen) >= _MAX_DEDUPE_SIZE:
            sorted_keys = sorted(_news_seen, key=_news_seen.get)  # type: ignore[arg-type]
            for k in sorted_keys[: len(sorted_keys) // 2]:
                del _news_seen[k]
        log.debug("News dedupe cache pruned %d → %d", before, len(_news_seen))

    _news_seen[key] = now + _NEWS_DEDUPE_TTL_S
    return True


def _in_universe(symbol: str) -> bool:
    """Return True if *symbol* is in the dynamic universe."""
    sym = symbol.upper()
    with _universe_lock:
        return sym in _universe


# ── Universe management ──────────────────────────────────────────────

def _load_liquid_universe_file() -> list[str]:
    """Load tickers from ``LIQUID_UNIVERSE_PATH`` (one per line, # comments)."""
    path = Path(_LIQUID_UNIVERSE_PATH)
    if not path.exists():
        log.info("Liquid-universe file not found: %s", path)
        return []
    try:
        tickers: list[str] = []
        for raw in path.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            sym = line.split()[0].upper()  # first token; ignore comments
            if 1 <= len(sym) <= 6:
                tickers.append(sym)
        log.info(
            "Loaded %d tickers from liquid-universe file %s",
            len(tickers), path,
        )
        return tickers
    except Exception as exc:
        log.warning("Failed to read liquid-universe file %s: %s", path, exc)
        return []


def _init_universe() -> None:
    """Seed the universe with BASE_SYMBOLS + LIQUID_UNIVERSE + file list + agent intel."""
    file_tickers = _load_liquid_universe_file()

    # Load agent intel symbols (non-expired, non-AVOID)
    agent_syms: list[str] = []
    try:
        agent_intel = _get_agent_intel()
        for sym, info in agent_intel.items():
            if info.conviction != "AVOID":
                agent_syms.append(sym)
        if agent_syms:
            log.info("Agent intel: injecting %d symbols into universe: %s",
                     len(agent_syms), sorted(agent_syms))
    except Exception as exc:
        log.warning("Agent intel load failed during universe init (non-fatal): %s", exc)

    now = time.time()
    with _universe_lock:
        for sym in _BASE_SYMBOLS:
            _universe[sym] = now + _UNIVERSE_SYMBOL_TTL_S
        for sym in _LIQUID_UNIVERSE:
            if sym and sym not in _universe:
                _universe[sym] = now + _UNIVERSE_SYMBOL_TTL_S
        file_added = 0
        for sym in file_tickers:
            if sym not in _universe and len(_universe) < _UNIVERSE_MAX:
                _universe[sym] = now + _UNIVERSE_SYMBOL_TTL_S
                file_added += 1
        # Inject agent intel symbols (HIGH, MEDIUM, LOW conviction)
        agent_added = 0
        for sym in agent_syms:
            if sym not in _universe and len(_universe) < _UNIVERSE_MAX:
                _universe[sym] = now + _UNIVERSE_SYMBOL_TTL_S
                agent_added += 1
    log.info(
        "Universe seeded: total=%d  base=%d  liquid_env=%d  liquid_file=%d  "
        "agent_intel=%d  universe_max=%d  sample=%s",
        len(_universe), len(_BASE_SYMBOLS), len(_LIQUID_UNIVERSE),
        file_added, agent_added, _UNIVERSE_MAX,
        sorted(_universe.keys())[:15],
    )


def _add_to_universe(symbol: str) -> bool:
    """Add/refresh *symbol* in the universe. Returns True if newly added."""
    sym = symbol.upper()
    if not sym or len(sym) > 5:
        return False
    now = time.time()
    with _universe_lock:
        is_new = sym not in _universe
        if len(_universe) >= _UNIVERSE_MAX and is_new:
            return False  # at capacity
        _universe[sym] = now + _UNIVERSE_SYMBOL_TTL_S
    return is_new


def _prune_universe() -> int:
    """Remove expired symbols (except BASE_SYMBOLS). Returns count removed."""
    now = time.time()
    base = set(_BASE_SYMBOLS)
    removed = 0
    with _universe_lock:
        expired = [s for s, exp in _universe.items() if exp < now and s not in base]
        for s in expired:
            del _universe[s]
            removed += 1
    return removed


def _get_universe_list() -> list[str]:
    """Snapshot of current universe as a sorted list."""
    with _universe_lock:
        return sorted(_universe.keys())


def _get_poll_slice() -> list[str]:
    """Return the next round-robin slice of symbols to poll.

    Returns up to ``_UNIVERSE_SYMBOLS_PER_POLL`` symbols.  BASE_SYMBOLS
    are always included; the rest rotate.

    In PREMARKET session, playbook symbols are always included in
    every slice (alongside BASE_SYMBOLS) for priority scanning.
    """
    global _round_robin_idx
    all_syms = _get_universe_list()
    if not all_syms:
        return list(_BASE_SYMBOLS)

    # Build the always-included set
    always_set = set(_BASE_SYMBOLS)
    session = get_us_equity_session()
    if session == PREMARKET and _playbook_symbols:
        always_set.update(_playbook_symbols)
    # Priority queue symbols always polled
    priority_syms = _get_priority_symbols()
    always_set.update(priority_syms)
    always_list = sorted(always_set)

    non_always = [s for s in all_syms if s not in always_set]

    budget = max(0, _UNIVERSE_SYMBOLS_PER_POLL - len(always_list))
    if non_always and budget > 0:
        start = _round_robin_idx % len(non_always)
        selected = (non_always[start:] + non_always[:start])[:budget]
        _round_robin_idx = (start + budget) % max(len(non_always), 1)
    else:
        selected = []

    return always_list + selected


def _extract_tickers_from_headline(headline: str) -> list[str]:
    """Parse uppercase ticker-like words from a headline string."""
    matches = _TICKER_RE.findall(headline)
    return [m for m in matches if len(m) >= 2 and _validate_symbol(m, source="headline")]


# ── Symbol hygiene: provisional list & per-cycle cap ──────────────────
_MAX_ADDITIONS_PER_CYCLE = int(os.environ.get("TL_UNIVERSE_MAX_ADD_PER_CYCLE", "10"))
_PROVISIONAL_MIN_MENTIONS = int(os.environ.get("TL_PROVISIONAL_MIN_MENTIONS", "2"))
_news_provisional: Dict[str, int] = {}  # symbol → mention count (before full promotion)
_news_provisional_lock = threading.Lock()


def _provisional_check(symbol: str) -> bool:
    """Track news-only symbols.  Return True when mention count meets threshold
    and the symbol should be promoted to the full universe."""
    with _news_provisional_lock:
        count = _news_provisional.get(symbol, 0) + 1
        _news_provisional[symbol] = count
        if count >= _PROVISIONAL_MIN_MENTIONS:
            # Graduated — remove from provisional
            del _news_provisional[symbol]
            return True
    return False


def _expand_universe_from_news(articles: list[dict]) -> int:
    """Extract symbols from news articles and add to universe.

    Uses ``related_tickers`` field from API when present;
    falls back to headline ticker parsing.

    Only symbols present in the US-symbol whitelist (*_valid_symbols*)
    are accepted.  Applies hygiene controls:
    - Hard cap of ``_MAX_ADDITIONS_PER_CYCLE`` new symbols per poll
    - New symbols go to provisional list first; promoted after
      ``_PROVISIONAL_MIN_MENTIONS`` mentions

    Returns the count of newly added symbols.
    """
    added = 0
    raw_count = 0
    ignored_count = 0
    provisional_count = 0

    for art in articles:
        if added >= _MAX_ADDITIONS_PER_CYCLE:
            break  # hard cap per cycle

        # Primary: API-provided related tickers
        tickers = art.get("related_tickers") or art.get("tickers") or []
        if isinstance(tickers, str):
            tickers = [t.strip() for t in tickers.split(",") if t.strip()]

        # Fallback: parse from headline
        if not tickers:
            headline = art.get("headline", "")
            tickers = _extract_tickers_from_headline(headline)

        raw_count += len(tickers)

        for t in tickers:
            if added >= _MAX_ADDITIONS_PER_CYCLE:
                break
            t_upper = t.upper()
            if not _validate_symbol(t_upper, source="news_related"):
                ignored_count += 1
                continue
            # Already in universe? Just refresh TTL
            with _universe_lock:
                if t_upper in _universe:
                    _universe[t_upper] = time.time() + _UNIVERSE_SYMBOL_TTL_S
                    continue
            # Provisional gate: require repeat mention before full promotion
            if not _provisional_check(t_upper):
                provisional_count += 1
                log.debug("Universe provisional: %s (mention=%d/%d)",
                          t_upper, _news_provisional.get(t_upper, 0),
                          _PROVISIONAL_MIN_MENTIONS)
                continue
            if _add_to_universe(t_upper):
                added += 1
                log.debug("Universe expanded: +%s (from news, graduated provisional)", t_upper)

        # Also ensure the article's own symbol stays fresh
        sym = art.get("symbol", "")
        if sym:
            sym_upper = sym.upper()
            if _validate_symbol(sym_upper, source="news_primary"):
                _add_to_universe(sym_upper)

    valid_count = raw_count - ignored_count
    log.info(
        "News universe expansion: expanded_by_news_raw=%d  "
        "expanded_by_news_valid=%d  ignored_invalid=%d  newly_added=%d  "
        "provisional_held=%d  max_per_cycle=%d",
        raw_count, valid_count, ignored_count, added,
        provisional_count, _MAX_ADDITIONS_PER_CYCLE,
    )
    return added


# ── Main loop ────────────────────────────────────────────────────────

def main() -> None:
    """Entry-point for the ingest arm."""
    global _last_rsi_log_ts, _last_universe_log_ts, _last_universe_refresh_ts

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    # Load US symbol whitelist (from local cache)
    _load_valid_symbols()

    # Seed the dynamic universe
    _init_universe()

    # Load cached prices from last session (eliminates $100 fallback)
    global _cached_prices
    _cached_prices = load_prices()
    if _cached_prices:
        log.info("Loaded %d cached prices from last session", len(_cached_prices))

    # Fast warmup: pre-fill RSI close cache so indicators are available sooner
    _seed_rsi_warmup(list(_BASE_SYMBOLS) + list(_LIQUID_UNIVERSE))

    log.info(
        "Ingest config:  provider=%s  providers=%s  benzinga_key_present=%s  "
        "finnhub_enabled=%s  FINNHUB_NEWS_MODE=%s  TL_INGEST_NEWS_INTERVAL_S=%s  "
        "dedupe_window=%ds  consensus_boost=%s(%d)",
        _NEWS_PROVIDER,
        _NEWS_PROVIDERS,
        bool(os.environ.get("BENZINGA_API_KEY", "").strip()),
        _NEWS_ENABLE_FINNHUB,
        os.environ.get("FINNHUB_NEWS_MODE", "(unset)"),
        _NEWS_INTERVAL_S,
        int(_NEWS_DEDUPE_WINDOW_S),
        _NEWS_CONSENSUS_BOOST_ENABLED,
        _NEWS_CONSENSUS_BOOST,
    )
    log.info(
        "Ingest arm starting  mode=%s  base_symbols=%s  liquid_universe=%s  "
        "universe_max=%d  symbols_per_poll=%d  poll=%ss  news_poll=%ss  "
        "universe_refresh=%ss  whitelist_loaded=%d  cache=%s",
        settings.trade_mode.value,
        _BASE_SYMBOLS,
        _LIQUID_UNIVERSE or "(none)",
        _UNIVERSE_MAX,
        _UNIVERSE_SYMBOLS_PER_POLL,
        _POLL_INTERVAL_S,
        _NEWS_INTERVAL_S,
        _UNIVERSE_REFRESH_S,
        len(_valid_symbols),
        _US_SYMBOLS_PATH,
    )

    bus = get_bus(max_retries=3)

    # Optionally connect to IB for live market data
    ib = _try_connect_ib()

    # ── Initial PAPER snapshot burst ─────────────────────────────────
    # Publish synthetic snapshots for all base symbols immediately so
    # downstream subscribers (signal arm) never start with zero data.
    if settings.is_paper:
        _burst_syms = list(_BASE_SYMBOLS) + list(_LIQUID_UNIVERSE or [])
        _burst_count = 0
        for _bsym in _burst_syms:
            _bsnap = _synthetic_snapshot(_bsym)
            if _bsnap:
                bus.publish(MARKET_SNAPSHOT, _bsnap)
                _burst_count += 1
        log.info(
            "Published startup snapshot burst  count=%d  symbols=%s",
            _burst_count, _burst_syms[:15],
        )

    # ── Counters for per-cycle publish diagnostics ───────────────────
    _total_snap_published = 0
    _total_news_published = 0

    tick = 0
    last_news_fetch: float = 0.0

    while _running:
        tick += 1
        now = time.time()

        # ── Premarket: load playbook symbols once ────────────────────
        global _playbook_loaded, _playbook_symbols
        session = get_us_equity_session()
        if session == PREMARKET and not _playbook_loaded:
            _playbook_symbols = load_playbook_symbols()
            _playbook_loaded = True
            # Ensure playbook symbols are in the universe
            for sym in _playbook_symbols:
                _add_to_universe(sym)
            log.info(
                "PREMARKET: loaded %d playbook symbols for priority polling: %s",
                len(_playbook_symbols), _playbook_symbols[:15],
            )

        # Adaptive poll interval: faster in PREMARKET
        effective_poll_s = (
            _PREMARKET_POLL_INTERVAL_S if session == PREMARKET else _POLL_INTERVAL_S
        )

        # ── Round-robin symbol slice for this poll ───────────────────
        poll_symbols = _get_poll_slice()

        # ── Market snapshots ─────────────────────────────────────────
        for sym in poll_symbols:
            if not _running:
                break

            snap: Optional[MarketSnapshot] = None
            try:
                snap = _fetch_ib_snapshot(ib, sym) if ib else _stub_snapshot(sym)
            except Exception as exc:
                if settings.is_paper:
                    log.debug("Quote fetch failed for %s (PAPER, will use synthetic): %s", sym, exc)
                else:
                    log.warning("Quote fetch failed for %s: %s", sym, exc)

            # Check for empty / zero quotes
            _quote_missing = snap is None
            _quote_zero = (not _quote_missing) and snap.last == 0 and snap.bid == 0 and snap.ask == 0

            if settings.is_paper and (_quote_missing or _quote_zero):
                snap = _synthetic_snapshot(sym)
                log.debug(
                    "Synthetic quote  sym=%s  last=%.2f  bid=%.2f  ask=%.2f  vol=%d",
                    sym, snap.last, snap.bid, snap.ask, snap.volume,
                )
                last_info = _synth_last_info_ts.get(sym, 0.0)
                if now - last_info >= _SYNTH_INFO_LOG_INTERVAL:
                    _synth_last_info_ts[sym] = now
                    log.info(
                        "Using synthetic quotes for %s (PAPER mode, no live data)",
                        sym,
                    )
            else:
                if _quote_missing:
                    log.warning("Quote returned None for %s", sym)
                elif _quote_zero:
                    log.warning("Quote returned all-zero prices for %s", sym)

            if snap:
                # ── RSI enrichment ─────────────────────────────────────
                _rvol_val: Optional[float] = None
                if snap.last > 0:
                    closes = _close_cache.setdefault(sym, [])
                    closes.append(snap.last)
                    if len(closes) > _RSI_MAX_CACHE:
                        _close_cache[sym] = closes[-_RSI_MAX_CACHE:]
                        closes = _close_cache[sym]

                    # Count real (non-synthetic) bars for RSI warmup guard
                    _is_real_bar = snap.session not in ("SYNTH", "STUB")
                    if _is_real_bar:
                        _real_bar_count[sym] = _real_bar_count.get(sym, 0) + 1

                    from src.signals.indicators import compute_rsi
                    rsi_val = compute_rsi(closes, period=_RSI_PERIOD)

                    # RVOL enrichment
                    _rvol_val = _compute_rvol(sym, snap.volume) if snap.volume > 0 else None

                    # Suppress RSI until enough real bars flush the synthetic seed
                    _warmup_count = _real_bar_count.get(sym, 0)
                    if rsi_val is not None and _warmup_count < _RSI_WARMUP_THRESHOLD:
                        log.debug(
                            "RSI suppressed for %s (warmup: %d/%d real bars)",
                            sym, _warmup_count, _RSI_WARMUP_THRESHOLD,
                        )
                        rsi_val = None  # not trustworthy yet

                    if rsi_val is not None:
                        # Log warmup_complete once per symbol
                        if sym not in _rsi_warmup_logged:
                            _rsi_warmup_logged.add(sym)
                            log.info(
                                "RSI warmup complete for %s (%d real bars) rsi14=%.1f rvol=%s",
                                sym, _warmup_count, rsi_val,
                                f"{_rvol_val:.2f}" if _rvol_val is not None else "n/a",
                            )
                        snap = MarketSnapshot(
                            symbol=snap.symbol,
                            ts=snap.ts,
                            session=snap.session,
                            last=snap.last,
                            bid=snap.bid,
                            ask=snap.ask,
                            volume=snap.volume,
                            cum_volume=getattr(snap, "cum_volume", 0),
                            vwap=snap.vwap,
                            atr=snap.atr,
                            rsi14=rsi_val,
                            rvol=_rvol_val,
                        )
                bus.publish(MARKET_SNAPSHOT, snap)
                _total_snap_published += 1
                log.debug("Published snapshot  sym=%s  last=%.2f  rsi14=%s  rvol=%s", sym, snap.last, snap.rsi14, getattr(snap, "rvol", None))

        # ── Per-cycle snapshot publish summary (INFO) ────────────────
        log.info(
            "Published snapshots  tick=%d  count=%d  symbols=%s  total=%d",
            tick, len(poll_symbols), poll_symbols[:10], _total_snap_published,
        )

        # ── Per-minute RSI summary log ─────────────────────────────
        if now - _last_rsi_log_ts >= _RSI_LOG_INTERVAL_S:
            _last_rsi_log_ts = now
            for _sym in poll_symbols:
                _closes = _close_cache.get(_sym, [])
                _last_price = _closes[-1] if _closes else 0.0
                from src.signals.indicators import compute_rsi as _rsi_fn
                _rsi = _rsi_fn(_closes, period=_RSI_PERIOD)
                log.info(
                    "RSI check  sym=%-5s  last=%8.2f  rsi14=%s  samples=%d",
                    _sym, _last_price,
                    f"{_rsi:.1f}" if _rsi is not None else "n/a",
                    len(_closes),
                )

        # ── News (less frequent, with watchdog timeout) ──────────────
        if _running and now - last_news_fetch >= _NEWS_INTERVAL_S:
            last_news_fetch = now

            # ── Legend Phase 2: Multi-provider fetch ─────────────────
            provider_results = _fetch_multi_provider_news()

            # Merge all articles, tagging each with its provider
            raw_articles: list[dict] = []
            provider_counts: dict[str, int] = {}
            for prov, items in provider_results.items():
                for art in items:
                    art["_provider"] = prov
                    raw_articles.append(art)
                provider_counts[prov] = len(items)

            fetched = len(raw_articles)

            # ── Canonicalize URLs for ALL articles BEFORE fan-out ──
            _gn_decoded_ok = 0
            _gn_fetched_ok = 0
            _gn_resolved_ok = 0
            _gn_resolved_fail = 0
            _gn_skipped_limit = 0
            _canon_non_gnews = 0
            _resolve_count = 0
            _resolve_max = _NEWS_CANONICALIZE_MAX_PER_POLL
            _resolve_timeout = _NEWS_CANONICALIZE_TIMEOUT_S
            # Total budget: don't let canonicalization stall the ingest loop
            _canon_budget_s = float(os.environ.get("TL_NEWS_CANON_BUDGET_S", "15"))
            _canon_start = time.monotonic()
            _canon_budget_exceeded = 0
            for art in raw_articles:
                orig_url = art.get("url", "") or art.get("canonical_url", "")
                _art_prov = art.get("_provider", "")
                _needs_gnews = (
                    _art_prov == "gnews"
                    or _is_gnews_url(orig_url)
                    or _is_gnews_url(art.get("canonical_url", ""))
                )
                if _needs_gnews:
                    _resolved = False
                    # Check total time budget before attempting network calls
                    _over_budget = (time.monotonic() - _canon_start) >= _canon_budget_s
                    if _over_budget:
                        art["canonical_url"] = _canonicalize_url(orig_url)
                        _canon_budget_exceeded += 1
                        continue
                    # Tier 0: RSS <source url="..."> — domain-only, used for
                    # cluster key domain (via source_url field on art), NOT for
                    # canonical_url which must keep a full path.
                    _src_url = art.get("source_url", "")
                    # Tier 1: base64 decode of token (no network call)
                    target = _extract_gnews_rss_target(orig_url)
                    if target and not _is_google_domain(target):
                        _canon1 = _canonicalize_url(target)
                        if not _is_bad_canonical(_canon1):
                            art["canonical_url"] = _canon1
                            _gn_decoded_ok += 1
                            _resolved = True
                    # Tier 2: GET article page, parse HTML (network call)
                    if not _resolved and _resolve_count < _resolve_max:
                        _resolve_count += 1
                        article_page = _gnews_articles_url_from_rss(orig_url)
                        target2 = _fetch_gnews_rss_article_page_target(article_page, _resolve_timeout)
                        if target2 and not _is_google_domain(target2):
                            _canon2 = _canonicalize_url(target2)
                            if not _is_bad_canonical(_canon2):
                                art["canonical_url"] = _canon2
                                _gn_fetched_ok += 1
                                _resolved = True
                        if not _resolved:
                            # Tier 3: plain redirect follow
                            resolved = _resolve_redirect_url(orig_url, _resolve_timeout)
                            if resolved and resolved != orig_url and not _is_google_domain(resolved):
                                _canon3 = _canonicalize_url(resolved)
                                if not _is_bad_canonical(_canon3):
                                    art["canonical_url"] = _canon3
                                    _gn_resolved_ok += 1
                                    _resolved = True
                    if not _resolved:
                        # Keep orig_url (Google wrapper — has unique path)
                        art["canonical_url"] = _canonicalize_url(orig_url)
                        if _resolve_count >= _resolve_max:
                            _gn_skipped_limit += 1
                        else:
                            _gn_resolved_fail += 1
                            if _NEWS_CONSENSUS_DEBUG:
                                log.debug(
                                    "gnews_canon_fail  hl=%s  orig=%s  source_url=%s",
                                    art.get("headline", "")[:60],
                                    orig_url[:80],
                                    art.get("source_url", "")[:60],
                                )
                elif _art_prov == "benzinga":
                    raw_url = (
                        art.get("original_url")
                        or art.get("source_url")
                        or art.get("originalLink")
                        or art.get("link")
                        or orig_url
                    )
                    art["canonical_url"] = _canonicalize_url(raw_url)
                    _canon_non_gnews += 1
                else:
                    art["canonical_url"] = _canonicalize_url(orig_url)
                    _canon_non_gnews += 1
            _canon_elapsed = time.monotonic() - _canon_start
            log.info(
                "GNews canonicalization: decoded_ok=%d  fetched_ok=%d  resolved_ok=%d  "
                "resolved_fail=%d  skipped_limit=%d  canon_non_gnews=%d  "
                "budget_exceeded=%d  elapsed=%.1fs",
                _gn_decoded_ok, _gn_fetched_ok, _gn_resolved_ok,
                _gn_resolved_fail, _gn_skipped_limit, _canon_non_gnews,
                _canon_budget_exceeded, _canon_elapsed,
            )
            # Log domain distribution for gnews articles (prefer source_url for real publisher domain)
            _gn_dom_counts: dict[str, int] = {}
            for art in raw_articles:
                if art.get("_provider") == "gnews" or _is_gnews_url(art.get("url", "")):
                    _du = art.get("source_url") or art.get("canonical_url", "")
                    _d = _extract_domain(_du)
                    _gn_dom_counts[_d] = _gn_dom_counts.get(_d, 0) + 1
            if _gn_dom_counts:
                _dom_parts = [f"{d}={c}" for d, c in sorted(_gn_dom_counts.items(), key=lambda x: -x[1])]
                log.info("gnews_domain_dist: %s", "  ".join(_dom_parts))
            # canon_sample: show up to 3 full canonical URLs (all providers)
            _canon_samples = []
            for art in raw_articles:
                _cu = art.get("canonical_url", "")
                if _cu and len(_canon_samples) < 3:
                    _canon_samples.append(_cu)
            if _canon_samples:
                log.info("canon_sample=%s", _canon_samples)
            if _NEWS_CONSENSUS_DEBUG:
                _gn_samples = []
                for art in raw_articles:
                    if (art.get("_provider") == "gnews" or _is_gnews_url(art.get("url", ""))) and len(_gn_samples) < 5:
                        _d = _extract_domain(art.get("canonical_url", ""))
                        _gn_samples.append(f"{_d}|{art.get('headline', '')[:60]}")
                if _gn_samples:
                    log.info("gnews_canon_domains_sample: %s", " || ".join(_gn_samples))

            # ── Universe expansion from news ─────────────────────────
            expansion_articles: list[dict] = []
            for art in raw_articles:
                expansion_articles.append({
                    "headline": art.get("headline", ""),
                    "related_tickers": art.get("related_tickers") or [],
                    "symbol": art.get("_primary_symbol", ""),
                })
            newly_added = _expand_universe_from_news(expansion_articles)
            if newly_added:
                log.info("Universe expanded by %d symbols from news", newly_added)

            # ── Fan-out: explode each article to related tickers ─────
            fanout_articles: list[dict] = []
            fanout_total = 0
            unique_fanout_syms: set[str] = set()
            for art in raw_articles:
                headline = art.get("headline", "")
                if not headline:
                    continue
                related = art.get("related_tickers") or []
                primary = art.get("_primary_symbol", "").upper()

                target_syms: list[str] = []
                if primary and _validate_symbol(primary, source="gnews_primary"):
                    target_syms.append(primary)
                for t in related:
                    t_up = t.strip().upper()
                    if t_up and t_up not in target_syms and _validate_symbol(t_up, source="gnews_related"):
                        target_syms.append(t_up)
                if len(target_syms) <= 1:
                    parsed = _extract_tickers_from_headline(headline)
                    for t in parsed:
                        if t not in target_syms:
                            target_syms.append(t)

                ts_val = art.get("ts")
                prov = art.get("_provider", _NEWS_PROVIDER)

                for sym in target_syms:
                    fanout_articles.append({
                        "symbol": sym,
                        "headline": headline,
                        "source": art.get("source", "unknown"),
                        "url": art.get("url"),
                        "canonical_url": art.get("canonical_url"),
                        "source_url": art.get("source_url", ""),
                        "source_name": art.get("source_name", ""),
                        "sentiment": art.get("sentiment"),
                        "ts": ts_val,
                        "_provider": prov,
                        "related_tickers": related,
                        "_primary_symbol": primary,
                    })
                fanout_total += len(target_syms)
                unique_fanout_syms.update(target_syms)

            avg_related = round(fanout_total / max(fetched, 1), 1)

            # Filter: symbol must be in universe
            filtered = [
                art for art in fanout_articles
                if art.get("headline") and art.get("symbol")
                and _in_universe(art["symbol"])
            ]
            in_universe = len(filtered)

            # ── Legend Phase 2.2: Cross-provider dedupe + consensus ────


            # --- Compute fingerprints and cluster keys (canonical_url already set) ---
            cluster_providers: dict[str, set[str]] = {}
            for art in filtered:
                art["_tokens"] = _extract_story_tokens(art.get("headline", ""))
                art["_bucket"] = _article_bucket(art)
                art["_fp"] = _story_fingerprint(art)
                # Register immediately so LATER articles in this same poll
                # can fuzzy-match against this one's tokens/bucket.
                prov = art.get("_provider", "unknown")
                _story_register_provider(
                    art["_fp"], prov,
                    token_set=art["_tokens"],
                    bucket=art["_bucket"],
                )
                # Compute cluster key
                bucket = str(art["_bucket"])
                symbols = sorted(_article_symbols(art))
                tokens = sorted(list(_extract_story_tokens(art.get("headline", ""))))[:8]
                # Cross-publisher cluster key: bucket + symbols + tokens
                # Domain is intentionally EXCLUDED so that articles from
                # different publishers (reuters via gnews, benzinga, etc.)
                # about the same story cluster together for consensus.
                cluster_key = f"{bucket}|{','.join(symbols)}|{','.join(tokens)}"
                art["_cluster_key"] = cluster_key
                prov = art.get("_provider", "unknown")
                cluster_providers.setdefault(cluster_key, set()).add(prov)


            dupes_dropped = 0
            consensus_hits = 0
            story_fp_consensus = 0
            consensus_examples: list[str] = []   # for log
            cluster_hits = 0
            cluster_ex: list[str] = []

            # --- Story-level consensus tracking ---
            _prune_story_cluster_cache()
            story_fp_cache: dict = {}
            story_fp_total = 0
            story_fp_consensus = 0
            consensus_hits = 0
            consensus_examples: list[str] = []
            to_publish: list[dict] = []
            # Track fp->providers for this poll
            # Use the already-computed art["_fp"] from _story_fingerprint()
            # which includes Tier C fuzzy Jaccard matching, so articles
            # from different providers with similar headlines share the
            # same fingerprint.
            fp_providers: dict[str, set] = {}
            for art in filtered:
                prov = art.get("_provider", _NEWS_PROVIDER)
                fp = art.get("_fp", "")
                if not fp:
                    # Fallback (should not happen — _fp set above)
                    fp = _hashlib.md5(
                        (art.get("canonical_url") or art.get("url") or art.get("headline", "")).encode(),
                        usedforsecurity=False,
                    ).hexdigest()[:20]
                art["_story_fp"] = fp
                # Register provider in story_fp_cache
                providers = fp_providers.setdefault(fp, set())
                providers.add(prov)
            story_fp_total = len(fp_providers)
            story_fp_consensus = sum(1 for v in fp_providers.values() if len(v) >= 2)

            if _NEWS_CONSENSUS_DEBUG:
                # show a few fingerprints and their provider sets
                examples = []
                for fp, provs in list(fp_providers.items())[:10]:
                    examples.append(f"{fp[:8]}:{','.join(sorted(provs))}")
                log.info("fp_provider_sets_sample=%s", " | ".join(examples))
            elif story_fp_consensus > 0:
                # Always show consensus fingerprints (even outside debug mode)
                consensus_fps = [
                    f"{fp[:8]}:{','.join(sorted(provs))}"
                    for fp, provs in list(fp_providers.items())
                    if len(provs) >= 2
                ][:5]
                if consensus_fps:
                    log.info("fp_consensus_matches=%s", " | ".join(consensus_fps))

            for art in filtered:
                sym = art["symbol"]
                hl = art["headline"]
                prov = art.get("_provider", _NEWS_PROVIDER)
                fp = art["_story_fp"]
                provider_count = len(fp_providers[fp])
                art["_story_fp_provider_count"] = provider_count
                # Per-(symbol, fingerprint) publish dedupe (legacy, still needed)
                is_new, story_n = _mp_dedupe_check(
                    sym, fp, prov,
                    token_set=art.get("_tokens"),
                    bucket=art.get("_bucket"),
                )
                if not is_new:
                    dupes_dropped += 1
                    continue
                if not _is_new_news(sym, hl):
                    dupes_dropped += 1
                    continue
                art["_story_n"] = story_n
                to_publish.append(art)
                if provider_count >= 2:
                    consensus_hits += 1
                    if len(consensus_examples) < 5:
                        consensus_examples.append(f"{sym}({provider_count}p)")
            to_publish = to_publish[:_NEWS_MAX_PUBLISHED_PER_POLL]

            # ── News Category Classification (Legend Phase 1) ────────
            try:
                from src.data.news_fetcher import classify_news as _classify
            except ImportError:
                _classify = None  # type: ignore[assignment]

            published = 0
            _cat_counts: dict[str, int] = {}


            for art in to_publish:
                ts_kwarg = {}
                if art.get("ts") and hasattr(art["ts"], "isoformat"):
                    ts_kwarg["ts"] = art["ts"]

                headline = art["headline"]
                impact_tags: list[str] = []
                impact_score = 0
                source_provider = art.get("_provider", _NEWS_PROVIDER)

                if _classify is not None:
                    cat, mult = _classify(headline)
                    impact_tags.append(cat)
                    base = 1 if cat != "GENERAL" else 0
                    impact_score = min(10, int(base * mult))
                    _cat_counts[cat] = _cat_counts.get(cat, 0) + 1

                # --- Story-fp/Cluster-key consensus tagging ---
                provider_count = art.get("_story_fp_provider_count", 1)
                ckey = art.get("_cluster_key", "")
                cluster_count = len(cluster_providers.get(ckey, set()))
                consensus_count = 0
                if provider_count >= 2:
                    consensus_count = provider_count
                elif cluster_count >= 2:
                    consensus_count = cluster_count
                if consensus_count >= 2:
                    impact_tags.append(f"CONSENSUS:{consensus_count}")
                    cluster_hits += 1 if consensus_count == cluster_count else 0
                    # Promote to priority queue for aggressive polling
                    _promote_to_priority(art["symbol"], f"consensus={consensus_count}")
                    log.info(
                        "CONSENSUS_TAG_APPLIED symbol=%s providers=%d "
                        "source=%s impact_tags=%s headline=%s",
                        art["symbol"],
                        consensus_count,
                        "fp" if provider_count >= 2 else "cluster",
                        impact_tags,
                        headline[:80],
                    )
                if _NEWS_CONSENSUS_BOOST_ENABLED and consensus_count >= 2:
                    boost = min(6, (consensus_count - 1) * _NEWS_CONSENSUS_BOOST)
                    impact_score = min(10, impact_score + boost)
                # Promote high-impact news symbols (earnings/FDA/macro) to priority
                _HIGH_IMPACT_CATS = {"EARNINGS", "FDA", "MERGER", "BANKRUPTCY"}
                if any(t in _HIGH_IMPACT_CATS for t in impact_tags) and impact_score >= 4:
                    _promote_to_priority(art["symbol"], f"high_impact={impact_tags}")
                event = NewsEvent(
                    symbol=art["symbol"],
                    headline=headline,
                    source=art.get("source", "unknown"),
                    url=art.get("url"),
                    sentiment=art.get("sentiment"),
                    impact_score=impact_score,
                    impact_tags=impact_tags,
                    source_provider=source_provider,
                    **ts_kwarg,
                )
                bus.publish(NEWS_EVENT, event)
                published += 1
                _total_news_published += 1
                if published <= 3:
                    log.info(
                        "Published NewsEvent  sym=%s  impact=%d  tags=%s  hl=%s",
                        event.symbol, event.impact_score, event.impact_tags,
                        headline[:60],
                    )

            cluster_total = len(cluster_providers)
            cluster_consensus = sum(1 for v in cluster_providers.values() if len(v) >= 2)

            prov_str = " ".join(f"{k}={v}" for k, v in sorted(provider_counts.items())) if provider_counts else "none"
            cat_str = " ".join(f"{k}={v}" for k, v in sorted(_cat_counts.items())) if _cat_counts else "none"
            consensus_str = " ".join(consensus_examples) if consensus_examples else ""
            cluster_str = " ".join(cluster_ex) if cluster_ex else ""
            log.info(
                "News poll: providers=[%s]  fetched=%d  fanout=%d  avg_related=%.1f  "
                "unique_syms=%d  in_universe=%d  dupes_dropped=%d  published=%d  "
                "story_fp_total=%d  story_fp_consensus=%d  consensus_hits=%d  (cap=%d)  categories=[%s]%s  "
                "cluster_total=%d  cluster_consensus=%d  cluster_hits=%d%s",
                prov_str, fetched, fanout_total, avg_related,
                len(unique_fanout_syms), in_universe, dupes_dropped, published,
                story_fp_total, story_fp_consensus, consensus_hits, _NEWS_MAX_PUBLISHED_PER_POLL, cat_str,
                f"  consensus_ex=[{consensus_str}]" if consensus_str else "",
                cluster_total, cluster_consensus, cluster_hits,
                f"  cluster_ex=[{cluster_str}]" if cluster_str else "",
            )

            # --- Zero-consensus diagnostic (one-time per poll) ---
            if story_fp_consensus == 0 and cluster_consensus == 0:
                bz_count = provider_counts.get("benzinga", 0)
                gn_count = provider_counts.get("gnews", 0)
                if bz_count > 0 and gn_count > 0:
                    _diag_samples: list[str] = []
                    for art in filtered[:30]:
                        if len(_diag_samples) >= 5:
                            break
                        _p = art.get("_provider", "?")
                        _t = (art.get("headline") or "")[:50]
                        _d = _extract_domain(art.get("canonical_url") or art.get("url") or "")
                        _diag_samples.append(f"{_p}|{_d}|{_t}")
                    log.info(
                        "zero_consensus_diag: bz=%d gn=%d resolve_count=%d samples=[%s]",
                        bz_count, gn_count, _resolve_count,
                        " || ".join(_diag_samples),
                    )

            # --- Consensus debug logging ---
            if _NEWS_CONSENSUS_DEBUG:
                # Provider domains breakdown for each provider
                for prov, items in provider_results.items():
                    domain_counts = {}
                    for art in items:
                        # For gnews, prefer source_url (publisher domain)
                        _du = art.get("source_url") if prov == "gnews" else None
                        if not _du:
                            _du = art.get("canonical_url") or art.get("url")
                        dom = _extract_canonical_domain(_du)
                        domain_counts[dom] = domain_counts.get(dom, 0) + 1
                    log.info(f"provider_domains[{prov}]: %s", " ".join(f"{k}={v}" for k, v in sorted(domain_counts.items())))
                # Story-fp consensus detail
                n_consensus = sum(1 for v in fp_providers.values() if len(v) >= 2)
                log.info(f"story_fp_consensus_detail: {n_consensus} fps with 2+ providers")

                # --- Near-match debug: cross-provider headline similarity ---
                _bz_arts = [a for a in filtered if a.get("_provider") == "benzinga"]
                _gn_arts = [a for a in filtered if a.get("_provider") == "gnews"]
                if _bz_arts and _gn_arts:
                    # Pre-compute benzinga token sets
                    _bz_tok = []
                    for a in _bz_arts:
                        toks = _extract_story_tokens(a.get("headline", ""))
                        _bz_tok.append((a, toks))
                    for gn_art in _gn_arts[:10]:
                        gn_toks = _extract_story_tokens(gn_art.get("headline", ""))
                        if not gn_toks:
                            continue
                        scored = []
                        for bz_art, bz_toks in _bz_tok:
                            if not bz_toks:
                                continue
                            sim = _jaccard(gn_toks, bz_toks)
                            if sim > 0:
                                scored.append((sim, bz_art))
                        scored.sort(key=lambda x: x[0], reverse=True)
                        for sim, bz_art in scored[:3]:
                            _gn_du = gn_art.get("source_url") or gn_art.get("canonical_url") or gn_art.get("url") or ""
                            gn_dom = _extract_domain(_gn_du)
                            bz_dom = _extract_domain(bz_art.get("canonical_url") or bz_art.get("url") or "")
                            gn_can = gn_art.get("canonical_url") or gn_art.get("url", "")
                            bz_can = bz_art.get("canonical_url") or bz_art.get("url", "")
                            log.info(
                                "CLUSTER_NEARMATCH: sim=%.3f  gn_dom=%s  bz_dom=%s  "
                                "gn_title='%s'  bz_title='%s'  gn_can=%s  bz_can=%s",
                                sim, gn_dom, bz_dom,
                                gn_art.get("headline", "")[:100],
                                bz_art.get("headline", "")[:100],
                                gn_can, bz_can,
                            )

        # ── Universe maintenance ─────────────────────────────────────
        if _running and now - _last_universe_refresh_ts >= _UNIVERSE_REFRESH_S:
            _last_universe_refresh_ts = now
            removed = _prune_universe()
            uni_list = _get_universe_list()
            if removed:
                log.info("Universe pruned: removed %d expired symbols", removed)

            # Inject squeeze watchlist candidates into universe
            try:
                sq_top = _squeeze_watchlist(min_score=_SQUEEZE_MIN_SCORE, max_results=_SQUEEZE_UNIVERSE_TOP_N)
                sq_added = 0
                sq_sample: list[str] = []
                for sq in sq_top:
                    if _add_to_universe(sq.symbol):
                        sq_added += 1
                    sq_sample.append(f"{sq.symbol}({sq.squeeze_score})")
                if sq_top:
                    log.info(
                        "squeeze_watchlist_added n=%d sample=%s",
                        sq_added, sq_sample[:10],
                    )
            except Exception:
                log.debug("squeeze_watchlist: no data yet")

            # Publish current universe
            bus.publish(
                UNIVERSE_CANDIDATES,
                UniverseCandidates(symbols=uni_list, size=len(uni_list)),
            )

        # ── Periodic universe size log ───────────────────────────────
        if now - _last_universe_log_ts >= _UNIVERSE_LOG_INTERVAL_S:
            _last_universe_log_ts = now
            uni = _get_universe_list()
            log.info(
                "Universe status  size=%d/%d  base=%d  polled=%d  symbols=%s",
                len(uni), _UNIVERSE_MAX, len(_BASE_SYMBOLS),
                len(poll_symbols),
                uni[:20] if len(uni) > 20 else uni,
            )
            _log_symbol_rejections()

        # ── Heartbeat + pipeline diagnostic ─────────────────────────
        if _running:
            bus.publish(HEARTBEAT, Heartbeat(arm="ingest"))
            log.info(
                "ingest_diag  tick=%d  snaps_published=%d  news_published=%d  "
                "universe=%d  bus_connected=%s",
                tick, _total_snap_published, _total_news_published,
                len(_get_universe_list()),
                getattr(bus, 'is_connected', '?'),
            )

        _interruptible_sleep(effective_poll_s)

    # Cleanup — save last-known prices for next session
    log.info("Ingest arm stopping...")
    _last_prices: Dict[str, float] = {}
    for sym, price in _SYNTH_PREV_LAST.items():
        if price > 0:
            _last_prices[sym.upper()] = price
    # Also capture last closes from RSI cache
    for sym, closes in _close_cache.items():
        if closes and sym.upper() not in _last_prices:
            _last_prices[sym.upper()] = closes[-1]
    if _last_prices:
        save_prices(_last_prices)
        log.info("Saved %d last-known prices at shutdown", len(_last_prices))

    if ib:
        try:
            ib.disconnect()
        except Exception:
            pass

    log.info("Ingest thread exiting")


if __name__ == "__main__":
    main()
