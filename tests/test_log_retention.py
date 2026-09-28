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
