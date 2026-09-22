"""Card metadata, read from the player's own install (see tools/extract_card_meta.py).

The data file is committed so the agent never needs a JDK at runtime: a run is
not the moment to shell out to `javap`. Re-run the tool after a game update.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

DATA = Path(__file__).resolve().parents[1] / "data" / "card_meta.json"


@lru_cache(maxsize=1)
def _table() -> dict[str, dict]:
    if not DATA.exists():
        return {}
    return json.loads(DATA.read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def _index() -> dict[str, dict]:
    """Every card under every name it is known by.

    Two systems meet here and they are not the same names: the class in the jar
    (`PommelStrike`, `Strike_Red`) and the in-game ID (`Pommel Strike`,
    `Strike_R`) that the localization, a live snapshot and the authored
    knowledge table all use. Indexing both means a lookup cannot quietly miss
    because the caller happened to hold the other spelling — the failure mode
    that made every multi-word card look like an unknown card.
    """
    index: dict[str, dict] = {}
    for class_id, entry in _table().items():
        index.setdefault(class_id, entry)
        game_id = entry.get("game_id")
        if isinstance(game_id, str) and game_id:
            index.setdefault(game_id, entry)
    return index


def card(card_id: str) -> dict:
    """Metadata for a card, by either its game ID or its class name; {} if unknown.

    A non-string id returns {} rather than raising: the live game hands out raw
    entries (dicts) in some snapshots, and a decision path that dies on a shape
    it did not expect is worse than one that degrades to "unknown card".
    """
    if not isinstance(card_id, str) or not card_id:
        return {}
    index = _index()
    if card_id in index:
        return index[card_id]
    for candidate in (card_id.replace("_R", "_Red"), card_id.replace("_Red", "_R")):
        if candidate in index:
            return index[candidate]
    return {}


def ids() -> list[str]:
    """Class-name ids, one per card (the canonical key for the data file)."""
    return sorted(_table())


def cost(card_id: str) -> int | None:
    return card(card_id).get("cost")


def type_of(card_id: str) -> str:
    return str(card(card_id).get("type", ""))


def rarity(card_id: str) -> str:
    return str(card(card_id).get("rarity", ""))


def character(card_id: str) -> str:
    return str(card(card_id).get("character", ""))


def is_attack(card_id: str) -> bool:
    return type_of(card_id) == "ATTACK"


def is_power(card_id: str) -> bool:
    return type_of(card_id) == "POWER"


def is_skill(card_id: str) -> bool:
    return type_of(card_id) == "SKILL"


def basic_rare(card_id: str) -> bool:
    return rarity(card_id) in ("BASIC",)
