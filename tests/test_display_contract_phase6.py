from spirebrain.driver.witness import Advice
from spirebrain.overlay.feed import DecisionFeed, advice_event
from spirebrain.overlay.server import _state_snapshot
import json
from pathlib import Path


def test_advice_display_status_is_semantic_and_backward_compatible():
    advice = Advice(point="combat", screen="COMBAT", command={"command": "end"},
                    key=("combat", "end"), label="结束回合", reason="先防守",
                    status="fast_advice", source_type="rule_fallback")
    event = advice_event(advice)
    assert event["status"] == "fast_advice"
    assert event["display_status"] == "local_advice"
    advice.status = "model_ready"
    assert advice_event(advice)["display_status"] == "complete_advice"


def test_no_alternative_is_serialized_when_missing():
    advice = Advice(point="shop", screen="SHOP", command={"command": "return"},
                    key=("shop", "return"), label="离开商店", reason="没有合适商品",
                    status="fast_advice", source_type="rule_fallback")
    event = advice_event(advice)
    assert event["alternative_label"] == ""
    assert event["alternative_command"] is None


def test_feed_replaces_unusable_advice_text_with_readable_fallback():
    advice = Advice(point="combat", screen="COMBAT", command={"command": "play"},
                    key=("combat", "play"), label="出 Strike → 文本不可用",
                    reason="目标：\ufffd", status="fast_advice",
                    alternative_label="文本不可用")
    event = advice_event(advice)
    assert event["label"] == "出 Strike → 目标信息缺失"
    assert event["reason"] == "目标：文本缺失"
    assert event["alternative_label"] == "目标信息缺失"


def test_shared_advice_fixture_is_consumable_by_python_feed():
    fixture_path = Path(__file__).parent / "fixtures" / "contracts" / "advice_event_v2.json"
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    payload = fixture["payload"]
    record = payload["last_advice"]
    advice = Advice(
        point=record["point"], screen=record["screen"],
        command=record["command"], key=("combat", "play"),
        label=record["label"], reason=record["reason"],
        status=record["status"], source_type=record["source_type"],
        fallback=record["fallback"], confidence=record["confidence"],
        state_id=record["state_id"], run_id=record["run_id"],
        run_epoch=record["run_epoch"], decision_id=record["decision_id"],
        advice_revision=record["advice_revision"],
        plan_id=record["plan_id"], request_id=record["request_id"],
        strategic_goal=record["strategic_goal"], uncertain=record["uncertain"],
        alternative_command=record["alternative_command"],
        alternative_label=record["alternative_label"],
    )
    event = advice_event(advice)
    for field, expected in fixture["expected_python"].items():
        assert event[field] == expected, field

    feed = DecisionFeed()
    feed.publish("run_state", payload["run_state"])
    feed.publish("advice", event)
    snapshot = _state_snapshot(feed)
    assert snapshot["current_state_id"] == payload["current_state_id"]
    java_advice = snapshot["last_advice"]
    for field in ("state_id", "plan_id", "strategic_goal", "alternative_label",
                  "alternative_condition", "confidence", "uncertain", "display_status",
                  "source_type"):
        assert java_advice[field] == event[field]
    java_run = snapshot["run_state"]
    for field in ("mode", "config_id", "model_connection_status"):
        assert java_run[field] == payload["run_state"][field]


def test_contract_checker_validates_java_parse_surface_without_game_jar():
    from tools.check_contracts import check

    assert check() == []


def test_contract_checker_rejects_fixture_type_or_enum_drift(tmp_path, monkeypatch):
    from tools import check_contracts

    fixture_path = Path(__file__).parent / "fixtures" / "contracts" / "advice_event_v2.json"
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    fixture["payload"]["last_advice"]["confidence"] = "unknown"
    fixture["payload"]["last_advice"]["display_status"] = "new_status_not_supported"
    broken = tmp_path / "broken-contract.json"
    broken.write_text(json.dumps(fixture, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(check_contracts, "FIXTURE", broken)

    errors = check_contracts.check()
    assert any("confidence" in error and "double" in error for error in errors)
    assert any("display status" in error for error in errors)
