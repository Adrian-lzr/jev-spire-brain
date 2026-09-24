"""Host-dependent lookups must be testable without Steam or a game install."""

from __future__ import annotations

import json
from pathlib import Path

from spirebrain import gamedata
from spirebrain.doctor import find_steam_libraries
from spirebrain.environment import EnvironmentAdapter
from spirebrain.install_mod_config import config_dir


FIXTURE = Path(__file__).parent / "fixtures" / "gamedata" / "localization.json"


def test_environment_adapter_resolves_explicit_paths_only(tmp_path):
    env = EnvironmentAdapter(environ={"LOCALAPPDATA": str(tmp_path / "appdata"),
                                      "STS_GAME_DIR": str(tmp_path / "game")})
    assert env.local_appdata == tmp_path / "appdata"
    assert env.game_dir == tmp_path / "game"
    assert config_dir("CommunicationMod", env) == (
        tmp_path / "appdata" / "ModTheSpire" / "CommunicationMod")
    assert find_steam_libraries(EnvironmentAdapter(environ={}, steam_roots=(tmp_path,))) == []


def test_game_directory_lookup_uses_injected_sts_path(tmp_path):
    game = tmp_path / "game"
    game.mkdir()
    (game / gamedata.JAR_NAME).write_bytes(b"synthetic jar placeholder")
    env = EnvironmentAdapter(environ={"STS_GAME_DIR": str(game)})
    assert gamedata.find_game_dir(env, candidates=[]) == game


def test_game_data_fixture_parses_names_and_effects_without_jar(tmp_path):
    tables = json.loads(FIXTURE.read_text(encoding="utf-8"))
    data = gamedata.GameData(tables=tables, loaded=True, source="synthetic-fixture")
    assert data.display_name("cards", "Strike_R", character="IRONCLAD") == "Strike"
    assert data.card_effect("Bash", values={"D": 8, "M": 2}) == (
        "Deal 8 damage. Apply 2 Vulnerable.")
    assert data.card_line("Unknown") == "Unknown: effect not found in the game's card data"
