"""
CandidatePool – a deduplicated queue of ScanResult items.

The live loop refills once the pool is low, then pops small batches each
iteration so the system progresses through a *longer* list of symbols
instead of re-scoring the same handful every cycle.

Cycle-aware behaviour
---------------------
Within one fill cycle (i.e. while the queue is non-empty) symbols are
deduplicated so the same ticker is never enqueued twice.  When the queue
is fully drained and ``add_many`` is called again, ``_seen`` is
automatically reset so that previously-seen symbols can re-enter the
pool for the next cycle.
"""
from __future__ import annotations

from collections import deque
from typing import Deque, Iterable, List, Set

from src.signals.market_scanner import ScanResult


class CandidatePool:
    """FIFO queue of ScanResult, deduplicated by symbol within one fill cycle.

    A *cycle* lasts from a refill until the queue is fully drained via
    ``pop_many``.  When ``add_many`` is called on an empty queue the
    seen-set is automatically cleared so symbols from previous cycles
    can be re-enqueued.
    """

    def __init__(self) -> None:
        self._q: Deque[ScanResult] = deque()
        self._seen: Set[str] = set()

    # ---- public API --------------------------------------------------

    def add_many(self, items: Iterable[ScanResult]) -> int:
        """Enqueue *items* not yet seen **in this cycle**.

        If the queue is empty when this method is called the seen-set is
        reset first, starting a fresh dedup cycle.

        Returns the number of items actually added.
        """
        if not self._q:
            # New cycle – allow previously-seen symbols back in.
            self._seen.clear()
            print("[POOL] queue empty, starting new dedup cycle")
        added = 0
        for item in items:
            sym = item.symbol.upper().strip()
            if not sym or sym in self._seen:
                continue
            self._seen.add(sym)
            self._q.append(ScanResult(symbol=sym, rank=item.rank))
            added += 1
        if added:
            print(f"[POOL] added {added} new symbols (queue={len(self._q)})")
        return added

    def pop_many(self, n: int) -> List[ScanResult]:
        """Pop up to *n* items from the front of the queue."""
        batch: List[ScanResult] = []
        while self._q and len(batch) < n:
            batch.append(self._q.popleft())
        if batch:
            print(f"[POOL] popped {len(batch)} (remaining={len(self._q)})")
        return batch

    def size(self) -> int:
        return len(self._q)

    def clear(self) -> None:
        """Reset pool *and* seen-set so the next refill starts fresh."""
        self._q.clear()
        self._seen.clear()
        print("[POOL] cleared")
