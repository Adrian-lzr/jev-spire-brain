"""Repeatability probe: does the SAME state produce the SAME decision?

Why this exists
---------------
Every threshold in this project — the 0.60 confidence floor, the 0.50/0.15
distribution margins, `SCORE_ACTION_FLOOR`, the rest-site ratio — assumes the
model answers consistently enough that a threshold *means* something. Nobody in
this project has measured that. It is a real gap, not a hypothetical one: a
LessWrong audit experiment repeats every input five times precisely because
decision models drift between calls, and "temperature 0" is not a guarantee — for
a non-generative model the question is simply empirical.

What it measures
----------------
For one identical request, repeated N times:

* **value drift** — how far the answer itself moves (per question)
* **confidence drift** — min/max/spread of the reported confidence
* **decision flip rate** — how often OUR OWN gate (`accept_choice` /
  `accept_score` / `noul_verdict`) would have changed its verdict

The flip rate is the number that matters. A probability that wanders but never
crosses a threshold is harmless; one that straddles it makes the agent
nondeterministic in a way no amount of downstream tuning will fix. If a scenario
flips, its threshold is not a knob — it is a coin.

It drives the real decision modules rather than a copy of their questions, so what
is measured is the question set we actually ship.

Usage
-----
    python -m spirebrain.analysis.repeatability                  # 5 repeats, 4 scenarios
    python -m spirebrain.analysis.repeatability --repeats 9
    python -m spirebrain.analysis.repeatability --backend mock   # offline sanity check
    python -m spirebrain.analysis.repeatability --json out.json  # machine-readable
"""

from __future__ import annotations

import json
import statistics
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from spirebrain.jev_brain.client import JevClient, JevResponse, get_client
from spirebrain.jev_brain.decisions import (
    BossRelicJudge,
    CardRewardJudge,
    MapRouter,
    ShopDecider,
)
from spirebrain.jev_brain.state import RunContext, deck_digest
from spirebrain.tactical.hp_budget import HPBudget

ROOT = Path(__file__).resolve().parents[2]

# A realistic Act-1 Ironclad state, held fixed across every repeat.
DECK = [{"name": "Strike", "cost": 1, "type": "Attack"}] * 5 + \
       [{"name": "Defend", "cost": 1, "type": "Skill"}] * 4 + \
       [{"name": "Bash", "cost": 2, "type": "Attack"}]

RELICS = ["Burning Blood"]
POTIONS = [{"name": "Fire Potion"}]


class RecordingClient(JevClient):
    """Wraps any client and keeps every response, so drift is measured per question."""

    def __init__(self, inner: JevClient) -> None:
        self.inner = inner
        self.backend_name = inner.backend_name
        self.responses: list[JevResponse] = []

    def ask(self, state, questions) -> JevResponse:
        resp = self.inner.ask(state, questions)
        self.responses.append(resp)
        return resp


@dataclass
class Scenario:
    """One fixed decision we repeat identically."""

    name: str
    point: str
    run: RunContext
    run_module: callable
    outcomes: list = field(default_factory=list)

    def once(self, jev: JevClient, hp: HPBudget):
        module = self.run_module(jev, hp, self.run)
        decision = module()
        self.outcomes.append(decision)
        return decision


def build_scenarios(acceptance: str | None = None) -> list[Scenario]:
    hp = HPBudget(act=1, max_hp=80, current_hp=62)
    run = RunContext(act=1, floor=7, character="IRONCLAD", hp=62, max_hp=80,
                     gold=300, deck=DECK, relics=RELICS, potions=POTIONS,
                     goal="ascension_20_win").with_budget(hp)
    deck_len = len(DECK)

    def map_scenario(jev, hp_budget, r):
        # One Choice over three nodes + one Noul probe per node, exactly as shipped.
        router = MapRouter(jev, hp_budget, run=r)
        return lambda: router.decide(
            {"n1": "monster fight (moderate HP cost)",
             "n2": "elite fight (hard, good relic reward)",
             "n3": "rest site (no HP cost, heal or upgrade)"},
            {"n1": 6, "n2": 24, "n3": 0},
        )

    def card_scenario(jev, hp_budget, r):
        judge = CardRewardJudge(jev, deck_len, 25, deck_digest=deck_digest(DECK, character=r.character),
                                goal=r.goal, run=r, acceptance=acceptance)
        return lambda: judge.decide({"Pommel Strike": "", "Twin Strike": "", "Anger": ""})

    def shop_scenario(jev, hp_budget, r):
        shop = ShopDecider(jev, goal=r.goal, run=r)
        return lambda: shop.decide(
            gold=300,
            items={"Ornamental Fan": (150, ""), "Meat on the Bone": (165, ""),
                   "Card Removal": (75, "")},
            removal_cost=75, remove_candidate="a starter Strike",
        )

    def relic_scenario(jev, hp_budget, r):
        judge = BossRelicJudge(jev, goal=r.goal, run=r, acceptance=acceptance)
        return lambda: judge.decide({"Philosopher's Stone": "", "Runic Dome": "",
                                     "Coffee Dripper": ""})

    return [
        Scenario("map routing", "map", run, map_scenario),
        Scenario("card reward", "card_reward", run, card_scenario),
        Scenario("shop purchase", "shop", run, shop_scenario),
        Scenario("boss relic", "boss_relic", run, relic_scenario),
    ]


def _fmt(v) -> str:
    return f"{v:.4f}" if isinstance(v, float) else repr(v)


def summarise(scenario: Scenario) -> dict:
    """Compress one scenario's repeats into drift + flip-rate numbers."""
    values = [_fmt(d.value) for d in scenario.outcomes]
    confs = [float(d.confidence) for d in scenario.outcomes]
    falls = [bool(d.used_fallback) for d in scenario.outcomes]
    value_counts = Counter(values)
    dominant, dominant_n = value_counts.most_common(1)[0]
    return {
        "scenario": scenario.name,
        "point": scenario.point,
        "repeats": len(scenario.outcomes),
        "distinct_answers": len(value_counts),
        "dominant_answer": dominant,
        "dominant_share": round(dominant_n / len(values), 3),
        # the number that matters: how often the gate changed its mind
        "fallback_flip_rate": round(_flip_rate(falls), 3),
        "fallback_count": sum(falls),
        "confidence_min": round(min(confs), 4),
        "confidence_max": round(max(confs), 4),
        "confidence_spread": round(max(confs) - min(confs), 4),
        "confidence_mean": round(statistics.fmean(confs), 4),
        "per_question": _per_question(scenario),
    }


def _flip_rate(flags: list[bool]) -> float:
    """Share of repeats that disagree with the majority verdict."""
    if not flags:
        return 0.0
    majority = Counter(flags).most_common(1)[0][0]
    return sum(1 for f in flags if f != majority) / len(flags)


def _per_question(scenario: Scenario) -> dict:
    """Drift per question name, read off the recording client's responses."""
    out: dict[str, dict] = {}
    responses = getattr(scenario, "responses", [])
    if not responses:
        return out
    names = list(responses[0].answers)
    for name in names:
        vals, confs = [], []
        for resp in responses:
            ans = resp.answers.get(name)
            if ans is None:
                continue
            vals.append(ans.value)
            confs.append(float(ans.confidence))
        if not vals:
            continue
        entry: dict = {"n": len(vals), "distinct_values": len(set(map(_fmt, vals)))}
        numeric = [v for v in vals if isinstance(v, (int, float))]
        if numeric:
            entry["value_min"] = round(min(numeric), 4)
            entry["value_max"] = round(max(numeric), 4)
            entry["value_mean"] = round(statistics.fmean(numeric), 4)
        else:
            entry["dominant_value"] = Counter(map(_fmt, vals)).most_common(1)[0][0]
        entry["confidence_min"] = round(min(confs), 4)
        entry["confidence_max"] = round(max(confs), 4)
        out[name] = entry
    return out


def run(repeats: int = 5, backend: str = "openrouter", as_json: str | None = None,
        acceptance: str | None = None) -> dict:
    inner = get_client(backend)
    recorder = RecordingClient(inner)
    hp = HPBudget(act=1, max_hp=80, current_hp=62)
    results = []

    print(f"backend={backend}  repeats={repeats}  scenarios=4  "
          f"score_gate={acceptance or 'margin'}")
    print("Each scenario sends the IDENTICAL request N times; a threshold is only "
          "meaningful if the verdict does not move.\n")

    for scenario in build_scenarios(acceptance):
        for _ in range(repeats):
            scenario.once(recorder, hp)
        # attach the slice of responses belonging to this scenario
        scenario.responses = recorder.responses[-repeats:]
        summary = summarise(scenario)
        results.append(summary)

        print(f"— {summary['scenario']} ({summary['point']})")
        print(f"    answers        : {summary['dominant_answer']} ×{summary['dominant_share']:.0%} "
              f"({summary['distinct_answers']} distinct / {summary['repeats']} repeats)")
        print(f"    fallback       : {summary['fallback_count']}/{summary['repeats']}  "
              f"FLIP RATE {summary['fallback_flip_rate']:.0%}")
        print(f"    confidence     : {summary['confidence_min']:.3f}–{summary['confidence_max']:.3f} "
              f"(spread {summary['confidence_spread']:.3f}, mean {summary['confidence_mean']:.3f})")
        for q, st in summary["per_question"].items():
            if "value_mean" in st:
                rng = f"{st['value_min']:.3f}–{st['value_max']:.3f}"
            else:
                rng = st.get("dominant_value", "?")
            print(f"      · {q:<28} value {rng:<20} conf {st['confidence_min']:.3f}–"
                  f"{st['confidence_max']:.3f}")
        print()

    flips = [r["fallback_flip_rate"] for r in results]
    worst = max(results, key=lambda r: r["fallback_flip_rate"])
    print("=" * 68)
    print(f"mean flip rate : {statistics.fmean(flips):.0%}")
    print(f"worst scenario : {worst['scenario']} at {worst['fallback_flip_rate']:.0%}")
    if max(flips) > 0:
        print("\nREAD THIS AS A WARNING: a scenario that flips means its threshold is a coin,\n"
              "not a knob. Do not tune that threshold further until repeatability improves —\n"
              "tuning it would only fit the noise.")
    else:
        print("\nNo threshold crossed back and forth at this repeat count. Raise --repeats\n"
              "before treating that as settled; absence of flips is not proof of stability.")

    payload = {"backend": backend, "repeats": repeats,
               "score_gate": acceptance or "margin", "results": results}
    if as_json:
        Path(as_json).write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                                 encoding="utf-8")
        print(f"\nwrote {as_json}")
    return payload


def main(argv: list[str]) -> None:
    # Windows consoles default to GBK here, which mangles the box-drawing and
    # multiplication glyphs we print. Reconfigure rather than avoid them.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    repeats, backend, as_json, acceptance = 5, "openrouter", None, None
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--repeats" and i + 1 < len(argv):
            repeats = int(argv[i + 1]); i += 2; continue
        if a.startswith("--repeats="):
            repeats = int(a.split("=", 1)[1]); i += 1; continue
        if a == "--backend" and i + 1 < len(argv):
            backend = argv[i + 1]; i += 2; continue
        if a.startswith("--backend="):
            backend = a.split("=", 1)[1]; i += 1; continue
        if a == "--acceptance" and i + 1 < len(argv):
            acceptance = argv[i + 1]; i += 2; continue
        if a.startswith("--acceptance="):
            acceptance = a.split("=", 1)[1]; i += 1; continue
        if a == "--json" and i + 1 < len(argv):
            as_json = argv[i + 1]; i += 2; continue
        if a.startswith("--json="):
            as_json = a.split("=", 1)[1]; i += 1; continue
        i += 1
    run(repeats=repeats, backend=backend, as_json=as_json, acceptance=acceptance)


if __name__ == "__main__":
    main(sys.argv[1:])
