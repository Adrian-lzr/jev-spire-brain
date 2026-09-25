"""Append-only, versioned decision evidence without prompts or credentials."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from spirebrain.redaction import redact_text
from spirebrain.analysis.event_contract import make_event, decision_chain
from spirebrain.analysis.replay_blob import ReplayBlobStore


_LOCK = threading.Lock()
_FIELDS = {
    "run_id", "run_epoch", "state_id", "decision_id", "request_id", "plan_id",
    "advice_revision", "screen", "point", "config_id", "brain_backend", "brain_source",
    "jev_backend", "source_type", "source", "status", "rule_ids", "candidates",
    "filtered_candidates", "selected_candidate_id", "candidate_signature", "command", "alternative",
    "reason", "confidence", "raw_model_confidence", "model_confidence", "local_confidence",
    "jev_confidence", "selection_basis", "combat_facts", "strategic_goal", "long_term_goal",
    "candidate_id", "alternative_candidate_id", "brain_error", "error", "model", "provider",
    "latency_ms", "request_latency_ms", "brain_latency_ms", "brain_request_id",
    "error_kind", "budget", "attempts", "network_errors", "auth_errors", "rate_limits",
    "decision_latency_ms", "first_advice_latency_ms", "publish_latency_ms", "fallback",
    "fallback_reason", "legal", "legality_reason", "uncertain", "player_action", "verdict",
    "result", "evidence", "record_kind", "act", "floor", "cost_usd", "timeout", "http_status", "expired_result",
    "expired_result_count", "fixture_type", "code_version", "previous_advice_revision", "replacement_reason", "usage", "provider_request_id",
    "state_blob", "candidate_blob", "response_blob",
}


def _safe(value, depth: int = 0):
    if depth > 5:
        return "[truncated]"
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {str(key)[:80]: _safe(item, depth + 1)
                for key, item in list(value.items())[:100]}
    if isinstance(value, (list, tuple)):
        return [_safe(item, depth + 1) for item in list(value)[:100]]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return redact_text(value)


class DecisionTrace:
    """A local trace sink; file failures never affect the game loop."""

    def __init__(self, path: str | Path, config_id: str = "") -> None:
        self.path = Path(path)
        self.config_id = str(config_id)
        self._sequence = 0
        self.blobs = ReplayBlobStore(self.path.parent / "decision_blobs")

    def record(self, event_type: str, fields: dict) -> bool:
        self._sequence += 1
        record = {
            "schema_version": 2,
            "timestamp": time.time(),
            "ts": time.time(),
            "trace_sequence": self._sequence,
            "event_type": str(event_type),
            "config_id": self.config_id,
        }
        safe_fields = {key: _safe(value) for key, value in fields.items()
                       if key in _FIELDS and key != "config_id"}
        # Preserve bounded, sanitized state and candidate snapshots by content
        # hash. The JSONL event remains small and references the immutable blob.
        for field_name in ("state_blob", "candidate_blob", "response_blob"):
            value = fields.get(field_name)
            if value is not None:
                try:
                    safe_fields[field_name] = self.blobs.put(value)
                except (OSError, TypeError, ValueError):
                    safe_fields[field_name] = None
        record.update(safe_fields)
        record = make_event(event_type, fields=record, timestamp=record["timestamp"])
        record["trace_sequence"] = self._sequence
        try:
            line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with _LOCK, self.path.open("a", encoding="utf-8") as stream:
                stream.write(line)
            return True
        except (OSError, UnicodeError, TypeError, ValueError):
            return False

    def find_decision(self, decision_id: str) -> dict:
        records = []
        try:
            with self.path.open("r", encoding="utf-8") as stream:
                for line in stream:
                    try:
                        value = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(value, dict):
                        records.append(value)
        except OSError:
            pass
        return decision_chain(records, decision_id)
