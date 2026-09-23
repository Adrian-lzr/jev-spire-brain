"""Bounded, single-run memory for the strategic model."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any


def _get(obj: Any, *names: str, default=None):
    if not isinstance(obj, dict):
        return default
    for name in names:
        if obj.get(name) is not None:
            return obj[name]
    return default


def _name(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return str(_get(value, "id", "card_id", "relic_id", "name", default=""))
    return str(value)


@dataclass
class RunMemory:
    """Facts and a short event tail; never persists across runs."""

    max_events: int = 20
    run_id: str = ""
    character: str = ""
    act: int = 1
    floor: int = 0
    hp: int = 0
    max_hp: int = 0
    gold: int = 0
    deck: list[str] = field(default_factory=list)
    relics: list[str] = field(default_factory=list)
    potions: list[str] = field(default_factory=list)
    current_objective: str = ""
    events: deque = field(default_factory=deque)
    deviations: deque = field(default_factory=deque)
    plan_id: str = ""
    plan_state_id: str = ""
    plan_reason: str = ""

    def __post_init__(self) -> None:
        self.max_events = max(1, int(self.max_events))
        self.events = deque(self.events, maxlen=self.max_events)
        self.deviations = deque(self.deviations, maxlen=self.max_events)

    def identify_run(self, game: dict) -> str:
        explicit = _get(game, "run_id", "runId", "seed", default=None)
        if explicit not in (None, ""):
            return str(explicit)
        # CommunicationMod versions without a run id still provide enough
        # stable identity for a process-local memory.  Act changes reset facts.
        return f"local:{_get(game, 'character', 'class', default='')}"

    def observe(self, game: dict, *, state_id: str = "") -> str:
        run_id = self.identify_run(game)
        act = int(_get(game, "act", default=1) or 1)
        screen = str(_get(game, "screen_type", "screen", default="")).upper()
        if screen in {"MENU", "DEATH", "VICTORY", "GAME_OVER"}:
            self.reset(run_id)
            return self.run_id
        if self.run_id and (run_id != self.run_id or act < self.act):
            self.reset(run_id)
        self.run_id = run_id
        self.character = str(_get(game, "character", "class", "player_class", default="")).upper()
        self.act = act
        self.floor = int(_get(game, "floor", "floor_num", default=0) or 0)
        self.hp = int(_get(game, "current_hp", "hp", default=self.hp) or 0)
        self.max_hp = int(_get(game, "max_hp", default=self.max_hp) or 0)
        self.gold = int(_get(game, "gold", default=self.gold) or 0)
        self.deck = [_name(x) for x in (_get(game, "deck", default=[]) or [])]
        self.relics = [_name(x) for x in (_get(game, "relics", default=[]) or [])]
        self.potions = [_name(x) for x in (_get(game, "potions", default=[]) or [])]
        # Planner and orchestrator can both observe the same immutable state
        # during one decision.  Avoid duplicating that snapshot while still
        # recording a fresh state after an intervening action event.
        if (state_id and self.events and self.events[-1].get("state_id") == state_id
                and self.events[-1].get("kind") != "action"):
            return self.run_id
        self.events.append({
            "state_id": state_id,
            "screen": screen,
            "act": self.act,
            "floor": self.floor,
            "hp": self.hp,
            "gold": self.gold,
        })
        return self.run_id

    def record_action(self, action: dict, *, state_id: str = "", result: str = "") -> None:
        self.events.append({
            "kind": "action",
            "state_id": state_id,
            "action": dict(action or {}),
            "result": str(result or ""),
        })

    def record_deviation(self, advised: str, actual: str, *, state_id: str = "") -> None:
        self.deviations.append({
            "state_id": state_id,
            "advised": str(advised),
            "actual": str(actual),
        })

    def set_plan(self, plan) -> None:
        self.plan_id = str(getattr(plan, "plan_id", "") or "")
        self.plan_state_id = str(getattr(plan, "state_id", "") or "")
        self.current_objective = str(getattr(plan, "current_objective", "") or "")
        self.plan_reason = str(getattr(plan, "reason", "") or "")

    def reset(self, run_id: str = "") -> None:
        self.run_id = str(run_id or "")
        self.character = ""
        self.act = 1
        self.floor = 0
        self.hp = 0
        self.max_hp = 0
        self.gold = 0
        self.deck.clear()
        self.relics.clear()
        self.potions.clear()
        self.current_objective = ""
        self.events.clear()
        self.deviations.clear()
        self.plan_id = ""
        self.plan_state_id = ""
        self.plan_reason = ""

    def digest(self, limit: int | None = None) -> dict:
        events = list(self.events)
        if limit is not None:
            events = events[-max(1, int(limit)):]
        return {
            "run_id": self.run_id,
            "character": self.character,
            "act": self.act,
            "floor": self.floor,
            "hp": {"current": self.hp, "max": self.max_hp},
            "gold": self.gold,
            "deck": list(self.deck),
            "relics": list(self.relics),
            "potions": list(self.potions),
            "current_objective": self.current_objective,
            "recent_events": events,
            "deviations": list(self.deviations),
            "plan": {
                "plan_id": self.plan_id,
                "state_id": self.plan_state_id,
                "reason": self.plan_reason,
            } if self.plan_id else None,
        }
