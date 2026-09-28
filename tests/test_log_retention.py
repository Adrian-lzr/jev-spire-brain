from __future__ import annotations

import json


def test_append_bounded_rotates_jsonl_without_truncating_records(tmp_path):
    from spirebrain.logging_utils import append_bounded

    path = tmp_path / "events.jsonl"
    assert append_bounded(path, json.dumps({"seq": 1}) + "\n",
                         max_bytes=20, backup_count=2)
    assert append_bounded(path, json.dumps({"seq": 2}) + "\n",
                         max_bytes=20, backup_count=2)
    assert append_bounded(path, json.dumps({"seq": 3}) + "\n",
                         max_bytes=20, backup_count=2)

    current = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert current == [{"seq": 3}]
    assert json.loads(path.with_name("events.jsonl.1").read_text(encoding="utf-8")) == {"seq": 2}
    assert json.loads(path.with_name("events.jsonl.2").read_text(encoding="utf-8")) == {"seq": 1}
    assert not path.with_name("events.jsonl.3").exists()


def test_trace_replay_reads_rotated_decisions_and_continues_sequence(tmp_path, monkeypatch):
    from spirebrain.driver.trace import DecisionTrace
    from spirebrain.analysis.replay import replay_decision_chain

    monkeypatch.setenv("SPIREBRAIN_LOG_MAX_BYTES", "250")
    monkeypatch.setenv("SPIREBRAIN_LOG_BACKUPS", "2")
    path = tmp_path / "decision_trace.jsonl"
    trace = DecisionTrace(path, config_id="cfg")
    for index in range(1, 4):
        assert trace.record("decision", {
            "decision_id": f"d{index}", "state_id": f"s{index}",
            "command": {"command": "state"}, "source_type": "rule",
        })
    assert trace.find_decision("d1")["decisions"]
    assert replay_decision_chain(path, "d1", require_blobs=False)["decisions"]
    restarted = DecisionTrace(path, config_id="cfg")
    assert restarted.record("decision", {
        "decision_id": "d4", "state_id": "s4",
        "command": {"command": "state"}, "source_type": "rule",
    })
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert max(record["trace_sequence"] for record in records) == 4


def test_logging_clients_use_bounded_files_and_keep_provider_available(tmp_path):
    from spirebrain.driver.trace import DecisionTrace
    from spirebrain.jev_brain.client import ChoiceSpec, MockJevClient
    from spirebrain.jev_brain.logging_client import LoggingJevClient

    client = LoggingJevClient(MockJevClient(), log_dir=tmp_path)
    response = client.ask({}, {"choice": ChoiceSpec("choose", {"a": "A"})})
    assert response.fallback is False

    trace = DecisionTrace(tmp_path / "decision_trace.jsonl", config_id="cfg")
    assert trace.record("state_observed", {"state_id": "s"}) is True

    # The active files exist and can still be parsed; retention is tested with
    # a tiny explicit limit above rather than changing process-wide settings.
    assert json.loads((tmp_path / "jev_calls.jsonl").read_text(encoding="utf-8"))
    assert json.loads((tmp_path / "decision_trace.jsonl").read_text(encoding="utf-8"))


def test_strategic_provider_log_uses_the_same_bounded_rotation(tmp_path, monkeypatch):
    from spirebrain.brain.gpt_client import LoggingStrategicClient
    from spirebrain.brain.protocol import BrainResponse

    class Provider:
        backend_name = "fixture"

        def plan(self, payload):
            return BrainResponse(backend=self.backend_name, request_id=payload.get("request_id", ""))

    monkeypatch.setenv("SPIREBRAIN_LOG_MAX_BYTES", "180")
    monkeypatch.setenv("SPIREBRAIN_LOG_BACKUPS", "2")
    path = tmp_path / "brain_calls.jsonl"
    client = LoggingStrategicClient(Provider(), path)
    for index in range(4):
        client.plan({"run_id": "run", "state_id": f"state-{index}",
                     "decision_id": f"decision-{index}"})

    assert path.exists()
    assert path.with_name("brain_calls.jsonl.1").exists()
    assert path.with_name("brain_calls.jsonl.2").exists()
    assert not path.with_name("brain_calls.jsonl.3").exists()
