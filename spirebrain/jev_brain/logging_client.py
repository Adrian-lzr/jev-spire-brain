"""Structured logging for every JEV call: state, questions, answers,
confidence, latency, backend, cost. JSONL to logs/ for calibration analysis."""

from __future__ import annotations

import json
import time
import uuid
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

    def __init__(self, inner: JevClient, log_dir: str | Path = "logs", event_sink=None) -> None:
        self.inner = inner
        self.backend_name = inner.backend_name
        self.async_required = bool(getattr(inner, "async_required", False))
        self.log_path = Path(log_dir)
        try:
            self.log_path.mkdir(parents=True, exist_ok=True)
        except OSError:
            # Logging is an observability side channel. A read-only disk must
            # never disable a valid JEV provider.
            pass
        self.calls: int = 0
        self.total_cost_usd: float = 0.0
        self.event_sink = event_sink

    def ask(self, state, questions: dict[str, QuestionSpec]) -> JevResponse:
        t0 = time.perf_counter()
        request_id = str(uuid.uuid4())
        context = state if isinstance(state, dict) else {}
        identity = {key: context.get(key) for key in
                    ("run_id", "run_epoch", "decision_id", "state_id", "plan_id", "config_id")}
        identity["request_id"] = request_id
        if self.event_sink is not None:
            try:
                self.event_sink("provider_request", {**identity, "provider": self.backend_name,
                                                       "model": getattr(self.inner, "model", "jev")})
            except Exception:
                pass
        resp = self.inner.ask(state, questions)
        wall_ms = int((time.perf_counter() - t0) * 1000)
        self.calls += 1
        self.total_cost_usd += resp.cost_usd
        if self.event_sink is not None:
            try:
                self.event_sink("provider_response", {**identity, "provider": resp.backend,
                    "model": resp.model, "latency_ms": wall_ms, "cost_usd": resp.cost_usd,
                    "usage": resp.usage})
            except Exception:
                pass

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
        try:
            with open(self.log_path / "jev_calls.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except (OSError, UnicodeError, TypeError, ValueError):
            pass
        return resp
