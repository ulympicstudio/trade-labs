"""
Minimal in-process event bus (placeholder).

Will be replaced by a real message broker (Redis Streams, ZMQ, etc.)
once the arms are running as separate processes.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Any, Callable, Dict, List


_subscribers: Dict[str, List[Callable]] = defaultdict(list)


def publish(topic: str, payload: Any) -> None:
    """Publish *payload* to all callbacks registered under *topic*."""
    for callback in _subscribers.get(topic, []):
        callback(payload)


def subscribe(topic: str, callback: Callable) -> None:
    """Register *callback* to receive messages on *topic*."""
    _subscribers[topic].append(callback)


def reset() -> None:
    """Clear all subscriptions (useful in tests)."""
    _subscribers.clear()
