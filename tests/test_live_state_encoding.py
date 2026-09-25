from spirebrain.driver.live_state import normalize_game_state


def test_normalize_repairs_utf8_decoded_as_windows_codepage():
    # "燃烧之血" encoded as UTF-8 and decoded once as a legacy code page.
    mojibake = "燃烧之血".encode("utf-8").decode("latin1")
    state = normalize_game_state({"relics": [mojibake], "screen_type": "MAP"})
    assert state["relics"] == ["燃烧之血"]


def test_normalize_preserves_normal_cjk_text():
    state = normalize_game_state({"event": "得到一件遗物", "screen_type": "EVENT"})
    assert state["event"] == "得到一件遗物"
