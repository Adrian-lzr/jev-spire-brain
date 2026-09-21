"""Tests for the Score acceptance modes and the batch aggregator.

These cover the machinery behind the pre-registered experiment in
docs/MEASUREMENTS.md: "does a value-floor argmax take card rewards, without
hurting decision quality?" The point of testing it offline is that the offline
run proves the *switch works* and nothing about whether argmax is a good idea —
only the live run with real JEV can say that.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from spirebrain.jev_brain.client import (
    JevAnswer,
    JevClient,
    JevResponse,
    ScoreSpec,
)
from spirebrain.jev_brain.decisions import (
    SCORE_ACCEPTANCE,
    CardRewardJudge,
    evaluate_score,
)
from spirebrain.sim.batch import summarise
from spirebrain.sim.run_offline import load_seeds

CARD_RUBRIC = ["bad", "filler", "solid", "excellent"]


def _score(value: float, probs: dict[str, float] | None = None,
           confidence: float = 0.35, level: float | None = None) -> JevAnswer:
    """A Score answer shaped like the real API's (probabilities keyed by level)."""
    raw = {"type": "score", "score": value if level is None else level,
           "legend": dict(enumerate(CARD_RUBRIC))}
    if probs is not None:
        raw["probabilities"] = probs
    return JevAnswer(value, confidence, raw=raw)


# --------------------------------------------------------------------------- #
# evaluate_score — margin mode (the incumbent)
# --------------------------------------------------------------------------- #
def test_the_default_gate_is_the_one_the_experiment_selected():
    """argmax was adopted 2026-09-21 on the pre-registered comparison
    (docs/MEASUREMENTS.md runs 6-9). If this flips back, it needs an experiment."""
    assert SCORE_ACCEPTANCE == "argmax"


def test_the_strategy_file_and_the_code_agree_on_the_gate():
    """The gate is a strategy-layer knob; a config/code mismatch would mean the
    config silently does nothing."""
    import json
    from pathlib import Path

    from spirebrain.jev_brain.decisions import VALID_SCORE_ACCEPTANCE

    root = Path(__file__).resolve().parents[1]
    strategy = json.loads((root / "config" / "strategy.json").read_text(encoding="utf-8"))
    configured = strategy["jev"]["score_acceptance"]
    assert configured in VALID_SCORE_ACCEPTANCE
    assert configured == SCORE_ACCEPTANCE, (
        f"config says {configured!r}, code defaults to {SCORE_ACCEPTANCE!r}"
    )


def test_margin_rejects_a_flat_distribution_even_above_the_value_floor():
    # Exactly the live shape: Pommel Strike normalised to 0.69, levels 2 and 3
    # nearly tied -> no margin -> skipped. This is run 5's finding, encoded.
    flat = _score(0.69, {"0": 0.02, "1": 0.18, "2": 0.44, "3": 0.36}, level=2.4)
    detail = evaluate_score(flat, mode="margin")
    assert detail["accepted"] is False
    assert detail["value_ok"] is True          # the value was never the problem
    assert detail["margin_ok"] is False
    assert "flat distribution" in detail["reason"]


def test_margin_accepts_a_peaked_distribution_at_the_same_value():
    peaked = _score(0.69, {"0": 0.01, "1": 0.06, "2": 0.83, "3": 0.10}, level=2.0)
    detail = evaluate_score(peaked, mode="margin")
    assert detail["accepted"] is True
    assert detail["peaked"] is True
    assert detail["reason"] == ""


def test_margin_rejects_below_the_action_floor():
    detail = evaluate_score(_score(0.32, {"0": 0.60, "1": 0.35, "2": 0.04, "3": 0.01},
                                   level=0.4), mode="margin")
    assert detail["accepted"] is False
    assert detail["value_ok"] is False
    assert "below action floor" in detail["reason"]


def test_margin_rejects_when_the_landed_level_is_not_the_favoured_one():
    # Peaked, but the distribution's mode is level 1 while the model scored 2.7.
    inconsistent = _score(0.9, {"0": 0.02, "1": 0.80, "2": 0.14, "3": 0.04}, level=2.7)
    detail = evaluate_score(inconsistent, mode="margin")
    assert detail["accepted"] is False
    assert detail["landed_is_top"] is False
    assert "distribution favours" in detail["reason"]


def test_margin_without_a_distribution_keeps_the_old_rule():
    # MockJevClient publishes no probabilities; behaviour must stay as before.
    assert evaluate_score(_score(0.70, confidence=0.90), mode="margin")["accepted"] is True
    assert evaluate_score(_score(0.70, confidence=0.55), mode="margin")["accepted"] is False
    assert evaluate_score(_score(0.40, confidence=0.90), mode="margin")["accepted"] is False


# --------------------------------------------------------------------------- #
# evaluate_score — argmax mode (the candidate)
# --------------------------------------------------------------------------- #
def test_argmax_accepts_the_same_flat_distribution_margin_rejected():
    flat = _score(0.69, {"0": 0.02, "1": 0.18, "2": 0.44, "3": 0.36}, level=2.4)
    assert evaluate_score(flat, mode="margin")["accepted"] is False
    argmax = evaluate_score(flat, mode="argmax")
    assert argmax["accepted"] is True
    assert argmax["mode"] == "argmax"
    # The distribution is still reported — the experiment needs to see both.
    assert argmax["margin_ok"] is False and argmax["value_ok"] is True


def test_argmax_still_respects_the_value_floor():
    low = _score(0.32, {"0": 0.60, "1": 0.35, "2": 0.04, "3": 0.01}, level=0.4)
    detail = evaluate_score(low, mode="argmax")
    assert detail["accepted"] is False
    assert "below action floor" in detail["reason"]


def test_argmax_without_a_distribution_behaves_like_margin():
    assert evaluate_score(_score(0.70, confidence=0.90), mode="argmax")["accepted"] is True
    assert evaluate_score(_score(0.70, confidence=0.55), mode="argmax")["accepted"] is False


def test_unknown_mode_is_an_error_not_a_silent_default():
    try:
        evaluate_score(_score(0.7, {"0": 0.9, "1": 0.1}, level=0.0), mode="whatever")
    except ValueError as exc:
        assert "unknown score acceptance mode" in str(exc)
    else:
        raise AssertionError("a typo in the mode must fail loudly")


# --------------------------------------------------------------------------- #
# The judges honour the switch
# --------------------------------------------------------------------------- #
class _ScoreClient(JevClient):
    """Returns a prescribed flat distribution per card, shaped like the real API."""

    backend_name = "prog"

    def __init__(self, per_card: dict[str, tuple[float, dict[str, float], float]]) -> None:
        self.per_card = per_card

    def ask(self, state, questions):
        out = {}
        for name, spec in questions.items():
            assert isinstance(spec, ScoreSpec)
            value, probs, level = self.per_card[name]
            out[name] = JevAnswer(value, 0.35,
                                  raw={"type": "score", "score": level,
                                       "probabilities": probs,
                                       "legend": dict(enumerate(spec.criteria))})
        return JevResponse(answers=out, latency_ms=3, backend=self.backend_name, model="prog")


CARDS = {
    "Pommel Strike": (0.69, {"0": 0.02, "1": 0.18, "2": 0.44, "3": 0.36}, 2.4),
    "Twin Strike": (0.56, {"0": 0.03, "1": 0.30, "2": 0.42, "3": 0.25}, 1.9),
    "Anger": (0.33, {"0": 0.45, "1": 0.38, "2": 0.12, "3": 0.05}, 0.7),
}


def test_card_reward_skips_under_margin_and_reports_why():
    d = CardRewardJudge(_ScoreClient(CARDS), deck_size=10,
                        acceptance="margin").decide({k: "text" for k in CARDS})
    assert d.value == "skip" and d.used_fallback is True
    assert "flat distribution" in d.detail["reason"]
    assert d.detail["gate"]["value_ok"] is True      # it was never about the value
    assert d.detail["gate"]["ranking"][0] == "Pommel Strike"


def test_card_reward_takes_the_argmax_under_argmax_mode():
    d = CardRewardJudge(_ScoreClient(CARDS), deck_size=10, acceptance="argmax").decide(
        {k: "text" for k in CARDS})
    assert d.value == "Pommel Strike"
    assert d.used_fallback is False
    assert d.detail["gate"]["mode"] == "argmax"


def test_acceptance_mode_never_overrides_the_deck_size_cap():
    d = CardRewardJudge(_ScoreClient(CARDS), deck_size=25, max_cards=25,
                        acceptance="argmax").decide({k: "text" for k in CARDS})
    assert d.value == "skip" and d.used_fallback is True
    assert "deck at 25/25" in d.detail["reason"]


# --------------------------------------------------------------------------- #
# Seed split and batch aggregation
# --------------------------------------------------------------------------- #
def test_seed_groups_exist_and_are_disjoint():
    cal, test = load_seeds("calibration"), load_seeds("test")
    assert cal and test
    assert not set(cal) & set(test), "the test set must not overlap the calibration set"


def test_unknown_seed_group_is_an_error():
    try:
        load_seeds("nope")
    except KeyError as exc:
        assert "nope" in str(exc)
    else:
        raise AssertionError("a typo in the seed group must fail loudly")


def _fake_run(seed: int, cards: int, hp_ends: list[int], fallbacks: int,
              cost: float, rejections: int = 0) -> dict:
    steps = [{"kind": "map", "fb": False}, {"kind": "card_reward", "fb": True}]
    return {
        "seed": seed, "acceptance": "argmax", "cards_taken": cards,
        "deck_size_start": 10, "deck_end": 10 + cards,
        "acts": [{"act": i + 1, "hp_end": hp, "steps": steps} for i, hp in enumerate(hp_ends)],
        "confidence_floor": 0.6, "jev_calls": 24, "jev_cost_usd": cost,
        "score_rejections": [{"act": 1, "reason": "flat distribution", "value": 0.69}] * rejections,
    }


def test_summarise_aggregates_cards_hp_and_cost():
    s = summarise([_fake_run(1, 2, [60, 45, 30], 2, 0.0008, rejections=1),
                   _fake_run(2, 0, [70, 55, 40], 4, 0.0009)])
    assert s["runs"] == 2
    assert s["cards_taken_total"] == 2 and s["cards_taken_mean"] == 1.0
    assert s["runs_with_cards"] == 1
    assert s["deck_end_mean"] == 11.0
    assert s["hp_act1_end_mean"] == 65.0 and s["hp_act3_end_mean"] == 35.0
    assert s["fallback_total"] == 6
    assert s["fallback_rate"] == 0.5
    assert s["cost_usd"] == 0.0017
    assert len(s["rejections"]) == 1


def test_summarise_of_nothing_does_not_pretend():
    assert summarise([]) == {"runs": 0}


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
