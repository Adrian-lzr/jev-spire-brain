"""Translate CommunicationMod's live shape into the router's screen vocabulary.

CommunicationMod reports an active fight as screen_type=NONE with room_phase=COMBAT
and puts the hand under combat_state. The older router expected screen_type=COMBAT
and a `combat` object. Keep the raw fields too: they remain the source of truth.
"""

from __future__ import annotations

from dataclasses import dataclass


def _repair_text(value):
    """Repair the common UTF-8 bytes decoded once as a Windows code page."""
    if not isinstance(value, str):
        return value
    if "\ufffd" in value:
        # Replacement characters mean the original bytes were already lost;
        # never send that mojibake to the overlay as if it were a name.
        return "文本不可用"
    if not any(ord(ch) > 0x7f for ch in value):
        return value
    for encoding in ("gb18030", "cp1252", "latin1"):
        try:
            repaired = value.encode(encoding).decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            continue
        # Prefer a result containing CJK text over the mojibake source.
        if sum(0x3400 <= ord(ch) <= 0x9fff for ch in repaired) > sum(
                0x3400 <= ord(ch) <= 0x9fff for ch in value):
            return repaired
    return value


def _repair_tree(value):
    if isinstance(value, str):
        return _repair_text(value)
    if isinstance(value, list):
        return [_repair_tree(item) for item in value]
    if isinstance(value, dict):
        repaired = {_repair_text(str(key)): _repair_tree(item) for key, item in value.items()}
        # CommunicationMod can expose a replacement character after its
        # original localized bytes have already been lost. Keep the stable
        # game id useful: resolve a local display name when possible, otherwise
        # show the id instead of propagating "文本不可用" into every panel.
        damaged_name = _missing_display_text(repaired.get("name"))
        damaged_label = _missing_display_text(repaired.get("label"))
        damaged_text = _missing_display_text(repaired.get("text"))
        if damaged_name or damaged_label or damaged_text:
            identity = next((str(repaired[key]).strip() for key in
                             ("id", "card_id", "relic_id", "potion_id", "monster_id",
                              "event_id")
                             if repaired.get(key) and repaired[key] != "文本不可用"), "")
            if damaged_name and identity:
                repaired["name"] = _localized_name(repaired, identity)
            fallback = identity or _option_fallback(repaired)
            if damaged_label:
                repaired["label"] = fallback
            if damaged_text:
                repaired["text"] = fallback
        return repaired
    return value


def _missing_display_text(value) -> bool:
    return isinstance(value, str) and (not value.strip() or value == "文本不可用")


def _localized_name(item: dict, identity: str) -> str:
    """Resolve damaged display names using the language-independent game id."""
    try:
        from spirebrain import gamedata

        data = gamedata.get()
        character = item.get("character")
        for table in ("cards", "relics", "potions", "monsters", "powers"):
            name = data.display_name(table, identity, character=character)
            if name:
                return str(name)
    except Exception:  # noqa: BLE001 - display enrichment cannot break state intake
        pass
    return identity


def _option_fallback(item: dict) -> str:
    for key in ("choice_index", "index", "slot"):
        try:
            return f"选项 {int(item[key]) + 1}"
        except (KeyError, TypeError, ValueError):
            continue
    return "未命名选项"


@dataclass(frozen=True)
class GameSnapshot:
    """Normalized CommunicationMod state plus stable identity for a decision."""

    payload: dict
    state_id: str
    run_id: str
    screen_type: str
    character: str

    @classmethod
    def from_communication(cls, game: dict) -> "GameSnapshot":
        payload = normalize_game_state(game if isinstance(game, dict) else {})
        from spirebrain.brain.planner import stable_state_id

        character = str(payload.get("character") or payload.get("class") or "").upper()
        run_id = str(payload.get("run_id") or payload.get("runId") or payload.get("seed")
                     or f"local:{character}")
        return cls(
            payload=payload,
            state_id=stable_state_id(payload),
            run_id=run_id,
            screen_type=str(payload.get("screen_type") or payload.get("screen") or "").upper(),
            character=character,
        )


def normalize_game_state(game: dict) -> dict:
    state = _repair_tree(dict(game))
    if not state.get("character"):
        state["character"] = state.get("class", "")
    if (str(state.get("screen_type", "")).upper() == "NONE"
            and str(state.get("room_phase", "")).upper() == "COMBAT"
            and isinstance(state.get("combat_state"), dict)):
        state["screen_type"] = "COMBAT"
        state["combat"] = state["combat_state"]
    if state.get("screen_type") == "MAP" and isinstance(state.get("map"), list):
        screen = state.get("screen_state") or {}
        if isinstance(screen, dict) and isinstance(screen.get("next_nodes"), list):
            state["full_map"] = state["map"]
            state["map"] = {"next_nodes": screen["next_nodes"]}
            return state
        # The full map is not itself a list of reachable choices. We only infer
        # the next row when the current floor identifies it unambiguously.
        if isinstance(screen, dict) and not screen.get("next_nodes"):
            floor = state.get("floor")
            try:
                target_y = max(0, int(floor) - (int(state.get("act", 1)) - 1) * 17)
            except (TypeError, ValueError):
                target_y = None
            if target_y is not None:
                row = [node for node in state["map"] if isinstance(node, dict)
                       and node.get("y") == target_y]
                if row:
                    # At the start of an act the first row is reachable. Later,
                    # keep only children of a known current node. Without one,
                    # do not pretend every node in the row can be selected.
                    current = state.get("current_map_node") or screen.get("current_node")
                    if target_y == 0:
                        screen = {**screen, "next_nodes": row}
                    elif isinstance(current, dict):
                        links = {(c.get("x"), c.get("y")) for c in current.get("children", [])
                                 if isinstance(c, dict)}
                        allowed = [n for n in row if (n.get("x"), n.get("y")) in links]
                        if allowed:
                            screen = {**screen, "next_nodes": allowed}
                    state["screen_state"] = screen
                    state["map"] = screen
    return state
