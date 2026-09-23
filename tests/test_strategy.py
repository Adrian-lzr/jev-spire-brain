"""Adaptive Ironclad strategy selection tests."""

from spirebrain.strategy import select_strategy, strategy_context
from spirebrain.cards.deck import grade, profile


def test_starter_deck_stays_adaptive():
    selection = select_strategy(["Strike_Red", "Strike_Red", "Defend_Red", "Bash"])
    assert selection.profile.id == "adaptive"
    assert selection.confidence == 0.0


def test_two_strength_signals_commit_to_strength():
    selection = select_strategy(["Inflame", "Heavy Blade", "Shrug It Off"], act=2,
                                hp=65, max_hp=80)
    assert selection.profile.id == "strength"
    assert "力量成长" in selection.profile.label
    assert selection.confidence > 0


def test_low_hp_keeps_strategy_adaptive_even_with_signals():
    selection = select_strategy(["Inflame", "Heavy Blade"], act=2,
                                hp=20, max_hp=80)
    assert selection.profile.id == "adaptive"
    assert "生命" in selection.reason


def test_strategy_context_is_bounded_and_json_friendly():
    context = strategy_context({
        "character": "IRONCLAD", "act": 2, "current_hp": 70, "max_hp": 80,
        "deck": [{"id": "Corruption"}, {"id": "Feel No Pain"}],
    })
    assert context["selected"]["id"] == "exhaust"
    assert isinstance(context["selected"]["focus"], list)


def test_strategy_bonus_changes_local_pick_explanation():
    deck = profile(["Inflame", "Shrug It Off"])
    generic = grade(deck, "Heavy Blade", act=2)
    focused = grade(deck, "Heavy Blade", act=2, strategy_id="strength")
    assert focused.score >= generic.score
    assert "力量成长" in focused.reason_text()


def test_live_class_ids_are_normalized_to_guide_names():
    selection = select_strategy(["Inflame", "HeavyBlade"], act=2,
                                hp=70, max_hp=80)
    assert selection.profile.id == "strength"


def test_nested_combat_hp_is_used_by_strategy_context():
    context = strategy_context({
        "character": "IRONCLAD", "act": 2,
        "combat": {"player": {"current_hp": 20, "max_hp": 80}},
        "deck": ["Inflame", "HeavyBlade"],
    })
    assert context["selected"]["id"] == "adaptive"
