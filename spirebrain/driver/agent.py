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
    deck_digest,
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
                 log_dir: str | Path | None = None) -> None:
        strategy_file = Path(strategy_path) if strategy_path else ROOT / "config" / "strategy.json"
        self.strategy = json.loads(strategy_file.read_text(encoding="utf-8"))
        self.goal = self.strategy.get("goal", "")

        client = get_client(jev_backend)
        self.jev = LoggingJevClient(
            client, log_dir=log_dir or ROOT / self.strategy.get("jev", {}).get("log_dir", "logs"))
        self.hp: HPBudget | None = None
        self.history: list[dict] = []

    # -- lifecycle --------------------------------------------------------- #
    def observe(self, game) -> None:
        """Sync our model of the world (currently: HP budget) from the game."""
        act = int(_get(game, "act", default=1))
        max_hp = int(_get(game, "max_hp", default=80))
        current_hp = int(_get(game, "current_hp", "hp", default=max_hp))
        if self.hp is None or self.hp.act != act:
            self.hp = HPBudget(act=act, max_hp=max_hp, current_hp=current_hp)
        else:
            self.hp.max_hp = max_hp
            self.hp.current_hp = current_hp

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
        d = MapRouter(self.jev, self._budget()).decide(choices, probes)
        index = {str(n["id"]): i for i, n in enumerate(nodes)}
        choice = index.get(str(d.value), 0)
        return self._record(d, {"command": "choose", "choice": choice})

    # -- 2. card reward ---------------------------------------------------- #
    def _on_card_reward(self, game) -> dict:
        screen = _get(game, "screen", default=game)
        raw = _get(screen, "cards", default=[]) or []
        names = [str(_get(c, "name", "card_id", default=f"card {i}")) for i, c in enumerate(raw)]
        labels, index = _unique_labels(names)
        descriptions = {
            label: str(_get(raw[i], "description", "raw_description", default=names[i]))
            for label, i in index.items()
        }
        deck = self._deck(game)
        d = CardRewardJudge(
            self.jev, len(deck), self.strategy["deck_policy"]["max_cards"],
            deck_digest=deck_digest(deck), goal=self.goal,
        ).decide(descriptions)
        if d.value == "skip":
            return self._record(d, {"command": "skip"})
        return self._record(d, {"command": "choose", "choice": index.get(str(d.value), 0)})

    # -- 3. event ---------------------------------------------------------- #
    def _on_event(self, game) -> dict:
        screen = _get(game, "screen", default=game)
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

        d = EventChooser(self.jev, goal=self.goal).decide(event_text, available)
        return self._record(d, {"command": "choose", "choice": index.get(str(d.value), 0)})

    # -- 4. rest site ------------------------------------------------------ #
    def _on_rest(self, game) -> dict:
        screen = _get(game, "screen", default=game)
        options = [str(o) for o in (_get(screen, "rest_options", default=[]) or [])]
        has_smith = any("smith" in o.lower() or "upgrade" in o.lower() for o in options)
        deck = self._deck(game)
        if not has_smith:
            # Nothing to decide: JEV is not consulted when there is no choice.
            d = RestSiteDecider(self.jev, self._budget(), self.goal).decide(
                hp_ratio=self._hp_ratio(), upgradable={})
            return self._record(d, {"command": "rest"})
        upgradable = {str(_get(c, "name", default=f"card {i}")): str(_get(c, "type", default=""))
                      for i, c in enumerate(deck)
                      if not _get(c, "upgrades", default=0)}
        d = RestSiteDecider(self.jev, self._budget(), self.goal).decide(
            hp_ratio=self._hp_ratio(), upgradable=upgradable)
        if d.value == "rest":
            return self._record(d, {"command": "rest"})
        cards = [str(_get(c, "name", default=f"card {i}")) for i, c in enumerate(deck)]
        _, index = _unique_labels(cards)
        return self._record(d, {"command": "smith", "choice": index.get(str(d.value), 0)})

    def _hp_ratio(self) -> float:
        hp = self._budget()
        return hp.current_hp / hp.max_hp if hp.max_hp else 0.0

    # -- 5. shop ----------------------------------------------------------- #
    def _on_shop(self, game) -> dict:
        """PHASE 1 VERIFY: CommunicationMod's shop commands and whether purchases
        pause the screen. Until a live run confirms it, we emit a conservative
        `leave` and let the callers of Phase 1 decide how aggressive to be."""
        screen = _get(game, "screen", default=game)
        gold = int(_get(game, "gold", default=0))
        raw = []
        for kind, key in (("card", "cards"), ("relic", "relics"), ("potion", "potions")):
            for item in (_get(screen, key, default=[]) or []):
                raw.append({"name": str(_get(item, "name", "card_id", default=kind)),
                            "price": int(_get(item, "price", default=0)),
                            "description": str(_get(item, "description", default=""))})
        items = shop_items(raw)
        d = ShopDecider(self.jev, goal=self.goal).decide(
            gold=gold, items=items,
            removal_cost=int(_get(screen, "purge_cost", default=0)) or None,
            remove_candidate="a starter Strike or Defend",
        )
        if d.value == "leave":
            return self._record(d, {"command": "leave"})
        if d.value == "remove":
            return self._record(d, {"command": "purge", "target": "Strike"})
        return self._record(d, {"command": "buy", "target": str(d.value)})

    # -- 6. boss relic ----------------------------------------------------- #
    def _on_boss_reward(self, game) -> dict:
        screen = _get(game, "screen", default=game)
        raw = _get(screen, "relics", default=[]) or []
        names = [str(_get(r, "name", "relic_id", default=f"relic {i}")) for i, r in enumerate(raw)]
        labels, index = _unique_labels(names)
        descriptions = {
            label: str(_get(raw[i], "description", default=names[i]))
            for label, i in index.items()
        }
        if not descriptions:
            return {"command": "choose", "choice": 0}
        d = BossRelicJudge(self.jev, goal=self.goal).decide(descriptions)
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
            gate = CombatRiskGate(self.jev, self._budget())
            d = gate.decide(str(_get(combat, "encounter_name", default="a fight")), incoming)
            self._record(d, {"command": "(posture only)"})

        if state.energy <= 0 or not state.hand:
            return {"command": "end"}
        return self._first_play(state)

    @staticmethod
    def _first_play(state: CombatState) -> dict:
        order = play_order(state)
        if not order:
            return {"command": "end"}
        target = order[0]
        for i, card in enumerate(state.hand):
            if card is target or card.name == target.name:
                return {"command": "play", "card_index": i, "target_index": 0}
        return {"command": "end"}
