"""Offline decision-trace metrics; never calls a provider or mutates game config."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path


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
    advice = [r for r in records if r.get("event_type") in {"advice", "decision"}]
    scenes = Counter(str(r.get("screen") or r.get("point") or "unknown") for r in advice)
    legal = [r for r in advice if r.get("legal") is not None]
    legal_ok = [r for r in legal if bool(r.get("legal"))]
    latency = []
    first_latency = []
    final_latency = []
    publish_latency = []
    for r in records:
        value = r.get("latency_ms", r.get("brain_latency_ms"))
        if isinstance(value, (int, float)) and value >= 0:
            latency.append(float(value))
        for field, target in (("first_advice_latency_ms", first_latency),
                              ("decision_latency_ms", final_latency),
                              ("publish_latency_ms", publish_latency)):
            value = r.get(field)
            if isinstance(value, (int, float)) and value >= 0:
                target.append(float(value))
    fallback = Counter(str(r.get("fallback_reason") or r.get("reason") or "unknown")
                       for r in records if r.get("fallback") is True)
    outcomes = [r for r in records if r.get("event_type") == "outcome"]
    real_results = [r for r in outcomes if r.get("result") in {"win", "loss", "death"}]
    provider_events = {"provider", "brain_call", "provider_request"}
    return {
        "sample_size": len(records),
        "advice_count": len(advice),
        "decision_count": len({r.get("decision_id") for r in advice if r.get("decision_id")}),
        "run_count": len({r.get("run_id") for r in records if r.get("run_id")}),
        "state_count": len({r.get("state_id") for r in records if r.get("state_id")}),
        "source": source,
        "config_ids": sorted({str(r.get("config_id")) for r in records if r.get("config_id")}),
        "scene_coverage": dict(scenes),
        "legal_action_rate": (len(legal_ok) / len(legal) if legal else None),
        "latency_ms": {"p50": _percentile(latency, 0.50), "p95": _percentile(latency, 0.95),
                       "samples": len(latency),
                       "first_advice_p50": _percentile(first_latency, 0.50),
                       "first_advice_p95": _percentile(first_latency, 0.95),
                       "final_advice_p50": _percentile(final_latency, 0.50),
                       "final_advice_p95": _percentile(final_latency, 0.95),
                       "publish_p50": _percentile(publish_latency, 0.50),
                       "publish_p95": _percentile(publish_latency, 0.95)},
        # ``provider_request`` is the canonical decision-trace event.  The
        # older provider/brain_call names remain accepted for replayed logs.
        "provider_calls": sum(1 for r in records if r.get("event_type") in provider_events),
        "fallback_rate": (sum(1 for r in advice if r.get("fallback")) / len(advice)
                           if advice else None),
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
