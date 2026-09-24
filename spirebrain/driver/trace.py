"""Append-only, versioned decision evidence without prompts or credentials."""

from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path


_LOCK = threading.Lock()
_SECRET = re.compile(
    r"(?i)(authorization\s*[:=]\s*bearer\s+|bearer\s+|api[_ -]?key\s*[:=]\s*)\S+"
)
_FIELDS = {
    "run_id", "state_id", "decision_id", "request_id", "plan_id", "screen", "point", "config_id",
    "brain_backend", "jev_backend", "source_type", "rule_ids", "candidates",
    "filtered_candidates", "selected_candidate_id", "candidate_signature", "command", "alternative",
    "reason", "confidence", "jev_confidence", "latency_ms", "request_latency_ms",
    "decision_latency_ms", "first_advice_latency_ms", "publish_latency_ms", "fallback",
    "fallback_reason", "legal", "legality_reason", "uncertain", "player_action", "verdict",
    "result", "act", "floor", "cost_usd", "timeout", "http_status", "expired_result",
    "expired_result_count", "fixture_type", "code_version",
}


def _safe(value, depth: int = 0):
    if depth > 5:
        return "[truncated]"
    if isinstance(value, str):
        return _SECRET.sub(lambda match: f"{match.group(1)}[redacted]", value[:1000])
    if isinstance(value, dict):
        return {str(key)[:80]: _safe(item, depth + 1)
                for key, item in list(value.items())[:100]}
    if isinstance(value, (list, tuple)):
        return [_safe(item, depth + 1) for item in list(value)[:100]]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:1000]


class DecisionTrace:
    """A local trace sink; file failures never affect the game loop."""

    def __init__(self, path: str | Path, config_id: str = "") -> None:
        self.path = Path(path)
        self.config_id = str(config_id)
        self._sequence = 0

    def record(self, event_type: str, fields: dict) -> bool:
        self._sequence += 1
        record = {
            "schema_version": 1,
            "ts": time.time(),
            "trace_sequence": self._sequence,
            "event_type": str(event_type),
            "config_id": self.config_id,
        }
        record.update({key: _safe(value) for key, value in fields.items()
                       if key in _FIELDS and key != "config_id"})
        try:
            line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with _LOCK, self.path.open("a", encoding="utf-8") as stream:
                stream.write(line)
            return True
        except (OSError, UnicodeError, TypeError, ValueError):
            return False
