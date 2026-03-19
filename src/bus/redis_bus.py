"""
Redis Pub/Sub event bus client.

Thin wrapper around ``redis-py`` that publishes and subscribes using
the dataclass codec in :mod:`src.schemas.codec`.

Resilience
----------
* If Redis is unreachable on startup, the client retries with
  exponential back-off (capped at ``MAX_RETRY_DELAY_S``).
* If a publish fails, the error is logged and the caller is **not**
  blocked — fire-and-forget semantics.
* The subscriber thread is a daemon so it won't prevent process exit.

Configuration
-------------
Reads ``TL_REDIS_URL`` from the environment (default ``redis://localhost:6379/0``).

Usage::

    from src.bus.redis_bus import RedisBus
    from src.schemas.messages import MarketSnapshot
    from src.bus.topics import MARKET_SNAPSHOT

    bus = RedisBus()

    # publish
    bus.publish(MARKET_SNAPSHOT, snapshot)

    # subscribe (callback receives a decoded dataclass)
    bus.subscribe(MARKET_SNAPSHOT, lambda msg: print(msg))
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Type

from src.monitoring.logger import get_logger
from src.schemas.codec import decode, encode

log = get_logger("redis_bus")

# ── Tunables ─────────────────────────────────────────────────────────

_REDIS_URL_DEFAULT = "redis://localhost:6379/0"
_INITIAL_RETRY_DELAY_S = 1.0
_MAX_RETRY_DELAY_S = 30.0

# ── Lazy redis import ────────────────────────────────────────────────
# redis-py is an optional dependency — we import lazily so the rest of
# the codebase can be exercised without it installed.

_redis_module = None


def _get_redis():
    """Import redis lazily and cache the module reference."""
    global _redis_module
    if _redis_module is None:
        try:
            import redis as _r  # type: ignore[import-untyped]

            _redis_module = _r
        except ImportError:
            raise ImportError(
                "redis package is required for RedisBus. "
                "Install it with:  pip install redis"
            )
    return _redis_module


# ── Bus implementation ───────────────────────────────────────────────


class RedisBus:
    """Lightweight Redis Pub/Sub bus for inter-arm messaging."""

    def __init__(self, url: Optional[str] = None, *, max_retries: int = 0) -> None:
        """Create a bus instance.

        Parameters
        ----------
        url:
            Redis URL.  Defaults to ``TL_REDIS_URL`` env var.
        max_retries:
            ``0`` (default) = retry forever.  A positive integer caps
            the number of connection attempts — useful when the caller
            wants to handle the failure itself (e.g. the monitor arm).
        """
        self._url = url or os.environ.get("TL_REDIS_URL", _REDIS_URL_DEFAULT)
        self._conn = None  # type: Any  # redis.Redis
        self._pubsub = None  # type: Any  # redis.client.PubSub
        self._sub_thread: Optional[threading.Thread] = None
        self._handlers: Dict[str, List[Callable]] = {}
        self._lock = threading.Lock()
        self._connected = False
        self._connect(max_retries=max_retries)

    # ── Connection helpers ───────────────────────────────────────────

    def _connect(self, *, max_retries: int = 0) -> None:
        """Establish connection to Redis with retry.

        If *max_retries* is 0 retry forever; otherwise give up after
        that many attempts and leave ``self._connected`` as False.
        """
        redis = _get_redis()
        delay = _INITIAL_RETRY_DELAY_S
        attempts = 0
        while True:
            attempts += 1
            try:
                self._conn = redis.Redis.from_url(self._url, decode_responses=False)
                self._conn.ping()
                self._connected = True
                log.info("Connected to Redis at %s", self._url)
                return
            except Exception as exc:
                if 0 < max_retries <= attempts:
                    log.warning(
                        "Redis unavailable after %d attempt(s): %s",
                        attempts,
                        exc,
                    )
                    return  # give up — caller checks self._connected
                log.warning(
                    "Redis unavailable (%s). Retrying in %.1fs…",
                    exc,
                    delay,
                )
                time.sleep(delay)
                delay = min(delay * 2, _MAX_RETRY_DELAY_S)

    @property
    def is_connected(self) -> bool:
        return self._connected

    def _ensure_connected(self) -> bool:
        """Return True if connection is healthy, attempt reconnect otherwise."""
        if not self._connected:
            self._connect(max_retries=1)
            return self._connected
        try:
            self._conn.ping()
            return True
        except Exception:
            self._connected = False
            log.warning("Redis connection lost — reconnecting…")
            self._connect(max_retries=1)
            return self._connected

    # ── Publish ──────────────────────────────────────────────────────

    def publish(self, topic: str, obj: Any) -> bool:
        """Publish a dataclass message to *topic*.

        Returns *True* on success, *False* on failure (logged, not raised).
        """
        try:
            payload = encode(obj)
        except Exception:
            log.exception("Failed to encode message for topic %s", topic)
            return False

        try:
            if not self._ensure_connected():
                return False
            self._conn.publish(topic, payload)
            return True
        except Exception:
            log.exception("Failed to publish to %s", topic)
            return False

    # ── Subscribe ────────────────────────────────────────────────────

    def subscribe(
        self,
        topic: str,
        handler: Callable,
        msg_type: Optional[Type] = None,
    ) -> None:
        """Register *handler* for messages on *topic*.

        Parameters
        ----------
        topic:
            Redis channel name (use constants from :mod:`src.bus.topics`).
        handler:
            Callable that receives one decoded dataclass instance.
        msg_type:
            If given, payloads are decoded to this class.  Otherwise
            the ``__type__`` key is used for automatic dispatch.
        """
        with self._lock:
            if topic not in self._handlers:
                self._handlers[topic] = []
            self._handlers[topic].append((handler, msg_type))
            self._ensure_subscriptions()

    def _ensure_subscriptions(self) -> None:
        """(Re-)create the PubSub object and listener thread."""
        if not self._ensure_connected():
            return

        # Tear down existing listener
        if self._pubsub is not None:
            try:
                self._pubsub.unsubscribe()
                self._pubsub.close()
            except Exception:
                pass

        self._pubsub = self._conn.pubsub()
        channels = list(self._handlers.keys())
        if channels:
            self._pubsub.subscribe(*channels)

        # Start daemon listener thread
        if self._sub_thread is None or not self._sub_thread.is_alive():
            self._sub_thread = threading.Thread(
                target=self._listen_loop, daemon=True, name="redis-bus-listener"
            )
            self._sub_thread.start()

    def _listen_loop(self) -> None:
        """Background loop that dispatches incoming messages."""
        while True:
            try:
                if self._pubsub is None:
                    time.sleep(_INITIAL_RETRY_DELAY_S)
                    continue
                for message in self._pubsub.listen():
                    if message["type"] != "message":
                        continue
                    topic = (
                        message["channel"].decode("utf-8")
                        if isinstance(message["channel"], bytes)
                        else message["channel"]
                    )
                    self._dispatch(topic, message["data"])
            except Exception:
                log.exception("Listener error — reconnecting in %.1fs", _INITIAL_RETRY_DELAY_S)
                time.sleep(_INITIAL_RETRY_DELAY_S)
                with self._lock:
                    self._ensure_subscriptions()

    def _dispatch(self, topic: str, raw: bytes) -> None:
        """Decode and fan-out to all registered handlers for *topic*."""
        handlers = self._handlers.get(topic, [])
        for handler, msg_type in handlers:
            try:
                obj = decode(raw, msg_type)
                handler(obj)
            except Exception:
                log.exception(
                    "Handler %s failed for topic %s",
                    getattr(handler, "__name__", handler),
                    topic,
                )

    # ── Teardown ─────────────────────────────────────────────────────

    def close(self) -> None:
        """Cleanly shut down subscriptions and the Redis connection."""
        if self._pubsub is not None:
            try:
                self._pubsub.unsubscribe()
                self._pubsub.close()
            except Exception:
                pass
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
        log.info("Redis bus closed.")
