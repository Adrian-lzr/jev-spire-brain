"""Tests for greedy combat tactics."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from spirebrain.tactical.combat_greedy import (
    Card,
    CombatState,
    incoming_damage,
    play_order,
    should_use_potion,
)


def _state(hand, hp=50, block=0, energy=3, enemies=None):
    return CombatState(player_hp=hp, player_block=block, energy=energy,
                       hand=hand, enemies=enemies or [])


def test_blocks_when_threatened():
    hand = [Card("strike", "attack", damage=6),
            Card("defend", "skill", block=5),
            Card("defend", "skill", block=5)]
    enemies = [{"name": "slime", "hp": 20, "intent": "attack", "damage": 11}]
    order = play_order(_state(hand, enemies=enemies))
    blocks = sum(c.block for c in order)
    assert blocks >= 11 or len(order) == 3  # blocks up to threat or hand exhausted


def test_attacks_when_safe():
    hand = [Card("strike", "attack", damage=6),
            Card("bash", "attack", damage=8),
            Card("defend", "skill", block=5)]
    enemies = [{"name": "slime", "hp": 20, "intent": "block", "damage": 0}]
    order = play_order(_state(hand, enemies=enemies))
    attacks = [c for c in order if c.type == "attack"]
    assert sum(c.damage for c in attacks) == 14  # both attacks, no block needed
    assert order.index(next(c for c in order if c.name == "bash")) < \
           order.index(next(c for c in order if c.name == "strike"))


def test_energy_respected():
    hand = [Card("big", "attack", damage=20, energy=3),
            Card("small", "attack", damage=5, energy=1)]
    enemies = [{"name": "x", "hp": 30, "intent": "attack", "damage": 0}]
    order = play_order(_state(hand, energy=2, enemies=enemies))
    assert all(c.energy <= 2 for c in order)
    assert "small" in [c.name for c in order]


def test_potion_rule():
    enemies = [{"name": "nob", "hp": 80, "intent": "attack", "damage": 18}]
    assert should_use_potion(_state([], hp=25, block=0, enemies=enemies), 20)
    assert not should_use_potion(_state([], hp=50, block=0, enemies=enemies), 20)
    assert incoming_damage(_state([], enemies=enemies)) == 18


if __name__ == "__main__":
    test_blocks_when_threatened()
    test_attacks_when_safe()
    test_energy_respected()
    test_potion_rule()
    print("all combat_greedy tests passed")
