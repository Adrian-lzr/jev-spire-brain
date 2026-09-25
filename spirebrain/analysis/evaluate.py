"""Offline comparison of rules, JEV, strategic and full pipelines.

This runner is intentionally conservative: synthetic fixtures can validate
protocol and legality, but cannot establish win rate or real decision quality.
"""
from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

from .replay import run_fixture

MODES = ("rules", "jev", "strategic", "full")


def evaluate_fixture(path: str | Path, modes=MODES) -> dict:
    path = Path(path)
    base = run_fixture(path)
    result = {}
    for mode in modes:
        # The current replay harness is local and deterministic.  Mode labels
        # are retained in the report so future scripted providers can be
        # substituted without changing the result schema.
        result[mode] = {
            "mode": mode,
            "fixture": str(path),
            "fixture_type": base.get("fixture_type", "synthetic"),
            "sample_count": base.get("messages", 0),
            "decision_count": base.get("recommendations", 0),
            "legal_action_rate": 1.0 if base.get("passed") else 0.0,
            "fallback_rate": (base.get("fallbacks", 0) / base.get("recommendations", 1)
                               if base.get("recommendations") else None),
            "provider_calls": 0,
            "providers_enabled": {
                "jev": mode in {"jev", "full"},
                "strategic": mode in {"strategic", "full"},
            },
            "first_advice_latency": None,
            "complete_advice_latency": None,
            "expired_result_count": 0,
            "unobserved_ratio": None,
            "adoption_rate": None,
            "real_outcomes": "unknown",
            "decision_quality": "unknown",
            "evaluation_basis": "synthetic_protocol_replay",
            "passed": bool(base.get("passed")),
        }
    return {"fixture": str(path), "fixture_type": base.get("fixture_type", "synthetic"),
            "modes": result,
            "limitations": ["synthetic replay validates legality and timing only; adoption and win rate are unknown"]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--mode", choices=("all", *MODES), default="all")
    parser.add_argument("--output")
    args = parser.parse_args(argv)
    target = Path(args.input)
    files = sorted(target.glob("*.json")) if target.is_dir() else [target]
    if not files or any(not f.exists() for f in files):
        print(json.dumps({"error": "input_missing", "input": str(target)}, ensure_ascii=False))
        return 2
    modes = MODES if args.mode == "all" else (args.mode,)
    reports = [evaluate_fixture(f, modes) for f in files]
    output = {"fixture_count": len(reports), "modes": modes, "reports": reports,
              "real_outcomes": "unknown"}
    rendered = json.dumps(output, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if all(all(m["passed"] for m in r["modes"].values()) for r in reports) else 1


if __name__ == "__main__":
    raise SystemExit(main())
