"""
Codec — JSON serialisation helpers for bus messages.

Every schema dataclass can be converted to a compact JSON ``bytes``
object and back again, suitable for Redis Pub/Sub payloads.

Usage::

    from src.schemas.codec import encode, decode
    from src.schemas.messages import MarketSnapshot

    raw = encode(snapshot)          # -> bytes
    obj = decode(raw, MarketSnapshot)  # -> MarketSnapshot
"""

from __future__ import annotations

import dataclasses
import json
import typing
from datetime import datetime, timezone
from typing import Any, Dict, Type, TypeVar

T = TypeVar("T")

# ── Encoding ─────────────────────────────────────────────────────────


class _Encoder(json.JSONEncoder):
    """Handle datetime and other non-primitive types."""

    def default(self, o: Any) -> Any:
        if isinstance(o, datetime):
            return o.isoformat()
        return super().default(o)


def to_dict(obj: Any) -> Dict[str, Any]:
    """Convert a dataclass instance to a plain dict (shallow)."""
    if not dataclasses.is_dataclass(obj) or isinstance(obj, type):
        raise TypeError(f"Expected a dataclass instance, got {type(obj).__name__}")
    return dataclasses.asdict(obj)


def encode(obj: Any) -> bytes:
    """Serialise a dataclass instance to compact JSON bytes.

    Includes a ``__type__`` key so the decoder can round-trip without
    the caller having to specify the target class.
    """
    d = to_dict(obj)
    d["__type__"] = type(obj).__qualname__
    return json.dumps(d, cls=_Encoder, separators=(",", ":")).encode("utf-8")


# ── Decoding ─────────────────────────────────────────────────────────

# Registry populated on first call to decode() — maps class name → class.
_registry: Dict[str, Type] = {}


def _ensure_registry() -> None:
    if _registry:
        return
    # Import here to avoid circular imports at module level.
    from src.schemas import messages  # noqa: F811

    for name in dir(messages):
        cls = getattr(messages, name)
        if dataclasses.is_dataclass(cls) and isinstance(cls, type):
            _registry[cls.__qualname__] = cls


def _coerce_field(value: Any, field_type: Any) -> Any:
    """Best-effort coercion for known types (datetime ISO strings, etc.)."""
    if value is None:
        return value
    # datetime stored as ISO string
    if field_type is datetime:
        if isinstance(value, str):
            # Handles both offset-aware and naive ISO strings
            return datetime.fromisoformat(value)
        return value
    return value


def decode(raw: bytes | str, cls: Type[T] | None = None) -> T:
    """Deserialise JSON bytes back to a dataclass instance.

    Parameters
    ----------
    raw:
        JSON payload (bytes or str).
    cls:
        Target dataclass.  If *None*, the ``__type__`` key in the
        payload is used to look up the class automatically.
    """
    _ensure_registry()

    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8")
    d: Dict[str, Any] = json.loads(raw)

    # Resolve target class
    type_name = d.pop("__type__", None)
    if cls is None:
        if type_name is None:
            raise ValueError("Payload has no __type__ and no cls was provided")
        cls = _registry.get(type_name)  # type: ignore[assignment]
        if cls is None:
            raise ValueError(f"Unknown message type: {type_name!r}")
    else:
        d.pop("__type__", None)  # discard if present

    # Build instance, coercing fields
    # Use get_type_hints() to resolve string annotations from
    # `from __future__ import annotations` to actual types.
    try:
        fields = typing.get_type_hints(cls)
    except Exception:
        fields = {f.name: f.type for f in dataclasses.fields(cls)}
    kwargs: Dict[str, Any] = {}
    for key, val in d.items():
        if key in fields:
            kwargs[key] = _coerce_field(val, fields[key])

    return cls(**kwargs)  # type: ignore[return-value]
