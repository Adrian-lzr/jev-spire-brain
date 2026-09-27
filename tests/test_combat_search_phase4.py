from spirebrain.tactical.combat_search import evaluate_card, search_sequences


def _combat(hand, *, hp=20, block=0, energy=3, enemy_hp=20, enemy_block=0, vulnerable=0):
    return {
        "player": {"energy": energy, "current_hp": hp, "block": block},
        "hand": hand,
        "monsters": [{"id": "cultist-a", "name": "Cultist",
                        "current_hp": enemy_hp, "block": enemy_block,
                        "intent": "ATTACK", "damage": 15,
                        "vulnerable": vulnerable}],
    }


def test_verified_lethal_beats_defense():
    state = _combat([
        {"id": "Defend_R", "type": "SKILL", "cost": 1, "block": 15, "is_playable": True},
        {"id": "Strike_R", "type": "ATTACK", "cost": 1, "damage": 20, "is_playable": True},
    ])
    result = search_sequences(state)
    assert result["lethal_confirmed"] is True
    assert result["sequence"] == (1,)
    assert result["confidence"] == "verified"


def test_without_lethal_search_prefers_covering_incoming_damage():
    state = _combat([
        {"id": "Defend_R", "type": "SKILL", "cost": 1, "block": 15, "is_playable": True},
        {"id": "Strike_R", "type": "ATTACK", "cost": 1, "damage": 6, "is_playable": True},
    ], enemy_hp=40)
    result = search_sequences(state)
    assert result["sequence"] == (0,)
    assert result["block"] == 15
    assert result["lethal_confirmed"] is False


def test_sequence_order_changes_damage_when_vulnerable_is_explicit():
    state = _combat([
        {"id": "Bash", "type": "ATTACK", "cost": 2, "damage": 8,
         "applies_vulnerable": 2, "is_playable": True},
        {"id": "Strike_R", "type": "ATTACK", "cost": 1, "damage": 6, "is_playable": True},
    ], enemy_hp=40)
    first = evaluate_card(state, 0)
    assert first.damage == 8
    result = search_sequences(state, depth=2)
    assert result["sequence"] in {(0, 1), (1, 0)}
    assert result["damage"] >= 14


def test_unknown_effect_degrades_without_guessing():
    state = _combat([
        {"id": "Unknown", "type": "SKILL", "cost": 1, "draw": 2, "is_playable": True},
    ])
    result = search_sequences(state)
    assert result["confidence"] == "unsupported"
    assert result["uncertainty"] == "unknown_effect"


def test_search_honors_node_budget():
    state = _combat([
        {"id": f"Strike{i}", "type": "ATTACK", "cost": 0, "damage": 1, "is_playable": True}
        for i in range(8)
    ])
    result = search_sequences(state, depth=3, node_limit=1)
    assert result["nodes"] <= 1
    assert result["budget_exhausted"] is True


def test_search_stops_when_injected_clock_exceeds_deadline(monkeypatch):
    import spirebrain.tactical.combat_search as combat_search

    ticks = iter((10.0, 10.0, 10.0, 10.010))
    monkeypatch.setattr(combat_search, "monotonic", lambda: next(ticks))
    state = _combat([
        {"id": "Strike_R", "type": "ATTACK", "cost": 1,
         "damage": 6, "is_playable": True},
    ])

    result = search_sequences(state, depth=2, node_limit=16, time_limit_ms=5)

    assert result["budget_exhausted"] is True
    assert result["nodes"] == 1
    assert result["uncertainty"] == "search_budget_exhausted"


def test_search_does_not_mutate_input_snapshot():
    from copy import deepcopy

    state = _combat([
        {"id": "Bash", "type": "ATTACK", "cost": 2, "damage": 8,
         "applies_vulnerable": 2, "is_playable": True},
        {"id": "Strike_R", "type": "ATTACK", "cost": 1,
         "damage": 6, "is_playable": True},
    ], enemy_hp=40)
    before = deepcopy(state)

    search_sequences(state, depth=2)

    assert state == before


def test_search_time_budget_falls_back_to_a_legal_greedy_candidate():
    from spirebrain.brain.action_broker import build_action_candidates
    from spirebrain.tactical.combat_engine import CombatTacticalEngine

    state = {"screen_type": "COMBAT", "available_commands": ["play", "end"],
             "combat": _combat([
                 {"id": "Strike_R", "type": "ATTACK", "cost": 1,
                  "damage": 6, "has_target": True, "is_playable": True},
             ], enemy_hp=20)}
    proposal = CombatTacticalEngine(time_limit_ms=0).propose(
        state, build_action_candidates(state))

    assert proposal.candidate is not None
    assert proposal.candidate.legal is True
    assert proposal.reason_code == "greedy_fallback"
    assert proposal.fallback_reason == "search_budget_exhausted"
