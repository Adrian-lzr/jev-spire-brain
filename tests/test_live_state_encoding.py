from spirebrain.driver.live_state import normalize_game_state


def test_normalize_repairs_utf8_decoded_as_windows_codepage():
    # "燃烧之血" encoded as UTF-8 and decoded once as a legacy code page.
    mojibake = "燃烧之血".encode("utf-8").decode("latin1")
    state = normalize_game_state({"relics": [mojibake], "screen_type": "MAP"})
    assert state["relics"] == ["燃烧之血"]


def test_normalize_preserves_normal_cjk_text():
    state = normalize_game_state({"event": "得到一件遗物", "screen_type": "EVENT"})
    assert state["event"] == "得到一件遗物"


def test_damaged_entity_names_fall_back_to_localized_game_identity(monkeypatch):
    from spirebrain import gamedata

    class LocalData:
        def display_name(self, table, identity, character=None):
            return {("relics", "Burning Blood"): "燃烧之血",
                    ("monsters", "Cultist"): "cultist"}.get((table, identity))

    monkeypatch.setattr(gamedata, "get", lambda: LocalData())
    state = normalize_game_state({
        "relics": [{"id": "Burning Blood", "name": "\ufffd"}],
        "combat": {"monsters": [{"id": "Cultist", "name": "\ufffd"}]},
        "screen_type": "COMBAT",
    })
    assert state["relics"][0]["name"] == "燃烧之血"
    assert state["combat"]["monsters"][0]["name"] == "cultist"


def test_damaged_event_text_uses_stable_choice_fallback():
    state = normalize_game_state({
        "screen_state": {"options": [{"choice_index": 0, "label": "\ufffd", "text": "\ufffd"}]},
        "screen_type": "EVENT",
    })
    option = state["screen_state"]["options"][0]
    assert option["label"] == "选项 1"
    assert option["text"] == "选项 1"
