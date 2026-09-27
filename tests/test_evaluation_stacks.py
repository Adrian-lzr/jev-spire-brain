import json
from pathlib import Path

import pytest

from spirebrain.analysis.evaluate import evaluate_fixture
from spirebrain.analysis.replay import run_fixture, load_fixed_responses


FIXTURE = Path(__file__).parent / "fixtures/replay/advisor_states.json"
REPRESENTATIVE = Path(__file__).parent / "fixtures/replay/representative_states.json"


def test_four_modes_execute_distinct_offline_stacks():
    modes = evaluate_fixture(FIXTURE)["modes"]
    assert (modes["rules"]["jev_calls"], modes["rules"]["strategic_calls"]) == (0, 0)
    assert modes["jev"]["jev_calls"] > 0 and modes["jev"]["strategic_calls"] == 0
    assert modes["strategic"]["jev_calls"] == 0 and modes["strategic"]["strategic_calls"] > 0
    assert modes["full"]["jev_calls"] > 0 and modes["full"]["strategic_calls"] > 0
    for mode in modes.values():
        assert mode["sample_count"] == 3
        assert mode["decision_count"] == 2
        assert mode["provider_calls"] == mode["jev_calls"] + mode["strategic_calls"]
        assert mode["candidate_legality"]["checked"] > 0
        assert mode["advise_wire_safety"] is True
        assert mode["real_outcomes"] == "unknown"
        assert mode["game_rejections"] is None


def test_representative_replay_has_thirty_distinct_states_and_real_provider_counts():
    modes = evaluate_fixture(REPRESENTATIVE)["modes"]
    assert all(report["sample_count"] >= 30 for report in modes.values())
    assert all(report["decision_count"] >= 30 for report in modes.values())
    assert modes["rules"]["provider_calls"] == 0
    assert modes["jev"]["jev_calls"] > 0
    assert modes["strategic"]["strategic_calls"] > 0
    assert modes["full"]["provider_calls"] == (
        modes["full"]["jev_calls"] + modes["full"]["strategic_calls"])
    assert all(report["real_outcomes"] == "unknown" for report in modes.values())


@pytest.mark.parametrize("payload", [[], {},
    {"fixture_type": "synthetic", "messages": [{}]},
    {"fixture_type": "synthetic", "kind": "unknown", "messages": [{}]},
    {"fixture_type": "synthetic", "kind": "state_sequence", "messages": []},
    {"fixture_type": "synthetic", "kind": "state_sequence", "messages": [None]},
])
def test_empty_or_untyped_fixtures_fail(tmp_path, payload):
    path = tmp_path / "invalid.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError):
        run_fixture(path)


def test_missing_fixed_responses_fail(tmp_path):
    path = tmp_path / "empty.json"
    path.write_text(json.dumps({"fixture_type": "synthetic", "kind": "fixed_provider_responses",
                               "responses": []}), encoding="utf-8")
    with pytest.raises(ValueError):
        load_fixed_responses(path)


def test_cli_fails_when_directory_has_no_state_replay(tmp_path, capsys):
    from spirebrain.analysis.evaluate import main

    (tmp_path / "timing.json").write_text(json.dumps({
        "fixture_type": "synthetic", "kind": "scheduler_scenario",
        "events": [{"event_type": "expired_result"}],
    }), encoding="utf-8")
    assert main(["--input", str(tmp_path)]) == 2
    assert "state_sequence_fixture_missing" in capsys.readouterr().out
