"""Greedy combat tactics — deterministic, free, fast.

The drone rule: JEV cannot run at control rate. Card play is arithmetic and
search; only the surrounding risk gate (CombatRiskGate) consults JEV.

Phase 4 upgrade path: replace play_order with a scumthespire-style search
using STSStateSaver rollbacks. The greedy policy below exists so Phase 1's
live pipe has something that can actually finish fights.

Policy (typical starter-deck heuristics, intentionally simple):
  1. If an enemy intends to attack and playing block is possible -> block.
  2. Else if an attack card is playable -> highest damage first.
  3. Else if a skill/power with no immediate downside is playable -> play it.
  4. Else end turn.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Card:
    name: str
    type: str            # "attack" | "skill" | "power"
    damage: int = 0      # for attacks
    block: int = 0       # for skills
    energy: int = 1


@dataclass
class CombatState:
    player_hp: int
    player_block: int
    energy: int
    hand: list[Card]
    enemies: list[dict]  # [{"name": ..., "hp": ..., "intent": "attack"|"block"|"unknown", "damage": N}]


def incoming_damage(state: CombatState) -> int:
    return sum(e.get("damage", 0) for e in state.enemies if e.get("intent") == "attack")


def play_order(state: CombatState) -> list[Card]:
    """Return the cards to play this turn, in order."""
    played: list[Card] = []
    energy = state.energy
    threat = incoming_damage(state)

    # 1. block against incoming attacks (up to the threat amount)
    if threat > 0:
        for card in sorted((c for c in state.hand if c.block > 0),
                           key=lambda c: (-c.block, c.energy)):
            if state.player_block + sum(c.block for c in played) >= threat:
                break
            if energy >= card.energy:
                played.append(card)
                energy -= card.energy

    # 2. highest-damage attacks with remaining energy
    for card in sorted((c for c in state.hand if c.type == "attack" and c.damage > 0),
                       key=lambda c: -c.damage):
        if energy >= card.energy:
            played.append(card)
            energy -= card.energy

    # 3. cheap non-attack leftovers (skip for now — starter decks rarely need)
    return played


def should_use_potion(state: CombatState, potion_heal: int) -> bool:
    """Blood potion rule: heal when below half and under pressure."""
    half = state.player_hp < 30
    pressured = incoming_damage(state) >= state.player_hp - state.player_block
    return half and (pressured or state.player_hp + potion_heal <= 60)
