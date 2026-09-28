import io
import json

from spirebrain.analysis.event_contract import decision_chain, make_event, validate_event
from spirebrain.analysis.replay import ReplayStrategicClient, load_fixed_responses, run_timing_fixture
from spirebrain.analysis.replay import run_async_scheduler_fixture
from spirebrain.brain.protocol import BrainResponse
from spirebrain.driver.trace import DecisionTrace
from spirebrain.brain.gpt_client import LoggingStrategicClient, MockStrategicClient


def test_event_contract_reconstructs_decision_chain(tmp_path):
    identity = {"run_id": "r", "run_epoch": 1, "decision_id": "d",
                "state_id": "s", "request_id": "q", "plan_id": "p",
                "advice_revision": 1, "config_id": "c"}
    events = [make_event(kind, identity=identity) for kind in
              ("state_observed", "provider_request", "provider_response",
               "decision", "advice", "advice_replaced", "outcome")]
    assert all(validate_event(event) == [] for event in events)
    chain = decision_chain(events, "d")
    assert len(chain["inputs"]) == 1
    assert len(chain["requests"]) == 1
    assert len(chain["responses"]) == 1
    assert len(chain["advice_versions"]) == 2
    assert len(chain["outcomes"]) == 1


def test_decision_chain_reports_request_lifecycle_gaps_and_mismatches():
    request = make_event("provider_request", identity={
        "decision_id": "d", "state_id": "s", "request_id": "q",
        "run_id": "r", "run_epoch": 1, "logical_request_id": "l",
        "attempt_id": "a",
    }, fields={"provider": "fixture"})
    mismatched_response = make_event("provider_response", identity={
        "decision_id": "d", "state_id": "new-state", "request_id": "q",
        "run_id": "r", "run_epoch": 1, "logical_request_id": "l",
        "attempt_id": "a",
    }, fields={"provider": "fixture", "latency_ms": 1,
               "error_kind": "", "fallback": False})
    chain = decision_chain([request, mismatched_response], "d")

    lifecycle = chain["request_lifecycle"]
    assert lifecycle["request_count"] == 1
    assert lifecycle["response_count"] == 1
    assert lifecycle["closed_request_count"] == 1
    assert lifecycle["open_request_ids"] == []
    assert lifecycle["identity_mismatches"] == [{
        "request_id": "q", "fields": ["state_id"],
    }]

    open_chain = decision_chain([request], "d")
    assert open_chain["request_lifecycle"]["open_request_ids"] == ["q"]


def test_production_memory_trace_events_use_canonical_record_kinds(tmp_path):
    from spirebrain.driver.agent import SpireBrainAgent

    agent = SpireBrainAgent(jev_backend="mock", brain_backend="none", log_dir=tmp_path)
    game = {
        "run_id": "trace-run", "character": "IRONCLAD", "screen_type": "COMBAT",
        "current_hp": 40, "max_hp": 80, "combat": {"player": {"energy": 1},
        "hand": [{"id": "Strike_R", "cost": 1, "damage": 6, "has_target": True}],
        "monsters": [{"id": "Cultist", "current_hp": 10, "intent": "ATTACK", "damage": 6}]},
    }
    advice = agent.choose_action(game)
    agent.record_observed_result("victory", state_id=agent.snapshot.state_id,
                                 decision_id=agent._active_decision_id)
    records = [json.loads(line) for line in
               (tmp_path / "decision_trace.jsonl").read_text(encoding="utf-8").splitlines()]
    assert any(record.get("event_type") == "result_record" for record in records)
    assert all(record.get("event_type") != "result" for record in records)
    assert advice["command"] in {"state", "wait", "play", "end", "choose", "skip", "rest"}


def test_first_advice_is_not_recorded_as_replacement_but_real_change_is(tmp_path):
    from spirebrain.driver.advisor import AdviseSession
    from spirebrain.driver.witness import Advice

    class Agent:
        def __init__(self):
            self.trace = DecisionTrace(tmp_path / "trace.jsonl", config_id="cfg")
            self.feed = None

        def record_advice_history(self, advice, *, displayed=True):
            return None

    session = AdviseSession(Agent(), advice_path=None, poll_frames=20,
                            warn_stream=io.StringIO(), ensure_offered=lambda *a, **k: "wait 20",
                            fingerprint=lambda state: "fp", message_count=lambda: 1)
    first = Advice(point="combat", screen="COMBAT", command={"command": "wait"},
                   key=("combat", "wait"), decision_id="d", state_id="s",
                   run_id="r", run_epoch=1, advice_revision=1)
    # Exercise production order: tracker.note happens before the publish hook.
    session.tracker.pending = first
    session._publish_advice(first)
    records = [json.loads(line) for line in (tmp_path / "trace.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [item["event_type"] for item in records] == ["advice"]
    second = Advice(point="combat", screen="COMBAT", command={"command": "state"},
                    key=("combat", "state"), decision_id="d", state_id="s",
                    run_id="r", run_epoch=1, advice_revision=2)
    session.tracker.pending = second
    session._publish_advice(second)
    session.tracker.pending = second
    session._publish_advice(second)  # duplicate revision is not another replacement
    records = [json.loads(line) for line in (tmp_path / "trace.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [item["event_type"] for item in records].count("advice_replaced") == 1
    assert records[0]["event_type"] == "advice"
    replacement = next(item for item in records if item["event_type"] == "advice_replaced")
    assert replacement["previous_advice_revision"] == 1
    assert replacement["advice_revision"] == 2


def test_decision_chain_marks_missing_legacy_identity_as_unknown():
    legacy = {"event_type": "decision", "decision_id": "old-decision",
              "command": {"command": "wait"}}

    chain = decision_chain([legacy], "old-decision")

    record = chain["decisions"][0]
    assert record["trace_quality"] == "legacy_partial"
    assert "schema_version" in record["unknown_fields"]
    assert "run_id" in record["unknown_fields"]
    assert "source_type" in record["unknown_fields"]


def test_decision_chain_surfaces_unattributed_legacy_events_without_linking_them():
    legacy = {"event_type": "jev_call", "request_id": "legacy-request",
              "state_id": "legacy-state"}

    chain = decision_chain([legacy], "current-decision")

    assert chain["requests"] == []
    assert chain["unattributed"][0]["request_id"] == "legacy-request"
    assert chain["unattributed"][0]["trace_quality"] == "legacy_partial"
    assert chain["unattributed"][0]["attribution"] == "unknown"


def test_postmortem_reports_unattributed_events_as_unlinked_evidence():
    from spirebrain.analysis.postmortem import review_decision

    report = review_decision([
        {"event_type": "provider_request", "request_id": "old-request"},
    ], "decision-current")

    assert report["unattributed_legacy_event_count"] == 1
    assert report["unattributed_legacy_events"][0]["attribution"] == "unknown"
    assert report["requests"] == []


def test_provider_attempts_for_one_decision_are_counted_and_joined_once(tmp_path):
    trace = DecisionTrace(tmp_path / "trace.jsonl", config_id="cfg")
    client = LoggingStrategicClient(
        MockStrategicClient(), tmp_path / "brain.jsonl", event_sink=trace.record,
    )
    payload = {
        "run_id": "run-a", "run_epoch": 1, "decision_id": "decision-a",
        "state_id": "state-a", "config_id": "cfg", "candidates": [],
        "state": {"screen_type": "EVENT", "screen_state": {"options": ["a", "b"]}},
    }

    first = client.plan(payload)
    second = client.plan(payload)

    assert first.request_id and second.request_id and first.request_id != second.request_id
    chain = trace.find_decision("decision-a")
    assert len(chain["requests"]) == 2
    assert len(chain["responses"]) == 2
    assert {item["request_id"] for item in chain["requests"]} == {
        item["request_id"] for item in chain["responses"]
    }
    assert all(item["run_epoch"] == 1 and item["state_id"] == "state-a"
               for item in chain["requests"] + chain["responses"])


def test_provider_failure_has_a_terminal_trace_event(tmp_path):
    class BrokenProvider:
        backend_name = "offline-fixture"

        def plan(self, payload):
            raise TimeoutError("fixture timeout")

    trace = DecisionTrace(tmp_path / "trace.jsonl", config_id="cfg")
    client = LoggingStrategicClient(
        BrokenProvider(), tmp_path / "brain.jsonl", event_sink=trace.record,
    )
    response = client.plan({"run_id": "r", "decision_id": "d", "state_id": "s",
                            "candidates": [], "state": {}})

    chain = trace.find_decision("d")
    assert response.fallback is True
    assert len(chain["requests"]) == len(chain["responses"]) == 1
    assert chain["requests"][0]["request_id"] == chain["responses"][0]["request_id"]
    assert "TimeoutError" in chain["responses"][0]["error"]


def test_runtime_provider_events_share_final_decision_and_state_identity(tmp_path):
    from spirebrain.driver.agent import SpireBrainAgent

    agent = SpireBrainAgent(jev_backend="mock", brain_backend="mock",
                            async_planning=False, log_dir=tmp_path)
    state = {
        "run_id": "trace-run", "character": "IRONCLAD", "screen_type": "MAP",
        "act": 1, "floor": 1, "current_hp": 40, "max_hp": 80,
        "available_commands": ["choose", "wait", "state"],
        "map": {"next_nodes": [
            {"x": 0, "y": 1, "symbol": "M"},
            {"x": 1, "y": 1, "symbol": "E"},
        ]},
    }

    agent.choose_action(state)
    chain = agent.trace.find_decision(agent._active_decision_id)
    assert len(chain["decisions"]) == 1
    assert len(chain["requests"]) >= 2  # strategic planning plus local JEV choice
    decision = chain["decisions"][0]
    assert all(event["decision_id"] == decision["decision_id"]
               for event in chain["requests"] + chain["responses"])
    assert all(event["state_id"] == decision["state_id"]
               for event in chain["requests"] + chain["responses"])
    assert len({event["request_id"] for event in chain["requests"]}) == len(chain["requests"])
    jev_request_ids = agent._logged_jev_client.request_ids_for_decision(
        agent._active_decision_id)
    brain_request_ids = agent.strategic.provider.request_ids_for_decision(
        agent._active_decision_id)
    assert set(decision["provider_request_ids"]) == set(jev_request_ids + brain_request_ids)
    assert all(not event.get("contract_errors")
               for event in chain["requests"] + chain["responses"])


def test_strict_provider_events_require_complete_attempt_identity():
    event = make_event("provider_request", fields={
        "request_id": "request-1", "provider": "fixture",
    })

    errors = validate_event(event, strict=True)

    assert "invalid:attempt_id" in errors


def test_provider_events_allow_unknown_identity_but_keep_request_ids_strict():
    event = make_event("provider_request", fields={
        "request_id": "request-1", "logical_request_id": "logical-1",
        "attempt_id": "attempt-1", "provider": "fixture",
    })

    assert validate_event(event, strict=True) == []


def test_jev_provider_exception_still_emits_response_event(tmp_path):
    from spirebrain.jev_brain.client import ChoiceSpec
    from spirebrain.jev_brain.logging_client import LoggingJevClient

    class BrokenJev:
        backend_name = "offline-je v-fixture"

        def ask(self, state, questions):
            raise TimeoutError("fixture timeout")

    events = []
    client = LoggingJevClient(BrokenJev(), log_dir=tmp_path,
                              event_sink=lambda kind, fields: events.append((kind, fields)))
    client.context_provider = lambda: {
        "run_id": "run", "run_epoch": 2, "decision_id": "decision",
        "state_id": "state", "config_id": "cfg", "generation": 7,
        "trigger": "combat_start",
    }

    response = client.ask({}, {"pick": ChoiceSpec("choose", {"a": "A"})})

    assert response.fallback is True
    assert response.error_kind == "timeout"
    assert [kind for kind, _ in events] == ["provider_request", "provider_response"]
    assert events[0][1]["request_id"] == events[1][1]["request_id"]
    assert events[1][1]["decision_id"] == "decision"
    assert events[0][1]["generation"] == events[1][1]["generation"] == 7
    assert events[0][1]["trigger"] == events[1][1]["trigger"] == "combat_start"


def test_jev_api_error_keeps_provider_error_kind_in_trace(tmp_path):
    from spirebrain.jev_brain.client import ChoiceSpec
    from spirebrain.jev_brain.client_real import JevApiError
    from spirebrain.jev_brain.logging_client import LoggingJevClient

    class BrokenJev:
        backend_name = "openrouter-jev"

        def ask(self, state, questions):
            raise JevApiError("network failure: URLError", error_kind="network_error")

    client = LoggingJevClient(BrokenJev(), log_dir=tmp_path)
    response = client.ask({}, {"pick": ChoiceSpec("choose", {"a": "A"})})

    assert response.fallback is True
    assert response.error_kind == "network_error"
    record = json.loads((tmp_path / "jev_calls.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert record["error_kind"] == "network_error"
    assert "Authorization" not in json.dumps(record)


def test_jev_wrapper_preserves_native_request_id_and_internal_link(tmp_path):
    from spirebrain.jev_brain.client import ChoiceSpec, JevResponse
    from spirebrain.jev_brain.logging_client import LoggingJevClient

    class Provider:
        backend_name = "fixture-jev"

        def ask(self, state, questions):
            return JevResponse(backend=self.backend_name,
                               provider_request_id="native-jev-42")

    events = []
    client = LoggingJevClient(Provider(), log_dir=tmp_path,
                              event_sink=lambda kind, fields: events.append((kind, fields)))
    client.context_provider = lambda: {
        "run_id": "run", "run_epoch": 1, "decision_id": "decision",
        "state_id": "state", "config_id": "cfg",
    }

    response = client.ask({}, {})

    assert response.request_id
    assert response.provider_request_id == "native-jev-42"
    assert events[-1][1]["request_id"] == response.request_id
    assert events[-1][1]["provider_request_id"] == "native-jev-42"


def test_decision_chain_replay_verifies_jev_input_and_response_blobs(tmp_path):
    from spirebrain.analysis.replay import replay_decision_chain
    from spirebrain.driver.trace import DecisionTrace
    from spirebrain.jev_brain.client import ChoiceSpec, MockJevClient
    from spirebrain.jev_brain.logging_client import LoggingJevClient

    trace = DecisionTrace(tmp_path / "decision_trace.jsonl", config_id="cfg")
    client = LoggingJevClient(MockJevClient(), log_dir=tmp_path,
                              event_sink=trace.record)
    client.context_provider = lambda: {
        "run_id": "run", "run_epoch": 1, "decision_id": "decision",
        "state_id": "state", "config_id": "cfg",
    }
    client.ask({"screen": "EVENT", "options": ["a", "b"]},
               {"choice": ChoiceSpec("choose", {"a": "A", "b": "B"})})

    chain = replay_decision_chain(tmp_path / "decision_trace.jsonl", "decision")

    assert chain["replay_blobs_verified"] == 2
    assert {item["field"] for item in chain["replay_blobs"]} == {
        "state_blob", "response_blob"
    }


def test_decision_chain_replay_rejects_missing_provider_blob(tmp_path):
    from spirebrain.analysis.replay import replay_decision_chain

    trace = tmp_path / "decision_trace.jsonl"
    base = {
        "schema_version": 2, "run_id": "r", "run_epoch": 1,
        "decision_id": "d", "state_id": "s", "request_id": "q",
        "logical_request_id": "q", "attempt_id": "q:1", "plan_id": None,
        "advice_revision": None, "config_id": "c",
    }
    request = {**base, "event_type": "provider_request", "timestamp": 1,
               "provider": "fixture"}
    response = {**base, "event_type": "provider_response", "timestamp": 2,
                "provider": "fixture", "latency_ms": 1,
                "error_kind": "", "fallback": False}
    trace.write_text(json.dumps(request) + "\n" + json.dumps(response) + "\n",
                     encoding="utf-8")

    try:
        replay_decision_chain(trace, "d")
    except ValueError as exc:
        assert "missing_provider_blob" in str(exc)
    else:
        raise AssertionError("missing provider blobs must fail replay")


def test_replay_cli_can_verify_one_decision_chain(tmp_path, capsys):
    from spirebrain.analysis.replay import main
    from spirebrain.driver.trace import DecisionTrace
    from spirebrain.jev_brain.client import ChoiceSpec, MockJevClient
    from spirebrain.jev_brain.logging_client import LoggingJevClient

    trace = DecisionTrace(tmp_path / "decision_trace.jsonl", config_id="cfg")
    client = LoggingJevClient(MockJevClient(), log_dir=tmp_path,
                              event_sink=trace.record)
    client.context_provider = lambda: {
        "run_id": "run", "run_epoch": 1, "decision_id": "cli-decision",
        "state_id": "state", "config_id": "cfg",
    }
    client.ask({"screen": "EVENT"}, {"choice": ChoiceSpec("choose", {"a": "A"})})

    assert main(["--input", str(tmp_path / "decision_trace.jsonl"),
                 "--decision-id", "cli-decision"]) == 0
    assert '"replay_blobs_verified": 2' in capsys.readouterr().out


def test_replay_cli_rejects_missing_decision_id(tmp_path, capsys):
    from spirebrain.analysis.replay import main

    trace = tmp_path / "decision_trace.jsonl"
    trace.write_text("", encoding="utf-8")

    assert main(["--input", str(trace), "--decision-id", "missing"]) == 2
    assert '"error": "invalid_decision_replay"' in capsys.readouterr().out


def test_replay_execute_reconstructs_candidates_and_checks_legality(tmp_path):
    from spirebrain.analysis.replay import replay_decision
    from spirebrain.driver.agent import SpireBrainAgent

    agent = SpireBrainAgent(jev_backend="mock", brain_backend="mock",
                            async_planning=False, log_dir=tmp_path)
    state = {
        "run_id": "replay-run", "character": "IRONCLAD", "screen_type": "MAP",
        "act": 1, "floor": 1, "current_hp": 40, "max_hp": 80,
        "available_commands": ["choose", "wait", "state"],
        "map": {"next_nodes": [
            {"x": 0, "y": 1, "symbol": "M"},
            {"x": 1, "y": 1, "symbol": "E"},
        ]},
    }
    agent.choose_action(state)

    result = replay_decision(tmp_path / "decision_trace.jsonl", agent._active_decision_id)

    execution = result["replay_execution"]
    assert execution["offline"] is True
    assert execution["provider_calls"] == 0
    assert execution["candidate_count"] >= 1
    assert execution["recorded_command_safe"] is True
    assert execution["replayed_command_safe"] is True


def test_replay_uses_latest_published_advice_after_decision(tmp_path):
    from spirebrain.analysis.replay import replay_decision
    from spirebrain.driver.agent import SpireBrainAgent

    agent = SpireBrainAgent(jev_backend="mock", brain_backend="mock",
                            async_planning=False, log_dir=tmp_path)
    state = {
        "run_id": "replay-advice-run", "character": "IRONCLAD", "screen_type": "MAP",
        "act": 1, "floor": 1, "current_hp": 40, "max_hp": 80,
        "available_commands": ["choose", "wait", "state"],
        "map": {"next_nodes": [{"x": 0, "y": 1, "symbol": "M"}]},
    }
    agent.choose_action(state)
    decision_id = agent._active_decision_id
    # Simulate a later published revision. It must win over the earlier owner
    # decision when replay reconstructs what the player actually saw.
    assert agent.trace.record("advice", {
        "decision_id": decision_id,
        "state_id": agent.history[-1].get("state_id") or "",
        "advice_revision": 2,
        "command": {"command": "state"},
        "source_type": "rule_fallback",
    })

    result = replay_decision(tmp_path / "decision_trace.jsonl", decision_id)
    execution = result["replay_execution"]
    assert execution["final_event_type"] == "advice"
    assert execution["advice_revision"] == 2
    assert execution["recorded_command"] == {"command": "state"}


def test_replay_cli_execute_runs_offline_reconciliation(tmp_path, capsys):
    from spirebrain.analysis.replay import main
    from spirebrain.driver.agent import SpireBrainAgent

    agent = SpireBrainAgent(jev_backend="mock", brain_backend="mock",
                            async_planning=False, log_dir=tmp_path)
    state = {
        "run_id": "cli-replay-run", "character": "IRONCLAD", "screen_type": "MAP",
        "act": 1, "floor": 1, "current_hp": 40, "max_hp": 80,
        "available_commands": ["choose", "wait", "state"],
        "map": {"next_nodes": [{"x": 0, "y": 1, "symbol": "M"}]},
    }
    agent.choose_action(state)

    assert main(["--input", str(tmp_path / "decision_trace.jsonl"),
                 "--decision-id", agent._active_decision_id, "--execute"]) == 0
    output = capsys.readouterr().out
    assert '"offline": true' in output
    assert '"provider_calls": 0' in output


def test_strict_event_contract_checks_types_and_event_semantics():
    identity = {"run_id": "r", "run_epoch": 1, "decision_id": "d",
                "state_id": "s", "request_id": "q", "plan_id": "p",
                "advice_revision": 1, "config_id": "c",
                "logical_request_id": "q", "attempt_id": "q:1"}
    request = make_event("provider_request", identity=identity,
                         fields={"provider": "mock", "model": "fixture"})
    assert validate_event(request, strict=True) == []

    invalid = dict(request, run_epoch="one", provider=12)
    errors = validate_event(invalid, strict=True)
    assert "invalid:run_epoch" in errors
    assert "invalid:provider" in errors

    missing = make_event("advice", identity=identity,
                         fields={"command": {"command": "wait"}})
    missing.pop("advice_revision", None)
    assert "missing:advice_revision" in validate_event(missing, strict=True)

    response = make_event("provider_response", identity=identity,
                          fields={"provider": "mock", "model": "fixture",
                                  "latency_ms": 1, "error_kind": "",
                                  "fallback": False, "provider_request_id": 42})
    assert "invalid:provider_request_id" in validate_event(response, strict=True)


def test_expired_result_contract_validates_pending_identity_types():
    event = make_event("expired_result", fields={
        "expired_result": True, "fallback_reason": "superseded_request",
        "pending_state_id": "state-new", "pending_generation": 3,
        "pending_run_id": "run-new", "generation": 2, "trigger": "screen_change",
    })
    assert validate_event(event, strict=True) == []

    invalid = dict(event, pending_generation="three")
    assert "invalid:pending_generation" in validate_event(invalid, strict=True)


def test_trace_schema_and_secret_redaction(tmp_path):
    path = tmp_path / "trace.jsonl"
    trace = DecisionTrace(path, config_id="cfg")
    assert trace.record("provider_request", {"decision_id": "d", "api_key": "secret"})
    record = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert record["schema_version"] == 2
    assert isinstance(record["timestamp"], float)
    assert "secret" not in json.dumps(record)
    assert trace.find_decision("d")["requests"]


def test_trace_sequences_are_unique_across_writers_for_same_file(tmp_path):
    path = tmp_path / "trace.jsonl"
    owner = DecisionTrace(path, config_id="cfg")
    worker = DecisionTrace(path, config_id="cfg")

    assert owner.record("decision", {"decision_id": "owner"})
    assert worker.record("provider_request", {"decision_id": "worker"})

    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert [record["trace_sequence"] for record in records] == [1, 2]


def test_trace_state_candidate_and_response_blob_references_are_verifiable(tmp_path):
    from spirebrain.analysis.replay_blob import ReplayBlobStore

    path = tmp_path / "trace.jsonl"
    trace = DecisionTrace(path, config_id="cfg")
    assert trace.record("decision", {
        "decision_id": "d", "state_blob": {"screen": "COMBAT"},
        "candidate_blob": [{"candidate_id": "play:0"}],
        "response_blob": {"plan_id": "p", "api_key": "secret"},
    })
    event = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    store = ReplayBlobStore(tmp_path / "decision_blobs")
    assert store.get(event["state_blob"]) == {"screen": "COMBAT"}
    assert store.get(event["response_blob"])["api_key"] == "[redacted]"


def test_replay_provider_is_fixed_and_offline():
    response = {"plan_id": "p", "current_objective": "survive",
                "long_term_goal": "finish", "priority": ["survive"],
                "preferred_candidates": [], "avoid_candidates": [],
                "resource_constraints": {}, "next_steps": [],
                "replan_triggers": [], "reason": "fixture",
                "uncertainty": "", "expires_after": 1}
    client = ReplayStrategicClient([response])
    result = client.plan({"state_id": "s", "run_id": "r"})
    assert isinstance(result, BrainResponse)
    assert result.plan is not None
    assert client.calls == 1


def test_provider_events_share_trace_identity(tmp_path):
    events = []
    client = LoggingStrategicClient(MockStrategicClient(), tmp_path / "brain.jsonl",
                                    event_sink=lambda kind, fields: events.append((kind, fields)))
    client.plan({"state_id": "s", "run_id": "r", "decision_id": "d",
                 "run_epoch": 1, "plan_id": "p", "config_id": "c",
                 "candidates": [], "max_plan_steps": 2,
                 "knowledge_context": {"citation_ids": ["source-a"]}})
    assert [kind for kind, _ in events] == ["provider_request", "provider_response"]
    assert all(fields["request_id"] for _, fields in events)
    assert all(fields["state_id"] == "s" and fields["run_id"] == "r" for _, fields in events)
    assert events[0][1]["knowledge_citation_ids"] == ["source-a"]
    assert client.request_ids_for_decision("d") == [events[0][1]["request_id"]]


def test_fixed_and_timing_fixtures_are_offline():
    root = __import__("pathlib").Path(__file__).parent / "fixtures" / "replay"
    assert load_fixed_responses(root / "fixed_model_responses.json")
    report = run_timing_fixture(root / "fault_timing.json")
    assert report["passed"] and report["expired_result_count"] == 1
    assert report["provider_request_count"] == 1


def test_async_scheduler_fixture_drives_owner_commit_and_discards_old_plan():
    root = __import__("pathlib").Path(__file__).parent / "fixtures" / "replay"
    report = run_async_scheduler_fixture(root / "async_scheduler.json")
    assert report["passed"] is True
    assert report["provider_calls"] == 2
    assert report["committed_plan_ids"] == ["scheduler-plan-2"]
    assert report["provider_decision_ids"] == ["decision-a", "decision-b"]
    assert report["committed_decision_ids"] == ["decision-b"]
    assert report["expired_result_count"] >= 1


def test_async_scheduler_fault_fixture_stays_offline_and_classifies_timeout():
    from spirebrain.analysis.replay import run_async_scheduler_fixture

    root = __import__("pathlib").Path(__file__).parent / "fixtures" / "replay"
    report = run_async_scheduler_fixture(root / "fault_scheduler.json")

    assert report["passed"] is True
    assert report["provider_calls"] == 1
    assert report["provider_error_kinds"] == ["timeout"]
    assert report["provider_error_counts"] == {"timeout": 1}
    assert report["committed_plan_ids"] == []


def test_async_scheduler_replay_cli_is_explicit_and_offline(tmp_path, capsys):
    from spirebrain.analysis.replay import main

    source = (__import__("pathlib").Path(__file__).parent /
              "fixtures" / "replay" / "async_scheduler.json")
    assert main(["--input", str(source), "--async-scheduler"]) == 0
    assert '"provider_calls": 2' in capsys.readouterr().out


def test_timing_replay_rejects_untyped_events_and_missing_causal_evidence(tmp_path):
    from spirebrain.analysis.replay import run_timing_fixture

    unknown = tmp_path / "unknown.json"
    unknown.write_text(json.dumps({
        "fixture_type": "synthetic", "kind": "scheduler_scenario",
        "events": [{"event_type": "typo"}],
    }), encoding="utf-8")
    try:
        run_timing_fixture(unknown)
    except ValueError as exc:
        assert "unknown event_type" in str(exc)
    else:
        raise AssertionError("unknown scheduler events must fail validation")

    orphan = tmp_path / "orphan.json"
    orphan.write_text(json.dumps({
        "fixture_type": "synthetic", "kind": "scheduler_scenario",
        "events": [
            {"event_type": "provider_response", "request_id": "missing"},
        ],
    }), encoding="utf-8")
    try:
        run_timing_fixture(orphan)
    except ValueError as exc:
        assert "unknown request_id" in str(exc)
    else:
        raise AssertionError("orphan responses must fail validation")

    no_change = tmp_path / "no_change.json"
    no_change.write_text(json.dumps({
        "fixture_type": "synthetic", "kind": "scheduler_scenario",
        "events": [
            {"event_type": "provider_request", "request_id": "q", "state_id": "s"},
            {"event_type": "expired_result", "request_id": "q", "state_id": "s",
             "fallback_reason": "timeout"},
        ],
    }), encoding="utf-8")
    try:
        run_timing_fixture(no_change)
    except ValueError as exc:
        assert "state-change evidence" in str(exc)
    else:
        raise AssertionError("expired results need a state-change witness")


def test_timing_replay_rejects_duplicate_provider_response(tmp_path):
    from spirebrain.analysis.replay import run_timing_fixture

    duplicate = tmp_path / "duplicate_response.json"
    duplicate.write_text(json.dumps({
        "fixture_type": "synthetic", "kind": "scheduler_scenario",
        "events": [
            {"event_type": "provider_request", "request_id": "r", "state_id": "s"},
            {"event_type": "provider_response", "request_id": "r"},
            {"event_type": "provider_response", "request_id": "r"},
        ],
    }), encoding="utf-8")
    try:
        run_timing_fixture(duplicate)
    except ValueError as exc:
        assert "duplicate provider_response" in str(exc)
    else:
        raise AssertionError("duplicate provider responses must fail validation")


def test_timing_replay_rejects_duplicate_expired_result(tmp_path):
    from spirebrain.analysis.replay import run_timing_fixture

    duplicate = tmp_path / "duplicate_expired.json"
    duplicate.write_text(json.dumps({
        "fixture_type": "synthetic", "kind": "scheduler_scenario",
        "events": [
            {"event_type": "state_observed", "state_id": "a"},
            {"event_type": "provider_request", "request_id": "r", "state_id": "a"},
            {"event_type": "state_observed", "state_id": "b"},
            {"event_type": "expired_result", "request_id": "r", "state_id": "b",
             "fallback_reason": "superseded_request"},
            {"event_type": "expired_result", "request_id": "r", "state_id": "b",
             "fallback_reason": "superseded_request"},
        ],
    }), encoding="utf-8")
    try:
        run_timing_fixture(duplicate)
    except ValueError as exc:
        assert "duplicate expired_result" in str(exc)
    else:
        raise AssertionError("duplicate expired results must fail validation")
