"""Append-only, versioned decision evidence without prompts or credentials."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from spirebrain.redaction import redact_text
from spirebrain.analysis.event_contract import make_event, decision_chain, validate_event
from spirebrain.analysis.replay_blob import ReplayBlobStore
from spirebrain.logging_utils import append_bounded, rotated_log_paths


_LOCK = threading.Lock()
_SEQUENCES: dict[str, int] = {}
_FIELDS = {
    "run_id", "run_epoch", "state_id", "decision_id", "request_id",
    "logical_request_id", "attempt_id", "plan_id",
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
    "result", "evidence", "record_kind", "act", "floor", "cost_usd", "timeout", "http_status",
    "http_error_code", "http_error_param", "expired_result",
    "expired_result_count", "fixture_type", "code_version", "previous_advice_revision", "replacement_reason", "usage", "provider_request_id",
    "provider_request_ids", "generation",
    "queue_wait_ms", "pre_provider_ms", "http_latency_ms",
    "parse_latency_ms", "worker_elapsed_ms", "commit_latency_ms",
    "response_bytes", "strategy_fingerprint", "strategic_scope_id",
    "result_disposition", "http_status",
    "trigger", "context_only", "pending_state_id", "pending_generation",
    "pending_run_id", "in_flight", "lifecycle_reason",
    "state_blob", "candidate_blob", "response_blob", "knowledge_citation_ids",
    "contract_errors",
    "character", "terminal_screen", "review_status", "proposal_count",
    "provider_attempted", "attempt_count", "review_request_id", "review_error_kind",
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
        self._sequence_key = str(self.path.resolve())
        self.config_id = str(config_id)
        with _LOCK:
            if self._sequence_key not in _SEQUENCES:
                _SEQUENCES[self._sequence_key] = self._read_last_sequence()
            self._sequence = _SEQUENCES[self._sequence_key]
        self.blobs = ReplayBlobStore(self.path.parent / "decision_blobs")

    def _read_last_sequence(self) -> int:
        sequence = 0
        for source in rotated_log_paths(self.path):
            try:
                stream = source.open("r", encoding="utf-8")
            except OSError:
                continue
            try:
                for line in stream:
                    try:
                        value = json.loads(line)
                        sequence = max(sequence, int(value.get("trace_sequence", 0)))
                    except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
                        continue
            finally:
                stream.close()
        return sequence

    def record(self, event_type: str, fields: dict) -> bool:
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
        with _LOCK:
            sequence = _SEQUENCES.get(self._sequence_key)
            if sequence is None:
                sequence = self._read_last_sequence()
            sequence += 1
            _SEQUENCES[self._sequence_key] = sequence
            self._sequence = sequence
            timestamp = time.time()
            record = {
                "schema_version": 2,
                "timestamp": timestamp,
                "ts": timestamp,
                "trace_sequence": sequence,
                "event_type": str(event_type),
                "config_id": self.config_id,
            }
            record.update(safe_fields)
            record = make_event(event_type, fields=record, timestamp=timestamp)
            record["trace_sequence"] = sequence
            # Validate producer semantics at the write boundary.  A malformed
            # diagnostic event must remain observable without ever blocking the
            # game loop, so errors are attached to the record rather than
            # raising or dropping the event.
            contract_errors = validate_event(record, strict=True)
            if contract_errors:
                record["contract_errors"] = contract_errors
            line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
            # ``record`` already holds the process-wide trace lock. Passing it
            # into the helper again would deadlock because ``threading.Lock``
            # is not re-entrant.
            return append_bounded(self.path, line)

    def find_decision(self, decision_id: str) -> dict:
        records = []
        by_sequence = {}
        for source in rotated_log_paths(self.path):
            try:
                stream = source.open("r", encoding="utf-8")
            except OSError:
                continue
            try:
                for line in stream:
                    try:
                        value = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(value, dict):
                        sequence = value.get("trace_sequence")
                        if isinstance(sequence, (int, float)):
                            by_sequence[sequence] = value
                        else:
                            records.append(value)
            finally:
                stream.close()
        records.extend(by_sequence[key] for key in sorted(by_sequence))
        return decision_chain(records, decision_id)
