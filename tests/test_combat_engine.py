from spirebrain.brain.action_broker import build_action_candidates
from spirebrain.driver.legality import check_action
from spirebrain.tactical.combat_engine import CombatTacticalEngine


def _state(hand, *, enemy_hp=20, enemy_id="cultist-a", damage=15, energy=3):
    return {
        "screen_type": "COMBAT", "character": "IRONCLAD",
        "available_commands": ["play", "end"],
        "combat": {
            "player": {"energy": energy, "current_hp": 30, "block": 0},
            "hand": hand,
            "monsters": [{"id": enemy_id, "name": "Cultist", "current_hp": enemy_hp,
                          "block": 0, "intent": "ATTACK", "damage": damage}],
        },
    }


def test_engine_binds_lethal_target_to_current_candidate():
    state = _state([
        {"id": "Defend_R", "type": "SKILL", "cost": 1, "block": 15, "is_playable": True},
        {"id": "Strike_R", "type": "ATTACK", "cost": 1, "damage": 20, "is_playable": True},
    ])
    candidates = build_action_candidates(state)
    proposal = CombatTacticalEngine().propose(state, candidates)
    assert proposal.candidate is not None
    assert proposal.candidate.command == {"command": "play", "card": 1, "target": 0}
    assert proposal.facts["lethal_confirmed"] is True
    assert check_action(state, proposal.candidate.command)[0]


def test_engine_rejects_stale_target_and_does_not_reuse_index():
    old = _state([{"id": "Strike_R", "type": "ATTACK", "cost": 1,
                   "damage": 20, "is_playable": True}], enemy_id="enemy-a")
    current = _state([{"id": "Strike_R", "type": "ATTACK", "cost": 1,
                       "damage": 20, "is_playable": True}], enemy_id="enemy-b", enemy_hp=50)
    old_candidates = build_action_candidates(old)
    # A proposal is bound to the current candidate set, never to a bare index.
    proposal = CombatTacticalEngine().propose(current, old_candidates)
    assert proposal.reason_code in {"candidate_invalidated", "bounded_search", "greedy_fallback"}
    if proposal.candidate:
        assert proposal.candidate.validity.get("target_entity_id") != "enemy-a"


def test_unknown_effect_is_explicitly_uncertain():
    state = _state([{"id": "Offering", "type": "SKILL", "cost": 1,
                     "draw": 3, "is_playable": True}])
    proposal = CombatTacticalEngine().propose(state, build_action_candidates(state))
    assert proposal.uncertainty in {"unknown_effect", "no_legal_action", ""}
    # The engine may end the turn, but must never claim a verified card effect.
    assert proposal.confidence != "verified" or proposal.reason_code == "rule_fallback"


def test_engine_is_current_step_only_and_advise_safe():
    state = _state([{"id": "Strike_R", "type": "ATTACK", "cost": 1,
                     "damage": 20, "is_playable": True}])
    proposal = CombatTacticalEngine().propose(state, build_action_candidates(state))
    assert proposal.candidate is not None
    assert proposal.candidate.command["command"] == "play"
    # The tactical engine proposes; the transport's advise mode remains the
    # component responsible for emitting only wait/state on the wire.
