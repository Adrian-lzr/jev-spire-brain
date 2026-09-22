"""Card knowledge for the agent: what the cards are, and what they are worth here.

Layers, bottom up:

* `meta`      — cost/type/rarity/numbers, extracted from the player's own game
                install (`tools/extract_card_meta.py`), so a rating can never be
                about a different version of the game.
* `knowledge` — what each card *does for a deck* (capability axes), when it is
                worth taking (per-act priority), which archetypes it belongs to,
                and which cards it is structurally synergistic with.
* `deck`      — turns the deck the player actually has into a profile, then
                grades each candidate against it, with auditable reasons.

The split matters: the data files are facts about the game, the authored tables
are judgement (with `docs/CARD_STRATEGY.md` as the receipts), and the scoring is
arithmetic over both. Changing one does not mean re-checking the others.
"""

from spirebrain.cards.deck import (
    Choice,
    DeckProfile,
    PickGrade,
    best_removal,
    best_upgrade,
    grade,
    profile,
    synergy,
)
from spirebrain.cards.knowledge import (
    AXES,
    IRONCLAD_ARCHETYPES,
    Archetype,
    CardKnowledge,
    knowledge,
    removal_value,
    upgrade_value,
)
from spirebrain.cards.meta import card, cost, ids, rarity, type_of

__all__ = [
    "AXES",
    "Archetype",
    "CardKnowledge",
    "Choice",
    "DeckProfile",
    "IRONCLAD_ARCHETYPES",
    "PickGrade",
    "best_removal",
    "best_upgrade",
    "card",
    "cost",
    "grade",
    "ids",
    "knowledge",
    "profile",
    "rarity",
    "removal_value",
    "synergy",
    "type_of",
    "upgrade_value",
]
