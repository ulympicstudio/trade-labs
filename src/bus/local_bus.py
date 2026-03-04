"""
In-memory event bus — Redis-free development mode.

Drop-in replacement for :class:`~src.bus.redis_bus.RedisBus`.
All routing happens in-process via plain Python dicts; no external
dependencies are required.

Wildcard subscriptions
----------------------
A topic ending with ``.*`` is treated as a prefix match.
For example, subscribing to ``"tl.monitor.*"`` will receive messages
published to ``"tl.monitor.heartbeat"``, ``"tl.monitor.status"``, etc.

Thread safety
-------------
* Publishers only serialize the message and push a ``(topic, json)``
  tuple onto a :class:`queue.Queue`.  **No handler is ever invoked on
  the publisher's thread.**
* A single **dispatcher** daemon thread pulls items from the queue and
  calls matching handlers sequentially.  The dispatcher owns its own
  :mod:`asyncio` event loop so handlers (or code they call) can use
  ``asyncio.get_event_loop()`` without errors.
"""

from __future__ import annotations

import asyncio
import queue
import threading
from typing import Any, Callable, Dict, List, Optional, Tuple, Type

from src.monitoring.logger import get_logger
from src.schemas.codec import decode, encode

log = get_logger("local_bus")

# Sentinel pushed onto the queue to tell the dispatcher to exit.
_STOP = object()

# Type alias for stored handler tuples
_HandlerEntry = Tuple[Callable, Optional[Type]]


class LocalBus:
    """Lightweight in-memory Pub/Sub bus for single-process development.

    Messages are dispatched on a dedicated background thread — never on
    the thread that called :meth:`publish`.
    """

    def __init__(self, **_kwargs: Any) -> None:
        """Create a local bus and start the dispatcher thread.

        Accepts (and ignores) arbitrary keyword arguments so it can be
        constructed with the same signature as
        :class:`~src.bus.redis_bus.RedisBus` (e.g. ``max_retries``).
        """
        self._handlers: Dict[str, List[_HandlerEntry]] = {}
        self._lock = threading.Lock()
        self._queue: queue.Queue[Any] = queue.Queue()
        self._closed = False

        # Start the single dispatcher thread
        self._dispatcher = threading.Thread(
            target=self._dispatch_loop,
            name="local-bus-dispatcher",
            daemon=True,
        )
        self._dispatcher.start()
        log.info("LocalBus initialised (in-memory, dispatcher thread started)")

    # ── Properties ───────────────────────────────────────────────────

    @property
    def is_connected(self) -> bool:  # noqa: D401
        """``True`` while the dispatcher is alive."""
        return self._dispatcher.is_alive() and not self._closed

    # ── Publish ──────────────────────────────────────────────────────

    def publish(self, topic: str, obj: Any) -> bool:
        """Enqueue *obj* for dispatch to handlers on *topic*.

        The message is encoded to JSON on the caller's thread and placed
        on an internal queue.  The dispatcher thread is responsible for
        decoding and invoking handlers.  Returns ``True`` on success.
        """
        if self._closed:
            return False
        try:
            payload = encode(obj)
        except Exception:
            log.exception("Failed to encode message for topic %s", topic)
            return False

        self._queue.put((topic, payload))
        return True

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
            Channel name.  Append ``.*`` for prefix/wildcard matching.
        handler:
            Callable that receives one decoded dataclass instance.
        msg_type:
            If given, payloads are decoded to this class.
        """
        with self._lock:
            self._handlers.setdefault(topic, []).append((handler, msg_type))
        log.debug("Subscribed handler to %s", topic)

    # ── Teardown ─────────────────────────────────────────────────────

    def close(self) -> None:
        """Stop the dispatcher thread and clear all handlers."""
        if self._closed:
            return
        self._closed = True
        self._queue.put(_STOP)
        self._dispatcher.join(timeout=5.0)
        if self._dispatcher.is_alive():
            log.warning("Dispatcher thread did not exit within 5 s")
        with self._lock:
            self._handlers.clear()
        log.info("LocalBus closed.")

    # ── Dispatcher thread ────────────────────────────────────────────

    def _dispatch_loop(self) -> None:
        """Pull ``(topic, payload_json)`` from the queue and fan out.

        Runs on its own daemon thread with a private :mod:`asyncio`
        event loop installed so that downstream code can safely call
        ``asyncio.get_event_loop()``.
        """
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        log.debug("Dispatcher thread started (asyncio loop installed)")

        try:
            while True:
                item = self._queue.get()
                if item is _STOP:
                    break
                topic, payload = item

                with self._lock:
                    matching = self._matching_handlers(topic)

                for handler, msg_type in matching:
                    try:
                        decoded = decode(payload, msg_type)
                        handler(decoded)
                    except Exception:
                        log.exception(
                            "Handler %s failed for topic %s",
                            getattr(handler, "__name__", handler),
                            topic,
                        )
        finally:
            loop.close()
            log.debug("Dispatcher thread exiting")

    # ── Internal helpers ─────────────────────────────────────────────

    def _matching_handlers(self, topic: str) -> List[_HandlerEntry]:
        """Return handlers whose subscription pattern matches *topic*.

        Exact matches are returned first, then wildcard (``.*``) matches.
        Must be called while holding ``self._lock``.
        """
        result: List[_HandlerEntry] = []
        for pattern, entries in self._handlers.items():
            if pattern == topic:
                result.extend(entries)
            elif pattern.endswith(".*") and topic.startswith(pattern[:-1]):
                # Wildcard prefix match: "foo.*" matches "foo.bar"
                result.extend(entries)
        return result
