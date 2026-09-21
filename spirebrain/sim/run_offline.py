"""Offline simulation harness — proves the whole loop without the game.

Simulates a minimal run: 3 acts, each with map routing, a card reward, an
event, a rest site, a shop, a boss relic and a combat-risk gate. A JEV client
answers; decision modules apply confidence floors and fallbacks; everything is
logged to logs/jev_calls.jsonl for calibration analysis.

Two modes:

    python -m spirebrain.sim.run_offline              # pessimistic mock (0.55)
    python -m spirebrain.sim.run_offline --optimistic # confident mock (0.90)

The pessimistic default is the important one: it drives every module down its
fallback path, which is the executable proof that "when the brain is unsure, the
code plays safe" is real behaviour and not a design claim. The optimistic run
proves the other branch: JEV's answers actually steer the run.

Known limit of the mock (so read the output correctly): MockJevClient does NOT
read the state. It answers every Noul with the confidence it was given, so in
--optimistic mode every HP-budget probe reads "yes, over budget" — including the
0-damage rest node — and map routing therefore always reroutes to the safest
node. That is the mock being state-blind, not the router misbehaving; the
happy-path branch is covered for real in
tests/test_new_modules.py::test_map_router_accepts_route_when_probe_clears_it.
Real JEV differentiates, because it reads the state.

No game, no API key, no network required.
"""

from __future__ import annotations

import json
import random
from pathlib import Path

from spirebrain.jev_brain.client import MockJevClient, get_client
from spirebrain.jev_brain.decisions import (
    BossRelicJudge,
    CardRewardJudge,
    CombatRiskGate,
    EventChooser,
    MapRouter,
    RestSiteDecider,
    ShopDecider,
)
from spirebrain.jev_brain.logging_client import LoggingJevClient
from spirebrain.jev_brain.state import (
    deck_digest,
    map_choices,
    path_damage_probes,
    run_state,
)
from spirebrain.tactical.hp_budget import HPBudget

ROOT = Path(__file__).resolve().parents[2]

STARTING_DECK = [
    {"name": "Strike", "cost": 1, "type": "Attack"},
    {"name": "Strike", "cost": 1, "type": "Attack"},
    {"name": "Strike", "cost": 1, "type": "Attack"},
    {"name": "Strike", "cost": 1, "type": "Attack"},
    {"name": "Strike", "cost": 1, "type": "Attack"},
    {"name": "Defend", "cost": 1, "type": "Skill"},
    {"name": "Defend", "cost": 1, "type": "Skill"},
    {"name": "Defend", "cost": 1, "type": "Skill"},
    {"name": "Defend", "cost": 1, "type": "Skill"},
    {"name": "Bash", "cost": 2, "type": "Attack"},
]

MAP_ROWS = [
    ([{"id": "n1", "symbol": "M", "y": 3}, {"id": "n2", "symbol": "E", "y": 4},
      {"id": "n3", "symbol": "R", "y": 5}]),
    ([{"id": "n4", "symbol": "$", "y": 7}, {"id": "n5", "symbol": "?", "y": 8},
      {"id": "n6", "symbol": "R", "y": 9}]),
]

CARD_REWARDS = [
    {"strike": "basic attack", "inflame": "gain strength each combat",
     "shrug_it_off": "block and draw a card"},
    {"pommel_strike": "damage and draw", "anger": "0-cost attack that copies itself",
     "twin_strike": "two hits"},
]

EVENTS = [
    ("A golden idol sits on an altar. Removing it would be easy, but the altar is trapped.",
     {"take_it": "gain a relic, take 25% max HP damage",
      "leave_it": "nothing happens"}),
    ("A bonfire of blue flames beckons. You could offer a card to it.",
     {"offer_card": "remove a card from your deck",
      "keep_card": "nothing happens"}),
]

RELICS = [
    {"name": "Philosopher's Stone", "description": "gain 1 energy each turn; enemies gain 1 strength"},
    {"name": "Runic Dome", "description": "gain 1 energy each turn; you cannot see enemy intents"},
    {"name": "Coffee Dripper", "description": "gain 1 energy each turn; you can no longer rest at campfires"},
]


def run_one_simulation(seed: int = 0, confidence: float | None = None) -> dict:
    """One ascent. `confidence=None` -> strategy.json's pessimistic mock."""
    strategy = json.loads((ROOT / "config" / "strategy.json").read_text(encoding="utf-8"))
    floor = strategy["jev"]["confidence_floor"]
    goal = strategy["goal"]

    inner = get_client("mock") if confidence is None else MockJevClient(confidence=confidence)
    jev = LoggingJevClient(inner, log_dir=ROOT / strategy["jev"]["log_dir"])
    rng = random.Random(seed)

    deck = [dict(c) for c in STARTING_DECK]
    outcome = {"seed": seed, "goal": goal, "acts": [], "deck_size_start": len(deck)}

    for act in (1, 2, 3):
        hp = HPBudget(act=act, max_hp=80, current_hp=80)
        router = MapRouter(jev, hp)
        risk = CombatRiskGate(jev, hp)
        rest = RestSiteDecider(jev, hp, goal=goal)
        shop = ShopDecider(jev, goal=goal)
        boss = BossRelicJudge(jev, goal=goal, deck_digest=deck_digest(deck))
        cards = CardRewardJudge(jev, len(deck), strategy["deck_policy"]["max_cards"],
                                deck_digest=deck_digest(deck), goal=goal)
        events = EventChooser(jev, goal=goal)

        act_log: dict = {"act": act, "steps": []}
        _step = lambda kind, d: act_log["steps"].append(  # noqa: E731
            {"kind": kind, "choice": d.value, "conf": round(d.confidence, 3),
             "fb": d.used_fallback, "why": d.detail.get("reason")})

        # -- routing -------------------------------------------------------- #
        for nodes in MAP_ROWS:
            choices = map_choices(nodes)
            probes = path_damage_probes(nodes, act)
            d = router.decide(choices, probes)
            _step("map", d)
            chosen = next((n for n in nodes if str(n["id"]) == str(d.value)), nodes[0])
            symbol = chosen["symbol"]
            if symbol == "E":
                hp.spend(rng.randint(14, 26))
            elif symbol == "M":
                hp.spend(rng.randint(4, 10))
            elif symbol == "?":
                hp.spend(rng.randint(0, 12))

        # -- card reward ---------------------------------------------------- #
        reward = rng.choice(CARD_REWARDS)
        c = cards.decide(reward)
        _step("card_reward", c)
        if c.value != "skip":
            deck.append({"name": str(c.value), "cost": 1, "type": "Attack"})

        # -- event ---------------------------------------------------------- #
        text, options = rng.choice(EVENTS)
        e = events.decide(text, options)
        _step("event", e)
        if e.value == "take_it":
            hp.spend(int(hp.max_hp * 0.25))

        # -- rest site ------------------------------------------------------ #
        upgradable = {card["name"]: f"{card['name']} ({card['type']})"
                      for card in deck if "+" not in card["name"]}
        r = rest.decide(hp_ratio=hp.current_hp / hp.max_hp, upgradable=upgradable)
        _step("rest", r)
        if r.value == "rest":
            hp.restore(int(hp.max_hp * 0.30))

        # -- shop ----------------------------------------------------------- #
        gold = 150 + act * 50
        items = {
            "Ornamental Fan": (150, "block 4 after 3 attacks in a turn"),
            "Meat on the Bone": (165, "heal 12 HP at the end of combat below 50% HP"),
            "Card Removal": (75, "remove a card from your deck"),
        }
        s = shop.decide(gold=gold, items=items)
        _step("shop", s)

        # -- boss relic (mandatory) ----------------------------------------- #
        b = boss.decide({rel["name"]: rel["description"] for rel in RELICS})
        _step("boss_relic", b)

        # -- combat risk gate ----------------------------------------------- #
        predicted = 25 + act * 4
        g = risk.decide("act boss", predicted_damage=predicted)
        _step("combat_risk", g)
        # Defensive posture trades speed for HP, so it blunts the incoming damage.
        hp.spend(int(predicted * (0.8 if g.detail.get("posture") == "defensive" else 1.0)))

        act_log["hp_end"] = hp.current_hp
        act_log["budget_left"] = hp.remaining_budget
        outcome["acts"].append(act_log)

    outcome["jev_calls"] = jev.calls
    outcome["jev_cost_usd"] = jev.total_cost_usd
    outcome["deck_end"] = len(deck)
    outcome["confidence_floor"] = floor
    return outcome


def _summary(result: dict) -> str:
    lines = [
        f"seed={result['seed']}  goal={result['goal']}  "
        f"calls={result['jev_calls']}  deck={result['deck_size_start']}->{result['deck_end']}",
    ]
    for act in result["acts"]:
        kinds = {p["kind"]: p for p in act["steps"]}
        fb = [p for p in act["steps"] if p["fb"]]
        lines.append(
            f"  act{act['act']}: hp_end={act['hp_end']:>3} budget={act['budget_left']:>3} "
            f"| map={kinds['map']['choice']} card={kinds['card_reward']['choice']} "
            f"event={kinds['event']['choice']} rest={kinds['rest']['choice']} "
            f"shop={kinds['shop']['choice']} relic={kinds['boss_relic']['choice']} "
            f"risk={kinds['combat_risk']['choice']} | {len(fb)}/{len(act['steps'])} fell back"
        )
        for step in fb:
            lines.append(f"      - {step['kind']}: {step['why']}")
    return "\n".join(lines)


if __name__ == "__main__":
    import sys

    optimistic = "--optimistic" in sys.argv
    conf = 0.90 if optimistic else None
    result = run_one_simulation(seed=42, confidence=conf)
    print(f"=== {'optimistic' if optimistic else 'pessimistic'} mock run ===")
    print(_summary(result))
    print(json.dumps(result, ensure_ascii=False, indent=2)[:1200])
    print(f"\njev calls: {result['jev_calls']} | cost: ${result['jev_cost_usd']:.8f} "
          "(mock: no real spend)")
