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
    RunContext,
    deck_digest,
    map_choices,
    path_damage_probes,
    run_state,
    worst_case_damage,
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

# --------------------------------------------------------------------------- #
# Maps
# --------------------------------------------------------------------------- #
# Maps are generated per seed. They used to be a fixed two-row constant, and
# that made the seed meaningless: the fallback router always walks the least
# damaging node, a rest node costs nothing, so every "different" seed produced
# the identical route and the identical HP trajectory. Ten ascents were really
# one ascent repeated (docs/MEASUREMENTS.md, run 6).
#
# The pools below are sampled three-at-a-time per row, and a row is NOT guaranteed
# to contain a free option — otherwise "safest" would always be "free" and
# routing would never cost anything.
SYMBOL_POOLS = {
    1: ["M", "M", "M", "E", "?", "$", "R"],
    2: ["M", "M", "E", "E", "?", "$", "R"],
    3: ["M", "E", "E", "?", "R", "R"],
}
# Node damage is the symbol's worst case, jittered by the seed so two maps of the
# same shape are not the same map.
ROWS_PER_ACT = 3


def make_map(rng: random.Random, act: int) -> list[list[dict]]:
    """Three rows of three reachable nodes, shaped and costed from the seed."""
    pool = SYMBOL_POOLS.get(act, SYMBOL_POOLS[1])
    rows: list[list[dict]] = []
    for row_i in range(ROWS_PER_ACT):
        symbols = rng.sample(pool, 3)
        nodes = []
        for col, symbol in enumerate(symbols):
            base = worst_case_damage(symbol, act)
            jitter = rng.uniform(0.8, 1.25) if base else 0.0
            nodes.append({
                "id": f"a{act}r{row_i}c{col}",
                "symbol": symbol,
                "y": act * 17 + row_i * 4 + col,
                "damage_hint": int(round(base * jitter)),
            })
        rows.append(nodes)
    return rows

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


def load_seeds(which: str) -> list[int]:
    """Resolve a seed group name ("calibration" | "test") from config/seeds.json.

    The split is the project's guard against fitting thresholds to the data it
    then reports as evidence (docs/MEASUREMENTS.md, protocol rule 1).
    """
    cfg = json.loads((ROOT / "config" / "seeds.json").read_text(encoding="utf-8"))
    key = f"{which}_seeds"
    if key not in cfg:
        raise KeyError(f"no such seed group: {which!r} (have {sorted(k for k in cfg if k.endswith('_seeds'))})")
    return [int(s) for s in cfg[key]]


def run_one_simulation(seed: int = 0, confidence: float | None = None,
                       backend: str | None = None,
                       acceptance: str | None = None,
                       feed=None) -> dict:
    """One ascent.

    `backend=None` uses the mock (pessimistic by default, or `confidence=` for the
    optimistic variant). Pass `backend="openrouter"` to run the real thing against
    JEV via OpenRouter — same code path, same questions, real answers.

    `acceptance` selects the Score gate: "margin" (default, demands a peaked
    distribution) or "argmax" (value floor only). See decisions.evaluate_score.

    `feed` is the optional live DecisionFeed (spirebrain.overlay.feed): when
    given, every decision is published as it happens and the run's HP/budget
    updates flow to any watching dashboard. None (default) changes nothing.
    """
    strategy = json.loads((ROOT / "config" / "strategy.json").read_text(encoding="utf-8"))
    goal = strategy["goal"]
    # The gate is a strategy-layer choice, so the config wins when the caller does
    # not name one: switching it should be a config edit, not a code edit.
    acceptance = acceptance or strategy.get("jev", {}).get("score_acceptance")

    if backend and backend != "mock":
        inner = get_client(backend)
    else:
        inner = get_client("mock") if confidence is None else MockJevClient(confidence=confidence)
    jev = LoggingJevClient(inner, log_dir=ROOT / strategy["jev"]["log_dir"])
    rng = random.Random(seed)

    deck = [dict(c) for c in STARTING_DECK]
    relics: list[str] = ["Burning Blood"]
    potions: list[str] = []
    gold = 150
    run = RunContext(character="Ironclad", goal=goal, deck=deck, relics=relics,
                     potions=potions, gold=gold)
    outcome = {"seed": seed, "goal": goal, "acts": [], "deck_size_start": len(deck),
               "acceptance": acceptance or "margin", "cards_taken": 0,
               "score_rejections": []}

    for act in (1, 2, 3):
        hp = HPBudget(act=act, max_hp=80, current_hp=80)
        # `run_floor` is the map floor we are standing on. It used to be called
        # `floor`, which silently collided with the confidence floor read from
        # strategy.json and made `outcome["confidence_floor"]` report a map row
        # instead of a threshold. Renamed so the two can never be confused again.
        run_floor = (act - 1) * 17
        act_map = make_map(rng, act)

        def _sync(hp_budget=hp, fl=run_floor) -> RunContext:
            """Push live run facts into the context every module already holds.

            The modules capture the RunContext once; mutating it in place is what
            keeps their view of the run current as HP, gold and deck change.
            """
            run.deck = deck
            run.relics = relics
            run.potions = potions
            run.gold = gold
            run.floor = fl
            run.with_budget(hp_budget)
            if feed is not None:
                try:
                    feed.publish("run_state", {
                        "act": hp_budget.act, "floor": fl,
                        "character": run.character,
                        "hp": hp_budget.current_hp, "max_hp": hp_budget.max_hp,
                        "reserved": hp_budget.reserved_hp,
                        "budget_remaining": hp_budget.remaining_budget,
                        "gold": gold, "deck_size": len(deck),
                        "relics": list(relics),
                    })
                except Exception:  # noqa: BLE001 - dashboard must not break the run
                    pass
            return run

        def _publish_state_now() -> None:
            """Re-publish after an in-place mutation (heal, spend, buy)."""
            _sync()

        router = MapRouter(jev, hp, run=run)
        risk = CombatRiskGate(jev, hp, run=run)
        rest = RestSiteDecider(jev, hp, goal=goal, run=run)
        shop = ShopDecider(jev, goal=goal, run=run)
        boss = BossRelicJudge(jev, goal=goal, run=run, acceptance=acceptance)
        events = EventChooser(jev, goal=goal, run=run)

        act_log: dict = {"act": act, "steps": []}

        def _step(kind: str, d) -> None:
            rec = {"kind": kind, "choice": d.value, "conf": round(d.confidence, 3),
                   "fb": d.used_fallback, "why": d.detail.get("reason")}
            gate = d.detail.get("gate")
            if gate:  # keep the full Score-gate breakdown: the experiment needs reasons
                rec["gate"] = gate
                if kind == "card_reward" and not gate["accepted"]:
                    outcome["score_rejections"].append(
                        {"act": act, "reason": gate["reason"], "value": gate["value"]})
            act_log["steps"].append(rec)
            if feed is not None and kind != "map":  # map publishes its own richer event
                try:
                    feed.publish("decision", {
                        "point": kind, "value": d.value,
                        "confidence": round(d.confidence, 4),
                        "fallback": bool(d.used_fallback),
                        "detail": d.detail, "command": {"command": kind},
                    })
                except Exception:  # noqa: BLE001
                    pass

        # -- routing -------------------------------------------------------- #
        for nodes in act_map:
            _sync()
            choices = map_choices(nodes)
            probes = path_damage_probes(nodes, act)
            d = router.decide(choices, probes)
            _step("map", d)
            run_floor += 2
            chosen = next((n for n in nodes if str(n["id"]) == str(d.value)), nodes[0])
            # Spend what the node actually costs, drawn BELOW the probe's worst
            # case — the probe is an upper bound, so the agent's budget reasoning
            # is conservative rather than fictional. Before this, probes said "24
            # HP for an elite" while the spend was an unrelated randint(14, 26).
            hint = int(chosen.get("damage_hint") or 0)
            spent = rng.randint(int(hint * 0.5), hint) if hint else 0
            if spent:
                hp.spend(spent)
            # Keep both numbers: the estimate the agent reasoned with, and what the
            # walk actually cost. That pair is the harness's own audit trail, and
            # tests/test_sim_harness.py checks it stays honest.
            act_log["steps"][-1]["probe"] = int(probes.get(str(d.value), 0))
            act_log["steps"][-1]["spent"] = spent
            if feed is not None:
                # The route's own probe + every rival's, so the dashboard can
                # render the full "Into the Breach" style preview of the choice.
                detail = dict(d.detail)
                detail["probes"] = {
                    node: {"value": 1 if p > hp.remaining_budget else 0,
                           "confidence": 0.5 + min(0.4, p / (hp.max_hp or 80) / 2),
                           "damage": int(p)}
                    for node, p in probes.items()
                }
                detail["choices"] = dict(choices)
                try:
                    feed.publish("decision", {
                        "point": "map", "value": d.value,
                        "confidence": round(d.confidence, 4),
                        "fallback": bool(d.used_fallback),
                        "detail": detail,
                        "command": {"command": "choose",
                                    "choice": next((i for i, n in enumerate(nodes)
                                                    if str(n["id"]) == str(d.value)), 0)},
                        "probe": int(probes.get(str(d.value), 0)),
                        "spent": spent,
                    })
                except Exception:  # noqa: BLE001
                    pass

        # -- card reward ---------------------------------------------------- #
        _sync()
        reward = rng.choice(CARD_REWARDS)
        c = CardRewardJudge(jev, len(deck), strategy["deck_policy"]["max_cards"],
                            goal=goal, run=run, acceptance=acceptance).decide(reward)
        _step("card_reward", c)
        run_floor += 1
        if c.value != "skip":
            deck.append({"name": str(c.value), "cost": 1, "type": "Attack"})
            outcome["cards_taken"] += 1

        # -- event ---------------------------------------------------------- #
        _sync()
        text, options = rng.choice(EVENTS)
        e = events.decide(text, options)
        _step("event", e)
        run_floor += 1
        if e.value == "take_it":
            hp.spend(int(hp.max_hp * 0.25))

        # -- rest site ------------------------------------------------------ #
        _sync()
        upgradable = {card["name"]: f"{card['name']} ({card['type']})"
                      for card in deck if "+" not in card["name"]}
        r = rest.decide(hp_ratio=hp.current_hp / hp.max_hp, upgradable=upgradable)
        _step("rest", r)
        run_floor += 1
        if r.value == "rest":
            hp.restore(int(hp.max_hp * 0.30))

        # -- shop ----------------------------------------------------------- #
        gold = max(gold, 150 + act * 50)  # the act's earnings
        items = {
            "Ornamental Fan": (150, "block 4 after 3 attacks in a turn"),
            "Meat on the Bone": (165, "heal 12 HP at the end of combat below 50% HP"),
            "Card Removal": (75, "remove a card from your deck"),
        }
        _sync()
        s = shop.decide(gold=gold, items=items)
        _step("shop", s)
        run_floor += 1
        if s.value not in ("leave", "remove") and s.value in items:
            gold -= items[s.value][0]

        # -- boss relic (mandatory) ----------------------------------------- #
        _sync()
        b = boss.decide({rel["name"]: rel["description"] for rel in RELICS})
        _step("boss_relic", b)
        run_floor += 1
        if isinstance(b.value, str):
            relics.append(b.value)

        # -- combat risk gate ----------------------------------------------- #
        _sync()
        predicted = 25 + act * 4
        g = risk.decide("act boss", predicted_damage=predicted)
        _step("combat_risk", g)
        # Defensive posture trades speed for HP, so it blunts the incoming damage.
        hp.spend(int(predicted * (0.8 if g.detail.get("posture") == "defensive" else 1.0)))

        act_log["hp_end"] = hp.current_hp
        act_log["budget_left"] = hp.remaining_budget
        outcome["acts"].append(act_log)

    outcome["relics_end"] = list(relics)
    outcome["state_digest_chars"] = len(json.dumps(run.digest(), ensure_ascii=False))

    outcome["jev_calls"] = jev.calls
    outcome["jev_cost_usd"] = jev.total_cost_usd
    outcome["deck_end"] = len(deck)
    outcome["floor_final"] = run_floor
    return outcome


def _summary(result: dict) -> str:
    lines = [
        f"seed={result['seed']}  goal={result['goal']}  acceptance={result.get('acceptance')}  "
        f"calls={result['jev_calls']}  "
        f"deck={result['deck_size_start']}->{result['deck_end']} "
        f"(cards taken: {result.get('cards_taken', 0)})",
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


def _cli_value(argv: list[str], name: str) -> str | None:
    """Read `--name value` or `--name=value` out of argv."""
    prefix = f"--{name}="
    hit = next((a.split("=", 1)[1] for a in argv if a.startswith(prefix)), None)
    if hit is None and f"--{name}" in argv:
        hit = argv[argv.index(f"--{name}") + 1]
    return hit


if __name__ == "__main__":
    import sys

    optimistic = "--optimistic" in sys.argv
    conf = 0.90 if optimistic else None

    backend = _cli_value(sys.argv, "backend")
    acceptance = _cli_value(sys.argv, "acceptance")
    seed = int(_cli_value(sys.argv, "seed") or 42)

    result = run_one_simulation(seed=seed, confidence=conf, backend=backend,
                                acceptance=acceptance)
    label = backend or ("optimistic mock" if optimistic else "pessimistic mock")
    print(f"=== run [{label}] ===")
    print(_summary(result))
    print(json.dumps(result, ensure_ascii=False, indent=2)[:1200])
    print(f"\njev calls: {result['jev_calls']} | cost: ${result['jev_cost_usd']:.8f}")
