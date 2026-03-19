"""
Bus factory — returns the appropriate bus backend.

The environment variable ``BUS_BACKEND`` controls which implementation
is used:

* ``"local"``  → :class:`~src.bus.local_bus.LocalBus`  (default)
* ``"redis"``  → :class:`~src.bus.redis_bus.RedisBus`

Usage::

    from src.bus.bus_factory import get_bus

    bus = get_bus()                # uses BUS_BACKEND env var
    bus = get_bus(max_retries=3)   # keyword args forwarded to backend

For the all-in-one dev runner you can inject a shared instance::

    from src.bus.bus_factory import set_shared_bus, get_bus
    shared = LocalBus()
    set_shared_bus(shared)
    bus = get_bus()   # returns the shared instance
"""

from __future__ import annotations

import os
from typing import Any, Optional

_BACKEND_ENV = "BUS_BACKEND"
_DEFAULT_BACKEND = "local"

# Optional shared instance — when set, get_bus() always returns it.
_shared_bus: Optional[Any] = None


def set_shared_bus(bus: Any) -> None:
    """Inject a shared bus instance that all future ``get_bus()`` calls return."""
    global _shared_bus
    _shared_bus = bus


def clear_shared_bus() -> None:
    """Clear the shared bus so ``get_bus()`` resumes normal construction."""
    global _shared_bus
    _shared_bus = None


def get_bus(**kwargs: Any):
    """Instantiate and return the configured bus backend.

    If a shared instance was set via :func:`set_shared_bus`, it is
    returned directly (kwargs are ignored).

    All *kwargs* (e.g. ``max_retries``) are forwarded to the backend
    constructor.  Unknown kwargs are silently ignored by
    :class:`~src.bus.local_bus.LocalBus`.
    """
    if _shared_bus is not None:
        return _shared_bus

    backend = os.environ.get(_BACKEND_ENV, _DEFAULT_BACKEND).lower().strip()

    if backend == "redis":
        from src.bus.redis_bus import RedisBus
        return RedisBus(**kwargs)

    if backend == "local":
        from src.bus.local_bus import LocalBus
        return LocalBus(**kwargs)

    raise ValueError(
        f"Unknown BUS_BACKEND={backend!r}. "
        f"Supported values: 'local', 'redis'."
    )
