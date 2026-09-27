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
    "request_id", "logical_request_id", "attempt_id", "plan_id",
    "advice_revision", "config_id", "event_type",
    "timestamp",
)

_IDENTITY_TYPES = {
    "schema_version": (int,),
    "run_id": (str, type(None)),
    "run_epoch": (int, type(None)),
    "decision_id": (str, type(None)),
    "state_id": (str, type(None)),
    "request_id": (str, type(None)),
    "logical_request_id": (str, type(None)),
    "attempt_id": (str, type(None)),
    "plan_id": (str, type(None)),
    "advice_revision": (int, type(None)),
    "config_id": (str, type(None)),
    "event_type": (str,),
    "timestamp": (int, float),
}

# Strict validation is opt-in so old schema-2 records remain readable, while
# new producer/consumer fixtures can assert semantic fields and types.
_EVENT_FIELDS = {
    "state_observed": {"state_id": (str,)},
    "provider_request": {"request_id": (str,), "provider": (str,)},
    "provider_response": {"request_id": (str,), "provider": (str,)},
    "decision": {"decision_id": (str,), "state_id": (str,),
                  "command": (dict,), "source_type": (str,)},
    "advice": {"decision_id": (str,), "state_id": (str,),
                "advice_revision": (int,), "command": (dict,)},
    "advice_replaced": {"decision_id": (str,),
                         "advice_revision": (int,),
                         "previous_advice_revision": (int,)},
    "outcome": {"decision_id": (str,), "verdict": (str,)},
    "expired_result": {"expired_result": (bool,)},
    "execution_attempt": {"state_id": (str,), "command": (dict,),
                           "status": (str,)},
    "fact_memory": {"state_id": (str,), "verdict": (str,)},
    "deviation_record": {"state_id": (str,), "decision_id": (str, type(None)),
                          "verdict": (str,)},
    "unobserved_record": {"state_id": (str,), "decision_id": (str, type(None)),
                          "verdict": (str,)},
    "result_record": {"state_id": (str,), "result":
                       (str, dict, list, int, float, bool, type(None))},
}


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


def validate_event(event: dict, *, strict: bool = False) -> list[str]:
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
    if strict:
        for field, types in _IDENTITY_TYPES.items():
            value = event.get(field)
            if not isinstance(value, types):
                errors.append(f"invalid:{field}")
        event_type = str(event.get("event_type") or "")
        expected = _EVENT_FIELDS.get(event_type)
        if expected is None:
            errors.append("invalid:event_type_semantics")
        else:
            for field, types in expected.items():
                if field not in event:
                    errors.append(f"missing:{field}")
                elif not isinstance(event.get(field), types):
                    errors.append(f"invalid:{field}")
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
        elif kind in {"outcome", "feedback", "observed_action", "fact_memory",
                      "deviation_record", "unobserved_record"}:
            bucket = "outcomes"
        elif kind in {"result", "result_record"}:
            bucket = "results"
        elif kind == "expired_result":
            bucket = "expired"
        else:
            continue
        groups[bucket].append(record)
    return {"decision_id": decision_id, **groups}
