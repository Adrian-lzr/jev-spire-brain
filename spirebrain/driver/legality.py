"""Validate a proposed player-facing action against the current live state."""

from __future__ import annotations


def check_action(game: dict, command: dict) -> tuple[bool, str]:
    verb = str(command.get("command", "")).lower()
    offered = {str(x).lower() for x in (game.get("available_commands") or [])}
    if not verb or verb in {"wait", "state", "start", "key", "click"}:
        return False, "当前没有可确认的玩家操作"
    if offered and verb not in offered:
        return False, f"游戏当前未提供 {verb} 操作"
    if verb == "choose":
        try:
            index = int(command.get("choice", command.get("index", -1)))
        except (TypeError, ValueError):
            return False, "选项序号无效"
        screen = str(game.get("screen_type", "")).upper()
        state = game.get("screen_state") or {}
        if screen == "MAP":
            map_state = game.get("map") or {}
            items = map_state.get("next_nodes", []) if isinstance(map_state, dict) else []
        elif screen == "REST":
            items = state.get("rest_options", [])
        elif screen == "EVENT":
            items = state.get("options", [])
        elif screen == "BOSS_REWARD":
            items = state.get("relics", [])
        elif screen in {"GRID", "CARD_SELECT", "HAND_SELECT", "CARD_REWARD"}:
            items = state.get("cards", [])
        elif screen in {"SHOP", "SHOP_SCREEN"}:
            items = (state.get("cards") or []) + (state.get("relics") or []) + (state.get("potions") or [])
            if state.get("purge_cost") is not None:
                items = [*items, {"price": state["purge_cost"]}]
        else:
            return False, "当前界面没有可核对的选项"
        if not isinstance(items, list) or not 0 <= index < len(items):
            return False, "建议的选项不在当前界面中"
        item = items[index]
        if isinstance(item, dict) and item.get("disabled"):
            return False, "建议的选项已被游戏禁用"
        if screen in {"SHOP", "SHOP_SCREEN"} and isinstance(item, dict):
            if int(item.get("price", 0) or 0) > int(game.get("gold", 0) or 0):
                return False, "金币不足以购买建议物品"
        return True, ""
    if verb == "play":
        combat = game.get("combat") or game.get("combat_state") or {}
        hand = combat.get("hand") or []
        try:
            index = int(command.get("card", -1))
            energy = int((combat.get("player") or {}).get("energy", 0))
        except (TypeError, ValueError):
            return False, "手牌或能量数据无效"
        if not 0 <= index < len(hand):
            return False, "建议的牌已不在手中"
        card = hand[index]
        if not isinstance(card, dict) or card.get("is_playable") is False:
            return False, "建议的牌当前不可打出"
        try:
            cost = int(card.get("cost", 0) or 0)
        except (TypeError, ValueError):
            return False, "卡牌费用数据无效"
        if cost < -1 or cost > energy:
            return False, "建议的牌当前能量不足"
        if card.get("has_target", str(card.get("type", "")).upper() == "ATTACK"):
            targets = combat.get("monsters") or []
            try:
                target = int(command.get("target", -1))
            except (TypeError, ValueError):
                return False, "目标序号无效"
            if not 0 <= target < len(targets):
                return False, "建议的敌人目标已不存在"
            enemy = targets[target]
            if enemy.get("is_gone") or enemy.get("half_dead") or int(enemy.get("current_hp", 0) or 0) <= 0:
                return False, "建议的敌人目标已失效"
        return True, ""
    if verb == "potion":
        potions = game.get("potions") or []
        try:
            slot = int(command.get("slot", -1))
        except (TypeError, ValueError):
            return False, "药水槽位无效"
        if (not 0 <= slot < len(potions) or not isinstance(potions[slot], dict)
                or not potions[slot].get("can_use")):
            return False, "建议的药水当前不可使用"
        if potions[slot].get("requires_target"):
            targets = (game.get("combat") or game.get("combat_state") or {}).get("monsters", [])
            try:
                target = int(command.get("target", -1))
            except (TypeError, ValueError):
                return False, "药水目标序号无效"
            if not 0 <= target < len(targets):
                return False, "药水缺少有效敌人目标"
            enemy = targets[target]
            if enemy.get("is_gone") or enemy.get("half_dead") or int(enemy.get("current_hp", 0) or 0) <= 0:
                return False, "药水的敌人目标已失效"
        return True, ""
    return verb in {"end", "return", "proceed", "confirm", "cancel"}, "不支持的建议动作"
