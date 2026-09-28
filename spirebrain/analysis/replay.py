"""Run synthetic JSON message fixtures through the advisor without a game/API."""

from __future__ import annotations

import argparse
import io
import json
import tempfile
import copy
from collections import Counter
from pathlib import Path

from spirebrain.driver.agent import SpireBrainAgent
from spirebrain.driver.stdio import StdioTransport
from spirebrain.brain.protocol import ActionCandidate, BrainResponse, StrategicPlan
from spirebrain.jev_brain.client import JevResponse
from spirebrain.driver.legality import check_action
from spirebrain.brain.action_broker import reconcile
from spirebrain.brain.gpt_client import MockStrategicClient, UnavailableStrategicClient
from spirebrain.analysis.event_contract import decision_chain
from spirebrain.analysis.replay_blob import ReplayBlobStore


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
        self.error_kinds: list[str] = []

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
        fault = str(raw.get("fault") or "").lower()
        if fault:
            error_kind = {
                "timeout": "timeout",
                "401": "auth_error", "403": "auth_error", "auth": "auth_error",
                "429": "rate_limited", "rate_limit": "rate_limited",
                "invalid_json": "parse_or_validation",
                "invalid_plan": "parse_or_validation",
            }.get(fault, "provider_error")
            self.error_kinds.append(error_kind)
            return BrainResponse(
                backend=self.backend_name, model="scheduler-fixture",
                error=f"fixture fault: {fault}", error_kind=error_kind,
                fallback=True, request_id=f"scheduler-request-{len(self.calls)}",
            )
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
    committed_decision_ids: list[str] = []
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
            decision_id = str(event.get("decision_id") or f"replay-decision-{index}")
            plan, _ = orchestrator.plan_for(
                current_state,
                candidates=build_action_candidates(current_state),
                decision_id=decision_id,
            )
            discarded_before += orchestrator.expired_plan_results - before
            if plan is not None:
                committed.append(plan.plan_id)
                committed_decision_ids.append(decision_id)
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
        "provider_error_kinds": list(provider.error_kinds),
        "provider_error_counts": dict(Counter(provider.error_kinds)),
        "provider_decision_ids": [str(call.get("decision_id") or "")
                                   for call in provider.calls],
        "committed_plan_ids": committed,
        "committed_decision_ids": committed_decision_ids,
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


def replay_decision_chain(trace_path: str | Path, decision_id: str,
                          *, require_blobs: bool = True) -> dict:
    """Load one decision chain and verify its content-addressed evidence.

    This is intentionally a read-only offline entry point.  It never invokes a
    provider: JEV/GPT inputs and responses are returned as verified blobs so a
    caller can feed them to a fixed replay harness.  Missing or tampered blobs
    fail loudly when ``require_blobs`` is enabled instead of silently replaying
    an incomplete decision.
    """
    path = Path(trace_path)
    records = []
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                records.append(value)
    except OSError as exc:
        raise FileNotFoundError(path) from exc
    chain = decision_chain(records, str(decision_id))
    if not any(chain.get(bucket) for bucket in (
            "inputs", "requests", "responses", "decisions", "advice_versions",
            "outcomes", "results", "expired", "skipped")):
        raise ValueError(f"decision replay evidence not found: {decision_id}")
    store = ReplayBlobStore(path.parent / "decision_blobs")
    blobs: list[dict] = []
    errors: list[str] = []
    for bucket in ("inputs", "requests", "responses", "decisions"):
        for event in chain.get(bucket, []):
            for field_name in ("state_blob", "candidate_blob", "response_blob"):
                reference = event.get(field_name)
                if reference is None:
                    continue
                try:
                    value = store.get(reference)
                    blobs.append({"bucket": bucket, "event_type": event.get("event_type"),
                                  "request_id": event.get("request_id"),
                                  "field": field_name, "value": value})
                except (FileNotFoundError, OSError, TypeError, ValueError) as exc:
                    errors.append(f"{field_name}:{type(exc).__name__}")
    if require_blobs:
        required = [event for bucket in ("requests", "responses")
                    for event in chain.get(bucket, [])]
        missing = [event.get("event_type", "event") for event in required
                   if not any(item.get("event_type") == event.get("event_type")
                              and item.get("request_id") == event.get("request_id")
                              for item in blobs)]
        for event in chain.get("requests", []):
            if not event.get("state_blob"):
                errors.append("missing_provider_request_blob")
        for event in chain.get("responses", []):
            if not event.get("response_blob"):
                errors.append("missing_provider_response_blob")
        if missing:
            errors.append("missing_provider_blob")
    if errors:
        raise ValueError("decision replay evidence invalid: " + ",".join(errors))
    return {**chain, "replay_blobs_verified": len(blobs), "replay_blobs": blobs}


def _blob_value(chain: dict, field: str):
    """Return the most complete verified blob of ``field`` from a chain.

    Provider request blobs intentionally contain compact/model-facing data;
    the owner-loop ``inputs``/``decisions`` blobs contain the executable state
    and command templates needed for local replay.  Prefer those owner blobs
    while retaining request blobs as a legacy fallback.
    """
    priority = {"inputs": 0, "decisions": 0, "requests": 1, "responses": 2}
    items = sorted(
        (item for item in chain.get("replay_blobs", []) if item.get("field") == field),
        key=lambda item: priority.get(str(item.get("bucket") or ""), 3),
    )
    for item in items:
        value = item.get("value")
        if field == "candidate_blob" and isinstance(value, list):
            if any(isinstance(candidate, dict) and candidate.get("command")
                   for candidate in value):
                return value
            continue
        if value is not None:
            return value
    return None


def _candidate_from_blob(value: dict) -> ActionCandidate:
    """Rehydrate only the local candidate shape used by the broker.

    Candidate blobs are produced by the local generator, not by a model.  The
    replay path therefore preserves the recorded signature and command
    template, while the final legality check still treats the state as the
    authority.
    """
    if not isinstance(value, dict):
        raise ValueError("candidate blob item must be an object")
    return ActionCandidate(
        candidate_id=str(value.get("candidate_id") or ""),
        kind=str(value.get("kind") or "unknown"),
        label=str(value.get("label") or value.get("candidate_id") or "candidate"),
        command=dict(value.get("command") or value.get("command_template") or {}),
        legal=bool(value.get("legal", True)),
        cost=value.get("cost"), target=value.get("target"),
        goal_tags=list(value.get("goal_tags") or []),
        risk_tags=list(value.get("risk_tags") or []),
        uncertainty=str(value.get("uncertainty") or ""),
        description=str(value.get("description") or ""),
        command_template=value.get("command_template"),
        candidate_signature=str(value.get("candidate_signature") or ""),
        entity_id=str(value.get("entity_id") or ""),
        upgrade_state=str(value.get("upgrade_state") or ""),
        validity=dict(value.get("validity") or {}),
    )


def replay_decision(trace_path: str | Path, decision_id: str) -> dict:
    """Execute one decision chain locally from its stored evidence.

    This is deliberately not a provider replay: it never makes a network call
    and never accepts a model-authored command.  It rehydrates the recorded
    candidates and strategic plan, runs the same local reconciliation, and
    checks both the recorded and replayed commands against the stored state.
    Missing or tampered evidence fails before any decision is produced.
    """
    chain = replay_decision_chain(trace_path, decision_id, require_blobs=True)
    # ``decision`` is the owner-loop finalization, while ``advice`` records
    # what was actually published.  A later advice revision may legitimately
    # replace that decision (for example after a local fast result), so choose
    # the last command-bearing event across both buckets instead of preferring
    # one bucket wholesale.  Trace sequence is the strongest ordering signal;
    # timestamp/index keeps legacy traces deterministic.
    final_events = [
        event for event in (list(chain.get("decisions") or []) +
                            list(chain.get("advice_versions") or []))
        if isinstance(event.get("command"), dict)
    ]
    if not final_events:
        raise ValueError(f"decision replay evidence missing final decision: {decision_id}")
    has_sequence = any(isinstance(event.get("trace_sequence"), (int, float))
                       for event in final_events)
    if has_sequence:
        final_events.sort(key=lambda event: (
            float(event.get("trace_sequence"))
            if isinstance(event.get("trace_sequence"), (int, float)) else -1.0,
            float(event.get("timestamp", event.get("ts", 0)) or 0),
        ))
    else:
        final_events.sort(key=lambda event: float(
            event.get("timestamp", event.get("ts", 0)) or 0))
    final_event = final_events[-1]
    state = _blob_value(chain, "state_blob")
    raw_candidates = _blob_value(chain, "candidate_blob")
    if not isinstance(state, dict):
        raise ValueError("decision replay evidence missing state blob")
    if not isinstance(raw_candidates, list) or not raw_candidates:
        raise ValueError("decision replay evidence missing candidate blob")
    candidates = [_candidate_from_blob(item) for item in raw_candidates]
    candidates = [candidate for candidate in candidates if candidate.candidate_id]
    if not candidates:
        raise ValueError("decision replay evidence has no usable candidates")

    # A strategic response blob is a plan object.  JEV answer blobs are kept in
    # the returned evidence for audit, but they do not author commands here.
    plan = None
    for item in chain.get("replay_blobs", []):
        if item.get("field") != "response_blob" or not isinstance(item.get("value"), dict):
            continue
        raw = item["value"]
        raw_plan = raw.get("plan", raw)
        if not isinstance(raw_plan, dict) or "preferred_candidates" not in raw_plan:
            continue
        try:
            plan = StrategicPlan.from_dict(
                raw_plan,
                state_id=str(final_event.get("state_id") or ""),
                run_id=str(final_event.get("run_id") or "local"),
            )
            plan.bind_candidates(candidates)
            break
        except (TypeError, ValueError, KeyError):
            continue

    replay_command, replay_detail = reconcile(
        game=state, candidates=candidates,
        fallback={"command": "state"}, plan=plan,
        state_id=str(final_event.get("state_id") or ""),
    )
    recorded_command = final_event.get("command")
    if not isinstance(recorded_command, dict):
        raise ValueError("decision replay evidence missing recorded command")
    recorded_verb = str(recorded_command.get("command") or "").lower()
    recorded_legal, recorded_reason = check_action(state, recorded_command)
    replay_verb = str(replay_command.get("command") or "").lower()
    replay_legal, replay_reason = check_action(state, replay_command)
    return {
        **chain,
        "replay_execution": {
            "offline": True,
            "provider_calls": 0,
            "candidate_count": len(candidates),
            "plan_id": getattr(plan, "plan_id", "") if plan else "",
            "recorded_command": recorded_command,
            "recorded_command_safe": recorded_verb in {"wait", "state"} or recorded_legal,
            "recorded_legality": recorded_reason,
            "replayed_command": replay_command,
            "replayed_command_safe": replay_verb in {"wait", "state"} or replay_legal,
            "replayed_legality": replay_reason,
            "matches_recorded": replay_command == recorded_command,
            "source_type": replay_detail.source_type,
            "final_event_type": str(final_event.get("event_type") or ""),
            "advice_revision": final_event.get("advice_revision"),
        },
    }


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
    responses: set[str] = set()
    expired_requests: set[str] = set()
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
            if event_type == "provider_response":
                if request_id in responses:
                    raise ValueError(
                        f"events[{index}]: duplicate provider_response for {request_id}")
                responses.add(request_id)
            if event_type == "expired_result":
                if request_id in expired_requests:
                    raise ValueError(
                        f"events[{index}]: duplicate expired_result for {request_id}")
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
                expired_requests.add(request_id)
                expired.append(event)
    if not requests:
        raise ValueError("timing fixture must contain a provider_request")
    return {"fixture": str(path), "fixture_type": "synthetic",
            "event_count": len(events), "expired_result_count": len(expired),
            "provider_request_count": len(requests),
            "state_count": len({state for _, state in state_events}), "passed": True}


def run_fixture(path: Path, *, mode: str = "rules",
                knowledge_enabled: bool = True) -> dict:
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
        agent = SpireBrainAgent(jev_backend="mock", brain_backend=brain_backend,
                                log_dir=tmp, knowledge_enabled=knowledge_enabled)
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
        knowledge_citation_count = sum(
            len((payload.get("knowledge_context") or {}).get("citation_ids") or [])
            for payload in getattr(strategic, "payloads", [])
            if isinstance(payload, dict)
        )
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
        "knowledge_enabled": bool(knowledge_enabled),
        "knowledge_citation_count": knowledge_citation_count,
        "game_rejections": None,
        "passed": bool(transport.messages) and not invalid and all(legality),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--async-scheduler", action="store_true",
                        help="run a synthetic scheduler_scenario through owner boundaries")
    parser.add_argument("--decision-id",
                        help="verify and print one decision chain from a decision_trace.jsonl")
    parser.add_argument("--execute", action="store_true",
                        help="with --decision-id, re-run local reconciliation offline")
    args = parser.parse_args(argv)
    root = Path(args.input)
    if args.decision_id:
        try:
            result = (replay_decision(root, args.decision_id)
                      if args.execute else replay_decision_chain(root, args.decision_id))
        except (OSError, ValueError) as exc:
            print(json.dumps({"error": "invalid_decision_replay",
                              "kind": type(exc).__name__, "message": str(exc)},
                             ensure_ascii=False))
            return 2
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
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
