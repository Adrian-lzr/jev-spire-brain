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


@dataclass(frozen=True)
class ActionSuggestion:
    """A single action for a human player, with its evidence boundary."""

    command: dict
    reason: str
    uncertain: bool = False


def recommend_action(game: dict) -> ActionSuggestion | None:
    """Recommend one legal action from a CommunicationMod combat snapshot.

    This uses only visible runtime facts. Missing damage/block values remain
    unknown; the helper never presents a guessed amount as exact. Every call is
    independent, so the next player action triggers a fresh recommendation.
    """
    combat = game.get("combat") or game.get("combat_state") or {}
    player = combat.get("player") or {}
    try:
        energy = int(player.get("energy", 0))
        hp = int(player.get("current_hp", game.get("current_hp", 0)))
        block = int(player.get("block", 0))
        max_hp = int(player.get("max_hp", game.get("max_hp", max(hp, 1))))
    except (TypeError, ValueError):
        return None
    offered = {str(x).lower() for x in game.get("available_commands", [])}
    monsters = []
    incoming = 0
    for i, monster in enumerate(combat.get("monsters") or []):
        if not isinstance(monster, dict) or monster.get("is_gone") or monster.get("half_dead"):
            continue
        enemy_hp = int(monster.get("current_hp", 0) or 0)
        if enemy_hp <= 0:
            continue
        effective_hp = enemy_hp + int(monster.get("block", 0) or 0)
        monsters.append((i, monster, effective_hp))
        if "ATTACK" in str(monster.get("intent", "")).upper():
            damage = int(monster.get("move_adjusted_damage", monster.get("damage", -1)) or -1)
            hits = int(monster.get("move_hits", 1) or 1)
            if damage >= 0:
                incoming += damage * max(1, hits)

    # Known healing potions can prevent a lethal turn without spending energy.
    # Potion Slot is an empty slot in CommunicationMod, never a consumable.
    if not offered or "potion" in offered:
        for slot, potion in enumerate(game.get("potions") or []):
            if not isinstance(potion, dict) or not potion.get("can_use"):
                continue
            identity = str(potion.get("id") or potion.get("name") or "").lower()
            if "blood" in identity or "regen" in identity or "fairy" in identity:
                if hp <= max(1, incoming - block) or hp / max(1, max_hp) < 0.30:
                    return ActionSuggestion(
                        {"command": "potion", "action": "use", "slot": slot},
                        "当前生命危险，先使用可用的回复药水；具体回复量以游戏说明为准。",
                        True)

    cards = []
    for index, card in enumerate(combat.get("hand") or []):
        if not isinstance(card, dict):
            continue
        try:
            cost = int(card.get("cost", 0))
        except (TypeError, ValueError):
            continue
        if card.get("is_playable") is False or cost < -1 or cost > energy:
            continue
        if offered and "play" not in offered:
            continue
        target_required = bool(card.get("has_target", str(card.get("type", "")).upper() == "ATTACK"))
        if target_required and not monsters:
            continue
        card_type = str(card.get("type", "")).upper()
        damage = card.get("damage")
        block_value = card.get("block")
        try:
            damage = int(damage) if damage is not None else None
            block_value = int(block_value) if block_value is not None else None
        except (TypeError, ValueError):
            damage, block_value = None, None
        identity = str(card.get("id") or card.get("name") or "")
        if damage is None:
            damage = {"Strike_R": 6, "Bash": 8}.get(identity)
            if damage is not None and int(card.get("upgrades", 0) or 0):
                damage += 2 if identity == "Bash" else 3
        if block_value is None:
            block_value = {"Defend_R": 5}.get(identity)
            if block_value is not None and int(card.get("upgrades", 0) or 0):
                block_value += 3
        cards.append((index, card, card_type, damage, block_value))

    def command_for(index: int, card: dict, damage: int | None) -> dict:
        cmd = {"command": "play", "card": index}
        if card.get("has_target", str(card.get("type", "")).upper() == "ATTACK"):
            viable = sorted(monsters, key=lambda m: (m[2], m[0]))
            lethal = next((m for m in viable if damage is not None and damage >= m[2]), None)
            if viable:
                cmd["target"] = (lethal or viable[0])[0]
        return cmd

    # A known kill on the only live attacker ends its damage; take it first.
    if len(monsters) == 1:
        lethal = [(i, c, d) for i, c, typ, d, _ in cards
                  if typ == "ATTACK" and c.get("damage") is not None
                  and not player.get("powers") and not monsters[0][1].get("powers")
                  and d is not None and d >= monsters[0][2]]
        if lethal:
            i, card, damage = min(lethal, key=lambda x: int(x[1].get("cost", 0) or 0))
            return ActionSuggestion(command_for(i, card, damage),
                                    "这张攻击牌已知可以击败当前敌人，先结束威胁。")

    uncovered = max(0, incoming - block)
    blockers = [(i, c, b) for i, c, _, _, b in cards if b is not None and b > 0]
    if uncovered > 0 and blockers:
        i, card, value = max(blockers, key=lambda x: (min(uncovered, x[2]), -int(x[1].get("cost", 0) or 0)))
        return ActionSuggestion(command_for(i, card, None),
                                f"已知将受到约 {incoming} 点攻击，当前格挡 {block}；先补格挡。")

    attacks = [(i, c, d) for i, c, typ, d, _ in cards if typ == "ATTACK"]
    if attacks:
        i, card, damage = max(attacks, key=lambda x: (x[2] or 0, -int(x[1].get("cost", 0) or 0)))
        return ActionSuggestion(command_for(i, card, damage),
                                "当前先用可打出的攻击牌推进战斗。" +
                                ("伤害及特殊效果请以当前牌面和敌人状态为准。"
                                 if card.get("damage") is None or player.get("powers") else ""),
                                card.get("damage") is None or bool(player.get("powers")))
    utilities = [(i, c) for i, c, typ, _, _ in cards if typ in {"POWER", "SKILL"}]
    if utilities:
        i, card = utilities[0]
        return ActionSuggestion(command_for(i, card, None),
                                "当前没有可确认收益的攻击或格挡；这张牌可打出，效果请以牌面为准。", True)
    if not offered or "end" in offered:
        return ActionSuggestion({"command": "end"}, "没有可打出的牌，结束回合。")
    return None
