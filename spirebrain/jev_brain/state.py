"""Game state digests: raw Slay the Spire data -> JEV-readable state.

Why this module exists
----------------------
The official docs recommend passing a **JSON object with named parts** rather
than one long string, and warn that Jev is English-first and text-only. So we
build small, well-named, English-language digests of exactly what a judgement
needs — nothing more. Every function here is pure and returns JSON-serialisable
data, so it can be unit-tested without the game.

Phase 1 wires these to real `spirecomm` game-state objects; until then the
functions accept plain dicts, which is also what the offline simulator feeds
them.
"""

from __future__ import annotations

from typing import Any, Iterable

# Map node symbols as they appear in the game's map data.
NODE_LABELS = {
    "M": "monster fight",
    "E": "elite fight (hard, good rewards)",
    "R": "rest site (heal or upgrade)",
    "$": "shop (spend gold)",
    "T": "treasure (free relic)",
    "?": "unknown event (random outcome)",
    "B": "boss",
}

# Rough damage bands per node type, used to seed HP-budget probes before the
# tactical layer can predict properly. Act index 1..3. These are deliberately
# coarse placeholders -- they exist so routing has *something*, and the tactical
# layer is expected to replace them with real predictions. Marked as estimates
# in the state we send, so the model knows how much to trust them.
NODE_DAMAGE_HINT = {
    "M": {1: (4, 10), 2: (8, 16), 3: (10, 20)},
    "E": {1: (14, 26), 2: (20, 34), 3: (24, 40)},
    "B": {1: (20, 32), 2: (28, 44), 3: (34, 52)},
    "?": {1: (0, 12), 2: (0, 18), 3: (0, 22)},
    "R": {1: (0, 0), 2: (0, 0), 3: (0, 0)},
    "$": {1: (0, 0), 2: (0, 0), 3: (0, 0)},
    "T": {1: (0, 0), 2: (0, 0), 3: (0, 0)},
}


def node_label(symbol: str) -> str:
    return NODE_LABELS.get(symbol, f"unknown node ({symbol})")


def worst_case_damage(symbol: str, act: int) -> int:
    """Upper end of the damage band for a node type. An estimate, not a prediction."""
    band = NODE_DAMAGE_HINT.get(symbol, {})
    return band.get(act, (0, 0))[1]


# --------------------------------------------------------------------------- #
# Cards / deck
# --------------------------------------------------------------------------- #
def card_line(card: dict) -> str:
    """One readable line for a card. Tolerates partial data."""
    name = card.get("name") or card.get("id") or "unknown card"
    upgraded = "+" if card.get("upgrades") or card.get("is_upgraded") else ""
    cost = card.get("cost")
    cost_txt = "X" if cost == -1 else ("-" if cost is None else str(cost))
    parts = [f"{name}{upgraded} ({cost_txt}E)"]
    if card.get("type"):
        parts.append(str(card["type"]))
    if card.get("description"):
        parts.append(str(card["description"]))
    return " - ".join(parts)


def deck_digest(cards: Iterable[dict], max_lines: int = 40) -> str:
    """Flat, countable digest of the deck. Sorted for stable prompts/logs."""
    lines = sorted(card_line(c) for c in cards)
    if len(lines) > max_lines:
        # Keep the digest cheap: the tail matters less than the archetype.
        extra = len(lines) - max_lines
        lines = lines[:max_lines] + [f"... and {extra} more cards"]
    return "; ".join(lines)


def deck_summary(cards: Iterable[dict]) -> dict:
    """Structural summary: counts by type and cost, plus sizes."""
    cards = list(cards)
    by_type: dict[str, int] = {}
    by_cost: dict[str, int] = {}
    for c in cards:
        by_type[str(c.get("type", "Unknown"))] = by_type.get(str(c.get("type", "Unknown")), 0) + 1
        cost = c.get("cost")
        key = "X" if cost == -1 else ("unplayable" if cost is None else str(cost))
        by_cost[key] = by_cost.get(key, 0) + 1
    return {"size": len(cards), "by_type": by_type, "by_cost": by_cost}


# --------------------------------------------------------------------------- #
# Relics / potions / gold
# --------------------------------------------------------------------------- #
def relic_digest(relics: Iterable[Any]) -> str:
    names = sorted(r.get("name", str(r)) if isinstance(r, dict) else str(r) for r in relics)
    return ", ".join(names) if names else "none"


def potion_digest(potions: Iterable[Any]) -> str:
    out = []
    for p in potions:
        if p is None:
            continue
        out.append(p.get("name", str(p)) if isinstance(p, dict) else str(p))
    return ", ".join(out) if out else "none"


# --------------------------------------------------------------------------- #
# Map
# --------------------------------------------------------------------------- #
def map_choices(nodes: Iterable[dict]) -> dict[str, str]:
    """Reachable nodes -> {node_id: description} for a Choice question.

    `nodes` items look like: {"id": "n3", "symbol": "E", "x": 3, "y": 7,
                              "children": [...]}.
    """
    choices: dict[str, str] = {}
    for n in nodes:
        symbol = str(n.get("symbol", "?"))
        desc = node_label(symbol)
        if n.get("y") is not None:
            desc += f" (floor {n['y']})"
        choices[str(n.get("id", f"{n.get('x')},{n.get('y')}"))] = desc
    return choices


def path_damage_probes(nodes: Iterable[dict], act: int) -> dict[str, int]:
    """{node_id: estimated worst-case damage} for the HP-budget Noul probes.

    Placeholder estimates from NODE_DAMAGE_HINT; the tactical layer should
    replace these with per-path predictions (sum over nodes to the next rest).
    """
    probes: dict[str, int] = {}
    for n in nodes:
        node_id = str(n.get("id", f"{n.get('x')},{n.get('y')}"))
        probes[node_id] = worst_case_damage(str(n.get("symbol", "?")), act)
    return probes


# --------------------------------------------------------------------------- #
# Composite states
# --------------------------------------------------------------------------- #
def run_state(
    *,
    act: int,
    floor: int,
    character: str = "",
    hp: int,
    max_hp: int,
    gold: int,
    deck: Iterable[dict] = (),
    relics: Iterable[Any] = (),
    potions: Iterable[Any] = (),
    budget_remaining: int | None = None,
    budget_reserved: int | None = None,
    goal: str = "",
) -> dict:
    """The standard digest attached to most judgement calls."""
    state: dict = {
        "act": act,
        "floor": floor,
        "character": character,
        "hp": {"current": hp, "max": max_hp, "ratio": round(hp / max_hp, 3) if max_hp else 0.0},
        "gold": gold,
        "deck": {"summary": deck_summary(deck), "contents": deck_digest(deck)},
        "relics": relic_digest(relics),
        "potions": potion_digest(potions),
    }
    if budget_remaining is not None:
        state["hp_budget"] = {"remaining_spendable": budget_remaining,
                              "reserved": budget_reserved}
    if goal:
        state["goal"] = goal
    return state


def combat_state(
    *,
    encounter: str,
    turn: int,
    hp: int,
    max_hp: int,
    monster_hp: Iterable[dict] = (),
    hand: Iterable[dict] = (),
    predicted_damage: int = 0,
    budget_remaining: int | None = None,
) -> dict:
    """Digest for the combat risk gate (and, later, tactical posture choice)."""
    state: dict = {
        "encounter": encounter,
        "turn": turn,
        "hp": {"current": hp, "max": max_hp},
        "monsters": list(monster_hp),
        "hand": [card_line(c) for c in hand],
        "predicted_incoming_damage": predicted_damage,
    }
    if budget_remaining is not None:
        state["hp_budget"] = {"remaining_spendable": budget_remaining}
    return state


def shop_items(raw_items: Iterable[dict]) -> dict[str, tuple[int, str]]:
    """Shop inventory -> the {label: (cost, description)} shape ShopDecider wants."""
    out: dict[str, tuple[int, str]] = {}
    for it in raw_items:
        label = str(it.get("name") or it.get("id") or "item")
        if label in out:  # same card twice on a shelf: disambiguate
            label = f"{label} (second copy)"
        out[label] = (int(it.get("price", it.get("cost", 0))), str(it.get("description", "")))
    return out
