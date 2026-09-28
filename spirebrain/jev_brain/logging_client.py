"""Structured logging for every JEV call: state, questions, answers,
confidence, latency, backend, cost. JSONL to logs/ for calibration analysis."""

from __future__ import annotations

import json
import threading
import time
import urllib.error
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
from spirebrain.jev_brain.client_real import JevApiError
from spirebrain.redaction import redact_text


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
        self.context_provider = None
        self._decision_request_ids: dict[str, list[str]] = {}
        self._request_ids_lock = threading.Lock()

    def request_ids_for_decision(self, decision_id: str) -> list[str]:
        with self._request_ids_lock:
            return list(self._decision_request_ids.get(str(decision_id or ""), ()))

    def _remember_request(self, decision_id: str, request_id: str) -> None:
        if not decision_id:
            return
        with self._request_ids_lock:
            values = self._decision_request_ids.setdefault(decision_id, [])
            if request_id not in values:
                values.append(request_id)
            if len(values) > 32:
                del values[:-32]
            while len(self._decision_request_ids) > 128:
                self._decision_request_ids.pop(next(iter(self._decision_request_ids)))

    def ask(self, state, questions: dict[str, QuestionSpec]) -> JevResponse:
        t0 = time.perf_counter()
        request_id = str(uuid.uuid4())
        context = state if isinstance(state, dict) else {}
        identity = {key: context.get(key) for key in
                    ("run_id", "run_epoch", "decision_id", "state_id", "plan_id",
                     "config_id", "generation", "trigger")}
        if callable(self.context_provider):
            try:
                supplied = self.context_provider()
                if isinstance(supplied, dict):
                    identity.update({key: supplied.get(key) for key in identity})
            except Exception:
                pass
        identity["request_id"] = request_id
        identity["logical_request_id"] = str(context.get("logical_request_id") or request_id)
        identity["attempt_id"] = str(context.get("attempt_id") or f"{request_id}:1")
        self._remember_request(str(identity.get("decision_id") or ""), request_id)
        request_blob = {
            "state": state,
            "questions": build_questions_json(questions),
        }
        if self.event_sink is not None:
            try:
                self.event_sink("provider_request", {**identity, "provider": self.backend_name,
                                                       "model": getattr(self.inner, "model", "jev"),
                                                       "state_blob": request_blob})
            except Exception:
                pass
        error_kind = ""
        error = ""
        http_status = None
        try:
            resp = self.inner.ask(state, questions)
        except Exception as exc:  # network providers are a non-fatal side channel
            if isinstance(exc, JevApiError):
                error_kind = str(getattr(exc, "error_kind", "provider_error")
                                 or "provider_error")
                status = getattr(exc, "http_status", None)
                http_status = int(status) if isinstance(status, int) else None
            elif isinstance(exc, urllib.error.HTTPError):
                http_status = int(exc.code)
                error_kind = ("auth_error" if http_status in {401, 402, 403}
                              else "rate_limited" if http_status == 429
                              else "http_error")
            elif isinstance(exc, TimeoutError):
                error_kind = "timeout"
            elif isinstance(exc, (urllib.error.URLError, OSError)):
                error_kind = "network_error"
            else:
                error_kind = "provider_error"
            error = redact_text(f"{type(exc).__name__}: {exc}")
            resp = JevResponse(backend=self.backend_name,
                               model=str(getattr(self.inner, "model", "") or ""),
                               error=error, error_kind=error_kind, fallback=True)
        if not isinstance(resp, JevResponse):
            resp = JevResponse(backend=self.backend_name,
                               error="invalid_provider_response",
                               error_kind="parse_or_validation", fallback=True)
        resp.request_id = request_id
        wall_ms = int((time.perf_counter() - t0) * 1000)
        resp.latency_ms = wall_ms
        self.calls += 1
        self.total_cost_usd += resp.cost_usd
        if self.event_sink is not None:
            try:
                response_blob = {
                    "answers": {name: {"value": answer.value,
                                       "confidence": answer.confidence,
                                       "raw": answer.raw}
                                for name, answer in resp.answers.items()},
                    "backend": resp.backend,
                    "model": resp.model,
                    "usage": resp.usage,
                    "error_kind": resp.error_kind,
                    "fallback": bool(resp.fallback),
                }
                self.event_sink("provider_response", {**identity, "provider": resp.backend,
                    "model": resp.model, "latency_ms": wall_ms, "cost_usd": resp.cost_usd,
                    "usage": resp.usage, "error": resp.error,
                    "error_kind": resp.error_kind, "fallback": bool(resp.fallback),
                    "timeout": resp.error_kind == "timeout", "http_status": http_status,
                    "provider_request_id": resp.provider_request_id or None,
                    "response_blob": response_blob})
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
            "error": resp.error,
            "error_kind": resp.error_kind,
            "fallback": bool(resp.fallback),
            "request_id": request_id,
            "provider_request_id": resp.provider_request_id or None,
        }
        try:
            with open(self.log_path / "jev_calls.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except (OSError, UnicodeError, TypeError, ValueError):
            pass
        return resp
