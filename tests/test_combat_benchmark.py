"""Regression tests for the deterministic Ironclad combat benchmark."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from spirebrain.sim.combat_benchmark import (
    benchmark_fixture,
    load_benchmark,
    main,
)


FIXTURE = Path(__file__).parent / "fixtures" / "sim" / "combat_benchmark.json"


def test_benchmark_fixture_is_explicit_synthetic_and_has_four_contracts():
    fixture = load_benchmark(FIXTURE)

    assert fixture["fixture_type"] == "synthetic"
    assert fixture["kind"] == "combat_benchmark"
    assert {case["objective"] for case in fixture["cases"]} == {
        "lethal", "cover", "target", "uncertain",
    }
    assert len(fixture["cases"]) == 4


def test_all_depth_arms_are_legal_and_meet_their_local_objectives():
    report = benchmark_fixture(FIXTURE)

    assert report["sample_count"] == 4
    assert set(report["arms"]) == {"depth1", "depth2", "depth3"}
    assert report["quality"] == {
        "status": "synthetic_objective_only", "win_rate": "unknown",
    }
    assert report["real_outcomes"] == "unknown"
    for arm in report["arms"].values():
        assert arm["sample_count"] == 4
        assert arm["legal_action_rate"] == 1.0
        assert arm["objective_coverage"] == 1.0
        assert arm["unknown_effect_rate"] > 0.0
        assert all(case["legal"] for case in arm["cases"])
        assert all(case["objective_met"] for case in arm["cases"])


def test_benchmark_captures_structured_facts_and_unknown_effect_fallback():
    report = benchmark_fixture(FIXTURE)
    cases = report["arms"]["depth2"]["cases"]
    by_id = {case["id"]: case for case in cases}

    lethal = by_id["lethal_over_block"]
    assert lethal["facts"]["lethal_confirmed"] is True
    assert lethal["facts"]["kills_target"] == "cultist-a"
    assert lethal["reason_code"] == "bounded_search"

    cover = by_id["cover_without_lethal"]
    assert cover["facts"]["incoming_threat_covered"] is True
    assert cover["facts"]["lethal_confirmed"] is False

    target = by_id["highest_threat_target"]
    assert target["target_id"] == "strong-attacker"
    assert target["command"]["target"] == 1

    unknown = by_id["unknown_effect_degrades"]
    assert unknown["uncertainty"] == "unknown_effect"
    assert unknown["reason_code"] == "greedy_fallback"
    assert unknown["confidence"] == "estimated"


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"fixture_type": "synthetic", "kind": "combat_benchmark"},
        {"fixture_type": "synthetic", "kind": "wrong", "cases": []},
        {"fixture_type": "unknown", "kind": "combat_benchmark", "cases": [{}]},
        {"fixture_type": "synthetic", "kind": "combat_benchmark", "cases": [{}]},
    ],
)
def test_empty_or_malformed_fixture_fails(tmp_path, payload):
    path = tmp_path / "invalid.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError):
        load_benchmark(path)


def test_missing_fixture_fails_instead_of_reporting_zero_samples(tmp_path):
    with pytest.raises(ValueError):
        load_benchmark(tmp_path / "does-not-exist.json")


def test_cli_returns_nonzero_for_invalid_fixture(tmp_path, capsys):
    path = tmp_path / "empty.json"
    path.write_text(json.dumps({"fixture_type": "synthetic", "kind": "combat_benchmark",
                                "cases": []}), encoding="utf-8")

    assert main(["--input", str(path)]) == 2
    output = json.loads(capsys.readouterr().out)
    assert output["error"] == "invalid_combat_benchmark"


def test_empty_configuration_set_is_rejected():
    with pytest.raises(ValueError, match="at least one"):
        benchmark_fixture(FIXTURE, configs={})


def test_offline_gate_rejects_a_benchmark_claiming_win_rate():
    from tools.verify_offline import _validate_combat_benchmark_output

    result = _validate_combat_benchmark_output({
        "passed": True,
        "output": json.dumps({
            "sample_count": 1,
            "arms": {"rules": {
                "sample_count": 1,
                "legal_action_rate": 1.0,
                "objective_coverage": 1.0,
            }},
            "quality": {"win_rate": 0.75},
            "real_outcomes": "unknown",
        }),
    })

    assert result["passed"] is False
    assert result["validation_error"] == "combat_benchmark_win_rate_must_be_unknown"
