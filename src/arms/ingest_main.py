
from __future__ import annotations
import logging
import requests
from urllib.parse import urlparse, parse_qs, urlencode

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
    resolution = min(resolution, 1.0)  # enforce <=1 s upper bound
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if _stop_event.is_set():
            return
        remaining = deadline - time.monotonic()
        _stop_event.wait(min(resolution, max(remaining, 0)))


# ── IB market-data helpers (optional) ────────────────────────────────

def _try_connect_ib():
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
    if _stopping:
        return [], "stopping"

    if _NEWS_PROVIDER in ("rss", "gnews"):
        return [], "provider=gnews"

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


def _fetch_gnews_fallback(symbols: list[str]) -> list[dict]:
    try:
        from urllib.parse import urlparse as _up
        return _up(url).netloc.lower().lstrip("www.")
    except Exception:
        return "unknown"


def _fetch_rss_feeds() -> dict[str, list[dict]]:
    if _stopping or not _RSS_FEEDS:
        return {}

    import feedparser  # already a dependency
    from datetime import datetime as _dt, timezone as _tz

    results: dict[str, list[dict]] = {}
    for feed_url in _RSS_FEEDS:
        if _stopping:
            break
        domain = _domain_from_url(feed_url)
        try:
            # Replace {today_iso} placeholder (for SEC EDGAR date filter)
            actual_url = feed_url.replace(
                "{today_iso}",
                _dt.now(_tz.utc).strftime("%Y-%m-%d"),
            )
            feed = feedparser.parse(
                actual_url,
                request_headers={"User-Agent": "TradeLabs/1.0"},
            )
            if feed.bozo and not feed.entries:
                log.warning("RSS feed unreachable/empty: %s (%s)", domain, feed.bozo_exception)
                continue

            articles: list[dict] = []
            for entry in feed.entries[:50]:  # cap per feed
                title = (entry.get("title") or "").strip()
                if not title or len(title) < 10:
                    continue
                link = entry.get("link") or ""
                source = entry.get("source", {}).get("title", domain)
                # Attempt to parse published date
                ts_val = None
                pub = entry.get("published_parsed") or entry.get("updated_parsed")
                if pub:
                    try:
                        ts_val = _dt(*pub[:6], tzinfo=_tz.utc)
                    except Exception:
                        pass

                # Extract tickers from title (reuse existing helper)
                tickers = _extract_tickers_from_headline(title)

                articles.append({
                    "headline": title,
                    "source": source,
                    "url": link,
                    "ts": ts_val,
                    "related_tickers": tickers,
                    "summary": (entry.get("summary") or "")[:200],
                    "_primary_symbol": tickers[0] if tickers else "",
                })

            if articles:
                # Tag each article with feed-specific provider domain
                prov_tag = f"rss:{domain}"
                for art in articles:
                    art["_provider"] = prov_tag
                results[prov_tag] = articles
                log.debug("RSS feed %s: %d articles", domain, len(articles))
            else:
                log.debug("RSS feed %s: 0 usable articles", domain)
        except Exception as exc:
            log.warning("RSS feed %s failed: %s", domain, exc)

    total = sum(len(v) for v in results.values())
    if total:
        log.info(
            "Multi-feed RSS: %d feeds, %d articles  [%s]",
            len(results), total,
            " ".join(f"{k.removeprefix('rss:')}={len(v)}" for k, v in sorted(results.items())),
        )
    return results


def _fetch_news_with_timeout() -> tuple[list[dict], str]:
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

            elif prov == "gnews":
                rss_syms = _get_universe_list()[:10] or list(_BASE_SYMBOLS)
                items = _fetch_gnews_fallback(rss_syms)
                if items:
                    results["gnews"] = items

            elif prov == "rssfeeds":
                feed_results = _fetch_rss_feeds()
                for feed_prov, feed_items in feed_results.items():
                    results[feed_prov] = feed_items

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

            items, reason = _fetch_news_with_timeout()
            if items:
                results["benzinga"] = items
            elif reason:
                log.debug("benzinga skip: %s", reason)

        elif prov == "gnews":
            rss_syms = _get_universe_list()[:10] or list(_BASE_SYMBOLS)
            items = _fetch_gnews_fallback(rss_syms)
            if items:
                results["gnews"] = items

        elif prov == "rssfeeds":
            feed_results = _fetch_rss_feeds()
            for feed_prov, feed_items in feed_results.items():
                results[feed_prov] = feed_items

        # finnhub is NOT in _NEWS_PROVIDERS list — gated separately
        else:
            log.debug("Unknown news provider '%s' — skipping", prov)




def _normalise_url(url: str) -> str:
    for sfx in _STEM_SUFFIXES:
        if len(token) > len(sfx) + 4 and token.endswith(sfx):
            return token[: -len(sfx)]
    return token


def _extract_story_tokens(headline: str) -> set[str]:
    raw_tokens = _TOKEN_RE.findall(headline.lower())
    stemmed = [_stem_token(t) for t in raw_tokens]
    filtered = [t for t in stemmed if t not in _STOPWORDS and len(t) >= 3]
    # Dedupe, sort, keep top 8 for a compact but discriminative signature
    unique = sorted(set(filtered))
    return set(unique[:8])


def _article_symbols(article: dict) -> list[str]:
    ts = article.get("ts")
    if ts and hasattr(ts, "timestamp"):
        return int(ts.timestamp()) // 900
    return int(time.time()) // 900


def _jaccard(a: set, b: set) -> float:
    fps_in_bucket = _story_bucket_index.get(bucket)
    if not fps_in_bucket:
        return None
    now = time.time()
    checked = 0
    art_syms = article_symbols or set()
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
        if sim < _FUZZY_JACCARD_THRESHOLD:
            continue
        # Symbol overlap guard — avoid false positives
        other_syms = entry.get("symbols") or set()
        other_primary = entry.get("primary", "")
        sym_overlap = len(art_syms & other_syms) if art_syms and other_syms else 0
        same_primary = (
            article_primary and other_primary
            and article_primary == other_primary
        )
        if not same_primary and sym_overlap < 1:
            continue
        if _NEWS_CONSENSUS_DEBUG:
            log.debug(
                "consensus_fuzzy_match provider -> matched_fp=%s "
                "jaccard=%.2f sym_overlap=%d primary_match=%s title='%s'",
                fp[:12], sim, sym_overlap, same_primary,
                title_hint[:80],
            )
        return fp
    return None


def _story_fingerprint(article: dict) -> str:
    headline = article.get("headline", "")
    token_set = _extract_story_tokens(headline)
    sym_list = _article_symbols(article)
    sym_set = set(sym_list)
    bucket = _article_bucket(article)
    primary = (article.get("_primary_symbol") or "").upper()

    # ── Tier A: URL-based ────────────────────────────────────────────
    raw_url = _pick_fp_url(article)
    url = raw_url.strip()
    if url:
        norm_url = _normalise_url(url)
        if norm_url:
            fp_url = _hashlib.md5(
                norm_url.encode(), usedforsecurity=False
            ).hexdigest()[:20]
            if _NEWS_CONSENSUS_DEBUG:
                log.debug("tierA_fp url=%r norm_url=%r fp=%s", url, norm_url, fp_url)
            # Check if this URL-fp already exists
            if fp_url in _story_fp_cache:
                entry = _story_fp_cache[fp_url]
                if not entry.get("tokens"):
                    entry["tokens"] = token_set
                    entry["bucket"] = bucket
                if not entry.get("symbols"):
                    entry["symbols"] = sym_set
                if not entry.get("primary"):
                    entry["primary"] = primary
                return fp_url
            # Before creating a new URL-fp, try fuzzy against existing
            # (the other provider may have used a different URL)
            if token_set:
                fuzzy_fp = _fuzzy_find_fp(
                    token_set, bucket, headline,
                    article_symbols=sym_set, article_primary=primary,
                )
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
        if not entry.get("symbols"):
            entry["symbols"] = sym_set
        if not entry.get("primary"):
            entry["primary"] = primary
        return fp_sig

    # ── Tier C: fuzzy Jaccard + symbol scan ──────────────────────────
    if token_set:
        fuzzy_fp = _fuzzy_find_fp(
            token_set, bucket, headline,
            article_symbols=sym_set, article_primary=primary,
        )
        if fuzzy_fp:
            return fuzzy_fp

    return fp_sig


def _story_register_provider(
    fp: str,
    provider: str,
    token_set: set[str] | None = None,
    bucket: int | None = None,
    symbols: set[str] | None = None,
    primary: str = "",
    title_hint: str = "",
) -> int:
    now = time.time()
    entry = _story_fp_cache.get(fp)
    if entry is not None:
        if now - entry["ts"] < _NEWS_DEDUPE_WINDOW_S:
            entry["providers"].add(provider)
            # Backfill tokens/bucket/symbols if they weren't set
            if token_set and not entry.get("tokens"):
                entry["tokens"] = token_set
            if bucket is not None and entry.get("bucket") is None:
                entry["bucket"] = bucket
            if symbols:
                existing = entry.get("symbols") or set()
                entry["symbols"] = existing | symbols
            if primary and not entry.get("primary"):
                entry["primary"] = primary
            if title_hint and not entry.get("title_hint"):
                entry["title_hint"] = title_hint
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
        "symbols": symbols or set(),
        "primary": primary,
        "title_hint": title_hint[:80] if title_hint else "",
    }
    # Register in bucket index for Tier C scanning
    if bucket is not None:
        bucket_list = _story_bucket_index.setdefault(bucket, [])
        if fp not in bucket_list:
            bucket_list.append(fp)
    return 1


def _story_consensus_count(fp: str) -> int:
    now = time.time()

    # Always register at story level (even if symbol-level is a dupe)
    story_n = _story_register_provider(
        fp, provider,
        token_set=kwargs.get("token_set"),
        bucket=kwargs.get("bucket"),
        symbols=kwargs.get("symbols"),
        primary=kwargs.get("primary", ""),
        title_hint=kwargs.get("title_hint", ""),
    )

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
    with _universe_lock:
        return sorted(_universe.keys())


def _get_poll_slice() -> list[str]:
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
