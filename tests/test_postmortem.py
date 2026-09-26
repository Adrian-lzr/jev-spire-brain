import json

from spirebrain.analysis.postmortem import release_report, review_decision


def _records():
    return [
        {"event_type": "state_observed", "decision_id": "d1", "run_id": "r1", "state_id": "s1"},
        {"event_type": "provider_request", "decision_id": "d1", "request_id": "q1"},
        {"event_type": "decision", "decision_id": "d1", "rule_ids": ["combat.lethal"], "uncertainty": "none"},
        {"event_type": "advice", "decision_id": "d1", "advice_revision": 1},
        {"event_type": "outcome", "decision_id": "d1", "result": "unknown"},
    ]


def test_review_reconstructs_decision_without_inventing_result():
    report = review_decision(_records(), "d1")
    assert report["final_selection"]["decision_id"] == "d1"
    assert report["advice_versions"][0]["advice_revision"] == 1
    assert report["review"]["rules_used"] == ["combat.lethal"]


def test_release_report_keeps_real_result_unknown():
    report = release_report(_records(), commit="abc", config_id="cfg")
    assert report["source"]["sample_count"] == 5
    assert report["environment"]["commit"] == "abc"
    assert report["real_game_results"] == "unknown"
