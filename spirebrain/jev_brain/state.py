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

from dataclasses import dataclass, field
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
def card_values(card: dict) -> dict:
    """The game's runtime numbers for a card, when the snapshot carries them.

    CommunicationMod exposes `damage` / `block` / `magic_number` per card
    instance, and those are exactly what the localization's `!D!` / `!B!` / `!M!`
    slots stand for. Passing them lets the real text render with real numbers
    instead of placeholders — and when they are absent we leave the visible
    placeholder rather than guess a number.
    """
    out: dict = {}
    for token, field in (("D", "damage"), ("B", "block"), ("M", "magic_number")):
        v = card.get(field)
        if isinstance(v, int) and v > 0:
            out[token] = v
    return out


def card_effect_text(name: str, *, upgraded: bool = False,
                     character: str | None = None, values: dict | None = None) -> str | None:
    """Real card text from the player's game install, or None. Never raises.

    Uses the process-wide `gamedata` singleton (read-only cache), so a machine
    without Slay the Spire installed simply gets None and every digest falls back
    to names — no hard dependency, no fabricated effects.
    """
    try:
        from spirebrain import gamedata

        return gamedata.get().card_effect(name, upgraded=upgraded,
                                          character=character, values=values)
    except Exception:  # noqa: BLE001 - enrichment must never break a decision
        return None


def lookup_keys(obj: dict) -> list[str]:
    """Candidate game-data lookup keys for a card/relic/potion, best first.

    **`id` before `name`, and this is not cosmetic.** Verified 2026-09-21 from a
    live launch: this machine's game runs with `"LANGUAGE": "ZHS"` in
    `preferences/STSGameplaySettings`, and CommunicationMod reports display names
    in the game's own language. `gamedata` indexes the *English* localization
    files, so a Chinese `name` ("打击") matches nothing and every card silently
    degrades to "effect not found" — the exact failure this module exists to
    prevent, reintroduced through the language setting.

    `id` is language-independent and is the same key the localization files use
    ("Strike_R", "Shrug It Off"), so it works under any language. `name` stays as
    a fallback for callers that have no id (the offline simulator's synthetic
    decks, and any hand-built state).
    """
    keys: list[str] = []
    for field in ("id", "card_id", "relic_id", "name"):
        value = obj.get(field)
        if isinstance(value, str) and value and value not in keys:
            keys.append(value)
    return keys


def english_name(keys: Iterable[str], table: str = "cards",
                 character: str | None = None) -> str | None:
    """The English display name from the game's data, if any of `keys` resolves.

    JEV is asked English questions about English effects, so feeding it "打击"
    alongside "Deal 6 damage." would be worse than feeding it "Strike". Returns
    None when nothing resolves, and the caller keeps whatever name it had.
    """
    try:
        from spirebrain import gamedata

        data = gamedata.get()
        for key in keys:
            name = data.display_name(table, key, character=character)
            if name:
                return name
    except Exception:  # noqa: BLE001 - enrichment must never break a decision
        return None
    return None


def card_line(card: dict, character: str | None = None) -> str:
    """One readable line for a card: cost, type, and its real effect text.

    The effect comes from the game's own data unless the caller supplied one. We
    never invent a card effect; if the text cannot be found the line simply omits
    it, and the question that uses this line is expected to say so.
    """
    keys = lookup_keys(card) or ["unknown card"]
    upgraded = bool(card.get("upgrades") or card.get("is_upgraded"))
    cost = card.get("cost")
    cost_txt = "X" if cost == -1 else ("-" if cost is None else str(cost))
    # Show the English name when the game data can supply it; the localised name
    # is what the game says, but the questions around it are English.
    label = english_name(keys, "cards", character) or card.get("name") or keys[0]
    parts = [f"{label}{'+' if upgraded else ''} ({cost_txt}E)"]
    if card.get("type"):
        parts.append(str(card["type"]))
    if card.get("description"):
        parts.append(str(card["description"]))
    else:
        text = None
        for key in keys:
            text = card_effect_text(key, upgraded=upgraded, character=character,
                                   values=card_values(card))
            if text:
                break
        if text:
            parts.append(text)
    return " - ".join(parts)


def relic_effect_text(name: str) -> str | None:
    """Real relic text from the player's game install, or None. Never raises."""
    try:
        from spirebrain import gamedata

        return gamedata.get().relic_effect(name)
    except Exception:  # noqa: BLE001
        return None


def deck_digest(cards: Iterable[dict], max_lines: int = 40,
                character: str | None = None) -> str:
    """Flat, countable digest of the deck, with real effect text per card.

    Sorted for stable prompts/logs. Intended to be the *inputs* to a judgement:
    a model asked "is this card good for this deck?" cannot answer while both
    sides of the comparison are bare names.
    """
    lines = sorted(card_line(c, character=character) for c in cards)
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

    Estimates come from NODE_DAMAGE_HINT, or from the node's own `damage_hint`
    when it has one. That override exists because the offline harness generates
    maps per seed, and without it every map of the same shape would cost the same
    — which is how this project spent ten "different" ascents producing one
    identical HP trajectory (docs/MEASUREMENTS.md, run 6).
    """
    probes: dict[str, int] = {}
    for n in nodes:
        node_id = str(n.get("id", f"{n.get('x')},{n.get('y')}"))
        hint = n.get("damage_hint")
        probes[node_id] = (int(hint) if hint is not None
                           else worst_case_damage(str(n.get("symbol", "?")), act))
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


# --------------------------------------------------------------------------- #
# RunContext — the one place the run's facts live
# --------------------------------------------------------------------------- #
@dataclass
class RunContext:
    """Everything a judgement might need to know about the run, in one object.

    Why this exists: the first live run against real JEV (2026-09-21) returned
    61 of 63 answers below the confidence floor, and the recorded payloads showed
    why — modules were hand-building thin states. The shop question passed
    `{goal, gold}` and asked "is this worth the gold for this run's goal?"; JEV
    answered ~0.4, which is its documented way of saying *this cannot be answered
    from what you gave me*.

    So the fix is not a prompt tweak, it is giving the model the run. Decision
    modules take an optional `run=RunContext(...)`; when present, every question
    is evaluated against `digest()` (deck contents and shape, relics, potions,
    HP ratio, act, floor, gold, HP budget, goal) instead of a two-field stub.

    Kept as plain data with no I/O so it is trivially constructible in tests and
    fillable from either spirecomm game objects or the offline simulator.
    """

    act: int = 1
    floor: int = 0
    character: str = ""
    hp: int = 80
    max_hp: int = 80
    gold: int = 0
    deck: list = field(default_factory=list)
    relics: list = field(default_factory=list)
    potions: list = field(default_factory=list)
    budget_remaining: int | None = None
    budget_reserved: int | None = None
    goal: str = ""

    @property
    def hp_ratio(self) -> float:
        return round(self.hp / self.max_hp, 3) if self.max_hp else 0.0

    def with_budget(self, budget) -> RunContext:
        """Fill the HP-budget fields from an HPBudget instance (in place, chained)."""
        if budget is not None:
            self.budget_remaining = budget.remaining_budget
            self.budget_reserved = budget.reserved_hp
            self.act = budget.act
            self.hp = budget.current_hp
            self.max_hp = budget.max_hp
        return self

    def deck_line(self, max_lines: int = 40) -> str:
        return deck_digest(self.deck, max_lines=max_lines, character=self.character)

    def digest(self, extra: dict | None = None) -> dict:
        """The state object sent to JEV: full run facts, plus call-specific extras."""
        state = run_state(
            act=self.act,
            floor=self.floor,
            character=self.character,
            hp=self.hp,
            max_hp=self.max_hp,
            gold=self.gold,
            deck=self.deck,
            relics=self.relics,
            potions=self.potions,
            budget_remaining=self.budget_remaining,
            budget_reserved=self.budget_reserved,
            goal=self.goal,
        )
        if extra:
            state.update(extra)
        return state
