"""Unit tests for the extracted URL-resolution helpers (src/data/url_resolver).

These cover the pure logic moved out of src/arms/ingest_main.py; behaviour
must be identical to the prior inline implementations.
"""

from src.data import url_resolver as ur


def test_canonicalize_strips_www_and_params():
    assert ur.canonicalize_url("https://www.example.com/path?utm_source=x") == \
        "https://example.com/path"


def test_canonicalize_returns_input_on_garbage():
    assert ur.canonicalize_url("not a url") == "not a url"


def test_extract_canonical_domain():
    assert ur.extract_canonical_domain("https://www.reuters.com/a") == "reuters.com"
    assert ur.extract_canonical_domain("") == "unknown"


def test_is_google_domain():
    assert ur.is_google_domain("https://news.google.com/rss/x") is True
    assert ur.is_google_domain("https://foo.googleapis.com/y") is True
    assert ur.is_google_domain("https://reuters.com/article") is False
    # Unparseable → rejected (treated as google/infra).
    assert ur.is_google_domain(None) is True  # type: ignore[arg-type]


def test_gnews_articles_url_from_rss_rewrites_rss_path():
    assert ur.gnews_articles_url_from_rss(
        "https://news.google.com/rss/articles/TOKEN"
    ) == "https://news.google.com/articles/TOKEN"
    # Non-matching URL returned unchanged.
    assert ur.gnews_articles_url_from_rss("https://x.com/a") == "https://x.com/a"


def test_extract_gnews_rss_target_short_token_returns_none():
    assert ur.extract_gnews_rss_target("https://news.google.com/rss/articles/abc") is None
    assert ur.extract_gnews_rss_target("https://x.com/notarticles") is None


def test_publisher_hint_domain_from_headline():
    assert ur.publisher_hint_domain_from_headline("Reuters: Big news") == "reuters"
    assert ur.publisher_hint_domain_from_headline("no delimiter here") == "unknown"
