"""Translate CommunicationMod's live shape into the router's screen vocabulary.

CommunicationMod reports an active fight as screen_type=NONE with room_phase=COMBAT
and puts the hand under combat_state. The older router expected screen_type=COMBAT
and a `combat` object. Keep the raw fields too: they remain the source of truth.
"""

from __future__ import annotations


def normalize_game_state(game: dict) -> dict:
    state = dict(game)
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
