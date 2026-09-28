"""Versioned decision-event envelope and replay helpers.

The envelope is deliberately provider-neutral.  It contains nullable identity
fields so events without a model call can still be joined to a decision.
"""
from __future__ import annotations

import time
from collections import Counter
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
    "provider_request": {
        "run_id": (str, type(None)), "run_epoch": (int, type(None)),
        "decision_id": (str, type(None)), "state_id": (str, type(None)),
        "request_id": (str,),
        "logical_request_id": (str,), "attempt_id": (str,),
        "provider": (str,),
    },
    "provider_response": {
        "run_id": (str, type(None)), "run_epoch": (int, type(None)),
        "decision_id": (str, type(None)), "state_id": (str, type(None)),
        "request_id": (str,),
        "logical_request_id": (str,), "attempt_id": (str,),
        "provider": (str,), "latency_ms": (int, float),
        "error_kind": (str,), "fallback": (bool,),
    },
    "decision": {"decision_id": (str,), "state_id": (str,),
                  "command": (dict,), "source_type": (str,)},
    "advice": {"decision_id": (str,), "state_id": (str,),
                "advice_revision": (int,), "command": (dict,)},
    "advice_replaced": {"decision_id": (str,),
                         "advice_revision": (int,),
                         "previous_advice_revision": (int,)},
    "outcome": {"decision_id": (str,), "verdict": (str,)},
    "expired_result": {"decision_id": (str, type(None)), "state_id": (str, type(None)),
                         "expired_result": (bool,), "fallback_reason": (str,)},
    "decision_skipped": {"decision_id": (str, type(None)),
                          "state_id": (str, type(None)), "reason": (str,)},
    "strategic_context_retained": {
        "state_id": (str, type(None)), "plan_id": (str, type(None)),
        "fallback_reason": (str,), "context_only": (bool,),
    },
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
_OPTIONAL_EVENT_FIELDS = {
    "provider_request": {"generation": (int, type(None)),
                          "trigger": (str, type(None))},
    "provider_response": {"generation": (int, type(None)),
                           "trigger": (str, type(None)),
                           "provider_request_id": (str, type(None))},
    "decision": {"provider_request_ids": (list,)},
    "state_observed": {"provider_request_ids": (list,)},
    "expired_result": {"provider_request_ids": (list,),
                        "generation": (int, type(None)),
                         "trigger": (str, type(None)),
                         "pending_state_id": (str, type(None)),
                         "pending_generation": (int, type(None)),
                         "pending_run_id": (str, type(None))},
    "decision_skipped": {"generation": (int,)},
    "strategic_context_retained": {"generation": (int, type(None))},
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
            for field, types in _OPTIONAL_EVENT_FIELDS.get(event_type, {}).items():
                if field in event and not isinstance(event.get(field), types):
                    errors.append(f"invalid:{field}")
    return errors


def decision_chain(records: Iterable[dict], decision_id: str) -> dict:
    """Return the evidence needed to reconstruct one decision point."""
    groups = {"inputs": [], "requests": [], "responses": [], "decisions": [],
              "advice_versions": [], "outcomes": [], "results": [], "expired": [],
              "skipped": [], "strategic_context": [], "unattributed": [],
              "unattributed_total": 0,
              "unattributed_truncated": False,
              "request_lifecycle": {"request_count": 0, "response_count": 0,
                                    "closed_request_count": 0, "open_request_ids": [],
                                    "expired_request_ids": [], "orphan_response_ids": [],
                                    "duplicate_request_ids": [],
                                    "duplicate_response_ids": [],
                                    "identity_mismatches": []}}
    buckets = {
        "state_observed": "inputs", "state": "inputs", "input": "inputs",
        "provider_request": "requests", "brain_call": "requests", "jev_call": "requests",
        "provider_response": "responses", "brain_response": "responses",
        "decision": "decisions", "final_decision": "decisions",
        "advice": "advice_versions", "advice_history": "advice_versions",
        "advice_replaced": "advice_versions",
        "outcome": "outcomes", "feedback": "outcomes", "observed_action": "outcomes",
        "fact_memory": "outcomes", "deviation_record": "outcomes",
        "unobserved_record": "outcomes", "result": "results", "result_record": "results",
        "expired_result": "expired", "decision_skipped": "skipped",
        "strategic_context_retained": "strategic_context",
    }
    for record in records:
        if not isinstance(record, dict):
            continue
        kind = str(record.get("event_type") or "")
        bucket = buckets.get(kind)
        if bucket is None:
            continue
        record_decision_id = str(record.get("decision_id") or "")
        if not record_decision_id:
            # Legacy events cannot be assigned to a decision merely because a
            # query happened to include them. Surface them separately instead.
            item = dict(record)
            missing = [field for field in BASE_FIELDS if field not in item]
            required = _EVENT_FIELDS.get(kind, {})
            missing.extend(field for field in required if field not in item and field not in missing)
            item["trace_quality"] = "legacy_partial"
            item["unknown_fields"] = missing
            item["attribution"] = "unknown"
            groups["unattributed_total"] += 1
            if len(groups["unattributed"]) < 200:
                groups["unattributed"].append(item)
            continue
        if record_decision_id != str(decision_id):
            continue
        # Keep old traces readable, but make absent historical evidence
        # explicit. Missing is not equivalent to a measured null.
        item = dict(record)
        missing = [field for field in BASE_FIELDS if field not in item]
        required = _EVENT_FIELDS.get(kind, {})
        missing.extend(field for field in required if field not in item and field not in missing)
        legacy_schema = item.get("schema_version") != SCHEMA_VERSION
        item["trace_quality"] = "legacy_partial" if missing or legacy_schema else "complete"
        item["unknown_fields"] = missing
        groups[bucket].append(item)
    groups["unattributed_truncated"] = (
        groups["unattributed_total"] > len(groups["unattributed"]))
    requests = groups["requests"]
    responses = groups["responses"]
    expired = groups["expired"]
    request_counts = Counter(str(item.get("request_id") or "") for item in requests
                             if item.get("request_id"))
    response_counts = Counter(str(item.get("request_id") or "") for item in responses
                              if item.get("request_id"))
    request_by_id = {str(item.get("request_id")): item for item in requests
                     if item.get("request_id")}
    response_by_id = {str(item.get("request_id")): item for item in responses
                      if item.get("request_id")}
    expired_ids = {str(item.get("request_id")) for item in expired
                   if item.get("request_id")}
    mismatches = []
    for request_id, request in request_by_id.items():
        response = response_by_id.get(request_id)
        if response is None:
            continue
        differing = []
        for field in ("decision_id", "state_id", "run_id", "run_epoch",
                      "logical_request_id", "attempt_id"):
            left, right = request.get(field), response.get(field)
            if left not in (None, "") and right not in (None, "") and left != right:
                differing.append(field)
        if differing:
            mismatches.append({"request_id": request_id, "fields": differing})
    lifecycle = groups["request_lifecycle"]
    lifecycle.update({
        "request_count": len(requests),
        "response_count": len(responses),
        "closed_request_count": len(set(request_by_id) & set(response_by_id)),
        "open_request_ids": sorted(set(request_by_id) - set(response_by_id) - expired_ids),
        "expired_request_ids": sorted(expired_ids),
        "orphan_response_ids": sorted(set(response_by_id) - set(request_by_id)),
        "duplicate_request_ids": sorted(key for key, count in request_counts.items()
                                         if count > 1),
        "duplicate_response_ids": sorted(key for key, count in response_counts.items()
                                          if count > 1),
        "identity_mismatches": mismatches,
    })
    return {"decision_id": decision_id, **groups}
