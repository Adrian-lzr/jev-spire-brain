from spirebrain.driver.witness import Advice
from spirebrain.overlay.feed import advice_event


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
