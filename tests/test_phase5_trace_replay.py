import json

from spirebrain.analysis.event_contract import decision_chain, make_event, validate_event
from spirebrain.analysis.replay import ReplayStrategicClient, load_fixed_responses, run_timing_fixture
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


def test_trace_schema_and_secret_redaction(tmp_path):
    path = tmp_path / "trace.jsonl"
    trace = DecisionTrace(path, config_id="cfg")
    assert trace.record("provider_request", {"decision_id": "d", "api_key": "secret"})
    record = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert record["schema_version"] == 2
    assert isinstance(record["timestamp"], float)
    assert "secret" not in json.dumps(record)
    assert trace.find_decision("d")["requests"]


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
