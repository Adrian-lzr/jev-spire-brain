"""Versioned decision-event envelope and replay helpers.

The envelope is deliberately provider-neutral.  It contains nullable identity
fields so events without a model call can still be joined to a decision.
"""
from __future__ import annotations

import time
from collections import defaultdict
from typing import Any, Iterable

from spirebrain.redaction import redact_text

SCHEMA_VERSION = 2
BASE_FIELDS = (
    "schema_version", "run_id", "run_epoch", "decision_id", "state_id",
    "request_id", "plan_id", "advice_revision", "config_id", "event_type",
    "timestamp",
)


def _redact(value: Any, depth: int = 0) -> Any:
    if depth > 5:
        return "[truncated]"
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {str(k)[:80]: _redact(v, depth + 1) for k, v in list(value.items())[:100]}
    if isinstance(value, (list, tuple)):
        return [_redact(v, depth + 1) for v in list(value)[:100]]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return redact_text(str(value))


def make_event(event_type: str, *, identity: dict | None = None,
               fields: dict | None = None, timestamp: float | None = None) -> dict:
    identity = identity or {}
    fields = fields or {}
    event = {key: identity.get(key) for key in BASE_FIELDS}
    event.update({key: fields.get(key) for key in BASE_FIELDS if key in fields})
    event.update({k: v for k, v in fields.items() if k not in BASE_FIELDS})
    event["schema_version"] = SCHEMA_VERSION
    event["event_type"] = str(event_type)
    event["timestamp"] = time.time() if timestamp is None else float(timestamp)
    # Keep ts for consumers written against schema v1.
    event["ts"] = event["timestamp"]
    return _redact(event)


def validate_event(event: dict) -> list[str]:
    errors: list[str] = []
    if not isinstance(event, dict):
        return ["event_not_object"]
    for field in BASE_FIELDS:
        if field not in event:
            errors.append(f"missing:{field}")
    if event.get("schema_version") != SCHEMA_VERSION:
        errors.append("invalid:schema_version")
    if not isinstance(event.get("event_type"), str) or not event.get("event_type"):
        errors.append("invalid:event_type")
    if not isinstance(event.get("timestamp"), (int, float)):
        errors.append("invalid:timestamp")
    return errors


def decision_chain(records: Iterable[dict], decision_id: str) -> dict:
    """Return the evidence needed to reconstruct one decision point."""
    groups = {"inputs": [], "requests": [], "responses": [], "decisions": [],
              "advice_versions": [], "outcomes": [], "results": [], "expired": []}
    for record in records:
        if not isinstance(record, dict) or str(record.get("decision_id") or "") != str(decision_id):
            continue
        kind = str(record.get("event_type") or "")
        if kind in {"state_observed", "state", "input"}:
            bucket = "inputs"
        elif kind in {"provider_request", "brain_call", "jev_call"}:
            bucket = "requests"
        elif kind in {"provider_response", "brain_response"}:
            bucket = "responses"
        elif kind in {"decision", "final_decision"}:
            bucket = "decisions"
        elif kind in {"advice", "advice_history", "advice_replaced"}:
            bucket = "advice_versions"
        elif kind in {"outcome", "feedback", "observed_action"}:
            bucket = "outcomes"
        elif kind == "result":
            bucket = "results"
        elif kind == "expired_result":
            bucket = "expired"
        else:
            continue
        groups[bucket].append(record)
    return {"decision_id": decision_id, **groups}
