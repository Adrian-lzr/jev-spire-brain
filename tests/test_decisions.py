"""Tests for decision modules + logging client (offline, mock-backed)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from spirebrain.jev_brain.client import MockJevClient
from spirebrain.jev_brain.decisions import (
    CardRewardJudge,
    CombatRiskGate,
    EventChooser,
    MapRouter,
)
from spirebrain.jev_brain.logging_client import LoggingJevClient
from spirebrain.tactical.hp_budget import HPBudget


def test_map_router_uses_budget_override():
    jev = MockJevClient()
    hp = HPBudget(act=1, max_hp=80, current_hp=80)
    router = MapRouter(jev, hp)
    d = router.decide(
        {"n1": "normal", "n2": "elite"},
        {"n1": 5, "n2": 70},  # n2 way over budget (56)
    )
    # mock returns first option with 0.55 conf < floor 0.60 -> fallback fires
    assert d.used_fallback is True
    assert d.value == "n1"  # least predicted damage


def test_card_reward_deck_cap():
    jev = MockJevClient()
    judge = CardRewardJudge(jev, deck_size=25, max_cards=25)
    d = judge.decide({"a": "x", "b": "y"})
    assert d.value == "skip" and d.used_fallback is True


def test_combat_risk_low_conf_means_defensive():
    jev = MockJevClient()
    hp = HPBudget(act=1, max_hp=80, current_hp=80)
    gate = CombatRiskGate(jev, hp)
    d = gate.decide("gremlin nob", predicted_damage=30)
    assert d.detail["posture"] == "defensive"


def test_event_chooser_returns_valid_option():
    jev = MockJevClient()
    chooser = EventChooser(jev)
    d = chooser.decide("shrine", {"opt_a": "safe", "opt_b": "risky"})
    assert d.value in ("opt_a", "opt_b")


def test_logging_client_writes_jsonl(tmp_path=None):
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        logged = LoggingJevClient(MockJevClient(), log_dir=td)
        from spirebrain.jev_brain.client import NoulSpec
        logged.ask("state", {"q": NoulSpec(instructions="test?")})
        p = Path(td) / "jev_calls.jsonl"
        assert p.exists()
        rec = json.loads(p.read_text(encoding="utf-8").splitlines()[0])
        assert rec["seq"] == 1 and "q" in rec["answers"]


def test_offline_simulation_runs():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from spirebrain.sim.run_offline import run_one_simulation
    result = run_one_simulation(seed=1)
    assert len(result["acts"]) == 3
    assert result["jev_calls"] > 0
    assert result["deck_end"] >= 10


if __name__ == "__main__":
    test_map_router_uses_budget_override()
    test_card_reward_deck_cap()
    test_combat_risk_low_conf_means_defensive()
    test_event_chooser_returns_valid_option()
    test_logging_client_writes_jsonl()
    test_offline_simulation_runs()
    print("all decisions/sim tests passed")
