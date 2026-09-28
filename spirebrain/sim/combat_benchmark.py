"""Deterministic offline benchmark for the bounded Ironclad combat engine.

This is a tactical regression and parameter-selection tool, not a Slay the
Spire win-rate simulator. Fixtures carry explicit synthetic objectives; the
report measures whether proposals satisfy those local objectives, stay legal,
and degrade honestly for unknown effects. No provider, network, credential,
save file, or installed game configuration is accessed.
"""

from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
from typing import Any, Mapping

from spirebrain.brain.action_broker import build_action_candidates
from spirebrain.driver.legality import check_action
from spirebrain.sim.ironclad import IroncladCombatSimulator, sanitize_snapshot
from spirebrain.tactical.combat_engine import CombatTacticalEngine


SCHEMA_VERSION = 1
DEFAULT_CONFIGS: dict[str, dict[str, int | float]] = {
    "depth1": {"depth": 1, "node_limit": 32, "time_limit_ms": 20.0},
    "depth2": {"depth": 2, "node_limit": 64, "time_limit_ms": 20.0},
    "depth3": {"depth": 3, "node_limit": 96, "time_limit_ms": 30.0},
}
OBJECTIVES = frozenset({"lethal", "cover", "target", "uncertain"})


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    return value


def load_benchmark(path: str | Path) -> dict[str, Any]:
    """Load and validate a bounded combat benchmark fixture."""
    target = Path(path)
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid combat benchmark: {target}") from exc
    if not isinstance(payload, dict):
        raise ValueError("combat benchmark must be an object")
    if payload.get("fixture_type") not in {"synthetic", "real_replay"}:
        raise ValueError("combat benchmark must declare fixture_type")
    if payload.get("kind") != "combat_benchmark":
        raise ValueError("combat benchmark kind must be combat_benchmark")
    cases = payload.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError("combat benchmark cases must be a non-empty array")
    normalized: list[dict[str, Any]] = []
    for index, raw in enumerate(cases):
        case = _mapping(raw, f"cases[{index}]")
        case_id = str(case.get("id") or "").strip()
        objective = str(case.get("objective") or "").strip().lower()
        state = case.get("state")
        if not case_id or objective not in OBJECTIVES:
            raise ValueError(f"cases[{index}] requires id and supported objective")
        _mapping(state, f"cases[{index}].state")
        expected = case.get("expected", {})
        if expected is not None and not isinstance(expected, Mapping):
            raise ValueError(f"cases[{index}].expected must be an object")
        normalized.append({
            "id": case_id[:120],
            "objective": objective,
            "state": copy.deepcopy(dict(state)),
            "expected": copy.deepcopy(dict(expected or {})),
        })
    return {
        "schema_version": int(payload.get("schema_version", SCHEMA_VERSION)),
        "fixture_type": payload["fixture_type"],
        "kind": payload["kind"],
        "description": str(payload.get("description") or "")[:500],
        "cases": normalized,
    }


def _target_id(state: Mapping[str, Any], index: int | None) -> str:
    if index is None:
        return ""
    combat = state.get("combat") or state.get("combat_state") or {}
    monsters = combat.get("monsters") if isinstance(combat, Mapping) else []
    if not isinstance(monsters, list) or not 0 <= int(index) < len(monsters):
        return ""
    monster = monsters[int(index)]
    if not isinstance(monster, Mapping):
        return ""
    return str(monster.get("instance_id") or monster.get("uuid") or
               monster.get("id") or monster.get("name") or "")


def _case_result(case: Mapping[str, Any], config: Mapping[str, Any]) -> dict[str, Any]:
    state = sanitize_snapshot(case["state"])
    candidates = build_action_candidates(state)
    engine = CombatTacticalEngine(
        depth=int(config.get("depth", 2)),
        node_limit=int(config.get("node_limit", 64)),
        time_limit_ms=float(config.get("time_limit_ms", 20.0)),
    )
    proposal = engine.propose(state, candidates)
    command = dict(proposal.candidate.command) if proposal.candidate else {"command": "end"}
    legal, legality_reason = check_action(state, command)
    simulation = (IroncladCombatSimulator(max_actions=1).run(state, [command])
                  if legal else None)
    facts = dict(proposal.facts or {})
    objective = str(case["objective"])
    expected = case.get("expected") or {}
    target_index = command.get("target") if command.get("command") == "play" else None
    target_id = _target_id(state, target_index if isinstance(target_index, int) else None)
    if objective == "lethal":
        objective_met = bool(facts.get("lethal_confirmed"))
    elif objective == "cover":
        objective_met = bool(facts.get("incoming_threat_covered"))
    elif objective == "target":
        objective_met = target_id == str(expected.get("target_id") or "")
    else:
        objective_met = bool(proposal.uncertainty or facts.get("uncertainty") not in (None, "", "none"))
    return {
        "id": str(case["id"]),
        "objective": objective,
        "objective_met": objective_met,
        "legal": bool(legal),
        "legality_reason": legality_reason,
        "command": command,
        "candidate_id": proposal.candidate.candidate_id if proposal.candidate else None,
        "candidate_signature": (proposal.candidate.candidate_signature
                                 if proposal.candidate else None),
        "reason_code": proposal.reason_code,
        "uncertainty": proposal.uncertainty or facts.get("uncertainty", "none"),
        "confidence": proposal.confidence,
        "facts": facts,
        "target_id": target_id,
        "simulation": simulation.as_dict() if simulation is not None else None,
    }


def benchmark_fixture(path: str | Path, *, configs: Mapping[str, Mapping[str, Any]] | None = None) -> dict[str, Any]:
    fixture = load_benchmark(path)
    configs = DEFAULT_CONFIGS if configs is None else configs
    if not configs:
        raise ValueError("at least one benchmark configuration is required")
    arms: dict[str, dict[str, Any]] = {}
    for name, config in configs.items():
        if not isinstance(config, Mapping):
            raise ValueError(f"configuration {name!r} must be an object")
        cases = [_case_result(case, config) for case in fixture["cases"]]
        legal_count = sum(1 for case in cases if case["legal"])
        objective_count = sum(1 for case in cases if case["objective_met"])
        unknown_count = sum(1 for case in cases if case["uncertainty"] not in {"", "none"})
        arms[str(name)] = {
            "config": dict(config),
            "sample_count": len(cases),
            "legal_action_rate": legal_count / len(cases),
            "objective_coverage": objective_count / len(cases),
            "unknown_effect_rate": unknown_count / len(cases),
            "cases": cases,
        }
    selected = max(arms, key=lambda name: (
        arms[name]["objective_coverage"],
        arms[name]["legal_action_rate"],
        -float(arms[name]["config"].get("time_limit_ms", 20.0)),
        name,
    ))
    return {
        "schema_version": SCHEMA_VERSION,
        "source": {"fixture_type": fixture["fixture_type"], "path": str(path)},
        "sample_count": len(fixture["cases"]),
        "arms": arms,
        "selected_config": selected,
        "quality": {"status": "synthetic_objective_only", "win_rate": "unknown"},
        "real_outcomes": "unknown",
        "limitations": [
            "fixture objectives cover local tactical contracts, not complete game outcomes",
            "simulation does not model random draws, powers, relic chains or map generation",
            "objective coverage, legality and simulated HP are not win-rate evidence",
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="offline Ironclad combat benchmark")
    parser.add_argument("--input", required=True)
    parser.add_argument("--json", dest="output")
    args = parser.parse_args(argv)
    try:
        report = benchmark_fixture(args.input)
    except (OSError, ValueError) as exc:
        print(json.dumps({"error": "invalid_combat_benchmark",
                          "kind": type(exc).__name__}, ensure_ascii=False))
        return 2
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
