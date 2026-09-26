"""Offline comparison of rules, JEV, strategic and full pipelines.

This runner is intentionally conservative: synthetic fixtures can validate
protocol and legality, but cannot establish win rate or real decision quality.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from .replay import run_fixture, _fixture_kind

MODES = ("rules", "jev", "strategic", "full")


def evaluate_fixture(path: str | Path, modes=MODES) -> dict:
    path = Path(path)
    result = {}
    if not modes:
        raise ValueError("at least one evaluation mode is required")
    for mode in modes:
        base = run_fixture(path, mode=mode)
        result[mode] = {
            "mode": mode,
            "fixture": str(path),
            "fixture_type": base.get("fixture_type", "synthetic"),
            "sample_count": base.get("messages", 0),
            "decision_count": base.get("recommendations", 0),
            "legal_action_rate": base["candidate_legality"]["rate"],
            "candidate_legality": base["candidate_legality"],
            "advise_wire_safety": base["advise_wire_safety"],
            "game_rejections": None,
            "fallback_rate": (base.get("fallbacks", 0) / base.get("recommendations", 1)
                               if base.get("recommendations") else None),
            "provider_calls": base.get("provider_calls", 0),
            "jev_calls": base.get("jev_calls", 0),
            "strategic_calls": base.get("strategic_calls", 0),
            "providers_enabled": {
                "jev": mode in {"jev", "full"},
                "strategic": mode in {"strategic", "full"},
            },
            "first_advice_latency": None,
            "complete_advice_latency": None,
            "expired_result_count": None,
            "unobserved_ratio": None,
            "adoption_rate": None,
            "real_outcomes": "unknown",
            "decision_quality": "unknown",
            "evaluation_basis": "synthetic_protocol_replay",
            "passed": bool(base.get("passed")),
        }
    return {"fixture": str(path), "fixture_type": base.get("fixture_type", "synthetic"),
            "modes": result,
            "limitations": ["synthetic state replay validates wire safety and candidate legality; scheduler timing, adoption and win rate are not measured"]}


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
    state_files = [path for path in files if _fixture_kind(path) == "state_sequence"]
    if not state_files:
        print(json.dumps({"error": "state_sequence_fixture_missing"}, ensure_ascii=False))
        return 2
    try:
        reports = [evaluate_fixture(f, modes) for f in state_files]
    except (ValueError, OSError) as exc:
        print(json.dumps({"error": "invalid_fixture", "kind": type(exc).__name__}))
        return 2
    output = {"fixture_count": len(reports), "modes": modes, "reports": reports,
              "real_outcomes": "unknown"}
    rendered = json.dumps(output, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if all(all(m["passed"] for m in r["modes"].values()) for r in reports) else 1


if __name__ == "__main__":
    raise SystemExit(main())
