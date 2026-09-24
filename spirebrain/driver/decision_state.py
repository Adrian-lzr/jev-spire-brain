"""Semantic state identities shared by transport, advisor and planners.

CommunicationMod emits a transport envelope that may change every poll while the
decision situation is unchanged.  This module strips those volatile fields and
produces two identities: ``state_id`` for the semantic game state and
``recommendation_key`` for the state plus the currently legal candidates.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, is_dataclass
from typing import Any, Iterable


_IGNORED_KEYS = {
    "timestamp", "time", "created_at", "updated_at", "received_at",
    "uuid", "request_id", "message_id", "event_id", "random_uuid",
    "animation", "animation_id", "animation_counter", "frame", "frames",
    "poll_id", "transport", "transport_state", "log", "logs", "debug",
    "trace", "trace_id", "ready_for_command", "raw_message",
}


def _clean(value: Any, *, key: str = "") -> Any:
    lowered = key.lower()
    if lowered in _IGNORED_KEYS or lowered.endswith(("_timestamp", "_uuid")):
        return None
    if is_dataclass(value):
        value = asdict(value)
    if isinstance(value, dict):
        out = {}
        for name, item in value.items():
            if not isinstance(name, str):
                name = str(name)
            cleaned = _clean(item, key=name)
            if cleaned is not None:
                out[name] = cleaned
        return {name: out[name] for name in sorted(out)}
    if isinstance(value, (list, tuple)):
        return [_clean(item, key=key) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return repr(value)


def semantic_projection(game: Any) -> dict:
    """Return a deterministic, redacted projection of decision-relevant state."""
    if not isinstance(game, dict):
        return {}
    cleaned = _clean(game)
    if not isinstance(cleaned, dict):
        return {}
    # These are transport capabilities, not game choices.  Legal commands are
    # retained separately below because they do affect which actions exist.
    cleaned.pop("available_commands", None)
    return cleaned


def _digest(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
                     default=str)
    return hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()


def state_id(game: Any) -> str:
    """Stable identity for the semantic game state."""
    return _digest(semantic_projection(game))


def _candidate_value(candidate: Any) -> Any:
    if is_dataclass(candidate):
        candidate = asdict(candidate)
    if isinstance(candidate, dict):
        return {key: candidate.get(key) for key in (
            "candidate_id", "candidate_signature", "kind", "label", "legal",
            "cost", "target", "goal_tags", "risk_tags", "uncertainty", "command",
        ) if key in candidate}
    return str(candidate)


def recommendation_key(game: Any, candidates: Iterable[Any] | None = None) -> str:
    """Identity used to suppress duplicate advice for one legal decision point."""
    projection = semantic_projection(game)
    if isinstance(game, dict) and isinstance(game.get("available_commands"), list):
        projection["available_commands"] = _clean(game["available_commands"], key="available_commands")
    if candidates is not None:
        projection["candidates"] = [_candidate_value(candidate) for candidate in candidates]
    return _digest(projection)


class GenerationCounter:
    """Small monotonic generation source for async result validity checks."""

    def __init__(self) -> None:
        self.value = 0

    def next(self) -> int:
        self.value += 1
        return self.value

