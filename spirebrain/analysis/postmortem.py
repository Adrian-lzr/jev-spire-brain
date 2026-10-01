"""Structured run review and release evidence helpers.

This module is intentionally offline and read-only: it consumes decision trace
events and produces reviewable facts without turning observations into rules.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import platform
import re
import sys
import time
import uuid
from collections import Counter
from pathlib import Path
from typing import Iterable

from .metrics import load_records
from .event_contract import decision_chain
from .experience import DEFAULT_LEDGER_PATH, ExperienceLedger, OpenAIExperienceReviewer
from spirebrain.redaction import redact_text, redact_value

ROOT = Path(__file__).resolve().parents[2]
_TERMINAL_RESULTS = frozenset({"victory", "death", "game_over", "win", "loss"})
_SENSITIVE_MODEL_KEYS = frozenset({
    "api_key", "apikey", "authorization", "auth", "token", "access_token",
    "secret", "password", "headers", "path", "filepath", "file_path", "cwd",
    "executable", "env", "environment", "command", "request", "request_body",
    "user", "user_id", "username", "player", "player_id", "player_name",
    "directory", "dir", "filename", "file", "game_path", "install_dir",
    "working_directory",
})
_WINDOWS_PATH = re.compile(r"(?i)[A-Za-z]:\\[^\s,}\]]+")
_POSIX_PATH = re.compile(r"(?<![\w])/(?:Users|home|tmp|var|private)/[^\s,}\]]+")


def _review_request_id() -> str:
    """Create a local correlation id without embedding run or credential data."""
    return "review-" + uuid.uuid4().hex[:24]


def _review_error_kind(exc: BaseException, *, stage: str = "provider") -> str:
    """Map review failures to a bounded diagnostic enum, never raw text."""
    detail = str(exc).lower()
    if "missing_strategic_key" in detail:
        return "missing_key"
    if "http_401" in detail or "http_403" in detail:
        return "auth_error"
    if "http_408" in detail or "timeout" in detail:
        return "timeout"
    if "http_429" in detail or "rate" in detail:
        return "rate_limited"
    if isinstance(exc, (OSError, ConnectionError)):
        return "network_error"
    if stage == "ledger":
        return "ledger_error"
    if isinstance(exc, ValueError):
        return "response_validation"
    return "provider_error"


def _record_review_event(path: str | Path, event_type: str, *, run_id: str,
                         run_epoch: int, request_id: str, provider: str,
                         model: str, provider_attempted: bool, **fields) -> bool:
    """Best-effort append to the same trace used by the game loop.

    Post-run review must not make an already completed run unusable when a log
    file is read-only or rotated away.  The return value is exposed in the
    report so missing audit evidence remains visible rather than implied.
    """
    try:
        from spirebrain.driver.trace import DecisionTrace
        return DecisionTrace(path).record(event_type, {
            "run_id": str(run_id), "run_epoch": int(run_epoch),
            "request_id": str(request_id), "provider": str(provider or "unknown"),
            "model": str(model or "unknown"),
            "provider_attempted": bool(provider_attempted),
            **fields,
        })
    except (OSError, TypeError, ValueError):
        return False


def _run_matches(record: dict, run_id: str, run_epoch: int | None) -> bool:
    if str(record.get("run_id") or "") != str(run_id):
        return False
    if run_epoch is None:
        return True
    try:
        return int(record.get("run_epoch")) == int(run_epoch)
    except (TypeError, ValueError):
        return False


def _run_event_id(record: dict, index: int) -> str:
    sequence = record.get("trace_sequence")
    return f"trace:{sequence}" if isinstance(sequence, int) and sequence > 0 else f"legacy:{index + 1}"


def _epoch_value(value: object) -> int | None:
    """Parse an epoch without treating booleans or arbitrary text as identity."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _lifecycle_event_matches(event: dict, event_type: str,
                             run_id: str, run_epoch: int | None) -> bool:
    """Require a source-tagged event to belong to the selected run identity."""
    if event.get("event_type") != event_type or event.get("source_type") != "real_game":
        return False
    if str(event.get("run_id") or "") != str(run_id):
        return False
    # A terminal event without an epoch cannot prove which incarnation ended.
    event_epoch = _epoch_value(event.get("run_epoch"))
    return run_epoch is not None and event_epoch == int(run_epoch)


def _observed_scope(events: list[dict]) -> tuple[list[str], list[str], list[int]]:
    characters: set[str] = set()
    scenes: set[str] = set()
    acts: set[int] = set()
    for event in events:
        character = str(event.get("character") or "").strip().upper()
        if character:
            characters.add(character[:40])
        for key in ("screen", "scene", "point"):
            scene = str(event.get(key) or "").strip().upper()
            if scene and scene not in {"UNKNOWN", "NONE"}:
                scenes.add(scene[:40])
        for key in ("act", "act_number"):
            act = _epoch_value(event.get(key))
            if act is not None and 1 <= act <= 3:
                acts.add(act)
    return sorted(characters), sorted(scenes), sorted(acts)


def _validate_proposal_scope(proposal: dict, run_review: dict) -> None:
    """Reject model claims about scenes/acts that were not observed in this run."""
    observed_characters = {str(value).upper() for value in
                           (run_review.get("observed_characters") or [])}
    observed_scenes = {str(value).upper() for value in
                       (run_review.get("observed_scenes") or [])}
    observed_acts = {int(value) for value in (run_review.get("observed_acts") or [])
                     if _epoch_value(value) is not None}
    character = str(proposal.get("character") or "").strip().upper()
    scenes = {str(value).strip().upper()[:40] for value in
              (proposal.get("scenes") or []) if str(value).strip()}
    acts = {_epoch_value(value) for value in (proposal.get("acts") or [])}
    acts.discard(None)
    if (not character or character not in observed_characters
            or not scenes or not scenes.issubset(observed_scenes)
            or not acts or not acts.issubset(observed_acts)):
        raise ValueError("experience proposal scope is outside observed run")


def _pseudonymize_run_id(run_id: object) -> str:
    digest = hashlib.sha256(str(run_id or "").encode("utf-8")).hexdigest()
    return f"run-{digest[:16]}"


def _safe_model_value(value: object, *, key: str | None = None) -> object:
    """Redact and bound nested values before they cross the model boundary."""
    normalized_key = key.lower().replace("-", "_") if key else ""
    if normalized_key in _SENSITIVE_MODEL_KEYS:
        return None
    value = redact_value(value)
    if isinstance(value, str):
        value = redact_text(value)
        value = _WINDOWS_PATH.sub("[path-redacted]", value)
        value = _POSIX_PATH.sub("[path-redacted]", value)
        return value
    if isinstance(value, dict):
        safe = {}
        for raw_key, item in value.items():
            text_key = str(raw_key)[:80]
            if text_key.lower() in _SENSITIVE_MODEL_KEYS:
                continue
            cleaned = _safe_model_value(item, key=text_key)
            if cleaned is not None:
                safe[text_key] = cleaned
        return safe
    if isinstance(value, list):
        return [_safe_model_value(item) for item in value]
    return value


def _prepare_model_review(run_review: dict) -> dict:
    """Create a bounded, pseudonymous copy used only for strategic review."""
    safe = {
        key: _safe_model_value(value)
        for key, value in run_review.items()
        if key not in {"events", "run_id", "results", "decisions", "advice_versions",
                       "provider_requests", "provider_responses", "player_feedback"}
    }
    safe["run_id"] = _pseudonymize_run_id(run_review.get("run_id"))
    safe["events"] = []
    allowed_event_keys = {
        "event_id", "event_type", "decision_id", "state_id", "screen", "point",
        "source_type", "source", "alternative", "reason", "legal",
        "legality_reason", "fallback", "fallback_reason", "error_kind", "latency_ms",
        "player_action", "verdict", "result", "act", "floor", "combat_facts",
        "rule_ids", "evidence", "terminal_screen", "result_source",
    }
    for event in list(run_review.get("events") or [])[:80]:
        reduced = {key: value for key, value in event.items() if key in allowed_event_keys}
        cleaned = _safe_model_value(reduced)
        if isinstance(cleaned, dict):
            cleaned.pop("run_id", None)
            cleaned.pop("command", None)
        safe["events"].append(cleaned)
    safe["evidence_event_ids"] = _safe_model_value(
        list(run_review.get("evidence_event_ids") or [])[:80]
    )
    # Keep model input bounded even if an upstream trace contains many fields.
    while len(json.dumps(safe, ensure_ascii=False).encode("utf-8")) > 48 * 1024 \
            and safe["events"]:
        safe["events"].pop()
    if len(json.dumps(safe, ensure_ascii=False).encode("utf-8")) > 48 * 1024:
        raise ValueError("run review exceeds the bounded model input size")
    return safe


def review_run(records: Iterable[dict], run_id: str, *,
               run_epoch: int | None = None) -> dict:
    """Build one auditable run summary without inferring missing outcomes.

    When the trace contains multiple epochs for the same external run id, the
    caller must name the epoch so a restarted seed cannot be joined to old data.
    """
    rows = [dict(row) for row in records if isinstance(row, dict)
            and _run_matches(row, run_id, run_epoch)]
    if not rows:
        raise ValueError(f"no trace records for run_id={run_id!r}")
    epochs = {int(row["run_epoch"]) for row in rows
              if isinstance(row.get("run_epoch"), int)}
    if run_epoch is None and len(epochs) > 1:
        raise ValueError("run has multiple run_epoch values; specify --run-epoch")
    has_unscoped_rows = any(not isinstance(row.get("run_epoch"), int) for row in rows)
    if run_epoch is None and epochs and has_unscoped_rows:
        raise ValueError("run contains events without run_epoch; specify --run-epoch")
    selected_epoch = int(run_epoch) if run_epoch is not None else next(iter(epochs), None)
    ordered = sorted(enumerate(rows), key=lambda item: (
        item[1].get("trace_sequence") if isinstance(item[1].get("trace_sequence"), int)
        else item[0]))
    events = []
    ids = []
    for original_index, row in ordered:
        event = dict(row)
        event["event_id"] = _run_event_id(row, original_index)
        ids.append(event["event_id"])
        events.append(event)

    decisions = [row for row in events
                 if row.get("event_type") in {"decision", "final_decision"}]
    advice = [row for row in events if row.get("event_type") in {"advice", "advice_history"}]
    requests = [row for row in events if row.get("event_type") == "provider_request"]
    responses = [row for row in events if row.get("event_type") == "provider_response"]
    review_requests = [row for row in events if row.get("event_type") == "review_started"]
    review_results = [row for row in events if row.get("event_type") == "review_completed"]
    feedback_kinds = {"outcome", "observed_action", "fact_memory", "deviation_record",
                      "unobserved_record", "execution_attempt"}
    feedback = [row for row in events if row.get("event_type") in feedback_kinds]
    results = [row for row in events
               if row.get("event_type") in {"result_record", "result", "run_ended"}]
    terminal_event = next((row for row in reversed(events)
                           if _lifecycle_event_matches(row, "run_ended", str(run_id),
                                                       selected_epoch)
                           and row.get("result") in _TERMINAL_RESULTS), None)
    terminal_result = terminal_event.get("result") if terminal_event else None
    started = any(_lifecycle_event_matches(row, "run_started", str(run_id),
                                            selected_epoch) for row in events)
    ended = terminal_event is not None
    synthetic = any(row.get("source_type") == "synthetic"
                    or row.get("fixture_type") == "synthetic" for row in events)
    lifecycle_complete = started and ended and terminal_result is not None
    if synthetic:
        source_type = "synthetic"
    elif lifecycle_complete:
        source_type = "real_game"
    else:
        source_type = "recorded_unverified"
    observed_characters, observed_scenes, observed_acts = _observed_scope(events)
    return {
        "schema_version": 1,
        "run_id": str(run_id),
        "run_epoch": selected_epoch,
        "source_type": source_type,
        "lifecycle_complete": lifecycle_complete,
        "character": observed_characters[-1] if observed_characters else "",
        "observed_characters": observed_characters,
        "observed_scenes": observed_scenes,
        "observed_acts": observed_acts,
        "terminal_result": terminal_result,
        "state_ids": sorted({str(row.get("state_id")) for row in events
                              if row.get("state_id")}),
        "decisions": decisions,
        "advice_versions": advice,
        "provider_requests": requests,
        "provider_responses": responses,
        "review_requests": review_requests,
        "review_results": review_results,
        "player_feedback": feedback,
        "results": results,
        "expired_results": [row for row in events
                            if row.get("event_type") == "expired_result"],
        "events": events,
        "evidence_event_ids": ids,
        "sample_counts": {
            "events": len(events), "states": len({row.get("state_id") for row in events
                                                   if row.get("state_id")}),
            "decisions": len(decisions), "advice_versions": len(advice),
            "provider_requests": len(requests), "provider_responses": len(responses),
            "review_requests": len(review_requests), "review_results": len(review_results),
            "player_feedback": len(feedback), "results": len(results),
        },
        "result_status": terminal_result if terminal_result is not None else "unknown",
    }


def review_decision(records: Iterable[dict], decision_id: str) -> dict:
    """Return a bounded, structured review for one decision."""
    chain = decision_chain(list(records), decision_id)
    decisions = chain.get("decisions", [])
    advice = chain.get("advice_versions", [])
    outcomes = chain.get("outcomes", [])
    return {
        "decision_id": decision_id,
        "inputs": chain.get("inputs", []),
        "requests": chain.get("requests", []),
        "responses": chain.get("responses", []),
        "final_selection": decisions[-1] if decisions else None,
        "advice_versions": advice,
        "observations": outcomes,
        "expired": chain.get("expired", []),
        "skipped": chain.get("skipped", []),
        "unattributed_legacy_events": chain.get("unattributed", []),
        "unattributed_legacy_event_count": chain.get("unattributed_total", 0),
        "unattributed_legacy_events_truncated": chain.get("unattributed_truncated", False),
        "review": {
            "plan_applicable": None,
            "rules_used": sorted({str(x) for event in decisions for x in (event.get("rule_ids") or [])}),
            "uncertainty": sorted({str(event.get("uncertain_reason") or event.get("uncertainty"))
                                        for event in decisions if event.get("uncertain_reason") or event.get("uncertainty")}),
            "next_check": "人工复核结构化事实后再调整规则",
        },
    }


def release_report(records: Iterable[dict], *, commit: str = "unknown",
                   config_id: str = "unknown", fixture_version: str = "unknown") -> dict:
    """Build release evidence; unknown values stay unknown."""
    rows = list(records)
    runs = {str(r.get("run_id")) for r in rows if r.get("run_id")}
    scenes = Counter(str(r.get("screen") or r.get("point") or "unknown")
                     for r in rows if r.get("event_type") in {"advice", "decision"})
    outcomes = [r for r in rows if r.get("event_type") in {"outcome", "result_record"}]
    real = [r for r in outcomes if r.get("result") in {"win", "loss", "death", "victory"}]
    decision_ids = {str(r.get("decision_id")) for r in rows if r.get("decision_id")}
    chains = [decision_chain(rows, decision_id) for decision_id in decision_ids]
    request_lifecycle = [chain.get("request_lifecycle", {}) for chain in chains]
    request_count = sum(int(item.get("request_count", 0) or 0)
                       for item in request_lifecycle)
    ended_count = sum(
        int(item.get("closed_request_count", 0) or 0)
        + len(item.get("expired_request_ids", []) or [])
        for item in request_lifecycle
    )
    citation_ids = sorted({str(value) for row in rows
                           for value in (row.get("knowledge_citation_ids") or [])
                           if value})
    return {
        "schema_version": 1,
        "source": {"fixture_type": "synthetic_or_recorded", "sample_count": len(rows),
                    "run_count": len(runs), "fixture_version": fixture_version},
        "environment": {"commit": commit, "python": sys.version.split()[0],
                       "platform": platform.platform()},
        "configuration": {"config_id": config_id},
        "scenes": dict(scenes),
        "validation": {
            "advise_wire_safety": None,
            "expired_submissions": sum(1 for r in rows if r.get("event_type") == "expired_result"),
            "cross_run_contamination": None,
            "unexecuted_memory_writes": None,
            "trace_request_completion_rate": (ended_count / request_count
                                                if request_count else None),
        },
        "strategy_retrieval": {
            "citation_ids": citation_ids,
            "citation_count": len(citation_ids),
            "quality": "unknown",
        },
        "real_game_results": "unknown" if not real else dict(Counter(str(r.get("result")) for r in real)),
        "limitations": ["人工标注、真实游戏结果和跨局污染需由回放或真机证据确认"],
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="logs/decision_trace.jsonl")
    parser.add_argument("--decision-id")
    parser.add_argument("--run-id")
    parser.add_argument("--run-epoch", type=int)
    parser.add_argument("--propose-experiences", action="store_true",
                        help="explicitly make one strategic-model request after a real run")
    parser.add_argument("--ledger", default=str(DEFAULT_LEDGER_PATH),
                        help="experience proposal ledger (use a temporary path for isolated review)")
    parser.add_argument("--commit", default="unknown")
    parser.add_argument("--config-id", default="unknown")
    parser.add_argument("--json", dest="output")
    args = parser.parse_args(argv)
    try:
        records = load_records(args.input)
    except FileNotFoundError:
        parser.error(f"trace input missing: {args.input}")
    if not records:
        parser.error(f"no trace records found: {args.input}")
    if args.run_id:
        try:
            result = review_run(records, args.run_id, run_epoch=args.run_epoch)
        except ValueError as exc:
            parser.error(str(exc))
        if args.propose_experiences:
            if result["source_type"] != "real_game" or not result["lifecycle_complete"]:
                parser.error("experience proposals require a run_ended real_game trace")
            review_epoch = result.get("run_epoch")
            if type(review_epoch) is not int:
                parser.error("experience proposals require a known run_epoch")
            from spirebrain.runtime_config import resolve_runtime_config
            runtime = resolve_runtime_config(ROOT)
            reviewer = OpenAIExperienceReviewer(
                endpoint=runtime.openai_endpoint, model=runtime.openai_model,
                api_key=runtime.brain_api_key, backend=runtime.brain_backend,
                timeout_ms=runtime.timeout_ms,
            )
            review_request_id = _review_request_id()
            review_started_at = time.monotonic()
            provider_attempted = bool(runtime.brain_api_key)
            review_trace_started = _record_review_event(
                args.input, "review_started", run_id=args.run_id,
                run_epoch=review_epoch, request_id=review_request_id,
                provider=runtime.brain_backend, model=runtime.openai_model,
                provider_attempted=provider_attempted,
                attempt_count=0,
                status="started",
            )
            try:
                # Only this pseudonymous, recursively redacted copy crosses the
                # strategic-model boundary. The full trace remains local evidence.
                proposed = reviewer.review(_prepare_model_review(result))
                for item in proposed:
                    _validate_proposal_scope(item, result)
                ledger = ExperienceLedger(args.ledger)
                pending = [ledger.add_pending(
                    item, result, provider=runtime.brain_backend,
                    model=runtime.openai_model,
                ) for item in proposed]
            except (RuntimeError, ValueError, OSError, TypeError) as exc:
                _record_review_event(
                    args.input, "review_completed", run_id=args.run_id,
                    run_epoch=review_epoch, request_id=review_request_id,
                    provider=runtime.brain_backend, model=runtime.openai_model,
                    provider_attempted=provider_attempted,
                    attempt_count=max(0, int(getattr(reviewer, "request_attempts", 0) or 0)),
                    latency_ms=round((time.monotonic() - review_started_at) * 1000, 3),
                    status="failed", proposal_count=None,
                    error_kind=_review_error_kind(exc),
                )
                parser.error(f"post-run review unavailable: {type(exc).__name__}")
            review_trace_completed = _record_review_event(
                args.input, "review_completed", run_id=args.run_id,
                run_epoch=review_epoch, request_id=review_request_id,
                provider=runtime.brain_backend, model=runtime.openai_model,
                provider_attempted=provider_attempted,
                attempt_count=max(0, int(getattr(reviewer, "request_attempts", 0) or 0)),
                latency_ms=round((time.monotonic() - review_started_at) * 1000, 3),
                status="completed", proposal_count=len(pending), error_kind=None,
            )
            result["experience_proposals"] = [
                {"proposal_id": item["proposal_id"], "status": item["status"],
                 "title": item["title"]} for item in pending]
            result["postmortem_model"] = runtime.brain_backend
            result["postmortem_request_count"] = 1
            result["postmortem_attempt_count"] = max(
                0, int(getattr(reviewer, "request_attempts", 0) or 0))
            result["postmortem_request_id"] = review_request_id
            result["postmortem_trace_written"] = bool(
                review_trace_started and review_trace_completed)
    elif args.propose_experiences:
        parser.error("--propose-experiences requires --run-id")
    elif args.decision_id:
        result = review_decision(records, args.decision_id)
    else:
        result = release_report(records, commit=args.commit, config_id=args.config_id)
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).write_text(text + "\n", encoding="utf-8")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
