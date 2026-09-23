"""SpireBrain agent — the dispatcher between the game and the brain.

This is the Phase 1 integration point. It is written against the documented
CommunicationMod / spirecomm layout, but *duck-typed on purpose*: every field is
read defensively through `_get()`, so the agent can be unit-tested with plain
dicts and will tolerate minor schema drift when the live pipe is finally wired.
Anything that could only be confirmed against a running game is flagged inline
with `PHASE 1 VERIFY`.

Responding to a screen means turning a `Decision` back into a CommunicationMod
command. Two rules keep that honest:

* The tactical layer (card play, indices, arithmetic) owns every concrete
  command; JEV only ever supplies the judgement — a label, a score or a
  probability. Nothing here asks JEV to produce a command.
* Every decision is recorded in `self.history` with its confidence and whether a
  fallback fired, so a run can be replayed and audited afterwards.
"""

from __future__ import annotations

import json
from pathlib import Path

from spirebrain.jev_brain.client import get_client
from spirebrain.jev_brain.decisions import (
    HEAL_WHEN_UNSURE_HP_RATIO,
    BossRelicJudge,
    CardRewardJudge,
    CombatRiskGate,
    Decision,
    EventChooser,
    MapRouter,
    RestSiteDecider,
    ShopDecider,
)
from spirebrain.jev_brain.logging_client import LoggingJevClient
from spirebrain.jev_brain.state import (
    RunContext,
    card_line,
    deck_digest,
    deck_keys,
    english_name,
    lookup_keys,
    map_choices,
    path_damage_probes,
    shop_items,
)
from spirebrain.cards import best_removal, best_upgrade
from spirebrain.guide.rules import GuideAwareClient, GuideBook, GuideResult
from spirebrain.overlay.feed import DecisionFeed, decision_event, run_state_event
from spirebrain.tactical.combat_greedy import Card, CombatState, play_order, recommend_action
from spirebrain.tactical.hp_budget import HPBudget

ROOT = Path(__file__).resolve().parents[2]

MAP_SCREEN = "MAP"
CARD_REWARD_SCREEN = "CARD_REWARD"
EVENT_SCREEN = "EVENT"
REST_SCREEN = "REST"
SHOP_SCREEN = "SHOP_SCREEN"
BOSS_REWARD_SCREEN = "BOSS_REWARD"
COMBAT_SCREEN = "COMBAT"
# The grid you pick a card from after choosing "smith" at a rest site.
GRID_SCREENS = ("GRID", "CARD_SELECT", "HAND_SELECT")

# --------------------------------------------------------------------------- #
# What we are allowed to say
# --------------------------------------------------------------------------- #
# CommunicationMod understands a fixed verb set (verified against its README,
# 2026-09-21). Our route to a command does NOT get to invent words: an unknown
# verb is ignored by the game, which from the agent's side is indistinguishable
# from the pipe having died. So this set is the contract, and
# `tests/test_stdio.py` asserts every command the router can emit is inside it.
#
# Two traps this list makes explicit: there is no `skip` (skipping a card reward
# is RETURN, "equivalent to SKIP, CANCEL, and LEAVE"), and there is no `purge` or
# `smith` — every screen choice is CHOOSE.
PROTOCOL_VERBS = frozenset({
    "start", "potion", "play", "end", "choose", "proceed", "return", "key",
    "click", "wait", "state",
})


def _get(obj, *names, default=None):
    """Read a field from a dict or an object, tolerating both."""
    for name in names:
        if isinstance(obj, dict):
            if obj.get(name) is not None:
                return obj[name]
        else:
            value = getattr(obj, name, None)
            if value is not None:
                return value
    return default


def _unique_labels(items: list[str]) -> tuple[dict[str, str], dict[str, int]]:
    """Label -> description-holder and label -> index, disambiguating duplicates.

    The game addresses choices by index; JEV addresses them by label. This is the
    translation, and it is the one place where a duplicate name could silently
    misroute, so duplicates get a suffix.
    """
    labels: dict[str, str] = {}
    index: dict[str, int] = {}
    for i, raw in enumerate(items):
        label = raw or f"option {i}"
        if label in labels:
            label = f"{label} (#{i})"
        labels[label] = ""
        index[label] = i
    return labels, index


class SpireBrainAgent:
    """Receives game state, routes to decision modules, returns one command."""

    def __init__(self, jev_backend: str = "mock", strategy_path: str | Path | None = None,
                 log_dir: str | Path | None = None,
                 acceptance: str | None = None,
                 feed: DecisionFeed | None = None) -> None:
        self._constructor = {"jev_backend": jev_backend, "strategy_path": strategy_path,
                             "log_dir": log_dir, "acceptance": acceptance}
        strategy_file = Path(strategy_path) if strategy_path else ROOT / "config" / "strategy.json"
        self.strategy = json.loads(strategy_file.read_text(encoding="utf-8"))
        self.goal = self.strategy.get("goal", "")
        # "margin" | "argmax" — the Score gate, see decisions.evaluate_score. The
        # default is the one the pre-registered comparison in docs/MEASUREMENTS.md
        # selected; the strategy file carries it so it stays a human choice.
        self.acceptance = acceptance or self.strategy.get("jev", {}).get("score_acceptance")

        client = get_client(jev_backend)
        self.guide = GuideBook()
        logged_client = LoggingJevClient(
            client, log_dir=log_dir or ROOT / self.strategy.get("jev", {}).get("log_dir", "logs"))
        self._guide_client = GuideAwareClient(logged_client)
        self.jev = self._guide_client
        self.guide_result = GuideResult()
        self.hp: HPBudget | None = None
        self.run: RunContext | None = None
        # Set when JEV picks an upgrade at a rest site; consumed by the card grid
        # that follows. The protocol is one command per screen, so the intent has
        # to survive across two states.
        self._pending_upgrade: int | None = None
        # Set when this handler picks a card on ANY grid; consumed on the
        # confirm-phase state of the same grid (measured live 2026-09-22:
        # grids are two-phase — choose N puts the card in the slot, the same
        # screen returns offering confirm/cancel, CONFIRM finalizes).
        self._grid_picked: int | None = None
        # Guard #3 (ported from Ethics03/jevspire): navigation never costs a JEV
        # call. Two rules, both marked with `reason_source: navigation`:
        #   - non-decision screens get Proceed, not a model question;
        #   - a shop is *asked* once per floor — the state that comes back after
        #     a purchase is the same room wanting an exit, not a new decision.
        self._shop_decided_on_floor: int | None = None
        self.history: list[dict] = []
        # Phase 1.5, the interaction layer: an optional live feed of everything
        # the brain is thinking. None (the default) changes nothing — the feed
        # is an observability side-channel, never a dependency.
        self.feed = feed

    def clone_for_advice(self) -> "SpireBrainAgent":
        """A private router for the background model worker, with no live feed.

        The polling thread owns the panel and player tracker. This clone keeps
        the model's mutable run/grid state off that thread and prevents a late
        answer from publishing an old HP snapshot into the game's panel.
        """
        return SpireBrainAgent(**self._constructor, feed=None)

    def quick_advice(self, game: dict) -> dict:
        """Immediate, model-free advice or a truthful waiting/coverage state."""
        character = str(_get(game, "character", "class", default="")).upper()
        if character != "IRONCLAD":
            return {"status": "unsupported", "label": "当前角色攻略尚未覆盖",
                    "reason": "首版攻略只验证了铁甲战士；不会套用铁甲战士专属规则。",
                    "source_type": "unavailable"}
        self.observe(game)
        guide = self.guide.evaluate(game, self._budget().remaining_budget)
        if guide.command is not None and guide.command_rule is not None:
            rule = guide.command_rule
            return {"status": "ready", "command": guide.command,
                    "reason": rule.reason, "source_type": "guide_rule",
                    "source": rule.source, "guide_rules": guide.evidence()}
        if str(_get(game, "screen_type", default="")).upper() == COMBAT_SCREEN:
            suggestion = recommend_action(game)
            if suggestion is not None:
                return {"status": "ready", "command": suggestion.command,
                        "reason": suggestion.reason, "source_type": "rule_fallback",
                        "source": "游戏实时战斗状态；本地战术规则",
                        "guide_rules": guide.evidence()}
        return {"status": "thinking", "label": "正在分析当前局面…",
                "reason": "结合攻略规则与 JEV 判断，稍后显示当前一步。",
                "source_type": "pending", "guide_rules": guide.evidence()}

    # -- lifecycle --------------------------------------------------------- #
    def observe(self, game) -> None:
        """Sync our model of the world (HP budget + run context) from the game."""
        act = int(_get(game, "act", default=1))
        max_hp = int(_get(game, "max_hp", default=80))
        current_hp = int(_get(game, "current_hp", "hp", default=max_hp))
        if self.hp is None or self.hp.act != act:
            self.hp = HPBudget(act=act, max_hp=max_hp, current_hp=current_hp)
        else:
            self.hp.max_hp = max_hp
            self.hp.current_hp = current_hp

        # The live state must be as rich as the simulator's, or every question we
        # ask JEV is under-specified in exactly the way run 1-2 measured. Same
        # object, same digest, both paths.
        deck = self._deck(game)
        self.run = RunContext(
            act=act,
            floor=int(_get(game, "floor", "floor_num", default=0)),
            character=str(_get(game, "character", "class", "player_class", default="")).upper(),
            hp=current_hp,
            max_hp=max_hp,
            gold=int(_get(game, "gold", default=0)),
            deck=deck,
            relics=[str(r if isinstance(r, str) else _get(r, "name", "relic_id", default=""))
                    for r in (_get(game, "relics", default=[]) or [])],
            potions=[str(p if isinstance(p, str) else _get(p, "name", "potion_id", default=""))
                     for p in (_get(game, "potions", default=[]) or [])],
            goal=self.goal,
        ).with_budget(self.hp)

        self._publish_state()

    def _publish_state(self) -> None:
        """Push a run snapshot to the feed, if anyone is watching."""
        if self.feed is None:
            return
        try:
            self.feed.publish("run_state", run_state_event(self))
        except Exception:  # noqa: BLE001 - the dashboard must not break the run
            pass

    def choose_action(self, game) -> dict:
        """Return ONE CommunicationMod command for the current screen."""
        self.observe(game)
        self.guide_result = self.guide.evaluate(game, self._budget().remaining_budget)
        self._guide_client.active = self.guide_result.evidence()
        screen = str(_get(game, "screen_type", "screen", default="")).upper()
        if self.guide_result.command is not None:
            rule = self.guide_result.command_rule
            assert rule is not None
            decision = Decision(
                {"REST": "rest", "CARD_REWARD": "card_reward"}.get(screen, screen.lower()),
                "rest" if screen == "REST" else "skip", 0.0, True,
                {"reason": rule.reason, "guide_rules": self.guide_result.evidence(),
                 "source_type": "guide_rule", "rule_id": rule.id, "source": rule.source},
            )
            return self._record(decision, dict(self.guide_result.command))
        handler = {
            MAP_SCREEN: self._on_map,
            CARD_REWARD_SCREEN: self._on_card_reward,
            EVENT_SCREEN: self._on_event,
            REST_SCREEN: self._on_rest,
            SHOP_SCREEN: self._on_shop,
            BOSS_REWARD_SCREEN: self._on_boss_reward,
            COMBAT_SCREEN: self._on_combat,
        }.get(screen)
        if handler is None and screen in GRID_SCREENS:
            handler = self._on_grid
        else:
            # A half-finished pick only means anything on the grid it was made
            # on. Clearing it here — on every screen that is not a grid — keeps
            # a stale selection from being confirmed on some later, unrelated
            # grid (Neow's removal, then a shop removal twenty floors on).
            self._grid_picked = None
        if handler is None:
            # Guard #3 (jespire): a screen that is not a decision point gets a
            # navigation Proceed — never a JEV call, and never a `wait`, which
            # would leave the pipe idle forever on a screen the game expects an
            # answer for. If Proceed is not offered, the transport's SAFE_VERBS
            # substitution picks the next safe word; if Proceed does nothing,
            # the transport's stall guard stops the loop instead of retrying.
            return {"command": "proceed", "reason_source": "navigation",
                    "reason": f"non-decision screen {screen!r}: navigation only, no JEV call"}
        return handler(game)

    # -- shared helpers ---------------------------------------------------- #
    def _record(self, decision, command: dict) -> dict:
        detail = dict(decision.detail)
        detail.setdefault("guide_rules", self.guide_result.evidence())
        detail.setdefault("source_type", "rule_fallback" if decision.used_fallback else "jev")
        decision.detail = detail
        self.history.append({
            "point": decision.point,
            "value": decision.value,
            "confidence": round(float(decision.confidence), 4),
            "fallback": decision.used_fallback,
            "detail": decision.detail,
            "command": command,
        })
        if self.feed is not None:
            try:
                self.feed.publish("decision", decision_event(decision, command))
            except Exception:  # noqa: BLE001 - the dashboard must not break the run
                pass
        return command

    def _budget(self) -> HPBudget:
        if self.hp is None:
            self.hp = HPBudget(act=1, max_hp=80, current_hp=80)
        return self.hp

    def _deck(self, game) -> list[dict]:
        deck = _get(game, "deck", default=[]) or []
        return [d if isinstance(d, dict) else {"name": str(d)} for d in deck]

    # -- 1. map ------------------------------------------------------------ #
    def _on_map(self, game) -> dict:
        raw = _get(_get(game, "map", default=game), "next_nodes", default=[]) or []
        nodes = []
        for i, n in enumerate(raw):
            if i in self.guide_result.forbidden_indices:
                continue
            nodes.append({
                "id": str(_get(n, "x", default=i)) + "," + str(_get(n, "y", default=0)),
                "symbol": str(_get(n, "symbol", default="?")),
                "y": _get(n, "y", default=None),
                "original_index": i,
            })
        if not nodes:
            # No reachable node is known.  Inventing index zero could point at
            # an unreachable or explicitly forbidden route.
            return {"command": "state", "reason_source": "insufficient_state"}
        act = self._budget().act
        choices = map_choices(nodes)
        probes = path_damage_probes(nodes, act)
        d = MapRouter(self.jev, self._budget(), run=self.run).decide(choices, probes)
        index = {str(n["id"]): n["original_index"] for n in nodes}
        choice = index.get(str(d.value), 0)
        return self._record(d, {"command": "choose", "choice": choice})

    # -- 2. card reward ---------------------------------------------------- #
    def _on_card_reward(self, game) -> dict:
        screen = _get(game, "screen_state", "screen", default=game)
        raw = _get(screen, "cards", default=[]) or []
        # `id` before `name`: the game reports names in its own language (this
        # machine runs ZHS Chinese) while gamedata indexes the English files, so
        # a name-first lookup silently yields "effect not found" for every card.
        # See state.lookup_keys for the evidence and the reasoning.
        names = [str(_get(c, "id", "card_id", "name", default=f"card {i}"))
                 for i, c in enumerate(raw)]
        labels, index = _unique_labels(names)
        descriptions = {
            label: str(_get(raw[i], "description", "raw_description", default=names[i]))
            for label, i in index.items()
        }
        deck = self._deck(game)
        d = CardRewardJudge(
            self.jev, len(deck), self.strategy["deck_policy"]["max_cards"],
            deck_digest=deck_digest(deck), goal=self.goal, run=self.run,
            acceptance=self.acceptance,
            # The deck itself, not just its size: the local grader needs to know
            # what the deck already does (and which act we are in) to say whether
            # an offered card fills a real gap. See spirebrain/cards/deck.py.
            # `deck_keys`, not `deck`: the live deck is raw game entries and the
            # knowledge base speaks card ids (the `id`-before-`name` rule).
            deck_cards=deck_keys(deck), act=self._budget().act,
        ).decide(descriptions)
        if d.value == "skip":
            # There is no `skip` verb in the protocol: RETURN *is* skip
            # ("equivalent to SKIP, CANCEL, and LEAVE").
            return self._record(d, {"command": "return"})
        return self._record(d, {"command": "choose", "choice": index.get(str(d.value), 0)})

    # -- 3. event ---------------------------------------------------------- #
    def _on_event(self, game) -> dict:
        screen = _get(game, "screen_state", "screen", default=game)
        raw = _get(screen, "options", default=[]) or []
        event_text = str(_get(screen, "body", "event_id", default="an event"))
        texts = [str(_get(o, "label", "text", default=f"option {i}"))
                 for i, o in enumerate(raw)]
        labels, index = _unique_labels(texts)
        if not labels:
            return {"command": "state", "reason_source": "insufficient_state"}

        # Options the game has greyed out are not choices; never ask about them.
        available = {label: "" for label, i in index.items()
                     if not _get(raw[i], "disabled", default=False)
                     and i not in self.guide_result.forbidden_indices}
        if not available:
            return {"command": "state", "reason_source": "no_safe_candidate"}

        d = EventChooser(self.jev, goal=self.goal, run=self.run).decide(event_text, available)
        return self._record(d, {"command": "choose", "choice": index.get(str(d.value), 0)})

    # -- 4. rest site ------------------------------------------------------ #
    def _on_rest(self, game) -> dict:
        """Rest sites are a CHOOSE, not a `rest`/`smith` verb (there are neither).

        The protocol takes one command per screen, so an upgrade decision spans
        two states: this answers the rest-site screen, and `_on_grid` answers the
        card grid that follows using the intent stashed in `_pending_upgrade`.
        """
        screen = _get(game, "screen_state", "screen", default=game)
        options = [str(o) for o in (_get(screen, "rest_options", default=[]) or [])]
        rest_i = next((i for i, o in enumerate(options) if "rest" in o.lower()), 0)
        smith_i = next((i for i, o in enumerate(options)
                        if "smith" in o.lower() or "upgrade" in o.lower()), None)
        if smith_i is None:
            # Nothing to decide: JEV is not consulted when there is no choice.
            d = RestSiteDecider(self.jev, self._budget(), self.goal,
                                run=self.run).decide(hp_ratio=self._hp_ratio(), upgradable={})
            self._pending_upgrade = None
            return self._record(d, {"command": "choose", "choice": rest_i})

        deck = self._deck(game)
        # One label list feeds both the question and the grid index, so they can
        # never drift apart. Labels come from `id` (language-independent) and the
        # description is the card's own text from the game's data — this decision
        # point used to receive nothing but a type word, which is not enough to
        # judge an upgrade against.
        character = self.run.character if self.run else None
        upgradable: dict[str, str] = {}
        labels: list[str] = []
        label_of_card: dict[str, str] = {}      # game id -> this screen's label
        for i, card in enumerate(deck):
            if _get(card, "upgrades", default=0):
                continue
            keys = lookup_keys(card)
            label = (english_name(keys, "cards", character)
                     or str(_get(card, "name", default=f"card {i}")))
            labels.append(label)
            upgradable[label] = card_line(card, character=character)
            if keys:
                label_of_card[keys[0]] = label
        d = RestSiteDecider(self.jev, self._budget(), self.goal,
                            run=self.run).decide(hp_ratio=self._hp_ratio(),
                                                 upgradable=upgradable)
        if d.value == "rest":
            # The safe fallback always heals ("an unnecessary upgrade can kill
            # the run"), and that is still the right answer at low HP. But a
            # coach can beat the safe default when the deck itself names a
            # target: the upgrade table IS the documented consensus, so a
            # value-3 card (Whirlwind, Bash early, an archetype core) is a
            # stronger recommendation than "rest" repeated for free. Still a
            # fallback decision -- the model did not make it -- and it only
            # fires with real HP headroom, the same line the decider uses.
            local = best_upgrade(deck_keys(deck), act=self._budget().act)
            if (d.used_fallback and local is not None and local.value >= 3
                    and self._hp_ratio() >= HEAL_WHEN_UNSURE_HP_RATIO):
                local_label = label_of_card.get(local.card_id)
                if local_label is not None:
                    # Keep the original fallback reason: an audit trail that
                    # replaced its evidence is not an audit trail.
                    d = Decision("rest", local_label, 0.0, True,
                                 {**d.detail,
                                  "fallback_reason": d.detail.get("reason", ""),
                                  "local_upgrade": {"model": "rest",
                                                    "local": local_label,
                                                    "reason": local.reason_text()},
                                  "reason": (f"血量充足，按升级优先级升级「{local_label}」"
                                             f"：{local.reason_text()}")})
            if d.value == "rest":
                # Still resting (the fallback stood, or the deck named nothing):
                # no grid intent, answer the rest-site screen with rest.
                self._pending_upgrade = None
                return self._record(d, {"command": "choose", "choice": rest_i})
            # An upgrade was chosen ON THIS SCREEN (model or deck): the command
            # is the smith slot, and the following grid gets the card's index.
            # Saying "upgrade Whirlwind" while choosing rest would be exactly
            # the kind of advice/command split a player cannot trust.
            _, index = _unique_labels(labels)
            self._pending_upgrade = index.get(str(d.value), 0)
            return self._record(d, {"command": "choose", "choice": smith_i})

        # The deck gets a vote, as it does on card rewards. The upgrade table is
        # the one the sources actually document ("Whirlwind first, then True Grit
        # and Body Slam; Powers > utility > defence > attacks", and Bash early),
        # so when the model is unsure this decides — an upgrade is permanent, and
        # spending it on the wrong card is the mistake the table exists to stop.
        local = best_upgrade(deck_keys(deck), act=self._budget().act)
        local_label = label_of_card.get(local.card_id) if local else None
        if local is not None and local_label is None:
            # The local pick is not on this screen (an already-upgraded copy, or
            # a name this screen renders differently): keep the model's answer but
            # record what the deck would have preferred, so it stays auditable.
            d.detail["local_preference"] = {"card": local.card_id,
                                            "reason": local.reason_text()}
        elif local_label is not None and local_label != str(d.value) \
                and float(d.confidence or 0.0) < 0.6:
            d.detail["local_override"] = {"model": str(d.value), "local": local_label,
                                          "reason": local.reason_text()}
            d = Decision("rest", local_label, 0.0, True,
                         {**d.detail, "reason": f"按升级优先级：{local.reason_text()}"})

        # PHASE 1 VERIFY: the grid's index is the position among *upgradable*
        # cards in the order the game presents them. We assume that order matches
        # the deck array; if the live game sorts the grid differently, the wrong
        # card gets upgraded — visible in the run, harmless, but worth checking.
        _, index = _unique_labels(labels)
        self._pending_upgrade = index.get(str(d.value), 0)
        return self._record(d, {"command": "choose", "choice": smith_i})

    def _on_grid(self, game) -> dict:
        """Card-grid screens: fulfil the pending upgrade, else name the removal.

        A grid we did not ask for is almost always a *card removal* (a shop's
        purge, an event's sacrifice), and taking the first card there is how you
        delete the best card in the deck. It now asks the deck which card to lose
        — the community's order is "Strikes first, then Defends", and a curse or
        a status outranks both — and says so in the reason.

        Two-phase grids, measured live (2026-09-22 18:54): after `choose N` the
        SAME screen returns, now offering [confirm, cancel, ...] — the pick is
        in the slot and the game wants CONFIRM to finalize. This handler tracks
        what it picked; when the screen offers confirm, it confirms instead of
        picking again (a second `choose` re-opens the slot, and the pipe
        ping-pongs forever — the third live death that night).
        """
        available = set()
        # `available_commands` lives on the message, not the game_state; the
        # router only sees game_state, so the transport passes it down when it
        # differs. Fall back to the screen's own hint: CommunicationMod keeps
        # the picked card in `screen_state.cards` and swaps the verb list.
        for cmd in (_get(game, "available_commands", default=[]) or []):
            available.add(str(cmd).strip().lower())
        confirm_offered = "confirm" in available or \
            bool(_get(_get(game, "screen_state", "screen", default={}),
                      "confirm_button", "picked", default=False))

        if confirm_offered and self._grid_picked is not None:
            # Finalize the pick we made one state ago.
            self._grid_picked = None
            return {"command": "confirm"}
        if confirm_offered:
            # A confirm-only grid we did not pick (opened by the player?).
            # Confirm is still the only way through; keep it honest in history.
            return {"command": "confirm",
                    "reason": "confirm offered with no pending pick; confirming to advance"}

        if self._pending_upgrade is not None:
            choice, self._pending_upgrade = self._pending_upgrade, None
            reason = None
        else:
            # No intent of ours: the game is offering a removal, and the deck
            # knows which card it can spare. The cards on screen are the deck's
            # own, in the order `_deck` reports them, so the index of the local
            # pick among them is the index this screen wants.
            choice = 0
            reason = "网格屏无待定意图；按删牌优先级选择的说明见 local_removal"
            detail_note = None
            deck = self._deck(game)
            removal = best_removal(deck_keys(deck))
            if removal is not None:
                ids_in_order = deck_keys(deck)
                if removal.card_id in ids_in_order:
                    choice = ids_in_order.index(removal.card_id)
                    reason = f"删牌优先级：{removal.reason_text()}"
                    detail_note = {"card": removal.card_id,
                                   "reason": removal.reason_text()}
            self._grid_picked = choice
            out = {"command": "choose", "choice": choice, "reason": reason}
            if detail_note:
                out["local_removal"] = detail_note
            return out
        self._grid_picked = choice
        out = {"command": "choose", "choice": choice}
        if reason:
            out["reason"] = reason
        return out

    def _hp_ratio(self) -> float:
        hp = self._budget()
        return hp.current_hp / hp.max_hp if hp.max_hp else 0.0

    # -- 5. shop ----------------------------------------------------------- #
    def _on_shop(self, game) -> dict:
        """Shop: buying is CHOOSE by shelf index, leaving is RETURN.

        PHASE 1 VERIFY: CHOOSE addresses the shelves in the order cards, then
        relics, then potions (the order we build `index` in below), and the
        card-removal service is one more choice after them. Confirm on the live
        pipe — if the ordering differs, we buy the wrong item, which is visible
        and recoverable, so this stays a flagged assumption rather than a blocker.
        """
        floor = int(_get(game, "floor", "floor_num", default=0))
        if self._shop_decided_on_floor == floor:
            # Guard #3 (jespire): the decision for this room already happened;
            # the state that comes back after a purchase is the same room
            # wanting an exit, not a new question. Re-asking JEV against a
            # half-empty shelf spends a call to re-derive "leave".
            return {"command": "return", "reason_source": "navigation",
                    "reason": "second visit to the same shop this floor: leaving, no JEV call"}
        self._shop_decided_on_floor = floor
        screen = _get(game, "screen_state", "screen", default=game)
        gold = int(_get(game, "gold", default=0))
        raw = []
        for kind, key in (("card", "cards"), ("relic", "relics"), ("potion", "potions")):
            for item in (_get(screen, key, default=[]) or []):
                raw.append({"name": str(_get(item, "id", "card_id", "name", default=kind)),
                            "price": int(_get(item, "price", default=0)),
                            "description": str(_get(item, "description", default=""))})
        items = shop_items(raw)
        purge_cost = int(_get(screen, "purge_cost", default=0)) or None
        d = ShopDecider(self.jev, goal=self.goal, run=self.run).decide(
            gold=gold, items=items,
            removal_cost=purge_cost,
            remove_candidate="a starter Strike or Defend",
        )
        if d.value == "leave":
            return self._record(d, {"command": "return"})
        if d.value == "remove":
            # The purge service sits after the shelves; PURGE is not a verb.
            return self._record(d, {"command": "choose", "choice": len(raw)})
        choice = next((i for i, r in enumerate(raw) if r["name"] == str(d.value)), None)
        if choice is None:  # label came from a disambiguated duplicate
            choice = next((i for i, r in enumerate(raw)
                           if str(d.value).startswith(r["name"])), 0)
        return self._record(d, {"command": "choose", "choice": choice})

    # -- 6. boss relic ----------------------------------------------------- #
    def _on_boss_reward(self, game) -> dict:
        screen = _get(game, "screen_state", "screen", default=game)
        raw = _get(screen, "relics", default=[]) or []
        names = [str(_get(r, "id", "relic_id", "name", default=f"relic {i}"))
                 for i, r in enumerate(raw)]
        labels, index = _unique_labels(names)
        descriptions = {
            label: str(_get(raw[i], "description", default=names[i]))
            for label, i in index.items()
        }
        if not descriptions:
            return {"command": "choose", "choice": 0}
        d = BossRelicJudge(self.jev, goal=self.goal, run=self.run,
                           acceptance=self.acceptance).decide(descriptions)
        return self._record(d, {"command": "choose", "choice": index.get(str(d.value), 0)})

    # -- 7. combat --------------------------------------------------------- #
    def _on_combat(self, game) -> dict:
        combat = _get(game, "combat", default=game)
        player = _get(combat, "player", default=combat)
        monsters = _get(combat, "monsters", default=[]) or []
        hand_raw = _get(combat, "hand", default=[]) or []

        state = CombatState(
            player_hp=int(_get(player, "current_hp", "hp", default=1)),
            player_block=int(_get(player, "block", default=0)),
            energy=int(_get(player, "energy", default=3)),
            hand=[Card(name=str(_get(c, "name", "card_id", default="card")),
                       type=str(_get(c, "type", default="skill")).lower(),
                       damage=int(_get(c, "damage", default=0)),
                       block=int(_get(c, "block", default=0)),
                       energy=int(_get(c, "cost", default=1)) if int(_get(c, "cost", default=1)) >= 0 else 0)
                  for c in hand_raw],
            enemies=[{"name": str(_get(m, "name", default="enemy")),
                      "hp": int(_get(m, "current_hp", "hp", default=0)),
                      "intent": str(_get(m, "intent", default="unknown")).lower(),
                      "damage": int(_get(m, "move_adjusted_damage", "damage", default=0))}
                     for m in monsters],
        )

        # JEV decides posture; the code decides the cards. The gate is only
        # consulted when the incoming damage threatens the act's HP budget, so a
        # routine turn costs zero JEV calls.
        incoming = sum(max(0, e["damage"]) for e in state.enemies if "attack" in e["intent"])
        if incoming > self._budget().remaining_budget:
            gate = CombatRiskGate(self.jev, self._budget(), run=self.run)
            d = gate.decide(str(_get(combat, "encounter_name", default="a fight")), incoming)
            self._record(d, {"command": "(posture only)"})

        suggestion = recommend_action(game)
        if suggestion is not None:
            return self._record(Decision("combat", suggestion.command, 0.0, True,
                                         {"reason": suggestion.reason,
                                          "source_type": "rule_fallback",
                                          "uncertain": suggestion.uncertain}),
                                suggestion.command)
        return {"command": "state", "reason": "战斗状态不足，暂无法给出可靠的一步建议"}

    @staticmethod
    def _first_play(state: CombatState) -> dict:
        """0-indexed card and target: the +1 the protocol wants happens at the wire."""
        order = play_order(state)
        if not order:
            return {"command": "end"}
        target = order[0]
        for i, card in enumerate(state.hand):
            if card is target or card.name == target.name:
                return {"command": "play", "card": i, "target": 0}
        return {"command": "end"}
