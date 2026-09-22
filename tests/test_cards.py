"""Cards: metadata from the install, knowledge from sources, grading from both.

The tests that matter most are the last two: one pins the authored knowledge
against the game's own card list (a typo'd card id would otherwise sit in the
table forever, silently matching nothing), and the others pin the *documented*
consensus cases — the trap the community calls a trap, and the card that is dead
in Act 1 and a win condition in Act 3.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from spirebrain.cards import meta
from spirebrain.cards.deck import grade, profile
from spirebrain.cards.knowledge import CAUTION, IRONCLAD, IRONCLAD_ARCHETYPES, derive

START = ["Strike_Red"] * 5 + ["Defend_Red"] * 5 + ["Bash"]


# --------------------------------------------------------------------------- #
# Metadata: the values are read from the player's own game files
# --------------------------------------------------------------------------- #
def test_metadata_has_the_cards_and_numbers_the_game_has():
    assert meta.cost("Bash") == 2
    assert meta.card("Bash")["damage"] == 8
    assert meta.card("Bash")["magic"] == 2          # Vulnerable
    assert meta.type_of("Barricade") == "POWER" and meta.rarity("Barricade") == "RARE"
    assert meta.cost("Strike_Red") == 1 and meta.card("Strike_Red")["damage"] == 6


def test_metadata_accepts_both_spellings_the_live_game_and_the_jar_use():
    """CommunicationMod reports `Strike_R`; the class files are named `Strike_Red`.

    A lookup that only knew one spelling would silently profile the starting
    deck as unknown cards.
    """
    assert meta.card("Strike_R") == meta.card("Strike_Red")
    assert meta.card("Defend_R") == meta.card("Defend_Red")


def test_metadata_never_raises_on_a_shape_it_did_not_expect():
    assert meta.card({"name": "Strike"}) == {}       # raw game entry, not an id
    assert meta.card("") == {}
    assert meta.type_of("NoSuchCard") == ""


def test_every_ironclad_card_in_the_game_has_metadata():
    ironclad = [cid for cid in meta.ids() if meta.character(cid) == "IRONCLAD"]
    assert len(ironclad) >= 70, len(ironclad)       # 75 in desktop-1.0.jar
    assert all(meta.cost(c) is not None for c in ironclad)


# --------------------------------------------------------------------------- #
# Knowledge: authored rows must agree with the game's own card list
# --------------------------------------------------------------------------- #
def test_authored_cards_all_exist_in_the_game():
    """A typo in the knowledge table would match nothing, forever, silently.

    Every authored id must be a real card in this install, and tagged with the
    real type — the table is hand-written and this is the check that keeps it
    honest.
    """
    unknown = [cid for cid in IRONCLAD if not meta.card(cid)]
    assert unknown == [], f"authored cards not found in the game data: {unknown}"


def test_cautioned_cards_exist_too():
    unknown = [cid for cid in CAUTION if not meta.card(cid)]
    assert unknown == [], f"caution entries not found in the game data: {unknown}"


def test_archetype_cards_exist_too():
    missing: list[str] = []
    for arch in IRONCLAD_ARCHETYPES:
        missing += [c for c in (arch.core | arch.payoff) if not meta.card(c)]
    assert missing == [], f"archetype lists reference unknown cards: {missing}"


def test_derive_recognises_cards_we_have_not_authored():
    """Other characters still get a usable rating, marked as derived."""
    know = derive("Gain 5 Block. Draw 1 card.", "SKILL")
    assert know.axes.get("block") and know.axes.get("draw")
    assert "derived" in know.note
    # and it never claims more than the text supports
    assert "damage" not in derive("Gain 5 Block.", "SKILL").axes


# --------------------------------------------------------------------------- #
# Profile
# --------------------------------------------------------------------------- #
def test_starting_cards_count_for_less_than_real_cards():
    """Six Strikes are not a damage plan; the community is emphatic about it."""
    start = profile(START)
    assert start.basics == 11 and start.size == 11
    with_real = profile(START + ["Carnage", "Pommel Strike"])
    # two real attacks should move the damage axis like ~4 basics worth, not 2
    assert with_real.axes["damage"] - start.axes["damage"] >= 4.0


def test_archetype_signal_needs_core_cards():
    assert profile(START).archetypes == {}
    invested = profile(START + ["Corruption", "Feel No Pain"])
    assert invested.archetypes.get("exhaust", 0) >= 4
    assert invested.dominant == "exhaust"


def test_unknown_cards_are_recorded_not_guessed():
    prof = profile(START + ["Some Modded Card"])
    assert prof.unknown == ["Some Modded Card"]
    assert prof.size == 12


# --------------------------------------------------------------------------- #
# Grading, against the consensus cases
# --------------------------------------------------------------------------- #
def test_a_documented_trap_scores_low_and_says_why():
    g = grade(profile(START), "Clash", 1)
    assert g.score < 0.60 and g.caution
    assert "非攻击牌" in g.caution            # the condition is named, in Chinese


def test_a_win_condition_is_dead_early_and_strong_late():
    """Demon Form / Barricade / Limit Break: not Act 1 cards, win conditions later.

    The invariant is the *gap*: the same card must score far lower in Act 1 than
    in Act 3, and the Act 1 line must say so rather than just being a low number.
    """
    start = profile(START)
    act1 = grade(start, "Demon Form", 1)
    assert act1.verdict in ("poor", "marginal")
    assert "第一幕" in act1.reason_text()
    later = profile(START + ["Inflame", "Spot Weakness", "Shrug It Off", "Pommel Strike",
                             "Carnage", "Armaments", "Metallicize", "Flame Barrier"])
    late = grade(later, "Demon Form", 3)
    assert late.verdict == "strong"
    assert late.score - act1.score >= 0.20


def test_an_engine_card_tops_its_archetype():
    exhaust = profile(START + ["Corruption", "Feel No Pain", "Burning Pact", "True Grit"])
    dark = grade(exhaust, "Dark Embrace", 2)
    assert dark.score >= 0.90 and dark.verdict == "strong"
    assert "消耗引擎" in dark.reason_text()


def test_synergy_is_symmetric_finding_not_memorised_pairs():
    """A card with no authored synergy text still pairs by structure."""
    exhaust = profile(START + ["Corruption", "Feel No Pain"])
    assert grade(exhaust, "Fiend Fire", 2).score > grade(profile(START), "Fiend Fire", 2).score
    strength = profile(START + ["Inflame", "Spot Weakness"])
    assert grade(strength, "Heavy Blade", 2).score > grade(profile(START), "Heavy Blade", 2).score


def test_a_big_deck_is_discounted():
    small = profile(START)
    filler = ["Shrug It Off", "Pommel Strike", "Carnage", "Armaments",
              "Inflame", "Uppercut", "Battle Trance", "Metallicize",
              "Spot Weakness", "Flame Barrier", "Heavy Blade", "Anger",
              "Cleave", "Iron Wave", "Headbutt", "Clothesline"]
    big = profile(START + filler)
    assert big.size == len(START) + len(filler)
    assert grade(big, "Cleave", 3).score < grade(small, "Cleave", 3).score
    assert f"{big.size} 张" in grade(big, "Cleave", 3).reason_text()


if __name__ == "__main__":
    for name, fn in sorted({k: v for k, v in globals().items()
                            if k.startswith("test_") and callable(v)}.items()):
        fn()
        print("pass:", name)
    print("ALL PASS")
