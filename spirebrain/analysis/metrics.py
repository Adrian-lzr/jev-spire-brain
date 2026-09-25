"""Offline decision-trace metrics; never calls a provider or mutates game config."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

from spirebrain.redaction import redact_text


def _percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    values = sorted(values)
    if len(values) == 1:
        return values[0]
    position = (len(values) - 1) * p
    low, high = int(position), min(int(position) + 1, len(values) - 1)
    return values[low] + (values[high] - values[low]) * (position - low)


def load_records(path: str | Path) -> list[dict]:
    records = []
    target = Path(path)
    if not target.exists():
        return records
    for line in target.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            records.append(value)
    return records


def summarize(records: list[dict], *, source: str = "") -> dict:
    advice_events = [r for r in records if r.get("event_type") == "advice"]
    decision_events = [r for r in records if r.get("event_type") == "decision"]
    recommendations = advice_events or decision_events
    scenes = Counter(str(r.get("screen") or r.get("point") or "unknown")
                     for r in recommendations)
    legal = [r for r in records if r.get("event_type") in {"advice", "decision"}
             and r.get("legal") is not None]
    legal_ok = [r for r in legal if bool(r.get("legal"))]
    request_latency = []
    decision_latency = []
    first_latency = []
    publish_latency = []
    legacy_latency = []
    provider_events = {"provider", "brain_call", "provider_request"}
    for r in records:
        if r.get("event_type") in provider_events:
            value = r.get("request_latency_ms", r.get("latency_ms"))
            if isinstance(value, (int, float)) and value >= 0:
                request_latency.append(float(value))
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
    fallback = Counter(redact_text(r.get("fallback_reason") or r.get("reason") or "unknown")
                       for r in records if r.get("fallback") is True)
    outcomes = [r for r in records if r.get("event_type") == "outcome"]
    real_results = [r for r in outcomes if r.get("result") in {"win", "loss", "death"}]
    provider_events = {"provider", "brain_call", "provider_request"}
    fallback_events = advice_events or decision_events
    return {
        "sample_size": len(records),
        "advice_count": len(advice_events),
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
            "request_p50": _percentile(request_latency, 0.50),
            "request_p95": _percentile(request_latency, 0.95),
            "request_samples": len(request_latency),
            "decision_p50": _percentile(decision_latency, 0.50),
            "decision_p95": _percentile(decision_latency, 0.95),
            "decision_samples": len(decision_latency),
            "first_advice_p50": _percentile(first_latency, 0.50),
            "first_advice_p95": _percentile(first_latency, 0.95),
            "first_advice_samples": len(first_latency),
            "publish_p50": _percentile(publish_latency, 0.50),
            "publish_p95": _percentile(publish_latency, 0.95),
            "publish_samples": len(publish_latency),
            "legacy_unclassified_p50": _percentile(legacy_latency, 0.50),
            "legacy_unclassified_p95": _percentile(legacy_latency, 0.95),
            "legacy_unclassified_samples": len(legacy_latency),
            "player_display_latency": "not_collected",
        },
        # ``provider_request`` is the canonical decision-trace event.  The
        # older provider/brain_call names remain accepted for replayed logs.
        "provider_calls": sum(1 for r in records if r.get("event_type") in provider_events),
        "fallback_rate": (sum(1 for r in fallback_events if r.get("fallback")) /
                           len(fallback_events) if fallback_events else None),
        "fallback_reasons": dict(fallback),
        "expired_result_count": sum(int(r.get("expired_result_count", 0) or 0)
                                     for r in records),
        "unobserved_ratio": (sum(1 for r in outcomes if r.get("verdict") == "unobserved") /
                             len(outcomes) if outcomes else None),
        "real_outcomes": ("unknown" if not real_results else dict(Counter(
            str(r.get("result")) for r in real_results))),
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
        for item in args.merge:
            records.extend(load_records(item))
        source = ",".join(args.merge)
    else:
        input_path = Path(args.input)
        if not input_path.exists():
            print(json.dumps({"error": "input_missing", "input": str(input_path)},
                             ensure_ascii=False))
            return 2
        records = load_records(input_path)
        source = str(input_path)
    result = summarize(records, source=source)
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
