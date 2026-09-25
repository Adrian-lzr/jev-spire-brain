"""Per-decision time and provider-call budget shared by model adapters.

The game loop owns one budget for a decision.  Provider adapters consume a
reservation from it before doing network I/O; a slow provider therefore cannot
silently spend the time intended for a later tactical call.  The clock is
injectable so replay tests do not depend on wall-clock sleeps.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import threading
import time
from typing import Callable, Iterator


@dataclass
class DecisionBudget:
    total_ms: int = 6000
    max_calls: int = 2
    clock: Callable[[], float] = time.monotonic
    started_at: float | None = None
    calls: int = 0
    exhausted: str = ""
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def __post_init__(self) -> None:
        self.total_ms = max(1, int(self.total_ms))
        self.max_calls = max(0, int(self.max_calls))
        if self.started_at is None:
            self.started_at = self.clock()

    @property
    def elapsed_ms(self) -> int:
        return max(0, int((self.clock() - float(self.started_at)) * 1000))

    @property
    def remaining_ms(self) -> int:
        return max(0, self.total_ms - self.elapsed_ms)

    def reserve(self, requested_ms: int, *, minimum_ms: int = 1) -> int:
        """Reserve one provider call and return its bounded timeout in ms."""
        with self._lock:
            if self.max_calls and self.calls >= self.max_calls:
                self.exhausted = "max_model_calls"
                return 0
            remaining = self.remaining_ms
            if remaining < max(1, int(minimum_ms)):
                self.exhausted = "decision_deadline"
                return 0
            self.calls += 1
            return max(1, min(int(requested_ms), remaining))

    def snapshot(self) -> dict:
        return {"total_ms": self.total_ms, "remaining_ms": self.remaining_ms,
                "calls": self.calls, "max_calls": self.max_calls,
                "exhausted": self.exhausted or None}


_CURRENT: ContextVar[DecisionBudget | None] = ContextVar(
    "spirebrain_decision_budget", default=None)


def current_budget() -> DecisionBudget | None:
    return _CURRENT.get()


@contextmanager
def use_budget(budget: DecisionBudget | None) -> Iterator[DecisionBudget | None]:
    token = _CURRENT.set(budget)
    try:
        yield budget
    finally:
        _CURRENT.reset(token)

