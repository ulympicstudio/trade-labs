"""
CandidatePool – a deduplicated queue of ScanResult items.

The live loop refills once the pool is low, then pops small batches each
iteration so the system progresses through a *longer* list of symbols
instead of re-scoring the same handful every cycle.
"""
from __future__ import annotations

from collections import deque
from typing import Deque, Iterable, List, Set

from src.signals.market_scanner import ScanResult


class CandidatePool:
    """FIFO queue of ScanResult, deduplicated by symbol within one fill cycle."""

    def __init__(self) -> None:
        self._q: Deque[ScanResult] = deque()
        self._seen: Set[str] = set()

    # ---- public API --------------------------------------------------

    def add_many(self, items: Iterable[ScanResult]) -> int:
        """Enqueue *items* that haven't been seen yet.  Returns count added."""
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
