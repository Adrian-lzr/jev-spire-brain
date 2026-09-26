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
from spirebrain.jev_brain.client import JevResponse
from spirebrain.driver.legality import check_action
from spirebrain.brain.gpt_client import MockStrategicClient, UnavailableStrategicClient


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
    if (not isinstance(payload, dict) or payload.get("fixture_type") != "synthetic"
            or payload.get("kind") != "fixed_provider_responses"):
        raise ValueError("fixed response fixture must be marked synthetic")
    responses = payload.get("responses", [])
    if not isinstance(responses, list) or not responses or not all(isinstance(item, dict) for item in responses):
        raise ValueError("responses must be an array of objects")
    return responses[:32]


def _fixture_kind(path: Path) -> str:
    """Read only the small header used to route a replay fixture."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    return str(payload.get("kind", "")) if isinstance(payload, dict) else ""


def run_timing_fixture(path: str | Path) -> dict:
    """Validate a synthetic event-order fixture without running providers."""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if (not isinstance(payload, dict) or payload.get("fixture_type") != "synthetic"
            or payload.get("kind") != "scheduler_scenario"):
        raise ValueError("timing fixture must be marked synthetic")
    events = payload.get("events", [])
    if not isinstance(events, list) or not events:
        raise ValueError("events must be an array")
    expired = [event for event in events if isinstance(event, dict)
               and event.get("event_type") == "expired_result"]
    return {"fixture": str(path), "fixture_type": "synthetic",
            "event_count": len(events), "expired_result_count": len(expired),
            "passed": all(isinstance(event, dict) for event in events)}


def run_fixture(path: Path, *, mode: str = "rules") -> dict:
    if mode not in {"rules", "jev", "strategic", "full"}:
        raise ValueError(f"unknown replay mode: {mode}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path}: fixture must be an object")
    if payload.get("fixture_type") != "synthetic":
        raise ValueError(f"{path}: fixture_type must be synthetic")
    kind = payload.get("kind")
    if kind != "state_sequence":
        raise ValueError(f"{path}: kind must be state_sequence")
    messages = payload.get("messages", [])
    if not isinstance(messages, list):
        raise ValueError(f"{path}: messages must be an array")
    if not messages:
        raise ValueError(f"{path}: messages must not be empty")
    if not all(isinstance(message, dict) and isinstance(message.get("game_state"), dict)
               for message in messages):
        raise ValueError("messages must contain structured game_state objects")
    with tempfile.TemporaryDirectory(prefix="spirebrain-replay-") as tmp:
        brain_backend = "mock" if mode in {"strategic", "full"} else "none"
        agent = SpireBrainAgent(jev_backend="mock", brain_backend=brain_backend, log_dir=tmp)
        strategic = (MockStrategicClient() if mode in {"strategic", "full"}
                     else UnavailableStrategicClient("disabled for replay"))
        agent.strategic.provider = strategic
        agent.strategic.planner.provider = strategic
        logged_jev = agent.jev.inner
        if mode in {"rules", "strategic"}:
            # A rules/strategic-only replay must not accidentally exercise JEV
            # through a scene handler.  The local adapter returns an empty
            # response, so callers take their documented rule fallback without
            # producing a provider event or a fake call count.
            agent.jev.ask = lambda state, questions: JevResponse(
                answers={}, latency_ms=0, backend="disabled", model="disabled")
        # Force synchronous local advice for deterministic offline replay. No
        # network provider or background worker is involved in this mode.
        agent.clone_for_advice = lambda: None
        legality = []
        choose = agent.choose_action

        def checked_choose(state):
            command = choose(state)
            if command.get("command") not in {"wait", "state"}:
                legality.append(check_action(state, command)[0])
            return command

        agent.choose_action = checked_choose
        output = io.StringIO()
        transport = StdioTransport(agent, mode="advise", log_path=None,
                                    advice_path=None, warn_stream=io.StringIO())
        lines = [json.dumps(message, ensure_ascii=False) for message in messages]
        transport.run(lines, output, send_ready=False)
        commands = [line.strip().split(" ", 1)[0] for line in output.getvalue().splitlines()
                    if line.strip()]
        invalid = [command for command in commands if command not in {"wait", "state"}]
        jev_calls = logged_jev.calls
        strategic_calls = getattr(strategic, "calls", 0)
        provider_calls = jev_calls + strategic_calls
    return {
        "fixture": str(path),
        "fixture_type": payload.get("fixture_type", "synthetic") if isinstance(payload, dict) else "synthetic",
        "messages": transport.messages,
        "recommendations": transport.advice_issued,
        "commands": len(commands),
        "advise_commands": commands,
        "invalid_advise_commands": invalid,
        "fallbacks": sum(1 for item in agent.history if item.get("fallback")),
        "provider_calls": provider_calls,
        "jev_calls": jev_calls,
        "strategic_calls": strategic_calls,
        "advise_wire_safety": not invalid,
        "candidate_legality": {"checked": len(legality), "legal": sum(legality),
                               "rate": sum(legality) / len(legality) if legality else None},
        "game_rejections": None,
        "passed": bool(transport.messages) and not invalid and all(legality),
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
    state_files = [path for path in files if _fixture_kind(path) == "state_sequence"]
    if not state_files:
        print(json.dumps({"error": "state_sequence_fixture_missing"}, ensure_ascii=False))
        return 2
    results = [run_fixture(path) for path in state_files]
    result = {"fixture_count": len(results), "passed": all(item["passed"] for item in results),
              "results": results}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
