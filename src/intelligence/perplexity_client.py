"""
Perplexity Sonar API client for catalyst / news intelligence.

Observe-only — never touches execution, risk, or signal thresholds.
Designed for easy future integration into signal ranking.
"""

import json
import os
import time
from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Optional

import requests
from dotenv import load_dotenv

from src.monitoring.logger import get_logger

load_dotenv()

log = get_logger("perplexity")

# ── Configuration ────────────────────────────────────────────────

_API_KEY: str = os.environ.get("PERPLEXITY_API_KEY", "").strip()
_BASE_URL = "https://api.perplexity.ai/chat/completions"
_MODEL = "sonar"
_TIMEOUT_SECONDS = 30
_MAX_RETRIES = 2
_RETRY_BACKOFF = 1.0  # seconds


# ── Response dataclass ───────────────────────────────────────────

@dataclass
class SymbolNewsResult:
    """Structured result from Perplexity news analysis."""
    symbol: str
    catalyst_type: str = "none"
    sentiment_score: float = 0.0     # -1.0 (bearish) to +1.0 (bullish)
    summary: str = ""
    risk_flags: List[str] = None
    citations: List[str] = None
    raw_response: str = ""
    latency_ms: int = 0
    error: str = ""

    def __post_init__(self):
        if self.risk_flags is None:
            self.risk_flags = []
        if self.citations is None:
            self.citations = []

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d.pop("raw_response", None)
        return d

    @property
    def has_catalyst(self) -> bool:
        return self.catalyst_type not in ("none", "unknown", "")

    @property
    def is_bullish(self) -> bool:
        return self.sentiment_score > 0.25

    @property
    def is_bearish(self) -> bool:
        return self.sentiment_score < -0.25


# ── Prompt construction ──────────────────────────────────────────

_SYSTEM_PROMPT = """\
You are a financial news analyst. Analyze recent news and catalysts for the \
given stock ticker. Return ONLY valid JSON with these exact keys:
{
  "catalyst_type": "<earnings|fda|partnership|product_launch|insider|macro|sector|legal|analyst|none>",
  "sentiment_score": <float from -1.0 to 1.0>,
  "summary": "<2-3 sentence summary of the most material catalyst>",
  "risk_flags": ["<flag1>", "<flag2>"]
}
Rules:
- sentiment_score: -1.0 = extremely bearish, 0.0 = neutral, 1.0 = extremely bullish
- risk_flags: list 0-3 specific risks (e.g. "dilution", "regulatory", "earnings_miss")
- If no material catalyst exists, set catalyst_type to "none" and sentiment_score to 0.0
- Do NOT include any text outside the JSON object\
"""


def _build_user_prompt(symbol: str, context: Optional[str] = None) -> str:
    prompt = f"Analyze the latest news and catalysts for ${symbol} in the last 48 hours."
    if context:
        prompt += f"\n\nAdditional context: {context}"
    return prompt


# ── API call with retry ──────────────────────────────────────────

def _call_sonar(symbol: str, context: Optional[str] = None,
                api_key: Optional[str] = None) -> Dict[str, Any]:
    """Call Perplexity Sonar API. Returns raw API response dict or raises."""
    key = api_key or _API_KEY
    if not key:
        raise ValueError("PERPLEXITY_API_KEY not set")

    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": _MODEL,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": _build_user_prompt(symbol, context)},
        ],
        "temperature": 0.1,
        "max_tokens": 512,
    }

    last_err: Optional[Exception] = None
    for attempt in range(_MAX_RETRIES + 1):
        try:
            resp = requests.post(
                _BASE_URL,
                headers=headers,
                json=payload,
                timeout=_TIMEOUT_SECONDS,
            )
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.Timeout:
            last_err = TimeoutError(f"Perplexity API timeout ({_TIMEOUT_SECONDS}s)")
            log.warning("Perplexity timeout for %s (attempt %d/%d)",
                        symbol, attempt + 1, _MAX_RETRIES + 1)
        except requests.exceptions.HTTPError as e:
            status = getattr(e.response, "status_code", None)
            if status == 429:
                last_err = e
                wait = _RETRY_BACKOFF * (attempt + 1)
                log.warning("Perplexity rate-limited for %s, waiting %.1fs", symbol, wait)
                time.sleep(wait)
                continue
            raise
        except requests.exceptions.ConnectionError as e:
            last_err = e
            log.warning("Perplexity connection error for %s (attempt %d/%d)",
                        symbol, attempt + 1, _MAX_RETRIES + 1)

        if attempt < _MAX_RETRIES:
            time.sleep(_RETRY_BACKOFF * (attempt + 1))

    raise last_err or RuntimeError("Perplexity API call failed")


# ── Response parsing ─────────────────────────────────────────────

def _parse_response(symbol: str, api_response: Dict[str, Any]) -> SymbolNewsResult:
    """Extract structured fields from the Sonar API response."""
    content = ""
    try:
        content = api_response["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return SymbolNewsResult(symbol=symbol, error="invalid_api_response",
                                raw_response=str(api_response)[:500])

    citations = api_response.get("citations", [])

    # Strip markdown fences if present
    text = content.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
    if text.endswith("```"):
        text = text.rsplit("```", 1)[0]
    text = text.strip()

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return SymbolNewsResult(
            symbol=symbol,
            summary=text[:300],
            error="json_parse_failed",
            raw_response=text[:500],
            citations=citations,
        )

    cat_type = str(data.get("catalyst_type", "none")).lower().strip()
    try:
        sentiment = float(data.get("sentiment_score", 0.0))
        sentiment = max(-1.0, min(1.0, sentiment))
    except (ValueError, TypeError):
        sentiment = 0.0

    risk_flags = data.get("risk_flags", [])
    if not isinstance(risk_flags, list):
        risk_flags = []
    risk_flags = [str(f) for f in risk_flags[:5]]

    return SymbolNewsResult(
        symbol=symbol,
        catalyst_type=cat_type,
        sentiment_score=round(sentiment, 3),
        summary=str(data.get("summary", ""))[:500],
        risk_flags=risk_flags,
        citations=citations if isinstance(citations, list) else [],
        raw_response=text[:500],
    )


# ── Public API ───────────────────────────────────────────────────

def analyze_symbol_news(symbol: str, context: Optional[str] = None,
                        api_key: Optional[str] = None) -> SymbolNewsResult:
    """Analyze recent news/catalysts for a single symbol via Perplexity Sonar.

    Parameters
    ----------
    symbol : str
        Ticker symbol (e.g. "NVDA").
    context : str, optional
        Additional context to include in the prompt (e.g. "earnings tomorrow").
    api_key : str, optional
        Override for PERPLEXITY_API_KEY env var.

    Returns
    -------
    SymbolNewsResult
        Always returns a result — never raises. Check ``.error`` for failures.
    """
    symbol = symbol.upper().strip()
    if not symbol:
        return SymbolNewsResult(symbol="", error="empty_symbol")

    log.info("Analyzing news for %s", symbol)
    t0 = time.time()

    try:
        raw = _call_sonar(symbol, context=context, api_key=api_key)
        result = _parse_response(symbol, raw)
        result.latency_ms = int((time.time() - t0) * 1000)
        log.info("Perplexity %s → catalyst=%s sentiment=%.2f latency=%dms",
                 symbol, result.catalyst_type, result.sentiment_score, result.latency_ms)
        return result
    except ValueError as e:
        log.error("Perplexity config error: %s", e)
        return SymbolNewsResult(symbol=symbol, error=str(e),
                                latency_ms=int((time.time() - t0) * 1000))
    except Exception as e:
        log.error("Perplexity failed for %s: %s", symbol, e)
        return SymbolNewsResult(symbol=symbol, error=str(e),
                                latency_ms=int((time.time() - t0) * 1000))


def analyze_batch(symbols: List[str], context: Optional[str] = None,
                  api_key: Optional[str] = None,
                  delay: float = 0.5) -> List[SymbolNewsResult]:
    """Analyze multiple symbols sequentially with a delay between calls.

    Parameters
    ----------
    symbols : list[str]
        List of ticker symbols.
    context : str, optional
        Shared context for all symbols.
    api_key : str, optional
        Override for PERPLEXITY_API_KEY env var.
    delay : float
        Seconds to wait between API calls (rate-limit courtesy).

    Returns
    -------
    list[SymbolNewsResult]
        One result per symbol, in input order.
    """
    results = []
    for i, sym in enumerate(symbols):
        results.append(analyze_symbol_news(sym, context=context, api_key=api_key))
        if i < len(symbols) - 1 and delay > 0:
            time.sleep(delay)
    return results
