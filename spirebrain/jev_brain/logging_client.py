"""Structured logging for every JEV call: state, questions, answers,
confidence, latency, backend, cost. JSONL to logs/ for calibration analysis."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from spirebrain.jev_brain.client import (
    PROMPT_VERSION,
    JevClient,
    JevResponse,
    QuestionSpec,
    build_questions_json,
    dump_state,
)


class LoggingJevClient(JevClient):
    """Decorator around any JevClient that records every ask()."""

    def __init__(self, inner: JevClient, log_dir: str | Path = "logs") -> None:
        self.inner = inner
        self.backend_name = inner.backend_name
        self.log_path = Path(log_dir)
        self.log_path.mkdir(parents=True, exist_ok=True)
        self.calls: int = 0
        self.total_cost_usd: float = 0.0

    def ask(self, state, questions: dict[str, QuestionSpec]) -> JevResponse:
        t0 = time.perf_counter()
        resp = self.inner.ask(state, questions)
        wall_ms = int((time.perf_counter() - t0) * 1000)
        self.calls += 1
        self.total_cost_usd += resp.cost_usd

        # Prompt version + payload size travel with every record: a threshold is
        # only meaningful next to the question set that produced it, and a state
        # that grew silently is indistinguishable from a model that changed its mind.
        request_bytes = len(
            json.dumps({"state": state, "questions": build_questions_json(questions)},
                       ensure_ascii=False).encode("utf-8")
        )
        record = {
            "seq": self.calls,
            "ts": time.time(),
            "prompt_version": PROMPT_VERSION,
            "request_bytes": request_bytes,
            "backend": resp.backend,
            "model": resp.model,
            "state": dump_state(state)[:2000],
            "questions": {
                k: {"type": type(v).__name__.removesuffix("Spec"),
                    "instructions": getattr(v, "instructions", "")[:400]}
                for k, v in questions.items()
            },
            "answers": {k: {"value": a.value, "confidence": a.confidence, "raw": a.raw}
                        for k, a in resp.answers.items()},
            "latency_ms": resp.latency_ms,
            "wall_ms": wall_ms,
            "cost_usd": resp.cost_usd,
            "usage": resp.usage,
        }
        with open(self.log_path / "jev_calls.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        return resp
