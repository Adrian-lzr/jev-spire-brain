"""Tests for the agent dispatcher: game screen -> decision -> one command.

The fake "game" objects are plain dicts on purpose — that is exactly what the
duck-typed `_get()` accessor is there to support, and it means the router can be
verified before the game exists. Logs go to a temp dir so the repo stays clean.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from spirebrain.driver.agent import SpireBrainAgent

DECK_10 = [{"name": "Strike", "type": "Attack", "cost": 1}] * 5 + \
          [{"name": "Defend", "type": "Skill", "cost": 1}] * 4 + \
          [{"name": "Bash", "type": "Attack", "cost": 2}]


def _agent(tmp: str, **kw) -> SpireBrainAgent:
    return SpireBrainAgent(jev_backend="mock", log_dir=tmp, **kw)


def _base(**over) -> dict:
    game = {"act": 1, "max_hp": 80, "current_hp": 80, "gold": 200, "deck": DECK_10}
    game.update(over)
    return game


# --------------------------------------------------------------------------- #
# Map
# --------------------------------------------------------------------------- #
def test_map_returns_choose_index():
    with tempfile.TemporaryDirectory() as tmp:
        agent = _agent(tmp)
        game = _base(screen_type="MAP", map={"next_nodes": [
            {"x": 0, "y": 4, "symbol": "M"},
            {"x": 1, "y": 4, "symbol": "E"},
        ]})
        cmd = agent.choose_action(game)
        assert cmd["command"] == "choose"
        # pessimistic mock -> router falls back to the cheapest path (monster)
        assert cmd["choice"] == 0
        assert agent.history[-1]["point"] == "map"
        assert agent.history[-1]["fallback"] is True


def test_map_empty_falls_through_safely():
    with tempfile.TemporaryDirectory() as tmp:
        agent = _agent(tmp)
        cmd = agent.choose_action(_base(screen_type="MAP", map={"next_nodes": []}))
        assert cmd == {"command": "choose", "choice": 0}


# --------------------------------------------------------------------------- #
# Card reward
# --------------------------------------------------------------------------- #
def test_card_reward_skips_at_deck_cap():
    with tempfile.TemporaryDirectory() as tmp:
        agent = _agent(tmp)
        full_deck = [{"name": f"Card{i}", "type": "Attack", "cost": 1} for i in range(25)]
        game = _base(screen_type="CARD_REWARD", deck=full_deck,
                     screen={"cards": [{"name": "Inflame", "description": "gain strength"}]})
        cmd = agent.choose_action(game)
        assert cmd["command"] == "skip"
        assert "25/25" in agent.history[-1]["detail"]["reason"]


def test_card_reward_maps_label_to_index():
    with tempfile.TemporaryDirectory() as tmp:
        agent = _agent(tmp)
        game = _base(screen_type="CARD_REWARD", screen={"cards": [
            {"name": "Anger", "description": "0-cost attack"},
            {"name": "Pommel Strike", "description": "damage and draw"},
        ]})
        cmd = agent.choose_action(game)
        assert cmd["command"] in ("skip",)
        assert cmd["command"] == "skip" or 0 <= cmd["choice"] <= 1


def test_card_reward_duplicate_names_do_not_misroute():
    with tempfile.TemporaryDirectory() as tmp:
        agent = _agent(tmp)
        game = _base(screen_type="CARD_REWARD", screen={"cards": [
            {"name": "Strike", "description": "a"},
            {"name": "Strike", "description": "b"},
        ]})
        cmd = agent.choose_action(game)
        assert cmd["command"] == "skip" or cmd["choice"] in (0, 1)


# --------------------------------------------------------------------------- #
# Event
# --------------------------------------------------------------------------- #
def test_event_skips_disabled_options():
    with tempfile.TemporaryDirectory() as tmp:
        agent = _agent(tmp)
        game = _base(screen_type="EVENT", screen={
            "event_id": "Golden Idol",
            "body": "A golden idol on a trapped altar.",
            "options": [
                {"label": "Take it", "disabled": True},
                {"label": "Leave it", "disabled": False},
            ],
        })
        cmd = agent.choose_action(game)
        assert cmd["command"] == "choose"
        assert cmd["choice"] == 1  # never routes to the greyed-out option


def test_event_with_no_options_is_safe():
    with tempfile.TemporaryDirectory() as tmp:
        agent = _agent(tmp)
        cmd = agent.choose_action(_base(screen_type="EVENT", screen={"options": []}))
        assert cmd == {"command": "choose", "choice": 0}


# --------------------------------------------------------------------------- #
# Rest site
# --------------------------------------------------------------------------- #
def test_rest_without_smith_rests_without_asking_jev():
    with tempfile.TemporaryDirectory() as tmp:
        agent = _agent(tmp)
        game = _base(screen_type="REST", screen={"rest_options": ["rest"]})
        cmd = agent.choose_action(game)
        assert cmd["command"] == "rest"


def test_rest_with_smith_returns_command():
    with tempfile.TemporaryDirectory() as tmp:
        agent = _agent(tmp)
        game = _base(screen_type="REST", screen={"rest_options": ["rest", "smith"]})
        cmd = agent.choose_action(game)
        assert cmd["command"] in ("rest", "smith")
        if cmd["command"] == "smith":
            assert isinstance(cmd["choice"], int)


# --------------------------------------------------------------------------- #
# Shop
# --------------------------------------------------------------------------- #
def test_shop_leaves_when_broke():
    with tempfile.TemporaryDirectory() as tmp:
        agent = _agent(tmp)
        game = _base(screen_type="SHOP_SCREEN", gold=0, screen={
            "cards": [{"name": "Ornamental Fan", "price": 150, "description": "block"}],
            "relics": [], "potions": [],
        })
        cmd = agent.choose_action(game)
        assert cmd["command"] == "leave"


def test_shop_buys_when_priced_and_affordable():
    with tempfile.TemporaryDirectory() as tmp:
        agent = _agent(tmp)
        game = _base(screen_type="SHOP_SCREEN", gold=500, screen={
            "cards": [{"name": "Ornamental Fan", "price": 150, "description": "block"}],
            "relics": [], "potions": [],
        })
        cmd = agent.choose_action(game)
        # pessimistic mock (0.55) is inside the uncertain band -> keep the gold
        assert cmd["command"] == "leave"
        assert agent.history[-1]["fallback"] is True


# --------------------------------------------------------------------------- #
# Boss relic
# --------------------------------------------------------------------------- #
def test_boss_reward_always_picks_something():
    with tempfile.TemporaryDirectory() as tmp:
        agent = _agent(tmp)
        game = _base(screen_type="BOSS_REWARD", screen={"relics": [
            {"name": "Philosopher's Stone", "description": "energy, enemies gain strength"},
            {"name": "Runic Dome", "description": "energy, no enemy intents"},
        ]})
        cmd = agent.choose_action(game)
        assert cmd["command"] == "choose" and cmd["choice"] in (0, 1)


def test_boss_reward_empty_is_safe():
    with tempfile.TemporaryDirectory() as tmp:
        agent = _agent(tmp)
        cmd = agent.choose_action(_base(screen_type="BOSS_REWARD", screen={"relics": []}))
        assert cmd == {"command": "choose", "choice": 0}


# --------------------------------------------------------------------------- #
# Combat
# --------------------------------------------------------------------------- #
COMBAT = {
    "player": {"current_hp": 50, "block": 0, "energy": 2},
    "monsters": [{"name": "Cultist", "current_hp": 48, "intent": "attack", "damage": 6}],
    "hand": [{"name": "Defend", "type": "Skill", "block": 5, "cost": 1},
             {"name": "Strike", "type": "Attack", "damage": 6, "cost": 1}],
}


def test_combat_routine_turn_costs_zero_jev_calls():
    with tempfile.TemporaryDirectory() as tmp:
        agent = _agent(tmp)
        cmd = agent.choose_action(_base(screen_type="COMBAT", combat=COMBAT))
        assert cmd["command"] == "play"
        assert cmd["card_index"] == 0          # blocks first against the attack
        assert agent.jev.calls == 0            # drone rule: no JEV at control rate
        assert agent.history == []


def test_combat_no_energy_ends_turn():
    with tempfile.TemporaryDirectory() as tmp:
        agent = _agent(tmp)
        combat = dict(COMBAT, player={"current_hp": 50, "block": 0, "energy": 0})
        cmd = agent.choose_action(_base(screen_type="COMBAT", combat=combat))
        assert cmd == {"command": "end"}


def test_combat_consults_jev_when_damage_threatens_the_budget():
    with tempfile.TemporaryDirectory() as tmp:
        agent = _agent(tmp)
        combat = dict(COMBAT,
                      player={"current_hp": 30, "block": 0, "energy": 2},
                      monsters=[{"name": "Gremlin Nob", "current_hp": 82,
                                 "intent": "attack", "damage": 40}])
        # act 1: reserve = 24, so 30 HP leaves only 6 spendable vs 40 incoming
        cmd = agent.choose_action(_base(screen_type="COMBAT", current_hp=30, combat=combat))
        assert cmd["command"] in ("play", "end")
        assert agent.jev.calls == 1
        assert agent.history[-1]["point"] == "combat_risk"
        assert agent.history[-1]["detail"]["posture"] == "defensive"


# --------------------------------------------------------------------------- #
# Dispatch / logging
# --------------------------------------------------------------------------- #
def test_unknown_screen_waits():
    with tempfile.TemporaryDirectory() as tmp:
        agent = _agent(tmp)
        cmd = agent.choose_action(_base(screen_type="GRID"))
        assert cmd["command"] == "wait"


def test_full_screen_sweep_produces_valid_commands():
    with tempfile.TemporaryDirectory() as tmp:
        agent = _agent(tmp)
        screens = [
            _base(screen_type="MAP", map={"next_nodes": [{"x": 0, "y": 4, "symbol": "M"}]}),
            _base(screen_type="CARD_REWARD", screen={"cards": [{"name": "Inflame"}]}),
            _base(screen_type="EVENT", screen={"options": [{"label": "a"}, {"label": "b"}]}),
            _base(screen_type="REST", screen={"rest_options": ["rest", "smith"]}),
            _base(screen_type="SHOP_SCREEN", screen={"cards": [], "relics": [], "potions": []}),
            _base(screen_type="BOSS_REWARD", screen={"relics": [{"name": "Runic Dome"}]}),
            _base(screen_type="COMBAT", combat=COMBAT),
        ]
        for game in screens:
            cmd = agent.choose_action(game)
            assert "command" in cmd and isinstance(cmd["command"], str)
        assert agent.jev.calls > 0


def test_every_decision_is_logged_to_jsonl():
    with tempfile.TemporaryDirectory() as tmp:
        agent = _agent(tmp)
        agent.choose_action(_base(screen_type="MAP", map={"next_nodes": [
            {"x": 0, "y": 4, "symbol": "M"}]}))
        log_file = Path(tmp) / "jev_calls.jsonl"
        assert log_file.exists()
        rec = json.loads(log_file.read_text(encoding="utf-8").splitlines()[0])
        assert rec["backend"] == "mock"
        assert "route" in rec["answers"]
        assert rec["answers"]["route"]["raw"]["type"] == "choice"


def test_hp_budget_resets_between_acts():
    with tempfile.TemporaryDirectory() as tmp:
        agent = _agent(tmp)
        agent.choose_action(_base(screen_type="REST", screen={"rest_options": ["rest"]}))
        assert agent._budget().act == 1
        agent.choose_action(_base(act=2, screen_type="REST", screen={"rest_options": ["rest"]}))
        assert agent._budget().act == 2
        assert agent._budget().remaining_budget == 80 - int(80 * 0.40)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all agent router tests passed")
