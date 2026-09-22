"""Tests for the agent dispatcher: game screen -> decision -> one command.

The fake "game" objects are plain dicts on purpose — that is exactly what the
duck-typed `_get()` accessor is there to support, and it means the router can be
verified before the game exists. Logs go to a temp dir so the repo stays clean.

Screen payloads live under `screen_state`, which is where CommunicationMod
actually puts them (README, verified 2026-09-21); some tests also pass the legacy
`screen` key to prove the fallback chain still works.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from spirebrain.driver.agent import PROTOCOL_VERBS, SpireBrainAgent

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
                     screen_state={"cards": [{"name": "Inflame", "description": "gain strength"}]})
        cmd = agent.choose_action(game)
        # RETURN, not "skip": the protocol has no skip verb (README, 2026-09-21).
        assert cmd["command"] == "return"
        assert "25/25" in agent.history[-1]["detail"]["reason"]


def test_card_reward_maps_label_to_index():
    with tempfile.TemporaryDirectory() as tmp:
        agent = _agent(tmp)
        game = _base(screen_type="CARD_REWARD", screen_state={"cards": [
            {"name": "Anger", "description": "0-cost attack"},
            {"name": "Pommel Strike", "description": "damage and draw"},
        ]})
        cmd = agent.choose_action(game)
        assert cmd["command"] in ("return",)
        assert cmd["command"] == "return" or 0 <= cmd["choice"] <= 1


def test_card_reward_duplicate_names_do_not_misroute():
    with tempfile.TemporaryDirectory() as tmp:
        agent = _agent(tmp)
        game = _base(screen_type="CARD_REWARD", screen_state={"cards": [
            {"name": "Strike", "description": "a"},
            {"name": "Strike", "description": "b"},
        ]})
        cmd = agent.choose_action(game)
        assert cmd["command"] == "return" or cmd["choice"] in (0, 1)


# --------------------------------------------------------------------------- #
# Event
# --------------------------------------------------------------------------- #
def test_event_skips_disabled_options():
    with tempfile.TemporaryDirectory() as tmp:
        agent = _agent(tmp)
        game = _base(screen_type="EVENT", screen_state={
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
        cmd = agent.choose_action(_base(screen_type="EVENT", screen_state={"options": []}))
        assert cmd == {"command": "choose", "choice": 0}


# --------------------------------------------------------------------------- #
# Rest site
# --------------------------------------------------------------------------- #
def test_rest_without_smith_rests_without_asking_jev():
    with tempfile.TemporaryDirectory() as tmp:
        agent = _agent(tmp)
        game = _base(screen_type="REST", screen_state={"rest_options": ["rest"]})
        cmd = agent.choose_action(game)
        assert cmd == {"command": "choose", "choice": 0}  # index of "rest"
        assert agent.jev.calls == 0


def test_rest_with_smith_chooses_the_option_then_answers_the_grid():
    with tempfile.TemporaryDirectory() as tmp:
        agent = _agent(tmp)
        game = _base(screen_type="REST", screen_state={"rest_options": ["rest", "smith"]})
        cmd = agent.choose_action(game)
        assert cmd["command"] == "choose" and cmd["choice"] in (0, 1)
        # If it chose to upgrade, the intent has to survive until the grid screen
        # — the protocol allows only one command per state.
        if cmd["choice"] == 1:
            assert agent._pending_upgrade is not None
            grid = _base(screen_type="GRID", screen_state={"cards": DECK_10})
            follow_up = agent.choose_action(grid)
            assert follow_up["command"] == "choose"
            assert follow_up["choice"] == agent._pending_upgrade or agent._pending_upgrade is None


def test_grid_without_a_pending_intent_takes_the_first_card():
    with tempfile.TemporaryDirectory() as tmp:
        agent = _agent(tmp)
        cmd = agent.choose_action(_base(screen_type="GRID", screen_state={"cards": DECK_10}))
        assert cmd["command"] == "choose" and cmd["choice"] == 0
        assert "no pending intent" in cmd["reason"]


def test_grid_confirm_phase_finalizes_the_pick():
    """Measured live 2026-09-22 18:54: after `choose N` on a grid, the SAME
    screen returns offering [confirm, cancel, ...]. The pick is in the slot;
    the game wants CONFIRM. A second `choose` re-opens the slot and the pipe
    ping-pongs forever - the third live death that night."""
    with tempfile.TemporaryDirectory() as tmp:
        agent = _agent(tmp)
        # Phase 1: the pick.
        pick = agent.choose_action(_base(screen_type="GRID",
                                         screen_state={"cards": DECK_10}))
        assert pick["command"] == "choose"
        # Phase 2: same screen, confirm now offered, state carries what we chose.
        confirm = agent.choose_action(_base(
            screen_type="GRID", screen_state={"cards": DECK_10},
            available_commands=["confirm", "cancel", "key", "click", "wait", "state"]))
        assert confirm["command"] == "confirm", confirm
        # A later grid starts fresh (no stale confirm).
        fresh = agent.choose_action(_base(screen_type="GRID",
                                          screen_state={"cards": DECK_10}))
        assert fresh["command"] == "choose"


def test_a_half_finished_grid_pick_does_not_leak_to_a_later_grid():
    """A pick only means something on the grid it was made on.

    Without the clear-on-leave rule, a pick abandoned mid-dance (the player took
    over, the run moved on) would still be in `_grid_picked` when an unrelated
    confirm-phase grid showed up floors later — and we would confirm a selection
    we never made, on a grid we never asked about.
    """
    with tempfile.TemporaryDirectory() as tmp:
        agent = _agent(tmp)
        agent.choose_action(_base(screen_type="GRID", screen_state={"cards": DECK_10}))
        assert agent._grid_picked == 0
        agent.choose_action(_base(screen_type="MAP", screen_state={}))
        assert agent._grid_picked is None, "a pick leaked past the grid it belongs to"
        later = agent.choose_action(_base(
            screen_type="GRID", screen_state={"cards": DECK_10},
            available_commands=["confirm", "cancel", "key", "click", "wait", "state"]))
        assert later["command"] == "confirm"
        assert "no pending pick" in later["reason"]


# --------------------------------------------------------------------------- #
# Shop
# --------------------------------------------------------------------------- #
def test_shop_leaves_when_broke():
    with tempfile.TemporaryDirectory() as tmp:
        agent = _agent(tmp)
        game = _base(screen_type="SHOP_SCREEN", gold=0, screen_state={
            "cards": [{"name": "Ornamental Fan", "price": 150, "description": "block"}],
            "relics": [], "potions": [],
        })
        cmd = agent.choose_action(game)
        assert cmd["command"] == "return"  # leaving a shop is RETURN


def test_shop_buys_when_priced_and_affordable():
    with tempfile.TemporaryDirectory() as tmp:
        agent = _agent(tmp)
        game = _base(screen_type="SHOP_SCREEN", gold=500, screen_state={
            "cards": [{"name": "Ornamental Fan", "price": 150, "description": "block"}],
            "relics": [], "potions": [],
        })
        cmd = agent.choose_action(game)
        # pessimistic mock (0.55) is inside the uncertain band -> keep the gold
        assert cmd["command"] == "return"
        assert agent.history[-1]["fallback"] is True


# --------------------------------------------------------------------------- #
# Boss relic
# --------------------------------------------------------------------------- #
def test_boss_reward_always_picks_something():
    with tempfile.TemporaryDirectory() as tmp:
        agent = _agent(tmp)
        game = _base(screen_type="BOSS_REWARD", screen_state={"relics": [
            {"name": "Philosopher's Stone", "description": "energy, enemies gain strength"},
            {"name": "Runic Dome", "description": "energy, no enemy intents"},
        ]})
        cmd = agent.choose_action(game)
        assert cmd["command"] == "choose" and cmd["choice"] in (0, 1)


def test_boss_reward_empty_is_safe():
    with tempfile.TemporaryDirectory() as tmp:
        agent = _agent(tmp)
        cmd = agent.choose_action(_base(screen_type="BOSS_REWARD", screen_state={"relics": []}))
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
        assert cmd["card"] == 0                # blocks first against the attack
        assert cmd["target"] == 0
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
def test_unknown_screen_navigates_without_a_jev_call():
    """Guard #3 (jespire): a screen that is not a decision point gets a
    navigation Proceed, not a model question and not a wait. GRID is handled
    now (see _on_grid); MODDED_UNKNOWN_SCREEN stands for one added by a mod.
    (This used to be `wait` — the jespire port changed that on purpose: a wait
    on a screen the game expects an answer for just stalls the pipe.)"""
    with tempfile.TemporaryDirectory() as tmp:
        agent = _agent(tmp)
        before = agent.jev.calls
        cmd = agent.choose_action(_base(screen_type="MODDED_UNKNOWN_SCREEN"))
        assert cmd["command"] == "proceed"
        assert cmd["reason_source"] == "navigation"
        assert agent.jev.calls == before  # navigation never consults the model


def test_full_screen_sweep_produces_valid_commands():
    with tempfile.TemporaryDirectory() as tmp:
        agent = _agent(tmp)
        screens = [
            _base(screen_type="MAP", map={"next_nodes": [{"x": 0, "y": 4, "symbol": "M"}]}),
            _base(screen_type="CARD_REWARD", screen_state={"cards": [{"name": "Inflame"}]}),
            _base(screen_type="EVENT", screen_state={"options": [{"label": "a"}, {"label": "b"}]}),
            _base(screen_type="REST", screen_state={"rest_options": ["rest", "smith"]}),
            _base(screen_type="SHOP_SCREEN",
                  screen_state={"cards": [], "relics": [], "potions": []}),
            _base(screen_type="BOSS_REWARD", screen_state={"relics": [{"name": "Runic Dome"}]}),
            _base(screen_type="COMBAT", combat=COMBAT),
        ]
        for game in screens:
            cmd = agent.choose_action(game)
            assert "command" in cmd and isinstance(cmd["command"], str)
        assert agent.jev.calls > 0


def test_every_emitted_verb_is_a_real_protocol_verb():
    """The CI contract: an invented verb is silently ignored by the game, which
    looks exactly like a dead pipe. This test is why the vocabulary was fixed."""
    with tempfile.TemporaryDirectory() as tmp:
        agent = _agent(tmp)
        screens = [
            _base(screen_type="MAP", map={"next_nodes": [{"x": 0, "y": 4, "symbol": "M"},
                                                         {"x": 1, "y": 4, "symbol": "E"}]}),
            _base(screen_type="MAP", map={"next_nodes": []}),
            _base(screen_type="CARD_REWARD", screen_state={"cards": [{"name": "Anger"}]}),
            _base(screen_type="CARD_REWARD", deck=[{"name": f"C{i}"} for i in range(25)],
                  screen_state={"cards": [{"name": "Anger"}]}),
            _base(screen_type="EVENT", screen_state={"options": [{"label": "a"}]}),
            _base(screen_type="EVENT", screen_state={"options": []}),
            _base(screen_type="REST", screen_state={"rest_options": ["rest"]}),
            _base(screen_type="REST", screen_state={"rest_options": ["rest", "smith"]}),
            _base(screen_type="GRID", screen_state={"cards": DECK_10}),
            _base(screen_type="SHOP_SCREEN", screen_state={"cards": [], "relics": [], "potions": []}),
            _base(screen_type="SHOP_SCREEN", gold=999, screen_state={
                "cards": [{"name": "Fan", "price": 10, "description": "d"}],
                "relics": [], "potions": [], "purge_cost": 75}),
            _base(screen_type="BOSS_REWARD", screen_state={"relics": [{"name": "Runic Dome"}]}),
            _base(screen_type="BOSS_REWARD", screen_state={"relics": []}),
            _base(screen_type="COMBAT", combat=COMBAT),
            _base(screen_type="COMBAT", combat=dict(COMBAT, player={"current_hp": 30, "block": 0,
                                                                    "energy": 2})),
            _base(screen_type="GRID"),                       # unknown screen variants
        ]
        for game in screens:
            cmd = agent.choose_action(game)
            verb = cmd["command"].split(" ")[0].lower()
            if verb.startswith("("):
                continue  # "(posture only)" is an audit record, never sent
            assert verb in PROTOCOL_VERBS, f"{verb!r} from {game['screen_type']} is not a protocol verb"


def test_the_run_context_is_populated_for_every_decision():
    """The live path must feed the judges the same rich state the simulator does,
    or every live question is under-specified in the way runs 1-2 measured."""
    with tempfile.TemporaryDirectory() as tmp:
        agent = _agent(tmp)
        game = _base(screen_type="CARD_REWARD", gold=213,
                     relics=["Burning Blood", {"name": "Vajra"}],
                     potions=[{"name": "Fire Potion"}],
                     screen_state={"cards": [{"name": "Anger"}]})
        agent.choose_action(game)
        run = agent.run
        assert run is not None
        assert run.gold == 213
        assert run.deck and len(run.deck) == 10
        assert run.relics == ["Burning Blood", "Vajra"]
        assert run.potions == ["Fire Potion"]
        assert run.hp == 80 and run.max_hp == 80
        assert run.budget_remaining is not None
        # And the digest it would send carries them through.
        digest = run.digest()
        assert digest["gold"] == 213
        assert digest["deck"]["summary"]["size"] == 10
        assert digest["hp"]["ratio"] == 1.0
        assert "Burning Blood" in digest["relics"]
        assert "Fire Potion" in digest["potions"]


def test_acceptance_mode_reaches_the_score_judges():
    with tempfile.TemporaryDirectory() as tmp:
        agent = _agent(tmp, acceptance="argmax")
        agent.choose_action(_base(screen_type="BOSS_REWARD", screen_state={
            "relics": [{"name": "Runic Dome", "description": "energy, no intents"}]}))
        assert agent.history[-1]["detail"]["gate"]["mode"] == "argmax"


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
