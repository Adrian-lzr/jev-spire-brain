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
    remaining_incoming_damage: int | None = None
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


def _enemy_can_attack(enemy: Mapping[str, Any]) -> bool:
    """Return whether the snapshot still describes a living threat.

    CommunicationMod may retain a defeated enemy in the array for a short
    death animation. Explicit gone/half-dead flags and a known non-positive HP
    value take precedence over its stale intent. Missing HP remains
    conservative: the intent is still considered until the game says
    otherwise.
    """
    if enemy.get("is_gone") or enemy.get("half_dead"):
        return False
    hp = _num(enemy.get("current_hp"))
    return hp is None or hp > 0


def incoming_damage(combat: Mapping[str, Any]) -> int:
    total = 0
    for enemy in combat.get("monsters") or []:
        if not isinstance(enemy, Mapping) or not _enemy_can_attack(enemy):
            continue
        if "ATTACK" not in str(enemy.get("intent", "")).upper():
            continue
        damage = _num(enemy.get("move_adjusted_damage", enemy.get("damage")))
        hits = max(1, _num(enemy.get("move_hits", 1), 1) or 1)
        if damage is not None:
            total += max(0, damage) * hits
    return total


def _enemy_threat(enemy: Mapping[str, Any]) -> int:
    if not _enemy_can_attack(enemy):
        return 0
    if "ATTACK" not in str(enemy.get("intent", "")).upper():
        return 0
    damage = _num(enemy.get("move_adjusted_damage", enemy.get("damage")))
    hits = max(1, _num(enemy.get("move_hits", 1), 1) or 1)
    return max(0, damage or 0) * hits if damage is not None else 0


def _remaining_threat(monsters: list[Mapping[str, Any]], killed_index: int | None = None) -> int:
    return sum(_enemy_threat(enemy) for i, enemy in enumerate(monsters)
               if isinstance(enemy, Mapping) and i != killed_index)


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


def search_sequences(combat: Mapping[str, Any], *, depth: int = 3, node_limit: int = 64, time_limit_ms: float = 20.0) -> dict[str, Any]:
    """Search deterministic card/target sequences within a small budget.

    The search state is deliberately small and visible: energy, player block,
    effective HP for each enemy, vulnerability turns and the remaining hand.
    A targeted card creates one branch per living enemy, so an index is never
    treated as a stable identity across branches.  Unsupported/random cards
    stop only that branch and are reported as uncertainty; no hidden card
    order or game RNG is inferred.
    """
    started = monotonic()
    deadline = started + max(0.0, float(time_limit_ms)) / 1000.0
    depth = max(1, min(3, int(depth)))
    node_limit = max(1, int(node_limit))
    player = combat.get("player") or {}
    hand = combat.get("hand") or []
    monsters = combat.get("monsters") or []
    alive = [i for i, enemy in enumerate(monsters)
             if isinstance(enemy, Mapping) and _enemy_can_attack(enemy)
             and (_num(enemy.get("current_hp"), 0) or 0) > 0]
    if not alive:
        return {"sequence": (), "score": None, "confidence": "unsupported",
                "uncertainty": "no_legal_action", "nodes": 0,
                "budget_exhausted": False}
    energy = _num(player.get("energy"))
    if energy is None:
        return {"sequence": (), "score": None, "confidence": "unsupported",
                "uncertainty": "unknown_energy", "nodes": 0,
                "budget_exhausted": False}
    initial_block = _num(player.get("block"), 0) or 0
    initial_threat = incoming_damage(combat)
    # Effective HP includes visible enemy block.  Damage is intentionally
    # applied to this scalar; unmodelled status changes are never guessed.
    hp0 = {i: max(0, (_num(monsters[i].get("current_hp"), 0) or 0)
                  + (_num(monsters[i].get("block"), 0) or 0)) for i in alive}
    threat0 = {i: _enemy_threat(monsters[i]) for i in alive}
    nodes = 0
    exhausted = False
    saw_unknown = False
    best: dict[str, Any] | None = None

    def rank(candidate: dict[str, Any]) -> tuple[Any, ...]:
        covered = candidate["initial_block"] + candidate["block"] >= candidate["remaining_threat"]
        lethal = bool(candidate["lethal"])
        # Explicitly prioritize removing all known lethal threats, then cover,
        # then useful damage/block. Stable index ordering keeps replay output.
        return (3 if lethal and candidate["remaining_threat"] == 0 else
                2 if covered else 1 if lethal else 0,
                1 if covered else 0,
                # Once the known incoming damage is covered, prefer the
                # current-step action over a speculative extra card.
                -len(candidate["sequence"]) if covered else candidate["score"],
                candidate["score"] if covered else -len(candidate["sequence"]),
                tuple(-i for i in candidate["sequence"]))

    def visit(sequence: tuple[int, ...], targets: tuple[int | None, ...], remaining: tuple[int, ...],
              available: int, block_gain: int, hp: dict[int, int], vulnerable: dict[int, int],
              dealt: int, facts_seen: tuple[CombatFacts, ...]) -> None:
        nonlocal nodes, exhausted, saw_unknown, best
        if nodes >= node_limit or monotonic() >= deadline:
            exhausted = True
            return
        if sequence:
            killed = {i for i, value in hp.items() if value <= 0}
            remaining_threat = sum(value for i, value in threat0.items() if i not in killed)
            lethal = bool(killed)
            uncovered = max(0, remaining_threat - initial_block - block_gain)
            score = ((100.0 if lethal and remaining_threat == 0 else 0.0)
                     + dealt + block_gain - uncovered)
            candidate = {"sequence": sequence, "targets": targets, "facts": facts_seen,
                         "dealt": dealt, "block": block_gain, "lethal": lethal,
                         "remaining_threat": remaining_threat, "initial_block": initial_block,
                         "score": score}
            if best is None or rank(candidate) > rank(best):
                best = candidate
            covered = uncovered == 0 and initial_threat > initial_block
            if len(sequence) >= depth or (lethal and remaining_threat == 0) or covered:
                return
        for index in remaining:
            if nodes and (nodes >= node_limit or monotonic() >= deadline):
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
            applies = _num(card.get("applies_vulnerable"), 0) or 0
            if (damage is None and block is None and not applies) or random_or_complex:
                saw_unknown = True
                continue
            targeted = bool(card.get("has_target", str(card.get("type", "")).upper() == "ATTACK"))
            target_choices = [i for i, value in hp.items() if value > 0] if targeted else [None]
            for target_index in target_choices:
                if nodes >= node_limit or monotonic() >= deadline:
                    exhausted = True
                    return
                nodes += 1
                next_hp = dict(hp)
                actual_damage = damage or 0
                if target_index is not None and vulnerable.get(target_index, 0) > 0 and actual_damage:
                    actual_damage = (actual_damage * 3 + 1) // 2
                if target_index is not None and actual_damage:
                    next_hp[target_index] = max(0, next_hp[target_index] - actual_damage)
                # Vulnerable duration is decremented at the end of the turn,
                # not between card plays. Keep it active across this sequence.
                next_vulnerable = dict(vulnerable)
                if target_index is not None and applies:
                    next_vulnerable[target_index] = max(next_vulnerable.get(target_index, 0), applies)
                next_dealt = dealt + actual_damage
                kills_target = (_target_id(monsters[target_index], target_index)
                                if target_index is not None and next_hp[target_index] <= 0 else None)
                remaining_threat = sum(value for i, value in threat0.items()
                                       if next_hp.get(i, 0) > 0)
                fact = CombatFacts(
                    damage=actual_damage if damage is not None else None,
                    block=block, incoming_damage=initial_threat,
                    remaining_incoming_damage=remaining_threat,
                    lethal_confirmed=bool(kills_target), kills_target=kills_target,
                    energy_after=available - cost, uncertainty="none",
                    confidence="verified",
                    scores={"damage_score": float(actual_damage),
                            "block_score": float(block or 0)})
                visit(sequence + (index,), targets + (target_index,),
                      tuple(i for i in remaining if i != index), available - cost,
                      block_gain + (block or 0), next_hp, next_vulnerable,
                      next_dealt, facts_seen + (fact,))

    visit((), (), tuple(range(len(hand))), energy, 0, hp0,
          {i: max(0, _num(monsters[i].get("vulnerable"), 0) or 0) for i in alive}, 0, ())
    if best is None:
        return {"sequence": (), "score": None, "confidence": "unsupported",
                "uncertainty": ("search_budget_exhausted" if exhausted else
                                "unknown_effect" if saw_unknown else "no_legal_action"),
                "nodes": nodes, "budget_exhausted": exhausted}
    facts = best["facts"]
    immediate = facts[0].__dict__.copy() if facts else {}
    return {"sequence": best["sequence"], "target_path": best["targets"],
            "score": best["score"], "facts": facts, "immediate_facts": immediate,
            "projected_facts": tuple(f.__dict__.copy() for f in facts[1:]),
            "projected_sequence": best["sequence"][1:], "damage": best["dealt"],
            "block": best["block"], "lethal_confirmed": bool(immediate.get("lethal_confirmed")),
            "target_lethal_confirmed": best["lethal"],
            "target_index": (best["targets"][0] if best["targets"] and
                             best["targets"][0] is not None else
                             max(alive, key=lambda i: (threat0[i], -i))),
            "remaining_incoming_damage": best["remaining_threat"],
            "confidence": "verified", "uncertainty": "none", "nodes": nodes,
            "budget_exhausted": exhausted}
