"""Bounded deterministic combat evaluation for offline replay and advice.

This module deliberately models only effects that are explicit in the incoming
CommunicationMod state. Unknown effects stop sequence prediction and are marked
unsupported instead of being guessed. It never emits a game command directly.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from time import monotonic
from typing import Any, Mapping


@dataclass(frozen=True)
class CombatFacts:
    damage: int | None = None
    block: int | None = None
    incoming_damage: int = 0
    lethal_confirmed: bool = False
    kills_target: str | None = None
    energy_after: int | None = None
    uncertainty: str = "none"
    confidence: str = "verified"
    scores: Mapping[str, float] = field(default_factory=dict)


def _num(value: Any, default: int | None = None) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def incoming_damage(combat: Mapping[str, Any]) -> int:
    total = 0
    for enemy in combat.get("monsters") or []:
        if not isinstance(enemy, Mapping) or enemy.get("is_gone"):
            continue
        if "ATTACK" not in str(enemy.get("intent", "")).upper():
            continue
        damage = _num(enemy.get("move_adjusted_damage", enemy.get("damage")))
        hits = max(1, _num(enemy.get("move_hits", 1), 1) or 1)
        if damage is not None:
            total += max(0, damage) * hits
    return total


def _target_id(enemy: Mapping[str, Any], index: int) -> str:
    return str(enemy.get("instance_id") or enemy.get("uuid") or
               enemy.get("id") or enemy.get("name") or f"enemy:{index}")


def evaluate_card(combat: Mapping[str, Any], card_index: int, target_index: int | None = None) -> CombatFacts:
    """Evaluate one explicit card without inventing unknown effects."""
    player = combat.get("player") or {}
    hand = combat.get("hand") or []
    card = hand[card_index] if 0 <= card_index < len(hand) else {}
    cost = _num(card.get("cost"))
    energy = _num(player.get("energy"))
    damage = _num(card.get("damage"))
    block = _num(card.get("block"))
    uncertainty = "none"
    confidence = "verified"
    if damage is None and block is None:
        uncertainty, confidence = "unknown_effect", "unsupported"
    target = None
    lethal = False
    if damage is not None:
        monsters = combat.get("monsters") or []
        if target_index is None:
            alive = [(i, m) for i, m in enumerate(monsters) if isinstance(m, Mapping) and _num(m.get("current_hp"), 0) > 0]
            target_index = alive[0][0] if alive else None
        if target_index is not None and target_index < len(monsters):
            enemy = monsters[target_index]
            hp = _num(enemy.get("current_hp"), 0) or 0
            shield = _num(enemy.get("block"), 0) or 0
            target = _target_id(enemy, target_index)
            lethal = damage >= hp + shield
    inc = incoming_damage(combat)
    current_block = _num(player.get("block"), 0) or 0
    energy_after = energy - cost if energy is not None and cost is not None else None
    effective_incoming = max(0, inc - current_block - (block or 0))
    scores = {
        "survival_score": -float(effective_incoming),
        "lethal_score": 100.0 if lethal else 0.0,
        "damage_score": float(damage or 0),
        "block_score": float(block or 0),
        "energy_score": float(energy_after if energy_after is not None else -100),
        "uncertainty_penalty": -50.0 if uncertainty != "none" else 0.0,
    }
    return CombatFacts(damage=damage, block=block, incoming_damage=inc,
                       lethal_confirmed=lethal, kills_target=target if lethal else None,
                       energy_after=energy_after, uncertainty=uncertainty,
                       confidence=confidence, scores=scores)


def rank_cards(combat: Mapping[str, Any]) -> list[tuple[int, CombatFacts]]:
    """Return playable cards ranked by facts; ties preserve hand order."""
    player = combat.get("player") or {}
    energy = _num(player.get("energy"), 0) or 0
    result: list[tuple[int, CombatFacts]] = []
    for index, card in enumerate(combat.get("hand") or []):
        if not isinstance(card, Mapping) or card.get("is_playable") is False:
            continue
        cost = _num(card.get("cost"))
        if cost is None or cost < 0 or cost > energy:
            continue
        facts = evaluate_card(combat, index)
        value = (facts.scores.get("lethal_score", 0) +
                 facts.scores.get("survival_score", 0) +
                 facts.scores.get("damage_score", 0) +
                 facts.scores.get("block_score", 0) +
                 facts.scores.get("uncertainty_penalty", 0))
        result.append((index, facts))
    return sorted(result, key=lambda item: (
        -(item[1].scores.get("lethal_score", 0) + item[1].scores.get("survival_score", 0) +
          item[1].scores.get("damage_score", 0) + item[1].scores.get("block_score", 0) +
          item[1].scores.get("uncertainty_penalty", 0)), item[0]))


def search_sequences(combat: Mapping[str, Any], *, depth: int = 3, node_limit: int = 64, time_limit_ms: float = 8.0) -> dict[str, Any]:
    """Search short, fully deterministic card sequences.

    Supported sequence effects are printed damage, printed block and an
    explicit ``applies_vulnerable`` value. A branch stops before any card with
    an unknown/random effect, leaving the live game state to resolve it.
    """
    started = monotonic()
    deadline = started + max(0.0, time_limit_ms) / 1000.0
    depth = max(1, min(3, int(depth)))
    node_limit = max(1, int(node_limit))
    player = combat.get("player") or {}
    monsters = combat.get("monsters") or []
    alive = [(i, m) for i, m in enumerate(monsters)
             if isinstance(m, Mapping) and (_num(m.get("current_hp"), 0) or 0) > 0]
    if not alive:
        return {"sequence": (), "score": None, "confidence": "unsupported",
                "uncertainty": "no_legal_action", "nodes": 0,
                "budget_exhausted": False}
    target_index, target = min(alive, key=lambda row: ((_num(row[1].get("current_hp"), 0) or 0) +
                                                       (_num(row[1].get("block"), 0) or 0), row[0]))
    target_hp = (_num(target.get("current_hp"), 0) or 0) + (_num(target.get("block"), 0) or 0)
    energy = _num(player.get("energy"))
    if energy is None:
        return {"sequence": (), "score": None, "confidence": "unsupported",
                "uncertainty": "unknown_energy", "nodes": 0,
                "budget_exhausted": False}
    initial_block = _num(player.get("block"), 0) or 0
    threat = incoming_damage(combat)
    hand = combat.get("hand") or []
    nodes = 0
    exhausted = False
    saw_unknown = False
    # score, sequence, damage, block, lethal, facts
    best: tuple[float, tuple[int, ...], int, int, bool, tuple[CombatFacts, ...]] | None = None

    def preference(row: tuple[float, tuple[int, ...], int, int, bool, tuple[CombatFacts, ...]]) -> tuple[Any, ...]:
        score, sequence, dealt, gained_block, lethal, _ = row
        covered = threat > initial_block and initial_block + gained_block >= threat
        if lethal:
            return (2, -len(sequence), dealt, tuple(-i for i in sequence))
        if covered:
            return (1, -len(sequence), gained_block, dealt, tuple(-i for i in sequence))
        return (0, score, -len(sequence), tuple(-i for i in sequence))

    def visit(sequence: tuple[int, ...], remaining: tuple[int, ...], available: int,
              dealt: int, gained_block: int, vulnerable: int, facts_seen: tuple[CombatFacts, ...]) -> None:
        nonlocal nodes, exhausted, saw_unknown, best
        if nodes >= node_limit or monotonic() >= deadline:
            exhausted = True
            return
        if sequence:
            lethal = dealt >= target_hp
            uncovered = max(0, threat - initial_block - gained_block) if not lethal else 0
            score = (100.0 if lethal else 0.0) + dealt + gained_block - uncovered
            candidate = (score, sequence, dealt, gained_block, lethal, facts_seen)
            if best is None or preference(candidate) > preference(best):
                best = candidate
            # Advice is recalculated after every player action. Once this
            # sequence has already removed the target or covered the known
            # incoming attack, extending it would turn a current-step
            # recommendation into an unnecessarily speculative combo.
            objective_complete = lethal or (threat > initial_block and uncovered == 0)
        else:
            objective_complete = False
        if len(sequence) >= depth or objective_complete:
            return
        for index in remaining:
            if nodes >= node_limit or monotonic() >= deadline:
                exhausted = True
                return
            card = hand[index]
            if not isinstance(card, Mapping) or card.get("is_playable") is False:
                continue
            cost = _num(card.get("cost"))
            if cost is None or cost < 0 or cost > available:
                continue
            damage, block = _num(card.get("damage")), _num(card.get("block"))
            random_or_complex = card.get("random") or card.get("draw") or card.get("complex_effect")
            if (damage is None and block is None and not card.get("applies_vulnerable")) or random_or_complex:
                saw_unknown = True
                continue
            nodes += 1
            actual_damage = damage or 0
            if vulnerable > 0 and actual_damage:
                actual_damage = (actual_damage * 3 + 1) // 2
            applied = max(0, _num(card.get("applies_vulnerable"), 0) or 0)
            facts = CombatFacts(
                damage=actual_damage if damage is not None else None, block=block,
                incoming_damage=threat, lethal_confirmed=dealt + actual_damage >= target_hp,
                kills_target=_target_id(target, target_index) if dealt + actual_damage >= target_hp else None,
                energy_after=available - cost, uncertainty="none", confidence="verified",
                scores={"damage_score": float(actual_damage), "block_score": float(block or 0)})
            visit(sequence + (index,), tuple(i for i in remaining if i != index),
                  available - cost, dealt + actual_damage, gained_block + (block or 0),
                  max(vulnerable - 1, applied), facts_seen + (facts,))

    visit((), tuple(range(len(hand))), energy, 0, 0,
          max(0, _num(target.get("vulnerable"), 0) or 0), ())
    if best is None:
        return {"sequence": (), "score": None, "confidence": "unsupported",
                "uncertainty": "unknown_effect" if saw_unknown else "no_legal_action",
                "nodes": nodes, "budget_exhausted": exhausted}
    return {"sequence": best[1], "score": best[0], "facts": best[5],
            "damage": best[2], "block": best[3], "lethal_confirmed": best[4],
            "target_index": target_index, "confidence": "verified",
            "uncertainty": "none", "nodes": nodes,
            "budget_exhausted": exhausted}
