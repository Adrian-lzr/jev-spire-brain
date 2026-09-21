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
    card_line,
    deck_digest,
    english_name,
    lookup_keys,
    map_choices,
    path_damage_probes,
    shop_items,
)
from spirebrain.tactical.combat_greedy import Card, CombatState, play_order
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
                 acceptance: str | None = None) -> None:
        strategy_file = Path(strategy_path) if strategy_path else ROOT / "config" / "strategy.json"
        self.strategy = json.loads(strategy_file.read_text(encoding="utf-8"))
        self.goal = self.strategy.get("goal", "")
        # "margin" | "argmax" — the Score gate, see decisions.evaluate_score. The
        # default is the one the pre-registered comparison in docs/MEASUREMENTS.md
        # selected; the strategy file carries it so it stays a human choice.
        self.acceptance = acceptance or self.strategy.get("jev", {}).get("score_acceptance")

        client = get_client(jev_backend)
        self.jev = LoggingJevClient(
            client, log_dir=log_dir or ROOT / self.strategy.get("jev", {}).get("log_dir", "logs"))
        self.hp: HPBudget | None = None
        self.run: RunContext | None = None
        # Set when JEV picks an upgrade at a rest site; consumed by the card grid
        # that follows. The protocol is one command per screen, so the intent has
        # to survive across two states.
        self._pending_upgrade: int | None = None
        self.history: list[dict] = []

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

    def choose_action(self, game) -> dict:
        """Return ONE CommunicationMod command for the current screen."""
        self.observe(game)
        screen = str(_get(game, "screen_type", "screen", default="")).upper()
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
        if handler is None:
            return {"command": "wait", "reason": f"no handler for screen {screen!r}"}
        return handler(game)

    # -- shared helpers ---------------------------------------------------- #
    def _record(self, decision, command: dict) -> dict:
        self.history.append({
            "point": decision.point,
            "value": decision.value,
            "confidence": round(float(decision.confidence), 4),
            "fallback": decision.used_fallback,
            "detail": decision.detail,
            "command": command,
        })
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
            nodes.append({
                "id": str(_get(n, "x", default=i)) + "," + str(_get(n, "y", default=0)),
                "symbol": str(_get(n, "symbol", default="?")),
                "y": _get(n, "y", default=None),
            })
        if not nodes:
            return {"command": "choose", "choice": 0}
        act = self._budget().act
        choices = map_choices(nodes)
        probes = path_damage_probes(nodes, act)
        d = MapRouter(self.jev, self._budget(), run=self.run).decide(choices, probes)
        index = {str(n["id"]): i for i, n in enumerate(nodes)}
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
            return {"command": "choose", "choice": 0}

        # Options the game has greyed out are not choices; never ask about them.
        available = {label: "" for label, i in index.items()
                     if not _get(raw[i], "disabled", default=False)}
        if not available:
            return {"command": "choose", "choice": 0}

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
        for i, card in enumerate(deck):
            if _get(card, "upgrades", default=0):
                continue
            keys = lookup_keys(card)
            label = (english_name(keys, "cards", character)
                     or str(_get(card, "name", default=f"card {i}")))
            labels.append(label)
            upgradable[label] = card_line(card, character=character)
        d = RestSiteDecider(self.jev, self._budget(), self.goal,
                            run=self.run).decide(hp_ratio=self._hp_ratio(),
                                                 upgradable=upgradable)
        if d.value == "rest":
            self._pending_upgrade = None
            return self._record(d, {"command": "choose", "choice": rest_i})

        # PHASE 1 VERIFY: the grid's index is the position among *upgradable*
        # cards in the order the game presents them. We assume that order matches
        # the deck array; if the live game sorts the grid differently, the wrong
        # card gets upgraded — visible in the run, harmless, but worth checking.
        _, index = _unique_labels(labels)
        self._pending_upgrade = index.get(str(d.value), 0)
        return self._record(d, {"command": "choose", "choice": smith_i})

    def _on_grid(self, game) -> dict:
        """Card-grid screens: fulfil the pending upgrade, else take the first card.

        A grid we did not ask for (card-removal, discard) gets the conservative
        first card — that is a real choice with a real cost, so it is logged as a
        fallback rather than passed off as a decision.
        """
        if self._pending_upgrade is not None:
            choice, self._pending_upgrade = self._pending_upgrade, None
            return {"command": "choose", "choice": choice}
        return {"command": "choose", "choice": 0,
                "reason": "grid screen with no pending intent; took the first option"}

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
        incoming = sum(e["damage"] for e in state.enemies if e["intent"] == "attack")
        if incoming > self._budget().remaining_budget:
            gate = CombatRiskGate(self.jev, self._budget(), run=self.run)
            d = gate.decide(str(_get(combat, "encounter_name", default="a fight")), incoming)
            self._record(d, {"command": "(posture only)"})

        if state.energy <= 0 or not state.hand:
            return {"command": "end"}
        return self._first_play(state)

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
