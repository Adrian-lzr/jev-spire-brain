"""Structured run review and release evidence helpers.

This module is intentionally offline and read-only: it consumes decision trace
events and produces reviewable facts without turning observations into rules.
"""
from __future__ import annotations

import argparse
import json
import platform
import sys
from collections import Counter
from pathlib import Path
from typing import Iterable

from .metrics import load_records
from .event_contract import decision_chain


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
    outcomes = [r for r in rows if r.get("event_type") == "outcome"]
    real = [r for r in outcomes if r.get("result") in {"win", "loss", "death", "victory"}]
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
        },
        "real_game_results": "unknown" if not real else dict(Counter(str(r.get("result")) for r in real)),
        "limitations": ["人工标注、真实游戏结果和跨局污染需由回放或真机证据确认"],
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="logs/decision_trace.jsonl")
    parser.add_argument("--decision-id")
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
    result = (review_decision(records, args.decision_id) if args.decision_id
              else release_report(records, commit=args.commit, config_id=args.config_id))
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).write_text(text + "\n", encoding="utf-8")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
