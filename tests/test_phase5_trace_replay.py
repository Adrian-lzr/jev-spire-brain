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
    session._publish_advice(first)
    session.tracker.pending = first
    second = Advice(point="combat", screen="COMBAT", command={"command": "state"},
                    key=("combat", "state"), decision_id="d", state_id="s",
                    run_id="r", run_epoch=1, advice_revision=2)
    session._publish_advice(second)
    records = [json.loads(line) for line in (tmp_path / "trace.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [item["event_type"] for item in records].count("advice_replaced") == 1
    assert records[0]["event_type"] == "advice"


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
                 "candidates": [], "max_plan_steps": 2})
    assert [kind for kind, _ in events] == ["provider_request", "provider_response"]
    assert all(fields["request_id"] for _, fields in events)
    assert all(fields["state_id"] == "s" and fields["run_id"] == "r" for _, fields in events)


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
    assert report["expired_result_count"] >= 1


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
