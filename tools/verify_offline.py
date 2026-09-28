"""Run independent offline checks and report every result.

Unlike a shell chain, one failed check does not hide the results of the later
checks.  This is the reproducibility entry point used by CI and by a clean
checkout; it never reads credentials, contacts a provider, or writes game
configuration.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path


def _configure_text_output() -> None:
    """Keep the aggregate gate printable on Windows locale code pages."""
    stream = getattr(sys, "stdout", None)
    reconfigure = getattr(stream, "reconfigure", None)
    if not callable(reconfigure):
        return
    try:
        reconfigure(encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        # Test runners may provide a wrapped stream without reconfiguration.
        pass


def run_check(name: str, command: list[str], cwd: Path) -> dict:
    completed = subprocess.run(
        command, cwd=cwd, text=True, encoding="utf-8", errors="replace",
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    return {"name": name, "command": command, "returncode": completed.returncode,
            "passed": completed.returncode == 0, "output": completed.stdout[-12000:]}


def _validate_strategy_rag_output(result: dict) -> dict:
    """Validate the RAG CLI's machine-readable contract after it exits.

    A zero exit code alone only proves that the CLI did not raise.  This check
    keeps the offline gate honest by requiring an Ironclad-only, source-cited
    result and rejecting command-like fields that could turn retrieved prose
    into an executable instruction.
    """
    if not result.get("passed"):
        return result
    try:
        payload = json.loads(str(result.get("output") or ""))
    except (TypeError, ValueError, json.JSONDecodeError):
        result["passed"] = False
        result["validation_error"] = "strategy_rag_output_not_json"
        return result
    errors: list[str] = []
    if payload.get("status") != "ready":
        errors.append("status_not_ready")
    if str(payload.get("character") or "").upper() != "IRONCLAD":
        errors.append("character_not_ironclad")
    items = payload.get("items")
    citations = payload.get("citation_ids")
    if not isinstance(items, list) or not items:
        errors.append("items_missing")
    item_ids = ([item.get("chunk_id") for item in items
                 if isinstance(item, dict)]
                if isinstance(items, list) else [])
    item_count = len(items) if isinstance(items, list) else -1
    if (not isinstance(citations, list) or len(item_ids) != item_count
            or citations != item_ids):
        errors.append("citation_ids_do_not_match_items")
    forbidden = {"command", "candidate_id", "card_index", "target_index", "protocol"}
    for index, item in enumerate(items if isinstance(items, list) else []):
        if not isinstance(item, dict):
            errors.append(f"item_{index}_not_object")
            continue
        if not str(item.get("source_url") or "").startswith("https://"):
            errors.append(f"item_{index}_source_missing")
        if forbidden.intersection(item):
            errors.append(f"item_{index}_contains_executable_field")
    result["passed"] = not errors
    result["validation"] = {
        "status": payload.get("status"),
        "character": payload.get("character"),
        "citation_count": len(citations) if isinstance(citations, list) else 0,
    }
    if errors:
        result["validation_error"] = ";".join(errors)
    return result


def run_strategy_rag_check(python: str, cwd: Path) -> dict:
    """Run and validate the local strategy retrieval CLI without networking."""
    result = run_check(
        "strategy_rag",
        [python, "-m", "spirebrain.knowledge.rag", "--query",
         "Ironclad Act 1 HP elite damage block", "--character", "IRONCLAD",
         "--top-k", "3"],
        cwd,
    )
    return _validate_strategy_rag_output(result)


def _validate_combat_benchmark_output(result: dict) -> dict:
    """Validate the benchmark result instead of trusting a zero exit code."""
    if not result.get("passed"):
        return result
    raw = str(result.get("output") or "")
    # ``python -m spirebrain.sim.combat_benchmark`` may emit a runpy warning
    # because the package exports the benchmark helper.  The JSON payload is
    # still the final machine-readable output, so discard only leading text.
    start = raw.find("{")
    try:
        payload = json.loads(raw[start:]) if start >= 0 else None
    except (TypeError, ValueError, json.JSONDecodeError):
        payload = None
    errors: list[str] = []
    if not isinstance(payload, dict):
        errors.append("combat_benchmark_output_not_json")
    else:
        if payload.get("sample_count", 0) <= 0:
            errors.append("combat_benchmark_has_no_samples")
        arms = payload.get("arms")
        if not isinstance(arms, dict) or not arms:
            errors.append("combat_benchmark_arms_missing")
        else:
            for name, arm in arms.items():
                if not isinstance(arm, dict):
                    errors.append(f"arm_{name}_not_object")
                    continue
                if arm.get("sample_count") != payload.get("sample_count"):
                    errors.append(f"arm_{name}_sample_count_mismatch")
                if arm.get("legal_action_rate") != 1.0:
                    errors.append(f"arm_{name}_illegal_action")
                if arm.get("objective_coverage") != 1.0:
                    errors.append(f"arm_{name}_objective_miss")
        quality = payload.get("quality")
        if not isinstance(quality, dict) or quality.get("win_rate") != "unknown":
            errors.append("combat_benchmark_win_rate_must_be_unknown")
        if payload.get("real_outcomes") != "unknown":
            errors.append("combat_benchmark_real_outcomes_must_be_unknown")
    result["passed"] = not errors
    result["validation"] = {
        "sample_count": payload.get("sample_count") if isinstance(payload, dict) else 0,
        "arm_count": len(payload.get("arms", {})) if isinstance(payload, dict)
        and isinstance(payload.get("arms"), dict) else 0,
    }
    if errors:
        result["validation_error"] = ";".join(errors)
    return result


def run_combat_benchmark_check(python: str, cwd: Path) -> dict:
    """Run the bounded combat benchmark with no provider or game dependency."""
    fixture = cwd / "tests" / "fixtures" / "sim" / "combat_benchmark.json"
    # The human-readable CLI prints every case and can exceed ``run_check``'s
    # bounded output tail.  Ask it to write the machine-readable report to an
    # isolated temporary file, then validate that complete report here.
    with tempfile.TemporaryDirectory(prefix="combat-benchmark-") as tmp:
        report_path = Path(tmp) / "report.json"
        result = run_check(
            "combat_benchmark",
            [python, "-m", "spirebrain.sim.combat_benchmark", "--input", str(fixture),
             "--json", str(report_path)],
            cwd,
        )
        if result.get("passed") and report_path.is_file():
            result["output"] = report_path.read_text(encoding="utf-8")
    return _validate_combat_benchmark_output(result)


def main(argv: list[str] | None = None) -> int:
    _configure_text_output()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", dest="output", help="also write the report here")
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[1]
    python = sys.executable
    fixture = root / "tests" / "fixtures" / "metrics" / "decision_trace.jsonl"
    replay = root / "tests" / "fixtures" / "replay" / "advisor_states.json"
    representative = root / "tests" / "fixtures" / "replay" / "representative_states.json"
    async_replay = root / "tests" / "fixtures" / "replay" / "async_scheduler.json"
    checks = [
        ("tests", [python, "-m", "pytest", "-q"]),
        ("compile", [python, "-m", "compileall", "-q", "spirebrain", "start.py", "run_agent.py",
                      "tools/auto_smoke.py", "tools/real_game_smoke.py"]),
        ("contracts", [python, "tools/check_contracts.py"]),
        ("metrics", [python, "-m", "spirebrain.analysis.metrics", "--input", str(fixture)]),
        ("evaluation", [python, "-m", "spirebrain.analysis.evaluate", "--input", str(representative)]),
        ("replay", [python, "-m", "spirebrain.analysis.replay", "--input", str(replay)]),
        ("async_replay", [python, "-m", "spirebrain.analysis.replay", "--input",
                           str(async_replay), "--async-scheduler"]),
        ("launcher_smoke", [python, "tools/auto_smoke.py"]),
    ]
    results = [run_check(name, command, root) for name, command in checks]
    # Keep strategy retrieval as its own offline check and validate its JSON
    # contract, rather than merely relying on the process exit code.
    results.insert(-1, run_strategy_rag_check(python, root))
    results.insert(-1, run_combat_benchmark_check(python, root))
    report = {"python": sys.version.split()[0], "root": str(root),
              "checks": results, "passed": all(item["passed"] for item in results)}
    for item in results:
        print(f"[{ 'PASS' if item['passed'] else 'FAIL' }] {item['name']} "
              f"(exit {item['returncode']})")
        if not item["passed"]:
            print(item["output"])
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).write_text(rendered + "\n", encoding="utf-8")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
