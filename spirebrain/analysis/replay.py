"""Run synthetic JSON message fixtures through the advisor without a game/API."""

from __future__ import annotations

import argparse
import io
import json
import tempfile
import copy
from pathlib import Path

from spirebrain.driver.agent import SpireBrainAgent
from spirebrain.driver.stdio import StdioTransport
from spirebrain.brain.protocol import BrainResponse, StrategicPlan
from spirebrain.jev_brain.client import JevResponse
from spirebrain.driver.legality import check_action
from spirebrain.brain.gpt_client import MockStrategicClient, UnavailableStrategicClient


_TIMING_EVENT_TYPES = {
    "state_observed", "provider_request", "provider_response",
    "expired_result", "advice", "decision", "feedback", "outcome",
}


class _ControlledReplayProvider:
    """A fixed provider used by the scheduler replay, never by live code."""

    backend_name = "replay-scheduler"
    async_required = True

    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.calls = []

    def plan(self, payload: dict) -> BrainResponse:
        self.calls.append(copy.deepcopy(payload))
        raw = self.responses[min(len(self.calls) - 1,
                                 max(0, len(self.responses) - 1))] \
            if self.responses else {}
        if not isinstance(raw, dict):
            return BrainResponse(backend=self.backend_name,
                                 error="scheduler fixture response invalid",
                                 error_kind="parse_or_validation", fallback=True)
        raw = dict(raw)
        raw.setdefault("plan_id", f"scheduler-plan-{len(self.calls)}")
        raw.setdefault("state_id", str(payload.get("state_id") or ""))
        raw.setdefault("run_id", str(payload.get("run_id") or "local"))
        try:
            plan = StrategicPlan.from_dict(
                raw, state_id=str(payload.get("state_id") or ""),
                run_id=str(payload.get("run_id") or "local"),
            )
        except (TypeError, ValueError, KeyError) as exc:
            return BrainResponse(backend=self.backend_name,
                                 error=f"scheduler fixture plan invalid: {type(exc).__name__}",
                                 error_kind="parse_or_validation", fallback=True)
        return BrainResponse(plan=plan, backend=self.backend_name,
                             model="scheduler-fixture", latency_ms=0,
                             request_id=f"scheduler-request-{len(self.calls)}")


def run_async_scheduler_fixture(path: str | Path) -> dict:
    """Execute a synthetic scheduler fixture through real owner boundaries.

    Events deliberately expose only deterministic control operations: ``queue``
    creates a task, ``complete`` runs the real orchestrator worker once, and
    ``poll`` drains/commits the result for the supplied current state. No
    provider thread or network is started by this harness.
    """
    payload = _read_json_object(path)
    if (payload.get("fixture_type") != "synthetic"
            or payload.get("kind") != "scheduler_scenario"):
        raise ValueError("async scheduler fixture must be marked synthetic")
    events = payload.get("replay_events")
    if not isinstance(events, list) or not events:
        raise ValueError("async scheduler fixture requires replay_events")
    responses = payload.get("responses", [])
    if not isinstance(responses, list):
        raise ValueError("async scheduler responses must be an array")

    from spirebrain.brain.action_broker import build_action_candidates
    from spirebrain.brain.orchestrator import StrategicOrchestrator

    provider = _ControlledReplayProvider(responses)
    orchestrator = StrategicOrchestrator(
        client=provider, backend="mock", async_planning=True,
    )
    current_state = None
    committed: list[str] = []
    discarded_before = 0
    for index, event in enumerate(events):
        if not isinstance(event, dict):
            raise ValueError(f"replay_events[{index}] must be an object")
        operation = str(event.get("op") or "")
        if operation in {"queue", "poll"}:
            state = event.get("state")
            if not isinstance(state, dict):
                raise ValueError(f"replay_events[{index}] {operation} requires state")
            current_state = copy.deepcopy(state)
            if operation == "queue":
                # The harness owns the worker turn, so prevent plan_for from
                # spawning an uncontrolled thread.
                orchestrator._plan_running = True
            before = orchestrator.expired_plan_results
            plan, _ = orchestrator.plan_for(
                current_state,
                candidates=build_action_candidates(current_state),
                decision_id=str(event.get("decision_id") or
                               f"replay-decision-{index}"),
            )
            discarded_before += orchestrator.expired_plan_results - before
            if plan is not None:
                committed.append(plan.plan_id)
        elif operation == "complete":
            if orchestrator._queued_plan is None:
                raise ValueError(f"replay_events[{index}] complete has no queued task")
            orchestrator._plan_worker()
        elif operation == "reset":
            orchestrator.reset(run_id=str(event.get("run_id") or ""))
            current_state = None
        else:
            raise ValueError(f"replay_events[{index}] unknown operation")

    report = {
        "fixture": str(path), "fixture_type": "synthetic",
        "event_count": len(events), "provider_calls": len(provider.calls),
        "committed_plan_ids": committed,
        "expired_result_count": orchestrator.expired_plan_results,
        "expired_result_reasons": dict(orchestrator.expired_plan_result_reasons),
        "discarded_during_poll": discarded_before,
        "final_run_id": orchestrator.memory.run_id,
        "final_run_epoch": orchestrator.memory.run_epoch,
        "passed": True,
    }
    orchestrator.close(timeout_ms=0)
    return report


def _read_json_object(path: str | Path) -> dict:
    """Read a fixture and give callers one consistent validation error.

    Replay is an evaluation boundary, not a best-effort log parser.  A
    malformed fixture must fail loudly instead of producing a green report
    with zero samples.
    """
    target = Path(path)
    try:
        value = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"{target}: invalid JSON fixture") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{target}: fixture must be an object")
    return value


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
    payload = _read_json_object(path)
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
        payload = _read_json_object(path)
    except (OSError, ValueError):
        return ""
    return str(payload.get("kind", "")) if isinstance(payload, dict) else ""


def run_timing_fixture(path: str | Path) -> dict:
    """Validate a synthetic event-order fixture without running providers.

    Timing fixtures are intentionally checked as a small state machine.  The
    old implementation accepted any list of dictionaries, so a typo in an
    event name or an expired response for a request that never existed could
    still be reported as ``passed``.  This validator does not pretend to run
    an async provider; it verifies the causal evidence that such a replay must
    contain and leaves execution to the runtime tests.
    """
    payload = _read_json_object(path)
    if (not isinstance(payload, dict) or payload.get("fixture_type") != "synthetic"
            or payload.get("kind") != "scheduler_scenario"):
        raise ValueError("timing fixture must be marked synthetic")
    events = payload.get("events", [])
    if not isinstance(events, list) or not events:
        raise ValueError("events must be an array")
    requests: dict[str, dict] = {}
    state_events: list[tuple[int, str]] = []
    expired: list[dict] = []
    for index, event in enumerate(events):
        if not isinstance(event, dict):
            raise ValueError(f"events[{index}] must be an object")
        event_type = event.get("event_type")
        if not isinstance(event_type, str) or event_type not in _TIMING_EVENT_TYPES:
            raise ValueError(f"events[{index}]: unknown event_type")
        if event_type == "state_observed":
            state_id = str(event.get("state_id") or "")
            if not state_id:
                raise ValueError(f"events[{index}]: state_observed requires state_id")
            state_events.append((index, state_id))
            continue
        if event_type == "provider_request":
            request_id = str(event.get("request_id") or "")
            if not request_id:
                raise ValueError(f"events[{index}]: provider_request requires request_id")
            if request_id in requests:
                raise ValueError(f"events[{index}]: duplicate request_id {request_id}")
            if not event.get("state_id"):
                raise ValueError(f"events[{index}]: provider_request requires state_id")
            requests[request_id] = event
            continue
        if event_type in {"provider_response", "expired_result"}:
            request_id = str(event.get("request_id") or "")
            if not request_id or request_id not in requests:
                raise ValueError(
                    f"events[{index}]: {event_type} references unknown request_id")
            if event_type == "expired_result":
                if not str(event.get("fallback_reason") or ""):
                    raise ValueError(f"events[{index}]: expired_result requires fallback_reason")
                request = requests[request_id]
                request_state = str(request.get("state_id") or "")
                result_state = str(event.get("state_id") or request_state)
                # A result is only demonstrably stale when a different state
                # was observed between request and completion.  Requiring the
                # evidence here prevents a fixture from claiming expiry based
                # on a label alone.
                request_position = next(
                    i for i, candidate in enumerate(events[:index])
                    if candidate is request
                )
                changed = any(
                    state != request_state
                    for state_index, state in state_events
                    if request_position < state_index < index
                )
                if not changed and result_state == request_state:
                    raise ValueError(
                        f"events[{index}]: expired_result lacks state-change evidence")
                expired.append(event)
    if not requests:
        raise ValueError("timing fixture must contain a provider_request")
    return {"fixture": str(path), "fixture_type": "synthetic",
            "event_count": len(events), "expired_result_count": len(expired),
            "provider_request_count": len(requests),
            "state_count": len({state for _, state in state_events}), "passed": True}


def run_fixture(path: Path, *, mode: str = "rules") -> dict:
    if mode not in {"rules", "jev", "strategic", "full"}:
        raise ValueError(f"unknown replay mode: {mode}")
    payload = _read_json_object(path)
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
        trace_records = []
        trace_path = Path(tmp) / "decision_trace.jsonl"
        if trace_path.exists():
            for line in trace_path.read_text(encoding="utf-8", errors="replace").splitlines():
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(value, dict):
                    trace_records.append(value)
        first_latency = [r["first_advice_latency_ms"] for r in trace_records
                         if isinstance(r.get("first_advice_latency_ms"), (int, float))]
        complete_latency = [r["decision_latency_ms"] for r in trace_records
                            if isinstance(r.get("decision_latency_ms"), (int, float))]
        expired_count = sum(1 for r in trace_records
                            if r.get("event_type") == "expired_result")
        outcomes = [r for r in trace_records if r.get("event_type") == "outcome"]
        unobserved = sum(1 for r in outcomes if r.get("verdict") == "unobserved")
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
        "first_advice_latency_ms": (min(first_latency) if first_latency else None),
        "complete_advice_latency_ms": (max(complete_latency) if complete_latency else None),
        "expired_result_count": expired_count,
        "unobserved_count": unobserved,
        "outcome_count": len(outcomes),
        "game_rejections": None,
        "passed": bool(transport.messages) and not invalid and all(legality),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--async-scheduler", action="store_true",
                        help="run a synthetic scheduler_scenario through owner boundaries")
    args = parser.parse_args(argv)
    root = Path(args.input)
    if args.async_scheduler:
        try:
            result = run_async_scheduler_fixture(root)
        except (OSError, ValueError) as exc:
            print(json.dumps({"error": "invalid_fixture", "kind": type(exc).__name__},
                             ensure_ascii=False))
            return 2
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("passed") else 1
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
