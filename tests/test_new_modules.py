"""Tests for the newly completed decision points (rest / shop / boss relic),
the Noul veracity band, and the state digests.

A tiny programmable client (`_PClient`) replies by question *type*, which is what
lets us drive each module down both its confident and its uncertain branch
without a network or an API key.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from spirebrain.jev_brain.client import (
    ChoiceSpec,
    JevAnswer,
    JevClient,
    JevResponse,
    MockJevClient,
    NoulSpec,
    ScoreSpec,
)
from spirebrain.jev_brain.decisions import (
    ALL_POINTS,
    BossRelicJudge,
    CardRewardJudge,
    CombatRiskGate,
    MapRouter,
    RestSiteDecider,
    ShopDecider,
    noul_verdict,
)
from spirebrain.jev_brain.state import (
    card_line,
    combat_state,
    deck_digest,
    deck_summary,
    map_choices,
    path_damage_probes,
    run_state,
    shop_items,
    worst_case_damage,
)
from spirebrain.tactical.hp_budget import HPBudget


class _PClient(JevClient):
    """Reply by question type with prescribed values/confidences."""

    backend_name = "prog"

    def __init__(self, *, noul: float = 0.5, choice: str | None = None,
                 choice_conf: float = 0.9, score: float = 0.5,
                 score_conf: float = 0.9) -> None:
        self.noul = noul
        self.choice = choice
        self.choice_conf = choice_conf
        self.score = score
        self.score_conf = score_conf
        self.seen: list[dict] = []

    def ask(self, state, questions):
        self.seen.append({"state": state, "questions": questions})
        out = {}
        for k, spec in questions.items():
            if isinstance(spec, NoulSpec):
                out[k] = JevAnswer(self.noul, self.noul, raw={"type": "noul", "noul": self.noul})
            elif isinstance(spec, ChoiceSpec):
                label = self.choice if self.choice in spec.criteria else next(iter(spec.criteria))
                out[k] = JevAnswer(label, self.choice_conf, raw={"type": "choice", "choice": label})
            elif isinstance(spec, ScoreSpec):
                out[k] = JevAnswer(self.score, self.score_conf,
                                   raw={"type": "score", "score": self.score,
                                        "legend": dict(enumerate(spec.criteria))})
            else:
                raise TypeError(spec)
        return JevResponse(answers=out, latency_ms=3, backend=self.backend_name, model="prog")


def _hp(act=1, cur=80):
    return HPBudget(act=act, max_hp=80, current_hp=cur)


# --------------------------------------------------------------------------- #
# Noul veracity band
# --------------------------------------------------------------------------- #
def test_noul_verdict_band():
    assert noul_verdict(0.99) is True
    assert noul_verdict(0.60) is True
    assert noul_verdict(0.59) is None
    assert noul_verdict(0.50) is None
    assert noul_verdict(0.41) is None
    assert noul_verdict(0.40) is False
    assert noul_verdict(0.01) is False


# --------------------------------------------------------------------------- #
# Choice acceptance: distribution strength over confidence
# --------------------------------------------------------------------------- #
def test_accept_choice_uses_distribution_not_confidence():
    from spirebrain.jev_brain.client import JevAnswer
    from spirebrain.jev_brain.decisions import accept_choice

    # Flat-ish distribution: top option only 0.14 ahead -> defer, even though a
    # 0.55 confidence would be "close". This is the real shape our live run showed.
    flat = JevAnswer("a", 0.33, raw={"probabilities": {"a": 0.55, "b": 0.41, "c": 0.04}})
    assert accept_choice(flat) is False

    # Clearly separated top: act, at the SAME low confidence. Confidence is not
    # what makes an answer actionable — the distribution is.
    clear = JevAnswer("a", 0.33, raw={"probabilities": {"a": 0.80, "b": 0.10, "c": 0.10}})
    assert accept_choice(clear) is True

    # Chosen option is not the most likely one -> never act, however confident.
    wrong = JevAnswer("b", 0.95, raw={"probabilities": {"a": 0.80, "b": 0.10}})
    assert accept_choice(wrong) is False


def test_accept_choice_falls_back_to_confidence_without_a_distribution():
    from spirebrain.jev_brain.client import JevAnswer
    from spirebrain.jev_brain.decisions import accept_choice

    # MockJevClient and ScriptedJevClient publish no probabilities; behaviour
    # must stay exactly as before so the offline tests remain meaningful.
    bare_low = JevAnswer("a", 0.55, raw={"type": "choice", "choice": "a"})
    bare_high = JevAnswer("a", 0.90, raw={"type": "choice", "choice": "a"})
    assert accept_choice(bare_low) is False
    assert accept_choice(bare_high) is True


# --------------------------------------------------------------------------- #
# Rest sites
# --------------------------------------------------------------------------- #
def test_rest_heals_when_model_says_so():
    d = RestSiteDecider(_PClient(noul=0.92), _hp(), goal="g").decide(
        hp_ratio=0.8, upgradable={"Bash": "attack"})
    assert d.point == "rest" and d.value == "rest"
    assert d.used_fallback is False


def test_rest_upgrades_when_heal_not_needed():
    d = RestSiteDecider(_PClient(noul=0.05, choice="Bash"), _hp(), goal="g").decide(
        hp_ratio=0.9, upgradable={"Bash": "attack", "Defend": "block"})
    assert d.value == "Bash" and d.used_fallback is False


def test_rest_unsure_and_low_hp_heals():
    d = RestSiteDecider(_PClient(noul=0.5), _hp(cur=30)).decide(
        hp_ratio=0.4, upgradable={"Bash": "attack"})
    assert d.value == "rest" and d.used_fallback is True
    assert "low HP" in d.detail["reason"]


def test_rest_unsure_and_high_hp_still_heals():
    """An unnecessary rest costs an opportunity; an unnecessary upgrade can cost the run."""
    d = RestSiteDecider(_PClient(noul=0.5), _hp(), None).decide(
        hp_ratio=0.95, upgradable={"Bash": "attack"})
    assert d.value == "rest" and d.used_fallback is True
    assert "rest is never wrong" in d.detail["reason"]


def test_rest_nothing_to_upgrade():
    d = RestSiteDecider(_PClient(noul=0.9), _hp()).decide(hp_ratio=0.9, upgradable={})
    assert d.value == "rest" and d.used_fallback is True


def test_rest_error_falls_back_to_heal():
    class Boom(JevClient):
        backend_name = "boom"

        def ask(self, state, questions):
            raise RuntimeError("network down")

    d = RestSiteDecider(Boom(), _hp()).decide(hp_ratio=0.8, upgradable={"Bash": "x"})
    assert d.value == "rest" and d.used_fallback is True


# --------------------------------------------------------------------------- #
# Shops
# --------------------------------------------------------------------------- #
ITEMS = {"Ornamental Fan": (150, "block after 3 attacks"),
         "Meat on the Bone": (165, "heal at end of combat")}


def test_shop_buys_the_highest_probability_item():
    from spirebrain.jev_brain.client import ScriptedJevClient

    jev = ScriptedJevClient({
        "buy_Ornamental Fan": (0.88, 0.88),
        "buy_Meat on the Bone": (0.20, 0.20),
    })
    d = ShopDecider(jev, goal="g").decide(gold=400, items=ITEMS)
    assert d.value == "Ornamental Fan" and d.used_fallback is False
    assert d.detail["probabilities"]["buy_Meat on the Bone"] == 0.20


def test_shop_leaves_when_nothing_is_worth_it():
    d = ShopDecider(_PClient(noul=0.10), goal="g").decide(gold=400, items=ITEMS)
    assert d.value == "leave" and d.used_fallback is False


def test_shop_uncertain_keeps_the_gold():
    d = ShopDecider(_PClient(noul=0.5), goal="g").decide(gold=400, items=ITEMS)
    assert d.value == "leave" and d.used_fallback is True
    assert "keep the gold" in d.detail["reason"]


def test_shop_nothing_affordable():
    d = ShopDecider(_PClient(noul=0.99), goal="g").decide(gold=10, items=ITEMS)
    assert d.value == "leave" and d.used_fallback is True
    assert "nothing affordable" in d.detail["reason"]


def test_shop_offers_card_removal_as_an_option():
    from spirebrain.jev_brain.client import ScriptedJevClient

    jev = ScriptedJevClient({
        "buy_Ornamental Fan": (0.30, 0.30),
        "buy_Meat on the Bone": (0.25, 0.25),
        "buy_remove": (0.91, 0.91),
    })
    d = ShopDecider(jev, goal="g").decide(
        gold=400, items=ITEMS, removal_cost=75, remove_candidate="Strike")
    assert d.value == "remove" and d.used_fallback is False


# --------------------------------------------------------------------------- #
# Boss relics (mandatory pick)
# --------------------------------------------------------------------------- #
RELICS = {"Philosopher's Stone": "energy, enemies gain strength",
          "Runic Dome": "energy, no enemy intents",
          "Coffee Dripper": "energy, cannot rest"}


def test_boss_relic_picks_highest_score():
    from spirebrain.jev_brain.client import ScriptedJevClient

    jev = ScriptedJevClient({
        "Philosopher's Stone": (0.90, 0.95),
        "Runic Dome": (0.20, 0.95),
        "Coffee Dripper": (0.50, 0.95),
    })
    d = BossRelicJudge(jev, goal="g").decide(RELICS)
    assert d.value == "Philosopher's Stone" and d.used_fallback is False
    assert set(d.detail["scores"]) == set(RELICS)


def test_boss_relic_mandatory_even_when_unsure():
    d = BossRelicJudge(_PClient(score=0.5, score_conf=0.10), goal="g").decide(RELICS)
    assert d.value in RELICS  # never skips: the game forces a choice
    assert d.used_fallback is True
    assert "mandatory" in d.detail["reason"]


def test_boss_relic_none_offered():
    d = BossRelicJudge(_PClient(), goal="g").decide({})
    assert d.value is None and d.used_fallback is True


# --------------------------------------------------------------------------- #
# Map probe override
# --------------------------------------------------------------------------- #
def test_map_router_overrides_when_probe_says_over_budget():
    jev = _PClient(noul=0.95, choice="n2")
    d = MapRouter(jev, _hp()).decide({"n1": "monster", "n2": "elite"}, {"n1": 5, "n2": 70})
    assert d.used_fallback is True
    assert d.value == "n1"  # rerouted to the cheapest path
    assert "over HP budget" in d.detail["reason"]


def test_map_router_accepts_route_when_probe_clears_it():
    jev = _PClient(noul=0.05, choice="n2", choice_conf=0.9)
    d = MapRouter(jev, _hp()).decide({"n1": "monster", "n2": "elite"}, {"n1": 5, "n2": 10})
    assert d.value == "n2" and d.used_fallback is False
    # one call carried the route choice plus both probes
    assert len(jev.seen[0]["questions"]) == 3


# --------------------------------------------------------------------------- #
# Combat risk gate
# --------------------------------------------------------------------------- #
def test_combat_risk_confident_risky_takes_defensive_posture():
    d = CombatRiskGate(_PClient(noul=0.93), _hp()).decide("gremlin nob", 30)
    assert d.value is True and d.detail["posture"] == "defensive" and d.used_fallback is False


def test_combat_risk_confident_safe_is_greedy():
    d = CombatRiskGate(_PClient(noul=0.05), _hp()).decide("louse", 4)
    assert d.value is False and d.detail["posture"] == "greedy"


# --------------------------------------------------------------------------- #
# Card rewards under the corrected Score semantics
# --------------------------------------------------------------------------- #
def test_card_reward_skip_when_scores_are_low():
    d = CardRewardJudge(_PClient(score=0.33, score_conf=0.9), 10).decide({"a": "x", "b": "y"})
    assert d.value == "skip" and d.used_fallback is True


def test_card_reward_takes_high_score():
    d = CardRewardJudge(_PClient(score=0.95, score_conf=0.9), 10).decide({"a": "x", "b": "y"})
    assert d.value in ("a", "b") and d.used_fallback is False


def test_mock_client_score_normalises_to_middle_level():
    """MockJevClient returns the middle rubric level, so 0.5 on any odd level count."""
    d = CardRewardJudge(MockJevClient(), 10).decide({"a": "x"})
    assert d.value == "skip"  # 0.5 < 0.55 action floor


def test_all_seven_points_are_covered():
    assert len(ALL_POINTS) == 7
    assert set(ALL_POINTS) == {"map", "card_reward", "event", "rest", "shop",
                              "boss_relic", "combat_risk"}


# --------------------------------------------------------------------------- #
# State digests
# --------------------------------------------------------------------------- #
def test_card_line_and_deck_digest():
    assert card_line({"name": "Strike", "cost": 1, "type": "Attack"}).startswith("Strike (1E)")
    assert card_line({"name": "Bash", "cost": 2, "upgrades": 1}).startswith("Bash+ (2E)")
    assert card_line({"name": "Whirlwind", "cost": -1}).startswith("Whirlwind (XE)")
    digest = deck_digest([{"name": "Strike"}, {"name": "Defend"}])
    assert "Strike" in digest and "Defend" in digest


def test_deck_digest_truncates():
    cards = [{"name": f"Card{i}", "cost": 1} for i in range(60)]
    assert "and 20 more cards" in deck_digest(cards, max_lines=40)


def test_deck_summary_counts():
    s = deck_summary([{"name": "A", "type": "Attack", "cost": 1},
                      {"name": "B", "type": "Attack", "cost": 1},
                      {"name": "C", "type": "Skill", "cost": 2}])
    assert s["size"] == 3 and s["by_type"]["Attack"] == 2 and s["by_cost"]["1"] == 2


def test_map_choices_and_probes():
    nodes = [{"id": "n1", "symbol": "E", "y": 4}, {"id": "n2", "symbol": "R", "y": 5}]
    choices = map_choices(nodes)
    assert choices["n1"].startswith("elite fight")
    assert "floor 4" in choices["n1"]
    probes = path_damage_probes(nodes, act=1)
    assert probes["n1"] == worst_case_damage("E", 1) == 26
    assert probes["n2"] == 0


def test_run_state_shape():
    st = run_state(act=2, floor=17, character="Ironclad", hp=40, max_hp=80, gold=120,
                   deck=[{"name": "Strike", "cost": 1}], relics=["Burning Blood"],
                   potions=[{"name": "Fire Potion"}], budget_remaining=8,
                   budget_reserved=32, goal="ascension_20_win")
    assert st["hp"]["ratio"] == 0.5
    assert st["relics"] == "Burning Blood"
    assert st["potions"] == "Fire Potion"
    assert st["hp_budget"]["remaining_spendable"] == 8
    assert st["goal"] == "ascension_20_win"
    import json
    json.dumps(st)  # must be JSON-serialisable: it is sent as the API `state`


def test_combat_state_shape():
    st = combat_state(encounter="Gremlin Nob", turn=2, hp=30, max_hp=80,
                      monster_hp=[{"name": "Nob", "hp": 82}],
                      hand=[{"name": "Strike", "cost": 1}],
                      predicted_damage=18, budget_remaining=5)
    assert st["predicted_incoming_damage"] == 18
    assert st["hand"][0].startswith("Strike")
    assert st["hp_budget"]["remaining_spendable"] == 5


def test_shop_items_shape_and_duplicate_handling():
    raw = [{"name": "Ornamental Fan", "price": 150, "description": "block"},
           {"name": "Ornamental Fan", "price": 150, "description": "block"},
           {"id": "potion_x", "cost": 50}]
    items = shop_items(raw)
    assert items["Ornamental Fan"] == (150, "block")
    assert items["Ornamental Fan (second copy)"][0] == 150
    assert items["potion_x"][0] == 50


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all new-module tests passed")
