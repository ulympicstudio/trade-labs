"""URL canonicalization and Google-News redirect resolution.

Pure, dependency-free helpers extracted from src/arms/ingest_main.py so the
ingest orchestrator stays a coordinator. These functions handle:

  - canonical domain / URL normalisation (strip www, tracking params),
  - Google-infrastructure domain detection (to skip non-article links),
  - decoding the publisher URL embedded in Google-News RSS article tokens,
  - HTTP redirect resolution.

Behaviour is identical to the prior inline implementations.
"""

from __future__ import annotations

import base64 as _b64
import re
from urllib.parse import urlparse, urlunparse

# Tracking params stripped during URL normalisation.
URL_STRIP_PARAMS = frozenset({
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "fbclid", "gclid", "msclkid", "ref", "source", "si",
})

# Domains to reject when extracting publisher URLs from Google News blobs.
GOOGLE_DOMAIN_BLOCKLIST = frozenset({
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


def extract_canonical_domain(url: str) -> str:
    try:
        if not url:
            return "unknown"
        p = urlparse(url)
        host = p.netloc.lower()
        if host:
            if host.startswith("www."):
                host = host[4:]
            return host
    except Exception:
        pass
    return "unknown"


def publisher_hint_domain_from_headline(headline: str) -> str:
    # e.g. "Reuters: ..." or "(Bloomberg) ..."
    m = re.match(r"([A-Za-z0-9\-\.]+)[:\)]", headline)
    if m:
        return m.group(1).lower()
    return "unknown"


def canonicalize_url(url: str) -> str:
    """Strip tracking params and normalize to scheme://netloc/path. Never raises."""
    try:
        p = urlparse(url)
        netloc = p.netloc.lower()
        if netloc.startswith("www."):
            netloc = netloc[4:]
        return f"{p.scheme}://{netloc}{p.path}" if netloc else url
    except Exception:
        return url


def resolve_redirect_url(url: str, timeout_s: float) -> str | None:
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


def is_google_domain(candidate_url: str) -> bool:
    """Check if a URL's domain belongs to Google/blocked infrastructure."""
    try:
        p = urlparse(candidate_url)
        dom = p.netloc.lower()
        if dom.startswith("www."):
            dom = dom[4:]
        if dom in GOOGLE_DOMAIN_BLOCKLIST:
            return True
        if dom.endswith(".google.com") or dom == "google.com":
            return True
        for suffix in (".googleapis.com", ".googleusercontent.com",
                       ".googlesyndication.com", ".gstatic.com",
                       ".doubleclick.net"):
            if dom.endswith(suffix):
                return True
        return False
    except Exception:
        return True  # if we can't parse, reject it


def extract_gnews_rss_target(url: str) -> str | None:
    """Extract the real publisher URL from a Google News RSS article link.

    GNews RSS links look like
    ``https://news.google.com/rss/articles/<base64url-token>?...``. The token
    is URL-safe base64 (often without padding); the decoded protobuf blob
    embeds the target URL. Returns the first non-Google URL found, else None.
    """
    try:
        p = urlparse(url)
        path = p.path
        if "/articles/" not in path:
            return None
        token = path.split("/articles/")[-1]
        token = token.split("/")[0].split("?")[0]
        if not token or len(token) < 20:
            return None

        _TERM = set(b' \t\n\r"\'\\\x3c\x3e\x00\x01\x02\x03\x04\x05\x06\x07\x08')

        def _scan_all_urls(data: bytes) -> list[str]:
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
            for u in urls:
                if not is_google_domain(u):
                    return u
            return None

        padded = token + "=" * (-len(token) % 4)
        try:
            decoded = _b64.urlsafe_b64decode(padded)
            hit = _pick_publisher(_scan_all_urls(decoded))
            if hit:
                return hit
        except Exception:
            pass
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


def gnews_articles_url_from_rss(rss_url: str) -> str:
    """Convert ``/rss/articles/<token>`` → ``/articles/<token>``.

    The non-RSS variant of Google News article pages exposes the outbound
    publisher URL more reliably. Unmatched URLs are returned unchanged.
    """
    p = urlparse(rss_url)
    if p.path.startswith("/rss/articles/"):
        new_path = p.path.replace("/rss/articles/", "/articles/", 1)
        return urlunparse(p._replace(path=new_path))
    return rss_url
