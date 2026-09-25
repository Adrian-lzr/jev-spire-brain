"""Run synthetic JSON message fixtures through the advisor without a game/API."""

from __future__ import annotations

import argparse
import io
import json
import tempfile
from pathlib import Path

from spirebrain.driver.agent import SpireBrainAgent
from spirebrain.driver.stdio import StdioTransport
from spirebrain.brain.protocol import BrainResponse, StrategicPlan


class ReplayStrategicClient:
    """Offline strategic provider backed by fixed structured responses.

    Fixtures contain plans only; commands are never accepted from a replay
    response.  This makes planning and reconciliation reproducible without a
    network request or an API credential.
    """
    backend_name = "replay"
    async_required = False

    def __init__(self, responses: list[dict] | tuple[dict, ...]):
        self.responses = list(responses)
        self.calls = 0

    def plan(self, payload: dict) -> BrainResponse:
        self.calls += 1
        if not self.responses:
            return BrainResponse(backend=self.backend_name, error="回放响应耗尽",
                                 error_kind="replay_exhausted", fallback=True)
        response = self.responses[min(self.calls - 1, len(self.responses) - 1)]
        if not isinstance(response, dict):
            return BrainResponse(backend=self.backend_name, error="回放响应不是对象",
                                 error_kind="parse_or_validation", fallback=True)
        raw_plan = dict(response.get("plan", response)) if isinstance(response.get("plan", response), dict) else response.get("plan", response)
        if isinstance(raw_plan, dict):
            raw_plan.setdefault("state_id", str(payload.get("state_id", "")))
            raw_plan.setdefault("run_id", str(payload.get("run_id", "local")))
        try:
            plan = StrategicPlan.from_dict(raw_plan,
                state_id=str(payload.get("state_id", "")),
                run_id=str(payload.get("run_id", "local")))
        except (TypeError, ValueError, KeyError) as exc:
            return BrainResponse(backend=self.backend_name,
                                 error=f"回放计划无效：{type(exc).__name__}",
                                 error_kind="parse_or_validation", fallback=True)
        return BrainResponse(plan=plan, backend=self.backend_name,
                             model="replay", latency_ms=0,
                             request_id=f"replay-{self.calls}")


def load_fixed_responses(path: str | Path) -> list[dict]:
    """Load only structured plans from a synthetic response fixture."""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("fixture_type") != "synthetic":
        raise ValueError("fixed response fixture must be marked synthetic")
    responses = payload.get("responses", [])
    if not isinstance(responses, list) or not all(isinstance(item, dict) for item in responses):
        raise ValueError("responses must be an array of objects")
    return responses[:32]


def run_timing_fixture(path: str | Path) -> dict:
    """Validate a synthetic event-order fixture without running providers."""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("fixture_type") != "synthetic":
        raise ValueError("timing fixture must be marked synthetic")
    events = payload.get("events", [])
    if not isinstance(events, list):
        raise ValueError("events must be an array")
    expired = [event for event in events if isinstance(event, dict)
               and event.get("event_type") == "expired_result"]
    return {"fixture": str(path), "fixture_type": "synthetic",
            "event_count": len(events), "expired_result_count": len(expired),
            "passed": all(isinstance(event, dict) for event in events)}


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
