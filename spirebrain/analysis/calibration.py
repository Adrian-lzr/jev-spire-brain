"""Calibration analysis — Phase 5's independent-evaluation payload.

Reads logs/jev_calls.jsonl produced by LoggingJevClient and answers the
question that matters for a probabilistic model: when the model says it is
X% confident, is it right about X% of the time?

Outputs:
  - bucket table: confidence band -> n, decisions, observed agreement proxy
  - calibration error summary (ECE-style, when ground-truth labels exist)
  - fallback ratio (how often the confidence floor fired)

In offline mode (mock backend) this mostly measures the *harness*; with the
real JEV backend it becomes the independent calibration evidence the JEV
community currently lacks.
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path


def load_calls(log_dir: str | Path = "logs") -> list[dict]:
    path = Path(log_dir) / "jev_calls.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in
            path.read_text(encoding="utf-8").splitlines() if line.strip()]


def confidence_buckets(calls: list[dict],
                       edges: tuple[float, ...] = (0.5, 0.6, 0.7, 0.8, 0.9, 1.0)) -> dict:
    """Count answers per confidence band. Ground truth joins later (Phase 5)."""
    buckets: dict[str, dict] = defaultdict(lambda: {"n": 0})
    for call in calls:
        for ans in call.get("answers", {}).values():
            conf = float(ans.get("confidence", 0.0))
            for lo, hi in zip(edges[:-1], edges[1:]):
                if lo <= conf < hi or (hi == 1.0 and conf == 1.0):
                    buckets[f"[{lo:.1f},{hi:.1f})"]["n"] += 1
                    break
    return dict(buckets)


def fallback_ratio(calls: list[dict], floor: float = 0.6) -> dict:
    below = sum(1 for c in calls for a in c.get("answers", {}).values()
                if float(a.get("confidence", 0.0)) < floor)
    total = sum(len(c.get("answers", {})) for c in calls)
    return {"answers": total, "below_floor": below,
            "fallback_ratio": round(below / total, 3) if total else 0.0}


def ece(bucket_table: dict[str, dict], truths: dict[str, float]) -> float:
    """Expected Calibration Error once per-decision ground truth exists.
    truths: {bucket_key: observed_correct_rate}. Stub until labels land."""
    n_total = sum(b["n"] for b in bucket_table.values())
    if not n_total:
        return 0.0
    err = 0.0
    for key, b in bucket_table.items():
        if key not in truths:
            continue
        conf_mid = (float(key.split(",")[0][1:]) + float(key.split(",")[1][:-1])) / 2
        err += (b["n"] / n_total) * abs(conf_mid - truths[key])
    return round(err, 4)


def report(log_dir: str | Path = "logs") -> str:
    calls = load_calls(log_dir)
    if not calls:
        return f"no calls logged in {log_dir}"
    lines = [f"jev calls: {len(calls)}",
             json.dumps(confidence_buckets(calls), indent=2),
             json.dumps(fallback_ratio(calls), indent=2)]
    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    log_dir = sys.argv[1] if len(sys.argv) > 1 else "logs"
    print(report(log_dir))
