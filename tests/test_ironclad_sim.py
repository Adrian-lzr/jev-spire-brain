from __future__ import annotations

import json
from pathlib import Path

import pytest

from spirebrain.sim.ironclad import (
    IroncladCombatSimulator,
    copy_game_data_manifest,
    extract_minimal_game_snapshot,
    load_fixture,
    sanitize_snapshot,
)


ROOT = Path(__file__).parent
FIXTURE = ROOT / "fixtures" / "sim" / "ironclad_combat.json"


def _state():
    return load_fixture(FIXTURE)["states"][0]


def test_fixture_is_explicitly_synthetic_and_deterministic():
    state = _state()
    actions = [{"command": "play", "card": 0, "target": 0},
               # Bash is removed from the hand, so Strike moves into slot 0.
               {"command": "play", "card": 0, "target": 0}]
    first = IroncladCombatSimulator().run(state, actions).as_dict()
    second = IroncladCombatSimulator().run(state, actions).as_dict()
    assert first == second
    # Bash applies vulnerable after its hit; the subsequent Strike therefore
    # deals 9 (6 * 1.5), leaving three HP.
    assert first["state"]["monsters"][0]["current_hp"] == 3
    assert first["state"]["player"]["energy"] == 0


def test_invalid_target_and_energy_are_rejected_without_mutating_input():
    state = _state()
    original = json.loads(json.dumps(state))
    result = IroncladCombatSimulator().run(state, [{"command": "play", "card": 0, "target": 9}])
    assert result.legal is False
    assert result.uncertainty == "invalid_action"
    assert state == original

    result = IroncladCombatSimulator().run(state, [
        {"command": "play", "card": 0, "target": 0},
        {"command": "play", "card": 0, "target": 0},
        {"command": "play", "card": 0, "target": 0},
    ])
    assert result.legal is False
    assert result.rejection == "insufficient energy"


def test_unknown_effect_is_explicitly_uncertain():
    state = _state()
    state["combat"]["hand"] = [{"id": "Offering", "cost": 0, "draw": 3, "is_playable": True}]
    result = IroncladCombatSimulator().run(state, [{"command": "play", "card": 0}])
    assert result.legal is True
    assert result.uncertainty == "unknown_effect"
    assert result.events[-1]["type"] == "unknown_card"


def test_sanitize_snapshot_drops_transport_and_bounds_payload():
    state = {"character": "IRONCLAD", "timestamp": 123, "api_key": "secret",
             "combat": {"player": {"energy": 3}, "hand": []}}
    clean = sanitize_snapshot(state)
    assert "timestamp" not in clean and "api_key" not in clean
    assert "secret" not in json.dumps(clean)


def test_fixture_requires_type_and_kind(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"states": []}), encoding="utf-8")
    with pytest.raises(ValueError):
        load_fixture(bad)


def test_data_manifest_copies_only_desktop_jar(tmp_path):
    game = tmp_path / "game"
    game.mkdir()
    (game / "desktop-1.0.jar").write_bytes(b"synthetic game data")
    (game / "saves").mkdir()
    (game / "saves" / "save.json").write_text("private", encoding="utf-8")
    destination = tmp_path / "isolated"
    manifest = copy_game_data_manifest(game, destination)
    assert (destination / "desktop-1.0.jar").read_bytes() == b"synthetic game data"
    assert not (destination / "saves").exists()
    assert manifest["included"] == ["desktop-1.0.jar"]
    assert "saves" in manifest["excluded"]


def test_extract_minimal_snapshot_reads_only_requested_localization(tmp_path):
    import zipfile

    game = tmp_path / "game"
    game.mkdir()
    jar = game / "desktop-1.0.jar"
    with zipfile.ZipFile(jar, "w") as archive:
        archive.writestr("localization/eng/cards.json", json.dumps({
            "Strike_R": {"NAME": "Strike", "DESCRIPTION": "Deal !D! damage."},
            "Bash": {"NAME": "Bash", "DESCRIPTION": "Deal !D! damage."},
            "Other": {"NAME": "Other"},
        }))
        archive.writestr("localization/eng/relics.json", json.dumps({
            "Burning Blood": {"NAME": "Burning Blood", "DESCRIPTIONS": ["Heal 6."]},
            "Other Relic": {"NAME": "Other Relic"},
        }))
    result = extract_minimal_game_snapshot(game, tmp_path / "snapshot",
                                           card_ids=("Strike_R", "Missing"),
                                           relic_ids=("Burning Blood",))
    data = json.loads((tmp_path / "snapshot" / "ironclad_data_snapshot.json").read_text(encoding="utf-8"))
    assert result["source_kind"] == "live_copy"
    assert data["tables"]["cards"].keys() == {"Strike_R"}
    assert data["missing"]["cards"] == ["Missing"]
    assert data["excluded"] == ["saves", "preferences", "mods", "logs", "secrets"]
    assert len(data["source"]["sha256"]) == 64
