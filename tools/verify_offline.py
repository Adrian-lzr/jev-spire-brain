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
from pathlib import Path


def run_check(name: str, command: list[str], cwd: Path) -> dict:
    completed = subprocess.run(command, cwd=cwd, text=True,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    return {"name": name, "command": command, "returncode": completed.returncode,
            "passed": completed.returncode == 0, "output": completed.stdout[-12000:]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", dest="output", help="also write the report here")
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[1]
    python = sys.executable
    fixture = root / "tests" / "fixtures" / "metrics" / "decision_trace.jsonl"
    replay = root / "tests" / "fixtures" / "replay" / "advisor_states.json"
    checks = [
        ("tests", [python, "-m", "pytest", "-q"]),
        ("compile", [python, "-m", "compileall", "-q", "spirebrain", "start.py", "run_agent.py",
                      "tools/auto_smoke.py", "tools/real_game_smoke.py"]),
        ("contracts", [python, "tools/check_contracts.py"]),
        ("metrics", [python, "-m", "spirebrain.analysis.metrics", "--input", str(fixture)]),
        ("replay", [python, "-m", "spirebrain.analysis.replay", "--input", str(replay)]),
        ("launcher_smoke", [python, "tools/auto_smoke.py"]),
    ]
    results = [run_check(name, command, root) for name, command in checks]
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
