"""Run synthetic JSON message fixtures through the advisor without a game/API."""

from __future__ import annotations

import argparse
import io
import json
import tempfile
from pathlib import Path

from spirebrain.driver.agent import SpireBrainAgent
from spirebrain.driver.stdio import StdioTransport


def run_fixture(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    messages = payload.get("messages", []) if isinstance(payload, dict) else payload
    if not isinstance(messages, list):
        raise ValueError(f"{path}: messages must be an array")
    with tempfile.TemporaryDirectory(prefix="spirebrain-replay-") as tmp:
        agent = SpireBrainAgent(jev_backend="mock", brain_backend="none", log_dir=tmp)
        # Force synchronous local advice for deterministic offline replay. No
        # network provider or background worker is involved in this mode.
        agent.clone_for_advice = lambda: None
        output = io.StringIO()
        transport = StdioTransport(agent, mode="advise", log_path=None,
                                    advice_path=None, warn_stream=io.StringIO())
        lines = [json.dumps(message, ensure_ascii=False) for message in messages]
        transport.run(lines, output, send_ready=False)
        commands = [line.strip().split(" ", 1)[0] for line in output.getvalue().splitlines()
                    if line.strip()]
        invalid = [command for command in commands if command not in {"wait", "state"}]
    return {
        "fixture": str(path),
        "fixture_type": payload.get("fixture_type", "synthetic") if isinstance(payload, dict) else "synthetic",
        "messages": transport.messages,
        "recommendations": transport.advice_issued,
        "commands": len(commands),
        "advise_commands": commands,
        "invalid_advise_commands": invalid,
        "fallbacks": sum(1 for item in agent.history if item.get("fallback")),
        "passed": not invalid,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    args = parser.parse_args(argv)
    root = Path(args.input)
    files = sorted(root.glob("*.json")) if root.is_dir() else [root]
    if not files:
        print(json.dumps({"error": "input_missing", "input": str(root)}, ensure_ascii=False))
        return 2
    results = [run_fixture(path) for path in files]
    result = {"fixture_count": len(results), "passed": all(item["passed"] for item in results),
              "results": results}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
