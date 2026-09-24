"""Offline tests for the explicit scene and candidate boundaries."""

from __future__ import annotations

from spirebrain.brain.action_broker import build_action_candidates
from spirebrain.driver.live_state import GameSnapshot, normalize_game_state
from spirebrain.driver.scenes import SceneRouter


def test_game_snapshot_normalizes_combat_and_has_stable_identity():
    raw = {"character": "IRONCLAD", "run_id": "r1", "screen_type": "NONE",
           "room_phase": "COMBAT", "combat_state": {"hand": []}}
    snapshot = GameSnapshot.from_communication(raw)
    assert snapshot.screen_type == "COMBAT"
    assert snapshot.payload["combat"] == {"hand": []}
    assert snapshot.run_id == "r1"
    assert snapshot.state_id


def test_scene_router_keeps_grid_compatibility():
    handler = object()
    router = SceneRouter({"COMBAT": handler}, ("GRID", "CARD_SELECT"))
    assert router.resolve("combat") is handler
    assert router.resolve("SHOP") is None
    assert router.is_grid("grid")


def test_shop_candidate_diagnostics_explain_filtered_items():
    state = {
        "character": "IRONCLAD", "screen_type": "SHOP", "gold": 50,
        "available_commands": ["choose", "return"],
        "screen_state": {
            "cards": [{"id": "Expensive", "price": 100}],
            "relics": [{"id": "Cheap", "price": 40}],
            "potions": [],
        },
    }
    diagnostics: list[dict] = []
    candidates = build_action_candidates(state, diagnostics=diagnostics)
    assert any(item.candidate_id == "shop:item:1" for item in candidates)
    assert any(item["reason"] == "金币不足" for item in diagnostics)
