"""Offline decision-trace metrics; never calls a provider or mutates game config."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

from spirebrain.redaction import redact_text
from spirebrain.analysis.event_contract import decision_chain


def _percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    values = sorted(values)
    if len(values) == 1:
        return values[0]
    position = (len(values) - 1) * p
    low, high = int(position), min(int(position) + 1, len(values) - 1)
    return values[low] + (values[high] - values[low]) * (position - low)


def _provider_attempt_key(record: dict, index: int) -> tuple:
    """Identify one provider attempt without merging anonymous legacy rows."""
    request_id = str(record.get("request_id") or "")
    if not request_id:
        return ("anonymous", index)
    return (
        str(record.get("run_id") or ""),
        str(record.get("decision_id") or ""),
        request_id,
        str(record.get("attempt_id") or ""),
    )


def _review_request_key(record: dict, index: int) -> tuple:
    """Identify one post-run review without merging run epochs."""
    request_id = str(record.get("request_id") or "")
    if not request_id:
        return ("anonymous", index)
    return (
        str(record.get("run_id") or ""),
        str(record.get("run_epoch") or ""),
        request_id,
    )


def _review_metrics(records: list[dict]) -> dict:
    """Summarize bounded post-run review evidence.

    Review start/completion rows are one logical request.  Grouping by the
    correlation identity prevents duplicate trace rows from inflating request,
    attempt or latency counts.  Missing completion and latency evidence stays
    visible as ``incomplete``/``None`` instead of being inferred as success.
    """
    review_events = [record for record in records
                     if record.get("event_type") in
                     {"review_started", "review_completed"}]
    groups: dict[tuple, list[dict]] = {}
    for index, record in enumerate(review_events):
        groups.setdefault(_review_request_key(record, index), []).append(record)

    identified = {key for key in groups if key[0] != "anonymous" and key[-1]}
    anonymous_count = sum(1 for key in groups if key[0] == "anonymous")
    completed_count = 0
    failed_count = 0
    incomplete_count = 0
    attempt_values: list[int] = []
    latencies: list[float] = []
    failure_reasons: Counter[str] = Counter()

    for key, rows in groups.items():
        completions = [row for row in rows
                       if row.get("event_type") == "review_completed"]
        canonical = completions[-1] if completions else rows[-1]
        if not completions:
            incomplete_count += 1

        valid_attempts = [int(row.get("attempt_count")) for row in rows
                          if isinstance(row.get("attempt_count"), int)
                          and not isinstance(row.get("attempt_count"), bool)
                          and row.get("attempt_count") >= 0]
        if valid_attempts:
            attempt_values.append(max(valid_attempts))

        latency = canonical.get("latency_ms")
        if (canonical.get("event_type") == "review_completed"
                and isinstance(latency, (int, float))
                and not isinstance(latency, bool) and latency >= 0):
            latencies.append(float(latency))

        status = str(canonical.get("status") or "").strip().lower()
        if status == "completed":
            completed_count += 1
        elif status == "failed":
            failed_count += 1
            failure_reasons[redact_text(canonical.get("error_kind") or "unknown")] += 1

    observed_count = len(groups)
    return {
        "request_count": len(identified),
        "unique_request_count": len(identified),
        "completed_count": completed_count,
        "failed_count": failed_count,
        "incomplete_count": incomplete_count,
        "unattributed_event_count": anonymous_count,
        "attempt_count": sum(attempt_values) if attempt_values else None,
        "attempt_samples": len(attempt_values),
        "failure_reasons": dict(failure_reasons),
        "latency_ms": {
            "p50": _percentile(latencies, 0.50),
            "p95": _percentile(latencies, 0.95),
            "samples": len(latencies),
        },
        "sample_count": observed_count,
        "status": "unknown" if not review_events else (
            "complete" if not incomplete_count else "incomplete"),
    }


def _stage_latencies(records: list[dict]) -> dict[str, list[float]]:
    owners = {
        "queue_wait": ("provider_request", "queue_wait_ms"),
        "pre_provider": ("provider_request", "pre_provider_ms"),
        "http": ("provider_response", "http_latency_ms"),
        "parse": ("provider_response", "parse_latency_ms"),
        "worker": ("provider_response", "worker_elapsed_ms"),
        "commit": ("strategic_plan_commit", "commit_latency_ms"),
    }
    collected: dict[str, list[float]] = {}
    for metric, (event_type, field) in owners.items():
        unique: dict[tuple, float] = {}
        for index, record in enumerate(records):
            if record.get("event_type") != event_type:
                continue
            value = record.get(field)
            if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
                continue
            if event_type.startswith("provider_"):
                key = _provider_attempt_key(record, index)
            else:
                key = (str(record.get("run_id") or ""),
                       str(record.get("decision_id") or ""),
                       str(record.get("request_id") or ""),
                       str(record.get("result_disposition") or ""))
            unique[key] = float(value)
        collected[metric] = list(unique.values())
    return collected


def load_records(path: str | Path) -> list[dict]:
    records = []
    target = Path(path)
    if not target.exists():
        raise FileNotFoundError(target)
    for line in target.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            records.append(value)
    return records


def _recommendation_events(records: list[dict]) -> list[dict]:
    """Return the last advice/decision event for each decision in log order.

    Advice can be provisional and a later decision can replace it, but logs can
    also contain a replacement advice event after a decision.  Selecting the
    last observed event instead of concatenating event-type groups preserves the
    actual publication order and keeps one sample per decision point.
    """
    latest: dict[str, tuple[int, dict]] = {}
    for index, record in enumerate(records):
        if record.get("event_type") not in {"advice", "decision"}:
            continue
        key = str(record.get("decision_id") or f"event:{index}")
        latest[key] = (index, record)
    return [record for _, record in sorted(latest.values(), key=lambda item: item[0])]


def _result_label(record: dict) -> str | None:
    """Read a scalar game result without hashing arbitrary structured data."""
    value = record.get("result")
    if isinstance(value, str):
        label = value.strip().lower()
        if label in {"win", "loss", "death", "victory"}:
            return label
    # Some producers wrap the scalar under status/outcome; structured result
    # evidence itself is not a win/loss label and must remain unobserved.
    if isinstance(value, dict):
        for key in ("status", "outcome", "result"):
            nested = value.get(key)
            if isinstance(nested, str) and nested.strip().lower() in {
                "win", "loss", "death", "victory"
            }:
                return nested.strip().lower()
    return None


def summarize(records: list[dict], *, source: str = "") -> dict:
    advice_events = [r for r in records if r.get("event_type") == "advice"]
    decision_events = [r for r in records if r.get("event_type") == "decision"]
    recommendations = _recommendation_events(records)
    scenes = Counter(str(r.get("screen") or r.get("point") or "unknown")
                     for r in recommendations)
    legal = [r for r in recommendations if r.get("legal") is not None]
    legal_ok = [r for r in legal if bool(r.get("legal"))]
    request_latency_by_attempt: dict[tuple, float] = {}
    decision_latency = []
    first_latency = []
    publish_latency = []
    legacy_latency = []
    provider_events = {"provider", "brain_call", "provider_request", "provider_response"}
    provider_attempts = {
        _provider_attempt_key(record, index)
        for index, record in enumerate(records)
        if record.get("event_type") in provider_events
    }
    provider_event_counts = Counter(
        (str(record.get("event_type") or ""), _provider_attempt_key(record, index))
        for index, record in enumerate(records)
        if record.get("event_type") in provider_events
    )
    for index, r in enumerate(records):
        if r.get("event_type") in provider_events:
            value = r.get("request_latency_ms", r.get("latency_ms"))
            if isinstance(value, (int, float)) and value >= 0:
                key = _provider_attempt_key(r, index)
                # The response event is canonical for current traces.  Keep a
                # legacy request-side latency only until its response arrives,
                # then replace it instead of counting the attempt twice.
                if (key not in request_latency_by_attempt
                        or r.get("event_type") == "provider_response"):
                    request_latency_by_attempt[key] = float(value)
        value = r.get("decision_latency_ms")
        if isinstance(value, (int, float)) and value >= 0:
            decision_latency.append(float(value))
        value = r.get("first_advice_latency_ms")
        if isinstance(value, (int, float)) and value >= 0:
            first_latency.append(float(value))
        value = r.get("publish_latency_ms")
        if isinstance(value, (int, float)) and value >= 0:
            publish_latency.append(float(value))
        # Older traces overloaded latency_ms with provider time. Keep them
        # visible, but never mix that ambiguous value into a named metric.
        if (r.get("event_type") in {"advice", "decision"}
                and not any(r.get(field) is not None for field in
                            ("request_latency_ms", "decision_latency_ms"))):
            value = r.get("latency_ms", r.get("brain_latency_ms"))
            if isinstance(value, (int, float)) and value >= 0:
                legacy_latency.append(float(value))
    outcomes = [r for r in records if r.get("event_type") == "outcome"]
    # Result records are separate from advice verdicts. Keep accepting the
    # legacy outcome shape for replay compatibility, but never treat a reason
    # or a match/mismatch verdict as a game result.
    result_events = [r for r in records if r.get("event_type") in {"result", "result_record"}]
    real_results = [r for r in result_events if _result_label(r) is not None]
    if not result_events:
        real_results = [r for r in outcomes if _result_label(r) is not None]
    # Count one final recommendation per decision ID.  A provisional advice
    # and a final decision may both exist for the same point and must not
    # inflate fallback or sample counts.
    fallback_events = recommendations
    fallback = Counter(
        redact_text(r.get("fallback_reason") or r.get("reason") or "unknown")
        for r in fallback_events if r.get("fallback") is True
    )
    expired_events = [r for r in records if r.get("event_type") == "expired_result"]
    skipped_events = [r for r in records if r.get("event_type") == "decision_skipped"]
    unattributed_events = [r for r in records
                           if (
                               r.get("event_type") in {
                                   "state_observed", "provider_request", "provider_response",
                                   "decision", "advice", "outcome", "expired_result",
                                   "decision_skipped",
                               } and not r.get("decision_id")
                           ) or (
                               r.get("event_type") in {"review_started", "review_completed"}
                               and not r.get("request_id")
                           )]
    chains = {str(r.get("decision_id")): decision_chain(records, str(r.get("decision_id")))
              for r in records if r.get("decision_id")}
    expired_decisions = sum(1 for chain in chains.values() if chain["expired"])
    stage_latencies = _stage_latencies(records)
    postmortem = _review_metrics(records)
    dispositions = Counter(
        redact_text(str(record.get("result_disposition")))
        for record in records
        if record.get("event_type") == "strategic_plan_commit"
        and record.get("result_disposition")
    )
    return {
        "sample_size": len(records),
        "advice_count": len(advice_events),
        "result_count": len(result_events),
        "decision_count": len({r.get("decision_id") for r in records
                                if r.get("event_type") in {"advice", "decision"}
                                and r.get("decision_id")}),
        "run_count": len({r.get("run_id") for r in records if r.get("run_id")}),
        "state_count": len({r.get("state_id") for r in records if r.get("state_id")}),
        "source": source,
        "config_ids": sorted({str(r.get("config_id")) for r in records if r.get("config_id")}),
        "scene_coverage": dict(scenes),
        "legal_action_rate": (len(legal_ok) / len(legal) if legal else None),
        "latency_ms": {
            "request_p50": _percentile(list(request_latency_by_attempt.values()), 0.50),
            "request_p95": _percentile(list(request_latency_by_attempt.values()), 0.95),
            "request_samples": len(request_latency_by_attempt),
            "decision_p50": _percentile(decision_latency, 0.50),
            "decision_p95": _percentile(decision_latency, 0.95),
            "decision_samples": len(decision_latency),
            "first_advice_p50": _percentile(first_latency, 0.50),
            "first_advice_p95": _percentile(first_latency, 0.95),
            "first_advice_samples": len(first_latency),
            "publish_p50": _percentile(publish_latency, 0.50),
            "publish_p95": _percentile(publish_latency, 0.95),
            "publish_samples": len(publish_latency),
            "queue_wait_p50": _percentile(stage_latencies["queue_wait"], 0.50),
            "queue_wait_p95": _percentile(stage_latencies["queue_wait"], 0.95),
            "queue_wait_samples": len(stage_latencies["queue_wait"]),
            "pre_provider_p50": _percentile(stage_latencies["pre_provider"], 0.50),
            "pre_provider_p95": _percentile(stage_latencies["pre_provider"], 0.95),
            "pre_provider_samples": len(stage_latencies["pre_provider"]),
            "http_p50": _percentile(stage_latencies["http"], 0.50),
            "http_p95": _percentile(stage_latencies["http"], 0.95),
            "http_samples": len(stage_latencies["http"]),
            "parse_p50": _percentile(stage_latencies["parse"], 0.50),
            "parse_p95": _percentile(stage_latencies["parse"], 0.95),
            "parse_samples": len(stage_latencies["parse"]),
            "worker_p50": _percentile(stage_latencies["worker"], 0.50),
            "worker_p95": _percentile(stage_latencies["worker"], 0.95),
            "worker_samples": len(stage_latencies["worker"]),
            "commit_p50": _percentile(stage_latencies["commit"], 0.50),
            "commit_p95": _percentile(stage_latencies["commit"], 0.95),
            "commit_samples": len(stage_latencies["commit"]),
            "legacy_unclassified_p50": _percentile(legacy_latency, 0.50),
            "legacy_unclassified_p95": _percentile(legacy_latency, 0.95),
            "legacy_unclassified_samples": len(legacy_latency),
            "player_display_latency": "not_collected",
        },
        # ``provider_request`` is the canonical decision-trace event.  The
        # older provider/brain_call names remain accepted for replayed logs.
        "provider_calls": len(provider_attempts),
        "strategic_result_dispositions": dict(dispositions),
        "postmortem": postmortem,
        "provider_duplicate_event_count": max(
            0, sum(max(0, count - 1) for count in provider_event_counts.values())),
        "fallback_rate": (sum(1 for r in fallback_events if r.get("fallback")) /
                           len(fallback_events) if fallback_events else None),
        "fallback_reasons": dict(fallback),
        "expired_result_count": len(expired_events) + sum(int(r.get("expired_result_count", 0) or 0)
                                     for r in records),
        "skipped_before_start_count": len(skipped_events),
        "unattributed_event_count": len(unattributed_events),
        "correctness": {
            "expired_decision_count": expired_decisions,
            "cross_run_contamination": "unknown",
            "unexecuted_action_memory": "unknown",
        },
        "experience": {
            "first_advice_samples": len(first_latency),
            "complete_advice_samples": len(decision_latency),
        },
        "efficiency": {
            "provider_calls": len(provider_attempts),
            "fallback_rate": (sum(1 for r in fallback_events if r.get("fallback")) /
                               len(fallback_events) if fallback_events else None),
        },
        "decision_quality": {
            "adoption_rate": None,
            "real_game_result": "unknown",
        },
        "unobserved_ratio": (sum(1 for r in outcomes if r.get("verdict") == "unobserved") /
                             len(outcomes) if outcomes else None),
        "real_outcomes": ("unknown" if not real_results else dict(Counter(
            _result_label(r) for r in real_results))),
        "limitations": ["回放、采纳率、模拟伤害和合成 HP 不是胜率证据。"],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="logs/decision_trace.jsonl")
    parser.add_argument("--merge", nargs="+", help="merge multiple JSONL traces")
    parser.add_argument("--json", dest="output")
    args = parser.parse_args(argv)
    if args.merge:
        records = []
        try:
            for item in args.merge:
                records.extend(load_records(item))
        except FileNotFoundError as exc:
            print(json.dumps({"error": "input_missing", "input": str(exc)},
                             ensure_ascii=False))
            return 2
        source = ",".join(args.merge)
    else:
        input_path = Path(args.input)
        if not input_path.exists():
            print(json.dumps({"error": "input_missing", "input": str(input_path)},
                             ensure_ascii=False))
            return 2
        records = load_records(input_path)
        source = str(input_path)
    if not records:
        print(json.dumps({"error": "no_records", "source": source}, ensure_ascii=False))
        return 2
    result = summarize(records, source=source)
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
