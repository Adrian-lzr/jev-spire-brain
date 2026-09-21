"""Decision modules: the seven JEV judgment points of Jev Spire Brain.

Each module takes game state, builds a JEV question set, asks the client, and
applies the confidence floor — falling back to a conservative rule when the
model is unsure or the call fails. This is the *semantic judgment* layer: the
place where a written, fuzzy trade-off ("is this HP loss acceptable?", "is this
card worth it?") becomes a typed value the rest of the code can branch on.

Two conventions enforced across all modules, both derived from the official docs:

1. ONE CALL, MANY QUESTIONS.  The API evaluates every question in parallel
   against the same state and adding questions barely costs latency. So each
   decision point asks everything it needs at once instead of looping.
2. NUOL HAS NO SEPARATE CONFIDENCE.  Its returned probability *is* the belief.
   A probability near 0.5 means the model cannot tell, so we treat the band
   (0.40, 0.60) as "uncertain" and fall back to a rule rather than act on a
   coin flip. Outside the band, the probability itself is the answer.

Score rubrics are **ordered word-labelled levels** (see client.py for why), and
every Score answer arrives here already normalised to 0..1.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from spirebrain.jev_brain.client import (
    NOUL_UNCERTAIN_BAND,
    ChoiceSpec,
    JevClient,
    JevResponse,
    NoulSpec,
    QuestionSpec,
    ScoreSpec,
)

CONFIDENCE_FLOOR = 0.60  # mirrored from config/strategy.json
SCORE_ACTION_FLOOR = 0.55  # below this, "nothing here is worth taking"
# When the model cannot tell whether to heal, heal below this HP ratio.
# (Same number as CONFIDENCE_FLOOR by coincidence, not by meaning.)
HEAL_WHEN_UNSURE_HP_RATIO = 0.60

# Shared rubrics: ordered lowest -> highest.
CARD_RUBRIC = [
    "Actively bad for this deck or works against the goal",
    "Filler: playable, but adds nothing the deck does not already have",
    "Solid: clearly improves the deck's plan",
    "Excellent: fixes a deck weakness or multiplies the goal",
]
UPGRADE_RUBRIC = [
    "Upgrade barely matters for this deck",
    "Minor improvement",
    "Meaningful improvement to a card we rely on",
    "Critical: a key card whose upgrade changes how the deck plays",
]
RELIC_RUBRIC = [
    "A net negative for this deck or goal",
    "Neutral: takes up the slot without changing much",
    "Good: a real upgrade to the run's prospects",
    "Excellent: shapes the whole plan for the rest of the run",
]


@dataclass
class Decision:
    """Uniform result across all decision points."""

    point: str  # map | card_reward | event | rest | shop | boss_relic | combat_risk
    value: object  # chosen label / normalised score / verdict
    confidence: float
    used_fallback: bool  # True if a conservative rule fired
    detail: dict = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def noul_verdict(probability: float) -> bool | None:
    """True / False / None(=cannot tell) from a Noul probability."""
    low, high = NOUL_UNCERTAIN_BAND
    if probability >= high:
        return True
    if probability <= low:
        return False
    return None


def _probe_details(resp: JevResponse) -> dict:
    return {k: {"value": a.value, "confidence": a.confidence} for k, a in resp.answers.items()}


# --------------------------------------------------------------------------- #
# 1. Map routing
# --------------------------------------------------------------------------- #
class MapRouter:
    """Map node selection: Choice over reachable nodes + parallel Noul HP probes.

    The user's original question — "is the HP I'd lose on this path acceptable?"
    — is asked once per candidate path in the same call as the route choice.
    """

    def __init__(self, jev: JevClient, hp_budget) -> None:
        self.jev = jev
        self.hp = hp_budget

    def decide(self, reachable: dict[str, str], path_probes: dict[str, int]) -> Decision:
        """reachable: {node_id: 'elite (risky)'}, path_probes: {node_id: predicted_worst_damage}."""
        try:
            questions: dict[str, QuestionSpec] = {
                "route": ChoiceSpec(
                    instructions=(
                        "Which map node best serves the run's goal while respecting the HP "
                        "budget? Weigh the reward (cards, gold, relics) against the HP the "
                        "node is likely to cost."
                    ),
                    criteria=reachable,
                )
            }
            for node_id, dmg in path_probes.items():
                questions[f"over_budget_{node_id}"] = NoulSpec(
                    instructions=(
                        f"Walking through node {node_id} is expected to cost about {dmg} HP "
                        f"before the next rest site, and only {self.hp.remaining_budget} HP is "
                        "spendable above the reserve this act. Does that cost exceed what this "
                        "run can afford to lose?"
                    )
                )
            resp = self.jev.ask(self._state(), questions)

            route = resp.answers["route"]
            probe_key = f"over_budget_{route.value}"
            probe = resp.answers.get(probe_key)
            if probe is not None and noul_verdict(probe.confidence) is True:
                return self._fallback(reachable, path_probes,
                                      reason="chosen route judged over HP budget")
            if route.confidence < CONFIDENCE_FLOOR:
                return self._fallback(reachable, path_probes, reason="low confidence")
            return Decision("map", route.value, route.confidence, False,
                            {"probes": _probe_details(resp)})
        except Exception as exc:  # noqa: BLE001 - any failure must not stop the run
            return self._fallback(reachable, path_probes, reason=f"jev error: {exc}")

    def _state(self) -> dict:
        return {
            "act": self.hp.act,
            "hp": {"current": self.hp.current_hp, "max": self.hp.max_hp},
            "hp_budget": {
                "remaining_spendable": self.hp.remaining_budget,
                "reserved": self.hp.reserved_hp,
            },
        }

    def _fallback(self, reachable, path_probes, reason: str) -> Decision:
        if path_probes:
            safest = min(path_probes, key=path_probes.get)  # least predicted damage
        else:
            safest = next(iter(reachable))
        return Decision("map", safest, 0.0, True, {"reason": reason})


# --------------------------------------------------------------------------- #
# 2. Card rewards
# --------------------------------------------------------------------------- #
class CardRewardJudge:
    """Card rewards: Score x candidates against the deck, with a skip path."""

    def __init__(self, jev: JevClient, deck_size: int, max_cards: int = 25,
                 deck_digest: str = "", goal: str = "") -> None:
        self.jev = jev
        self.deck_size = deck_size
        self.max_cards = max_cards
        self.deck_digest = deck_digest
        self.goal = goal

    def decide(self, candidates: dict[str, str]) -> Decision:
        """candidates: {card_name: description}. Returns a card name or 'skip'."""
        if self.deck_size >= self.max_cards:
            return Decision("card_reward", "skip", 1.0, True,
                            {"reason": f"deck at {self.deck_size}/{self.max_cards} cards"})
        if not candidates:
            return Decision("card_reward", "skip", 1.0, True, {"reason": "empty reward"})
        try:
            questions = {
                name: ScoreSpec(
                    instructions=(
                        "How much would adding this card improve the deck, given what the deck "
                        f"already does and the run's goal? Card: {desc}"
                    ),
                    criteria=list(CARD_RUBRIC),
                )
                for name, desc in candidates.items()
            }
            resp = self.jev.ask(self._state(), questions)
            best_name, best = max(resp.answers.items(), key=lambda kv: kv[1].value)
            scores = {k: round(v.value, 4) for k, v in resp.answers.items()}
            if best.confidence < CONFIDENCE_FLOOR or best.value < SCORE_ACTION_FLOOR:
                return Decision("card_reward", "skip", best.confidence, True,
                                {"scores": scores, "best": best_name,
                                 "reason": "best card below action floor"})
            return Decision("card_reward", best_name, best.confidence, False, {"scores": scores})
        except Exception:  # noqa: BLE001
            return Decision("card_reward", "skip", 0.0, True, {"reason": "jev error"})

    def _state(self) -> dict:
        state: dict = {"deck": {"size": self.deck_size, "contents": self.deck_digest}}
        if self.goal:
            state["goal"] = self.goal
        return state


# --------------------------------------------------------------------------- #
# 3. Events
# --------------------------------------------------------------------------- #
class EventChooser:
    """Event options: a single Choice. Never forced — low confidence defers to rule."""

    def __init__(self, jev: JevClient, goal: str = "") -> None:
        self.jev = jev
        self.goal = goal

    def decide(self, event_text: str, options: dict[str, str]) -> Decision:
        try:
            resp = self.jev.ask(
                {"event": event_text, "goal": self.goal} if self.goal else {"event": event_text},
                {"choice": ChoiceSpec(
                    instructions=(
                        "Which option best serves the run, weighing the reward against HP, "
                        "gold, curse or relic consequences? Pick the option whose downside this "
                        "run can actually absorb."
                    ),
                    criteria=options)},
            )
            ans = resp.answers["choice"]
            if ans.confidence < CONFIDENCE_FLOOR:
                return self._fallback(options, f"low confidence ({ans.confidence:.2f})")
            return Decision("event", ans.value, ans.confidence, False,
                            {"probabilities": ans.raw.get("probabilities", {})})
        except Exception as exc:  # noqa: BLE001
            return self._fallback(options, f"jev error: {exc}")

    @staticmethod
    def _fallback(options: dict, reason: str) -> Decision:
        first = next(iter(options))  # conservative: take the first listed option
        return Decision("event", first, 0.0, True, {"reason": reason})


# --------------------------------------------------------------------------- #
# 4. Rest sites
# --------------------------------------------------------------------------- #
class RestSiteDecider:
    """Rest site: Noul(heal?) + Score x upgrade candidates, in one call.

    Note the asymmetry we encode: an unnecessary rest is a wasted opportunity,
    but an unnecessary upgrade can kill the run. So the fallback always heals.
    """

    def __init__(self, jev: JevClient, hp_budget=None, goal: str = "") -> None:
        self.jev = jev
        self.hp = hp_budget
        self.goal = goal

    def decide(self, *, hp_ratio: float, upgradable: dict[str, str]) -> Decision:
        """upgradable: {card_name: description}. Returns 'rest' or a card name."""
        if not upgradable:
            return Decision("rest", "rest", 1.0, True, {"reason": "nothing to upgrade"})
        try:
            questions: dict[str, QuestionSpec] = {
                "need_heal": NoulSpec(
                    instructions=(
                        f"Current HP is {hp_ratio:.0%} of max"
                        + (f" with {self.hp.remaining_budget} HP spendable above the act's "
                           "reserve" if self.hp else "")
                        + ". Does this run need healing now more than it needs one upgrade?"
                    )
                ),
                "upgrade": ChoiceSpec(
                    instructions=(
                        "If we upgrade instead of resting, which card is the best upgrade? "
                        "Include 'rest' if no upgrade is worth the HP."
                    ),
                    criteria={**upgradable, "rest": "Skip the upgrade and heal instead"},
                ),
            }
            resp = self.jev.ask(self._state(hp_ratio), questions)
            heal = noul_verdict(resp.answers["need_heal"].confidence)
            up = resp.answers["upgrade"]

            if heal is True:
                return Decision("rest", "rest", resp.answers["need_heal"].confidence, False,
                                {"reason": "judged to need healing"})
            if heal is None or up.confidence < CONFIDENCE_FLOOR:
                return self._fallback(upgradable, hp_ratio, "uncertain")
            if up.value == "rest" or up.value not in upgradable:
                return Decision("rest", "rest", up.confidence, False,
                                {"reason": "no upgrade worth the HP"})
            return Decision("rest", up.value, up.confidence, False,
                            {"probabilities": up.raw.get("probabilities", {})})
        except Exception as exc:  # noqa: BLE001
            return self._fallback(upgradable, hp_ratio, f"jev error: {exc}")

    def _state(self, hp_ratio: float) -> dict:
        state: dict = {"hp_ratio": round(hp_ratio, 3)}
        if self.hp:
            state["hp_budget"] = {
                "remaining_spendable": self.hp.remaining_budget,
                "reserved": self.hp.reserved_hp,
                "act": self.hp.act,
            }
        if self.goal:
            state["goal"] = self.goal
        return state

    def _fallback(self, upgradable: dict, hp_ratio: float, reason: str) -> Decision:
        if hp_ratio < HEAL_WHEN_UNSURE_HP_RATIO:  # unsure and low -> heal
            return Decision("rest", "rest", 0.0, True, {"reason": f"{reason}; low HP"})
        return Decision("rest", "rest", 0.0, True, {"reason": f"{reason}; rest is never wrong"})


# --------------------------------------------------------------------------- #
# 5. Shops
# --------------------------------------------------------------------------- #
class ShopDecider:
    """Shop: one parallel Noul per affordable item — "worth the gold for this goal?"

    Deliberately uses Noul rather than Score: the buy/no-buy call is a yes/no
    judgement, and Noul returns the probability directly, so "all items below
    0.6 -> save the gold" needs no second-order threshold.
    """

    def __init__(self, jev: JevClient, goal: str = "") -> None:
        self.jev = jev
        self.goal = goal

    def decide(self, *, gold: int, items: dict[str, tuple[int, str]],
               removal_cost: int | None = None, remove_candidate: str = "") -> Decision:
        """items: {label: (cost, description)}. Returns a label or 'leave'."""
        affordable = {k: v for k, v in items.items() if v[0] <= gold}
        if not affordable:
            return Decision("shop", "leave", 1.0, True,
                            {"reason": f"nothing affordable with {gold} gold"})
        try:
            questions: dict[str, QuestionSpec] = {}
            for label, (cost, desc) in affordable.items():
                questions[f"buy_{label}"] = NoulSpec(
                    instructions=(
                        f"Is buying {label} for {cost} gold worth it for this run's goal, "
                        f"compared with saving the gold? Item: {desc}"
                    )
                )
            if removal_cost is not None and remove_candidate and removal_cost <= gold:
                questions["buy_remove"] = NoulSpec(
                    instructions=(
                        f"Is paying {removal_cost} gold to remove {remove_candidate} from the "
                        "deck worth it for this run's goal?"
                    )
                )
            resp = self.jev.ask(self._state(gold), questions)
            probs = {k: a.confidence for k, a in resp.answers.items()}
            best_key = max(probs, key=probs.get)
            best_p = probs[best_key]

            if best_p >= NOUL_UNCERTAIN_BAND[1]:
                label = "remove" if best_key == "buy_remove" else best_key.removeprefix("buy_")
                return Decision("shop", label, best_p, False, {"probabilities": probs})
            if best_p <= NOUL_UNCERTAIN_BAND[0]:
                return Decision("shop", "leave", best_p, False, {"probabilities": probs})
            return Decision("shop", "leave", best_p, True,
                            {"probabilities": probs, "reason": "cannot tell -> keep the gold"})
        except Exception as exc:  # noqa: BLE001
            return Decision("shop", "leave", 0.0, True, {"reason": f"jev error: {exc}"})

    def _state(self, gold: int) -> dict:
        state = {"gold": gold}
        if self.goal:
            state["goal"] = self.goal
        return state


# --------------------------------------------------------------------------- #
# 6. Boss relics
# --------------------------------------------------------------------------- #
class BossRelicJudge:
    """Boss relics: Score x 3. The choice is mandatory, so we never skip — but a
    low-confidence pick is flagged, which is what a later review reads."""

    def __init__(self, jev: JevClient, goal: str = "", deck_digest: str = "") -> None:
        self.jev = jev
        self.goal = goal
        self.deck_digest = deck_digest

    def decide(self, relics: dict[str, str]) -> Decision:
        if not relics:
            return Decision("boss_relic", None, 0.0, True, {"reason": "no relics offered"})
        try:
            questions = {
                name: ScoreSpec(
                    instructions=(
                        "How good is this boss relic for this run's plan? Account for its "
                        f"drawback, not just its upside. Relic: {desc}"
                    ),
                    criteria=list(RELIC_RUBRIC),
                )
                for name, desc in relics.items()
            }
            resp = self.jev.ask(self._state(), questions)
            best_name, best = max(resp.answers.items(), key=lambda kv: kv[1].value)
            scores = {k: round(v.value, 4) for k, v in resp.answers.items()}
            # Mandatory pick: take the best even when unsure, but flag it.
            low = best.confidence < CONFIDENCE_FLOOR
            return Decision("boss_relic", best_name, best.confidence, low,
                            {"scores": scores, **({"reason": "mandatory pick, low confidence"} if low else {})})
        except Exception as exc:  # noqa: BLE001
            first = next(iter(relics))
            return Decision("boss_relic", first, 0.0, True,
                            {"reason": f"jev error: {exc}; took first offered"})

    def _state(self) -> dict:
        state: dict = {"deck": self.deck_digest}
        if self.goal:
            state["goal"] = self.goal
        return state


# --------------------------------------------------------------------------- #
# 7. Combat risk gate
# --------------------------------------------------------------------------- #
class CombatRiskGate:
    """Combat risk: Noul — "will this fight cost more HP than we can spend?"

    Used as the posture selector for the tactical layer: a risky verdict makes
    the greedy policy block more and play safer.
    """

    def __init__(self, jev: JevClient, hp_budget) -> None:
        self.jev = jev
        self.hp = hp_budget

    def decide(self, encounter: str, predicted_damage: int) -> Decision:
        try:
            resp = self.jev.ask(
                {
                    "encounter": encounter,
                    "predicted_damage": predicted_damage,
                    "hp_budget": {
                        "remaining_spendable": self.hp.remaining_budget,
                        "reserved": self.hp.reserved_hp,
                        "current_hp": self.hp.current_hp,
                        "max_hp": self.hp.max_hp,
                    },
                },
                {"exceeds": NoulSpec(
                    instructions=(
                        "Taking about this much damage would leave the run below its HP "
                        "reserve for the act. Is that cost more than this fight is worth?"
                    ))},
            )
            ans = resp.answers["exceeds"]
            verdict = noul_verdict(ans.confidence)
            if verdict is None:
                return Decision("combat_risk", True, ans.confidence, True,
                                {"posture": "defensive", "reason": "cannot tell -> play safe"})
            return Decision("combat_risk", verdict, ans.confidence, False,
                            {"posture": "defensive" if verdict else "greedy"})
        except Exception as exc:  # noqa: BLE001
            return Decision("combat_risk", True, 0.0, True,
                            {"posture": "defensive", "reason": f"jev error: {exc}"})


ALL_POINTS = (
    "map", "card_reward", "event", "rest", "shop", "boss_relic", "combat_risk",
)
