"""Decision modules: the seven JEV judgment points of Jev Spire Brain.

Each module takes game state, builds a JEV question set, asks the client, and
applies the confidence floor — falling back to a conservative rule when the
model is unsure or the call fails. This is the *semantic judgment* layer: the
place where a written, fuzzy trade-off ("is this HP loss acceptable?", "is this
card worth it?") becomes a typed value the rest of the code can branch on.

Four conventions, all derived from primary sources or from measurement:

1. ONE CALL, MANY QUESTIONS.  The API evaluates every question in parallel
   against the same state and adding questions barely costs latency. So each
   decision point asks everything it needs at once instead of looping.
2. NUOL HAS NO SEPARATE CONFIDENCE.  Its returned probability *is* the belief.
   A probability near 0.5 means the model cannot tell, so we treat the band
   (0.40, 0.60) as "uncertain" and fall back to a rule rather than act on a
   coin flip. Outside the band, the probability itself is the answer.
3. SCORE RUBRICS ARE ORDERED WORD-LABELLED LEVELS (see client.py), and every
   Score answer arrives here already normalised to 0..1.
4. **GIVE THE MODEL THE RUN.**  Measured, not assumed: the first live run
   against real JEV (2026-09-21) returned 61 of 63 answers below the confidence
   floor, because modules were hand-building two-field states while asking
   questions those two fields could not possibly answer. Passing a `RunContext`
   makes every question evaluate against the full run digest (deck contents and
   shape, relics, potions, HP and HP budget, act, floor, gold, goal).
   `run=None` still works — it produces the old thin state, which is what the
   offline tests exercise — but a real run should always pass one.
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
from spirebrain.jev_brain.state import RunContext, card_effect_text, relic_effect_text

CONFIDENCE_FLOOR = 0.60  # mirrored from config/strategy.json
SCORE_ACTION_FLOOR = 0.55  # below this, "nothing here is worth taking"
# When the model cannot tell, heal ONLY below this HP ratio — otherwise upgrade.
#
# 0.50 is not a guess. Five independent strategy guides converge on the same
# number and the same reasoning, in both languages:
#   "新手太容易选择回血——实际上强化更重要。除非生命<50%" (ntgame.com/sts/strategy)
#   "半血以上敲牌比补血划算得多" (3h3.com 晋升心得)
#   "upgrading whenever it's safe to do so can significantly strengthen your deck" (spire-codex)
# A rest heals a flat 30 HP and is repeatable; an upgrade is permanent. So
# upgrading is the default and healing is the exception. Our previous 0.60 floor
# plus a fallback that ALWAYS healed had it exactly backwards.
HEAL_WHEN_UNSURE_HP_RATIO = 0.50

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


# Thresholds for accepting a Choice answer on the strength of its DISTRIBUTION
# rather than on its confidence. See accept_choice() for why.
CHOICE_TOP_FLOOR = 0.50   # the chosen option must actually be the most likely one
CHOICE_MARGIN = 0.15      # and must beat the runner-up by this much


def accept_choice(answer, *, floor: float = CONFIDENCE_FLOOR,
                  top_floor: float = CHOICE_TOP_FLOOR,
                  margin: float = CHOICE_MARGIN) -> bool:
    """Should we act on this Choice, or defer to the safe rule?

    Measured, not assumed (2026-09-21). Enriching the decision state to the full
    run digest — deck contents, relics, potions, HP budget, act, floor, gold,
    goal — did NOT raise JEV's confidence on our *preference* questions: the
    fallback rate stayed at 61 of 63 answers below 0.60 while input cost rose
    ~48%. The confidences cluster at 0.29-0.47 across every module.

    That is not the model failing. Confidence summarises how peaked the answer
    distribution is, and "which of these cards is better for an abstract goal"
    genuinely has no sharp answer — a flat-ish distribution *is* the honest
    response. The type docs' own worked example shows the same thing (p=0.84 on
    the top option, confidence 0.596). The docs are explicit that the automation
    threshold "cannot be deduced from this single case; it has to be chosen
    against the risk and validated on labelled data from your own domain".

    So for preference questions we stop asking "how sure are you?" and ask the
    question the distribution can actually answer: **is the top option clearly
    ahead?** Accept when the chosen option is the most likely one, its
    probability clears `top_floor`, and it leads the runner-up by `margin`.
    Otherwise defer to the rule, which is the old behaviour.

    Noul deliberately does NOT use this: for a yes/no fact-like question the
    probability is the belief and the (0.40, 0.60) band already means "cannot
    tell" — see noul_verdict().
    """
    raw = getattr(answer, "raw", None) or {}
    probs = raw.get("probabilities") or {}
    if not probs:
        # No distribution to reason about (mocks, or a provider that omits it):
        # fall back to the confidence floor so behaviour stays predictable.
        return getattr(answer, "confidence", 0.0) >= floor
    try:
        items = sorted(((k, float(v)) for k, v in probs.items()), key=lambda kv: -kv[1])
    except (TypeError, ValueError):
        return getattr(answer, "confidence", 0.0) >= floor
    top_key, top_p = items[0]
    runner_up = items[1][1] if len(items) > 1 else 0.0
    chosen_is_top = str(answer.value) == top_key
    return bool(chosen_is_top and top_p >= top_floor and (top_p - runner_up) >= margin)


def accept_score(answer, *, spec=None, floor: float = CONFIDENCE_FLOOR,
                 action_floor: float = SCORE_ACTION_FLOOR,
                 top_floor: float = CHOICE_TOP_FLOOR,
                 margin: float = CHOICE_MARGIN) -> bool:
    """The Score sibling of accept_choice().

    Same reasoning, one wrinkle: a Score answer's `probabilities` are keyed by
    LEVEL INDEX (`"0"`, `"1"`, ...) while the value we normalise is a fractional
    position between levels. So "is the chosen option clearly ahead?" becomes
    "is the level the model actually landed on the same level the distribution
    favours, and does it lead the runner-up?" — plus the value must still clear
    the action floor, since a clearly-favoured 'filler card' is still filler.

    Without a distribution (mocks) this degrades to the old rule exactly:
    value above the action floor AND confidence above the floor.
    """
    want = getattr(answer, "value", 0.0)
    raw = getattr(answer, "raw", None) or {}
    probs = raw.get("probabilities") or {}
    if not probs:
        return bool(want >= action_floor and getattr(answer, "confidence", 0.0) >= floor)
    try:
        items = sorted(((k, float(v)) for k, v in probs.items()), key=lambda kv: -kv[1])
        top_key, top_p = items[0]
        runner_up = items[1][1] if len(items) > 1 else 0.0
        # Which level did the model land on? Round the raw level when we have it.
        level = raw.get("score", raw.get("level"))
        landed = str(int(round(float(level)))) if level is not None else None
    except (TypeError, ValueError):
        return bool(want >= action_floor and getattr(answer, "confidence", 0.0) >= floor)
    if landed is not None and landed != top_key:
        return False
    return bool(want >= action_floor and top_p >= top_floor and (top_p - runner_up) >= margin)


def _probe_details(resp: JevResponse) -> dict:
    return {k: {"value": a.value, "confidence": a.confidence} for k, a in resp.answers.items()}


def _state_from(run: RunContext | None, extra: dict, thin: dict) -> dict:
    """Full run digest when a RunContext is available, else the legacy thin state.

    The two branches are kept side by side on purpose: `thin` documents exactly
    what the modules used to send, which is what made JEV answer ~0.4 to
    questions it could not possibly resolve.
    """
    if run is not None:
        return run.digest(extra)
    return {**thin, **extra}


# --------------------------------------------------------------------------- #
# 1. Map routing
# --------------------------------------------------------------------------- #
class MapRouter:
    """Map node selection: Choice over reachable nodes + parallel Noul HP probes.

    The user's original question — "is the HP I'd lose on this path acceptable?"
    — is asked once per candidate path in the same call as the route choice.
    """

    def __init__(self, jev: JevClient, hp_budget, run: RunContext | None = None) -> None:
        self.jev = jev
        self.hp = hp_budget
        self.run = run

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
            resp = self.jev.ask(self._state(reachable, path_probes), questions)

            route = resp.answers["route"]
            probe_key = f"over_budget_{route.value}"
            probe = resp.answers.get(probe_key)
            if probe is not None and noul_verdict(probe.confidence) is True:
                return self._fallback(reachable, path_probes,
                                      reason="chosen route judged over HP budget")
            if not accept_choice(route):
                return self._fallback(reachable, path_probes,
                                      reason="route not clearly ahead")
            return Decision("map", route.value, route.confidence, False,
                            {"probes": _probe_details(resp)})
        except Exception as exc:  # noqa: BLE001 - any failure must not stop the run
            return self._fallback(reachable, path_probes, reason=f"jev error: {exc}")

    def _state(self, reachable: dict, path_probes: dict) -> dict:
        extra = {
            "reachable_nodes": reachable,
            "estimated_damage_to_next_rest": path_probes,
        }
        thin = {
            "act": self.hp.act,
            "hp": {"current": self.hp.current_hp, "max": self.hp.max_hp},
            "hp_budget": {"remaining_spendable": self.hp.remaining_budget,
                          "reserved": self.hp.reserved_hp},
        }
        return _state_from(self.run, extra, thin)

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
                 deck_digest: str = "", goal: str = "", run: RunContext | None = None) -> None:
        self.jev = jev
        self.deck_size = deck_size
        self.max_cards = max_cards
        self.deck_digest = deck_digest
        self.goal = goal
        self.run = run

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
                        f"already does and the run's goal? Card: {self._describe(name, desc)}"
                    ),
                    criteria=list(CARD_RUBRIC),
                )
                for name, desc in candidates.items()
            }
            resp = self.jev.ask(self._state(candidates), questions)
            best_name, best = max(resp.answers.items(), key=lambda kv: kv[1].value)
            scores = {k: round(v.value, 4) for k, v in resp.answers.items()}
            if not accept_score(best):
                return Decision("card_reward", "skip", best.confidence, True,
                                {"scores": scores, "best": best_name,
                                 "reason": "best card not clearly worth taking"})
            return Decision("card_reward", best_name, best.confidence, False, {"scores": scores})
        except Exception:  # noqa: BLE001
            return Decision("card_reward", "skip", 0.0, True, {"reason": "jev error"})

    def _state(self, candidates: dict) -> dict:
        thin: dict = {"deck": {"size": self.deck_size, "contents": self.deck_digest}}
        if self.goal:
            thin["goal"] = self.goal
        extra = {"card_reward_offered": list(candidates)}
        return _state_from(self.run, extra, thin)

    def _describe(self, name: str, fallback: str) -> str:
        """The card's own text from the game install; the caller's gloss only if absent.

        Measured motivation: with names and one-line glosses only, JEV's Score
        answers for cards came back flat and every reward got skipped
        (docs/JEV_API.md, run 3). A card's real text is the minimum a model needs
        to compare it against a deck — and it is precisely the gap the only other
        JEV + Slay-the-Spire project documents as a limitation ("Card text and
        some observations are missing from upstream snapshots").
        """
        text = card_effect_text(name, character=self.run.character if self.run else None)
        if text:
            return text
        if fallback:
            return f"{fallback} (no text found in the game's card data)"
        return "no text found in the game's card data"


# --------------------------------------------------------------------------- #
# 3. Events
# --------------------------------------------------------------------------- #
class EventChooser:
    """Event options: a single Choice. Never forced — low confidence defers to rule."""

    def __init__(self, jev: JevClient, goal: str = "", run: RunContext | None = None) -> None:
        self.jev = jev
        self.goal = goal
        self.run = run

    def decide(self, event_text: str, options: dict[str, str]) -> Decision:
        thin = {"event": event_text}
        if self.goal:
            thin["goal"] = self.goal
        try:
            resp = self.jev.ask(
                _state_from(self.run, {"event": event_text}, thin),
                {"choice": ChoiceSpec(
                    instructions=(
                        "Which option best serves the run, weighing the reward against HP, "
                        "gold, curse or relic consequences? Pick the option whose downside this "
                        "run can actually absorb."
                    ),
                    criteria=options)},
            )
            ans = resp.answers["choice"]
            if not accept_choice(ans):
                return self._fallback(
                    options, f"not clearly ahead (confidence {ans.confidence:.2f})")
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
    """Rest site: Noul(heal?) + Choice(which upgrade) in one call.

    Note the asymmetry we encode: an unnecessary rest is a wasted opportunity,
    but an unnecessary upgrade can kill the run. So the fallback always heals.
    """

    def __init__(self, jev: JevClient, hp_budget=None, goal: str = "",
                 run: RunContext | None = None) -> None:
        self.jev = jev
        self.hp = hp_budget
        self.goal = goal
        self.run = run

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
            resp = self.jev.ask(self._state(hp_ratio, upgradable), questions)
            heal = noul_verdict(resp.answers["need_heal"].confidence)
            up = resp.answers["upgrade"]

            if heal is True:
                return Decision("rest", "rest", resp.answers["need_heal"].confidence, False,
                                {"reason": "judged to need healing"})
            if heal is None or not accept_choice(up):
                return self._fallback(upgradable, hp_ratio, "uncertain")
            if up.value == "rest" or up.value not in upgradable:
                return Decision("rest", "rest", up.confidence, False,
                                {"reason": "no upgrade worth the HP"})
            return Decision("rest", up.value, up.confidence, False,
                            {"probabilities": up.raw.get("probabilities", {})})
        except Exception as exc:  # noqa: BLE001
            return self._fallback(upgradable, hp_ratio, f"jev error: {exc}")

    def _state(self, hp_ratio: float, upgradable: dict) -> dict:
        thin: dict = {"hp_ratio": round(hp_ratio, 3)}
        if self.hp:
            thin["hp_budget"] = {
                "remaining_spendable": self.hp.remaining_budget,
                "reserved": self.hp.reserved_hp,
                "act": self.hp.act,
            }
        if self.goal:
            thin["goal"] = self.goal
        extra = {"upgradable_cards": list(upgradable)}
        return _state_from(self.run, extra, thin)

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

    def __init__(self, jev: JevClient, goal: str = "", run: RunContext | None = None) -> None:
        self.jev = jev
        self.goal = goal
        self.run = run

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
            resp = self.jev.ask(self._state(gold, affordable), questions)
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

    def _state(self, gold: int, affordable: dict) -> dict:
        thin = {"gold": gold}
        if self.goal:
            thin["goal"] = self.goal
        extra = {"shop_affordable": {k: v[0] for k, v in affordable.items()}}
        return _state_from(self.run, extra, thin)


# --------------------------------------------------------------------------- #
# 6. Boss relics
# --------------------------------------------------------------------------- #
class BossRelicJudge:
    """Boss relics: Score x 3. The choice is mandatory, so we never skip — but a
    low-confidence pick is flagged, which is what a later review reads."""

    def __init__(self, jev: JevClient, goal: str = "", deck_digest: str = "",
                 run: RunContext | None = None) -> None:
        self.jev = jev
        self.goal = goal
        self.deck_digest = deck_digest
        self.run = run

    def decide(self, relics: dict[str, str]) -> Decision:
        if not relics:
            return Decision("boss_relic", None, 0.0, True, {"reason": "no relics offered"})
        try:
            questions = {
                name: ScoreSpec(
                    instructions=(
                        "How good is this boss relic for this run's plan? Account for its "
                        f"drawback, not just its upside. Relic: {self._describe(name, desc)}"
                    ),
                    criteria=list(RELIC_RUBRIC),
                )
                for name, desc in relics.items()
            }
            resp = self.jev.ask(self._state(relics), questions)
            best_name, best = max(resp.answers.items(), key=lambda kv: kv[1].value)
            scores = {k: round(v.value, 4) for k, v in resp.answers.items()}
            # Mandatory pick: take the best even when unsure, but flag it.
            low = not accept_score(best)
            return Decision("boss_relic", best_name, best.confidence, low,
                            {"scores": scores,
                             **({"reason": "mandatory pick, low confidence"} if low else {})})
        except Exception as exc:  # noqa: BLE001
            first = next(iter(relics))
            return Decision("boss_relic", first, 0.0, True,
                            {"reason": f"jev error: {exc}; took first offered"})

    def _state(self, relics: dict) -> dict:
        thin: dict = {"deck": self.deck_digest}
        if self.goal:
            thin["goal"] = self.goal
        extra = {"boss_relics_offered": list(relics)}
        return _state_from(self.run, extra, thin)

    @staticmethod
    def _describe(name: str, fallback: str) -> str:
        """The relic's own text from the game install; the caller's gloss only if absent."""
        text = relic_effect_text(name)
        if text:
            return text
        if fallback:
            return f"{fallback} (no text found in the game's relic data)"
        return "no text found in the game's relic data"


# --------------------------------------------------------------------------- #
# 7. Combat risk gate
# --------------------------------------------------------------------------- #
class CombatRiskGate:
    """Combat risk: Noul — "will this fight cost more HP than we can spend?"

    Used as the posture selector for the tactical layer: a risky verdict makes
    the greedy policy block more and play safer.
    """

    def __init__(self, jev: JevClient, hp_budget, run: RunContext | None = None) -> None:
        self.jev = jev
        self.hp = hp_budget
        self.run = run

    def decide(self, encounter: str, predicted_damage: int) -> Decision:
        try:
            resp = self.jev.ask(
                self._state(encounter, predicted_damage),
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

    def _state(self, encounter: str, predicted_damage: int) -> dict:
        extra = {"encounter": encounter, "predicted_incoming_damage": predicted_damage}
        thin = {
            "encounter": encounter,
            "predicted_damage": predicted_damage,
            "hp_budget": {
                "remaining_spendable": self.hp.remaining_budget,
                "reserved": self.hp.reserved_hp,
                "current_hp": self.hp.current_hp,
                "max_hp": self.hp.max_hp,
            },
        }
        return _state_from(self.run, extra, thin)


ALL_POINTS = (
    "map", "card_reward", "event", "rest", "shop", "boss_relic", "combat_risk",
)
