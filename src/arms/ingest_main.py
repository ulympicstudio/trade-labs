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

from __future__ import annotations

import json
import os
import random
import re
import signal
import threading
import time
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

log = get_logger("ingest")

# ── Tunables from environment ────────────────────────────────────────

_SYMBOLS: list[str] = [
    s.strip()
    for s in os.environ.get("TL_INGEST_SYMBOLS", "SPY,QQQ,AAPL,MSFT,NVDA").split(",")
    if s.strip()
]
_POLL_INTERVAL_S: float = float(os.environ.get("TL_INGEST_INTERVAL_S", "10"))
_NEWS_INTERVAL_S: float = float(os.environ.get("TL_INGEST_NEWS_INTERVAL_S", "20"))
_NEWS_DAYS: int = int(os.environ.get("TL_INGEST_NEWS_DAYS", "1"))
_USE_IB: bool = os.environ.get("TL_INGEST_USE_IB", "0") in ("1", "true", "yes")

# ── News provider ────────────────────────────────────────────────────
# benzinga = Benzinga v2 general-news endpoint (default)
# rss      = RSS only (no API calls)
_NEWS_PROVIDER: str = os.environ.get("NEWS_PROVIDER_PRIMARY", "benzinga").lower()
_BENZINGA_NEWS_MAX_ITEMS: int = int(os.environ.get("BENZINGA_NEWS_MAX_ITEMS", "100"))

# ── Legend Phase 2: Multi-Provider News Config ───────────────────────
_NEWS_PROVIDERS: list[str] = [
    p.strip().lower()
    for p in os.environ.get("TL_NEWS_PROVIDERS", "benzinga,rss").split(",")
    if p.strip()
]
_NEWS_ENABLE_FINNHUB: bool = os.environ.get(
    "TL_NEWS_ENABLE_FINNHUB", "false"
).lower() in ("1", "true", "yes")
_NEWS_DEDUPE_WINDOW_S: float = float(
    os.environ.get("TL_NEWS_DEDUPE_WINDOW_S", "7200")
)
_NEWS_CONSENSUS_BOOST_ENABLED: bool = os.environ.get(
    "TL_NEWS_CONSENSUS_BOOST_ENABLED", "true"
).lower() in ("1", "true", "yes")
_NEWS_CONSENSUS_BOOST: int = int(
    os.environ.get("TL_NEWS_CONSENSUS_BOOST", "2")
)
_NEWS_MAX_PUBLISHED_PER_POLL: int = int(
    os.environ.get("TL_NEWS_MAX_PUBLISHED_PER_POLL", "200")
)

# ── Universe expansion settings ──────────────────────────────────────
_BASE_SYMBOLS: list[str] = [
    s.strip().upper()
    for s in os.environ.get("BASE_SYMBOLS", ",".join(_SYMBOLS)).split(",")
    if s.strip()
]
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
_story_fp_cache: Dict[str, dict] = {}
# Bucket index: bucket_int → list[fingerprint]  (for fuzzy scan)
_story_bucket_index: Dict[int, list] = {}
# Per-(symbol, fingerprint) publish-dedupe
_mp_dedupe_cache: Dict[Tuple[str, str], dict] = {}  # (symbol, fp) → {ts, providers}
_MP_DEDUPE_MAX = 100_000
_STORY_FP_MAX = 50_000
_FUZZY_JACCARD_THRESHOLD = 0.75
_FUZZY_MAX_CANDIDATES = 50

# Tracking params stripped during URL normalisation
_URL_STRIP_PARAMS = frozenset({
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "fbclid", "gclid", "msclkid", "ref", "source", "si",
})

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

# ── Synthetic-quote state (PAPER mode only) ──────────────────────────
_SYNTH_RNGS: Dict[str, random.Random] = {}    # per-symbol independent RNG
_SYNTH_PREV_LAST: Dict[str, float] = {}      # symbol → last synthetic price
_SYNTH_VOLUME_CTR: Dict[str, int] = {}       # symbol → running volume counter
_SYNTH_SEED_PRICES: Dict[str, float] = {     # reasonable starting prices
    "SPY": 520.0, "QQQ": 440.0, "AAPL": 195.0,
    "MSFT": 420.0, "NVDA": 135.0,
}
_SYNTH_DEFAULT_SEED = 100.0
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


# ── RSI candle cache ───────────────────────────────────────────────
_RSI_PERIOD = 14
_RSI_MAX_CACHE = 200                             # keep at most N closes per symbol
_close_cache: Dict[str, list[float]] = {}        # symbol → [close_0, close_1, …]
_RSI_LOG_INTERVAL_S = 60.0                       # log RSI summary once per minute
_last_rsi_log_ts: float = 0.0


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
        symbol, _SYNTH_SEED_PRICES.get(symbol.upper(), _SYNTH_DEFAULT_SEED)
    )
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
    """Fetch from all enabled providers. Returns {provider: [articles]}.

    Each article dict has:
      headline, source, url, ts, related_tickers, summary, _primary_symbol
    Errors in any single provider are isolated — others still return.
    """
    results: dict[str, list[dict]] = {}

    for prov in _NEWS_PROVIDERS:
        if _stopping:
            break

        if prov == "benzinga":
            items, reason = _fetch_news_with_timeout()
            if items:
                results["benzinga"] = items
            elif reason:
                log.debug("benzinga skip: %s", reason)

        elif prov == "rss":
            rss_syms = _get_universe_list()[:10] or list(_BASE_SYMBOLS)
            items = _fetch_rss_fallback(rss_syms)
            if items:
                results["rss"] = items

        # finnhub is NOT in _NEWS_PROVIDERS list — gated separately
        else:
            log.debug("Unknown news provider '%s' — skipping", prov)

    # Finnhub tertiary: only if explicitly enabled via its own flag
    if _NEWS_ENABLE_FINNHUB and not _stopping:
        items, reason = _fetch_finnhub_news()
        if items:
            results["finnhub"] = items
        elif reason:
            log.debug("finnhub skip: %s", reason)

    return results


def _normalise_url(url: str) -> str:
    """Strip tracking query params and normalise scheme+host+path."""
    if not url:
        return ""
    try:
        p = urlparse(url)
        # Keep only non-tracking query params
        qs = parse_qs(p.query, keep_blank_values=False)
        clean_qs = {k: v for k, v in qs.items() if k.lower() not in _URL_STRIP_PARAMS}
        # Rebuild with sorted params for determinism
        query = urlencode(clean_qs, doseq=True) if clean_qs else ""
        return f"{p.scheme}://{p.netloc.lower()}{p.path.rstrip('/')}"
    except Exception:
        return url.split("?")[0].rstrip("/").lower()


# ── Story token extraction (for Tier B + C) ─────────────────────────

def _extract_story_tokens(headline: str) -> set[str]:
    """Extract normalised token set from headline for signature/fuzzy matching.

    Steps: lowercase → extract alphanumeric tokens → drop stopwords
    → drop tokens < 3 chars → keep top-10 by sorted order (stable).
    """
    tokens = _TOKEN_RE.findall(headline.lower())
    tokens = [t for t in tokens if t not in _STOPWORDS and len(t) >= 3]
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

    # ── Tier A: URL-based ────────────────────────────────────────────
    url = (article.get("url") or "").strip()
    if url:
        norm_url = _normalise_url(url)
        if norm_url:
            fp_url = _hashlib.md5(
                norm_url.encode(), usedforsecurity=False
            ).hexdigest()[:20]
            # Check if this URL-fp already exists
            if fp_url in _story_fp_cache:
                # Attach tokens/bucket if first time seeing them
                entry = _story_fp_cache[fp_url]
                if not entry.get("tokens"):
                    entry["tokens"] = token_set
                    entry["bucket"] = bucket
                return fp_url
            # Before creating a new URL-fp, try fuzzy against existing
            # (the other provider may have used a different URL)
            fuzzy_fp = _fuzzy_find_fp(token_set, bucket, headline) if token_set else None
            if fuzzy_fp:
                return fuzzy_fp
            # New story via URL
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
    """Seed the universe with BASE_SYMBOLS + LIQUID_UNIVERSE + file list."""
    file_tickers = _load_liquid_universe_file()
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
    log.info(
        "Universe seeded: total=%d  base=%d  liquid_env=%d  liquid_file=%d  "
        "universe_max=%d  sample=%s",
        len(_universe), len(_BASE_SYMBOLS), len(_LIQUID_UNIVERSE),
        file_added, _UNIVERSE_MAX,
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
    return [m for m in matches if m not in _TICKER_BLACKLIST and len(m) >= 2]


def _expand_universe_from_news(articles: list[dict]) -> int:
    """Extract symbols from news articles and add to universe.

    Uses ``related_tickers`` field from API when present;
    falls back to headline ticker parsing.

    Only symbols present in the US-symbol whitelist (*_valid_symbols*)
    are accepted.  Logs raw / valid / ignored counts.

    Returns the count of newly added symbols.
    """
    added = 0
    raw_count = 0
    ignored_count = 0
    # Always require whitelist — if it failed to load, reject all parsed tickers
    # to avoid flooding the universe with junk uppercase tokens.
    use_whitelist = True

    for art in articles:
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
            t_upper = t.upper()
            if use_whitelist and t_upper not in _valid_symbols:
                ignored_count += 1
                log.debug("Universe skip (not a valid symbol): %s", t_upper)
                continue
            if _add_to_universe(t_upper):
                added += 1
                log.debug("Universe expanded: +%s (from news)", t_upper)

        # Also ensure the article's own symbol stays fresh
        sym = art.get("symbol", "")
        if sym:
            sym_upper = sym.upper()
            if not use_whitelist or sym_upper in _valid_symbols:
                _add_to_universe(sym_upper)

    valid_count = raw_count - ignored_count
    log.info(
        "News universe expansion: expanded_by_news_raw=%d  "
        "expanded_by_news_valid=%d  ignored_invalid=%d  newly_added=%d",
        raw_count, valid_count, ignored_count, added,
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
                if snap.last > 0:
                    closes = _close_cache.setdefault(sym, [])
                    closes.append(snap.last)
                    if len(closes) > _RSI_MAX_CACHE:
                        _close_cache[sym] = closes[-_RSI_MAX_CACHE:]
                        closes = _close_cache[sym]
                    from src.signals.indicators import compute_rsi
                    rsi_val = compute_rsi(closes, period=_RSI_PERIOD)
                    if rsi_val is not None:
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
                        )
                bus.publish(MARKET_SNAPSHOT, snap)
                log.debug("Published snapshot  sym=%s  last=%.2f  rsi14=%s", sym, snap.last, snap.rsi14)

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
                if primary:
                    target_syms.append(primary)
                for t in related:
                    t_up = t.strip().upper()
                    if t_up and t_up not in target_syms and 1 <= len(t_up) <= 6:
                        if _valid_symbols and t_up not in _valid_symbols:
                            continue
                        target_syms.append(t_up)
                if len(target_syms) <= 1:
                    parsed = _extract_tickers_from_headline(headline)
                    for t in parsed:
                        if t not in target_syms and t in _valid_symbols:
                            target_syms.append(t)

                ts_val = art.get("ts")
                prov = art.get("_provider", _NEWS_PROVIDER)

                for sym in target_syms:
                    fanout_articles.append({
                        "symbol": sym,
                        "headline": headline,
                        "source": art.get("source", "unknown"),
                        "url": art.get("url"),
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
            # Compute story fingerprints + token sets for each fanout article
            for art in filtered:
                art["_fp"] = _story_fingerprint(art)
                art["_tokens"] = _extract_story_tokens(art.get("headline", ""))
                art["_bucket"] = _article_bucket(art)

            dupes_dropped = 0
            consensus_hits = 0
            consensus_examples: list[str] = []   # for log
            to_publish: list[dict] = []

            for art in filtered:
                sym = art["symbol"]
                hl = art["headline"]
                prov = art.get("_provider", _NEWS_PROVIDER)
                fp = art["_fp"]

                is_new, story_n = _mp_dedupe_check(
                    sym, fp, prov,
                    token_set=art.get("_tokens"),
                    bucket=art.get("_bucket"),
                )

                if not is_new:
                    dupes_dropped += 1
                    if story_n >= 2:
                        consensus_hits += 1
                    continue

                # Legacy single-provider dedupe
                if not _is_new_news(sym, hl):
                    dupes_dropped += 1
                    continue

                # Attach story-level provider count for consensus tagging
                art["_story_n"] = story_n
                to_publish.append(art)

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

                # ── Consensus boost (Legend Phase 2) ─────────────────
                fp = art.get("_fp", "")
                n_consensus = _story_consensus_count(fp) if fp else 1
                if _NEWS_CONSENSUS_BOOST_ENABLED and n_consensus >= 2:
                    boost = min(6, (n_consensus - 1) * _NEWS_CONSENSUS_BOOST)
                    impact_score = min(10, impact_score + boost)
                    impact_tags.append(f"CONSENSUS:{n_consensus}")
                    if len(consensus_examples) < 3:
                        consensus_examples.append(
                            f"{art['symbol']}({n_consensus}p)"
                        )
                elif art.get("_story_n", 1) >= 2:
                    # Provider count grew after initial publish
                    n2 = art["_story_n"]
                    impact_tags.append(f"CONSENSUS:{n2}")
                    if _NEWS_CONSENSUS_BOOST_ENABLED:
                        boost = min(6, (n2 - 1) * _NEWS_CONSENSUS_BOOST)
                        impact_score = min(10, impact_score + boost)
                    if len(consensus_examples) < 3:
                        consensus_examples.append(f"{art['symbol']}({n2}p)")
                    consensus_hits += 1

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

            prov_str = " ".join(f"{k}={v}" for k, v in sorted(provider_counts.items())) if provider_counts else "none"
            cat_str = " ".join(f"{k}={v}" for k, v in sorted(_cat_counts.items())) if _cat_counts else "none"
            consensus_str = " ".join(consensus_examples) if consensus_examples else ""
            log.info(
                "News poll: providers=[%s]  fetched=%d  fanout=%d  avg_related=%.1f  "
                "unique_syms=%d  in_universe=%d  dupes_dropped=%d  consensus_hits=%d  "
                "published=%d  (cap=%d)  categories=[%s]%s",
                prov_str, fetched, fanout_total, avg_related,
                len(unique_fanout_syms), in_universe, dupes_dropped, consensus_hits,
                published, _NEWS_MAX_PUBLISHED_PER_POLL, cat_str,
                f"  consensus_ex=[{consensus_str}]" if consensus_str else "",
            )

        # ── Universe maintenance ─────────────────────────────────────
        if _running and now - _last_universe_refresh_ts >= _UNIVERSE_REFRESH_S:
            _last_universe_refresh_ts = now
            removed = _prune_universe()
            uni_list = _get_universe_list()
            if removed:
                log.info("Universe pruned: removed %d expired symbols", removed)

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

        # ── Heartbeat ────────────────────────────────────────────────
        if _running:
            bus.publish(HEARTBEAT, Heartbeat(arm="ingest"))

        _interruptible_sleep(effective_poll_s)

    # Cleanup
    log.info("Ingest arm stopping...")
    if ib:
        try:
            ib.disconnect()
        except Exception:
            pass

    log.info("Ingest thread exiting")


if __name__ == "__main__":
    main()
