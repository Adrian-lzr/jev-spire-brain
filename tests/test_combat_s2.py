from spirebrain.brain.action_broker import build_action_candidates
from spirebrain.tactical.combat_engine import CombatTacticalEngine
from spirebrain.tactical.combat_search import incoming_damage, search_sequences


def _combat(hand, monsters, energy=1):
    return {"player": {"energy": energy, "block": 0}, "hand": hand,
            "monsters": monsters}


def test_killing_non_attacker_does_not_remove_other_attack_threat():
    state = _combat([
        {"id": "Strike_R", "type": "ATTACK", "cost": 1, "damage": 6, "is_playable": True},
        {"id": "Defend_R", "type": "SKILL", "cost": 1, "block": 20, "is_playable": True},
    ], [
        {"id": "small", "current_hp": 5, "intent": "BUFF", "damage": 0},
        {"id": "elite", "current_hp": 40, "intent": "ATTACK", "damage": 20},
    ])
    result = search_sequences(state)
    assert result["target_index"] == 1
    assert result["remaining_incoming_damage"] == 20
    assert result["lethal_confirmed"] is False


def test_low_hp_non_attacker_does_not_beat_blocking_the_live_attack():
    state = {
        "screen_type": "COMBAT",
        "available_commands": ["play", "end"],
        "combat": _combat([
            {"id": "Strike_R", "type": "ATTACK", "cost": 1,
             "damage": 6, "is_playable": True, "has_target": True},
            {"id": "Defend_R", "type": "SKILL", "cost": 1,
             "block": 20, "is_playable": True},
        ], [
            {"id": "small", "current_hp": 5, "intent": "BUFF", "damage": 0},
            {"id": "attacker", "current_hp": 40, "intent": "ATTACK", "damage": 20},
        ]),
    }
    proposal = CombatTacticalEngine().propose(state, build_action_candidates(state))

    assert proposal.candidate is not None
    assert proposal.candidate.command == {"command": "play", "card": 1}
    assert proposal.facts["remaining_incoming_damage"] == 20
    assert proposal.facts["incoming_threat_covered"] is True


def test_killing_one_attacker_keeps_other_attack_incoming_and_reason_explicit():
    state = {
        "screen_type": "COMBAT",
        "available_commands": ["play", "end"],
        "combat": _combat([
            {"id": "Strike_R", "type": "ATTACK", "cost": 1,
             "damage": 6, "is_playable": True, "has_target": True},
            {"id": "Defend_R", "type": "SKILL", "cost": 1,
             "block": 20, "is_playable": True},
        ], [
            {"id": "attacker-a", "current_hp": 5, "intent": "ATTACK", "damage": 8},
            {"id": "attacker-b", "current_hp": 40, "intent": "ATTACK", "damage": 20},
        ]),
    }
    proposal = CombatTacticalEngine().propose(state, build_action_candidates(state))

    assert proposal.candidate is not None
    assert proposal.candidate.command == {"command": "play", "card": 0, "target": 0}
    assert proposal.facts["remaining_incoming_damage"] == 20
    assert proposal.facts["incoming_threat_covered"] is False
    assert "仍有" in proposal.reason and "20" in proposal.reason


def test_target_enumeration_prefers_killable_highest_threat_enemy():
    state = {
        "screen_type": "COMBAT",
        "available_commands": ["play", "end"],
        "combat": _combat([
            {"id": "Strike_R", "type": "ATTACK", "cost": 1,
             "damage": 6, "is_playable": True, "has_target": True},
        ], [
            {"id": "weak-attacker", "current_hp": 5,
             "intent": "ATTACK", "damage": 6},
            {"id": "strong-attacker", "current_hp": 6,
             "intent": "ATTACK", "damage": 20},
        ]),
    }
    proposal = CombatTacticalEngine().propose(state, build_action_candidates(state))

    assert proposal.candidate is not None
    assert proposal.candidate.command == {"command": "play", "card": 0, "target": 1}
    assert proposal.facts["kills_target"] == "strong-attacker"


def test_sequence_reselects_a_different_target_after_first_enemy_dies():
    state = _combat([
        {"id": "Strike_A", "type": "ATTACK", "cost": 1,
         "damage": 6, "is_playable": True, "has_target": True},
        {"id": "Strike_B", "type": "ATTACK", "cost": 1,
         "damage": 6, "is_playable": True, "has_target": True},
    ], [
        {"id": "attacker-a", "current_hp": 6, "intent": "ATTACK", "damage": 8},
        {"id": "attacker-b", "current_hp": 6, "intent": "ATTACK", "damage": 9},
    ], energy=2)

    result = search_sequences(state, depth=2)

    assert result["sequence"] == (0, 1)
    assert result["target_path"] == (0, 1)
    assert result["target_lethal_confirmed"] is True
    assert result["remaining_incoming_damage"] == 0


def test_combat_facts_respect_enemy_block_and_ignore_dead_multihit_threat():
    state = _combat([
        {"id": "Strike_R", "type": "ATTACK", "cost": 1,
         "damage": 8, "is_playable": True, "has_target": True},
    ], [
        {"id": "gone", "current_hp": 0, "is_gone": True,
         "intent": "ATTACK", "damage": 100, "move_hits": 3},
        {"id": "shielded", "current_hp": 5, "block": 3,
         "intent": "ATTACK", "damage": 4, "move_hits": 2},
    ])

    result = search_sequences(state, depth=1)

    assert result["target_index"] == 1
    assert result["immediate_facts"]["lethal_confirmed"] is True
    assert result["immediate_facts"]["kills_target"] == "shielded"
    assert result["remaining_incoming_damage"] == 0


def test_unknown_card_effect_remains_marked_on_engine_fallback():
    state = {"screen_type": "COMBAT", "available_commands": ["play", "end"],
             "combat": _combat([
                 {"id": "Unknown", "type": "SKILL", "cost": 1,
                  "draw": 2, "is_playable": True},
             ], [{"id": "enemy", "current_hp": 20,
                  "intent": "BUFF", "damage": 0}])}

    proposal = CombatTacticalEngine().propose(state, build_action_candidates(state))

    assert proposal.candidate is not None
    assert proposal.reason_code == "greedy_fallback"
    assert proposal.uncertainty == "unknown_effect"
    assert proposal.confidence == "estimated"


def test_incoming_damage_ignores_gone_and_half_dead_enemies_but_counts_multihit():
    state = _combat([], [
        {"id": "gone", "is_gone": True, "current_hp": 0,
         "intent": "ATTACK", "damage": 99, "move_hits": 1},
        {"id": "half-dead", "half_dead": True, "current_hp": 0,
         "intent": "ATTACK", "damage": 50, "move_hits": 2},
        {"id": "live", "current_hp": 10,
         "intent": "ATTACK", "damage": 4, "move_hits": 3},
    ])

    assert incoming_damage(state) == 12


def test_adjusted_enemy_damage_is_used_without_reapplying_modifiers():
    state = _combat([
        {"id": "Defend_R", "type": "SKILL", "cost": 1,
         "block": 10, "is_playable": True},
    ], [{"id": "elite", "current_hp": 50, "intent": "ATTACK",
         "damage": 8, "move_adjusted_damage": 14}])

    facts = search_sequences(state, depth=1)["immediate_facts"]

    assert incoming_damage(state) == 14
    assert facts["incoming_damage"] == 14
    assert facts["remaining_incoming_damage"] == 14


def test_sequence_forecast_does_not_overwrite_first_action_facts():
    state = _combat([
        {"id": "Strike_A", "type": "ATTACK", "cost": 1, "damage": 6, "is_playable": True},
        {"id": "Strike_B", "type": "ATTACK", "cost": 0, "damage": 6, "is_playable": True},
    ], [{"id": "elite", "current_hp": 10, "intent": "ATTACK", "damage": 0}])
    result = search_sequences(state, depth=2)
    assert result["damage"] == 12
    assert result["immediate_facts"]["damage"] == 6
    assert result["immediate_facts"]["lethal_confirmed"] is False
    assert result["projected_sequence"]


def test_rebinding_facts_after_action_arbitration_uses_final_card():
    state = {"screen_type": "COMBAT", "available_commands": ["play", "end"],
             "combat": {"player": {"energy": 1, "block": 0},
                        "hand": [{"id": "Strike_R", "type": "ATTACK", "cost": 1, "damage": 6,
                                  "is_playable": True},
                                 {"id": "Defend_R", "type": "SKILL", "cost": 1, "block": 8,
                                  "is_playable": True}],
                        "monsters": [{"id": "elite", "current_hp": 20, "intent": "ATTACK", "damage": 10}]}}
    candidates = build_action_candidates(state)
    attack = next(c for c in candidates if c.command.get("card") == 0)
    defend = next(c for c in candidates if c.command.get("card") == 1)
    facts = CombatTacticalEngine().facts_for_candidate(state, defend,
                                                        prior={"damage": 6, "lethal_confirmed": True})
    assert facts["block"] == 8
    assert facts["damage"] is None
    assert facts["lethal_confirmed"] is False
    assert facts["candidate_signature"] == defend.candidate_signature
