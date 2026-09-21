"""The live-game state we were never able to test until the first real launch.

Every fixture here is shaped like what CommunicationMod actually sent on
2026-09-21 22:28 (or what the same game sends mid-combat per its README example):
`id` fields are camel-cased, `name` fields are in the game's display language,
and `cost: -2` marks an unplayable card. The bugs these tests pin were all real:

- `LANGUAGE: ZHS` on this machine means display names arrive in Chinese, and the
  English effect lookup used to match on `name` — silently finding nothing.
- Card ids ("PommelStrike") do not equal localization keys ("Pommel Strike").
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from spirebrain.jev_brain.state import (
    card_line,
    card_values,
    english_name,
    lookup_keys,
)

# --------------------------------------------------------------------------- #
# lookup_keys: id must win over a localised name
# --------------------------------------------------------------------------- #
def test_id_comes_before_a_localised_name():
    card = {"id": "Strike_R", "name": "打击"}
    assert lookup_keys(card) == ["Strike_R", "打击"]


def test_name_only_cards_still_work_offline():
    assert lookup_keys({"name": "Pommel Strike"}) == ["Pommel Strike"]
    assert lookup_keys({}) == []


def test_empty_strings_are_not_candidates():
    assert lookup_keys({"id": "", "name": "Strike"}) == ["Strike"]


# --------------------------------------------------------------------------- #
# english_name / effect resolution through the game's own data
# --------------------------------------------------------------------------- #
def test_english_name_resolves_through_a_chinese_display_name():
    assert english_name(["Strike_R", "打击"], "cards", "IRONCLAD") == "Strike"


def test_camel_case_ids_match_space_preserving_keys():
    # CommunicationMod sends "PommelStrike"; the localization key is "Pommel Strike".
    assert english_name(["PommelStrike"], "cards") == "Pommel Strike"


def test_unknown_ids_return_none_without_raising():
    assert english_name(["NotARealCard", "不存在"], "cards") is None


def test_relics_resolve_by_id_too():
    assert english_name(["BurningBlood"], "relics") == "Burning Blood"


# --------------------------------------------------------------------------- #
# card_line: the digest line a JEV question actually embeds
# --------------------------------------------------------------------------- #
def test_chinese_card_renders_as_english_name_plus_real_text():
    card = {"id": "Strike_R", "name": "打击", "cost": 1, "type": "ATTACK",
            "upgrades": 0, "damage": 6}
    line = card_line(card, character="IRONCLAD")
    assert line.startswith("Strike (1E)")
    assert "Deal 6 damage." in line
    assert "打击" not in line  # the question language is English throughout


def test_camel_id_card_gets_effect_text_and_runtime_numbers():
    card = {"id": "PommelStrike", "name": "连枷打击", "cost": 1,
            "damage": 9, "magic_number": 1}
    line = card_line(card, character="IRONCLAD")
    assert line.startswith("Pommel Strike (1E)")
    assert "Deal 9 damage. Draw 1 card." in line


def test_unplayable_cost_renders_as_minus_two():
    card = {"id": "AscendersBane", "name": "飞升祸害", "cost": -2}
    line = card_line(card, character="IRONCLAD")
    assert line.startswith("Ascender's Bane (-2E)")


def test_a_total_unknown_stays_honest():
    card = {"id": "NotARealCard", "name": "不存在", "cost": 1}
    line = card_line(card)
    assert line == "不存在 (1E)"  # no invented text, localised name kept


def test_the_simulators_english_decks_are_unchanged():
    card = {"name": "Pommel Strike", "cost": 1, "type": "Attack"}
    line = card_line(card, character="IRONCLAD")
    assert line.startswith("Pommel Strike (1E) - Attack - ")


# --------------------------------------------------------------------------- #
# card_values: only real numbers, never a guess
# --------------------------------------------------------------------------- #
def test_runtime_numbers_map_to_the_right_slots():
    assert card_values({"damage": 9, "block": 5, "magic_number": 2}) == {
        "D": 9, "B": 5, "M": 2}


def test_missing_or_useless_values_are_absent_not_zero():
    assert card_values({}) == {}
    assert card_values({"damage": 0, "block": -1}) == {}  # 0/negative = no data


if __name__ == "__main__":
    import traceback

    passed = failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                passed += 1
                print(f"ok   {name}")
            except Exception:
                failed += 1
                print(f"FAIL {name}")
                traceback.print_exc()
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
