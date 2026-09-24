import json
from pathlib import Path


ROOT = Path(__file__).parent


def test_synthetic_replay_keeps_advise_commands_safe():
    from spirebrain.analysis.replay import run_fixture

    result = run_fixture(ROOT / "fixtures" / "replay" / "advisor_states.json")
    assert result["fixture_type"] == "synthetic"
    assert result["passed"] is True
    assert result["invalid_advise_commands"] == []
    assert set(result["advise_commands"]) <= {"wait", "state"}


def test_metrics_contract_counts_provider_request_and_unknown_outcome():
    from spirebrain.analysis.metrics import load_records, summarize

    path = ROOT / "fixtures" / "metrics" / "decision_trace.jsonl"
    report = summarize(load_records(path), source=str(path))
    assert report["provider_calls"] == 1
    assert report["decision_count"] == 2
    assert report["legal_action_rate"] == 0.5
    assert report["unobserved_ratio"] == 1.0
    assert report["real_outcomes"] == "unknown"
    # Metrics must never echo request bodies or credentials.  This fixture is
    # intentionally the only source rendered by the assertion.
    assert "Authorization" not in json.dumps(report)
