"""Bounded, single-run memory and its main-loop event boundary.

The live agent owns :class:`RunMemory`. Providers receive a
``RunMemorySnapshot`` and therefore cannot mutate the current run while a
request is in flight. Events intentionally use different kinds: a decision
explanation is not an execution result, and an inferred player action is not a
model recommendation.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
import copy
from types import MappingProxyType
from typing import Any


ADVICE_HISTORY = "advice_history"
EXECUTION_ATTEMPT = "execution_attempt"
FACT_MEMORY = "fact_memory"
RESULT_RECORD = "result_record"
DEVIATION_RECORD = "deviation_record"
UNOBSERVED_RECORD = "unobserved_record"
STATE_OBSERVED = "state_observed"


class MemoryEventKind:
    """String constants shared by the main loop, replay and dashboards."""

    ADVICE_HISTORY = ADVICE_HISTORY
    EXECUTION_ATTEMPT = EXECUTION_ATTEMPT
    FACT_MEMORY = FACT_MEMORY
    RESULT_RECORD = RESULT_RECORD
    DEVIATION_RECORD = DEVIATION_RECORD
    UNOBSERVED_RECORD = UNOBSERVED_RECORD
    STATE_OBSERVED = STATE_OBSERVED


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


def _freeze(value: Any) -> Any:
    """Recursively freeze a snapshot value without retaining live objects."""
    if isinstance(value, dict):
        return MappingProxyType({str(k): _freeze(v) for k, v in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(v) for v in value)
    if isinstance(value, set):
        return frozenset(_freeze(v) for v in value)
    return copy.deepcopy(value)


def _thaw(value: Any) -> Any:
    if isinstance(value, MappingProxyType):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    if isinstance(value, frozenset):
        return [_thaw(item) for item in value]
    return copy.deepcopy(value)


@dataclass(frozen=True)
class RunMemorySnapshot:
    """Immutable read-only memory passed to a background provider."""

    max_events: int = 20
    run_id: str = ""
    run_epoch: int = 0
    character: str = ""
    act: int = 1
    floor: int = 0
    hp: int = 0
    max_hp: int = 0
    gold: int = 0
    deck: tuple = ()
    relics: tuple = ()
    potions: tuple = ()
    current_objective: str = ""
    events: tuple = ()
    deviations: tuple = ()
    plan_id: str = ""
    plan_state_id: str = ""
    plan_reason: str = ""

    def digest(self, limit: int | None = None) -> dict:
        events = list(self.events)
        if limit is not None:
            events = events[-max(1, int(limit)):]
        return {
            "run_id": self.run_id,
            "run_epoch": self.run_epoch,
            "character": self.character,
            "act": self.act,
            "floor": self.floor,
            "hp": {"current": self.hp, "max": self.max_hp},
            "gold": self.gold,
            "deck": list(self.deck),
            "relics": list(self.relics),
            "potions": list(self.potions),
            "current_objective": self.current_objective,
            "recent_events": [_thaw(event) for event in events],
            "deviations": [_thaw(event) for event in self.deviations],
            "plan": {
                "plan_id": self.plan_id,
                "state_id": self.plan_state_id,
                "reason": self.plan_reason,
            } if self.plan_id else None,
        }


@dataclass
class RunMemory:
    """Facts and a short event tail; never persists across runs."""

    max_events: int = 20
    run_id: str = ""
    run_epoch: int = 0
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
    _terminal_run_id: str = field(default="", init=False, repr=False)

    def __post_init__(self) -> None:
        self.max_events = max(1, int(self.max_events))
        self.events = deque(self.events, maxlen=self.max_events)
        self.deviations = deque(self.deviations, maxlen=self.max_events)

    def snapshot(self) -> RunMemorySnapshot:
        """Capture a deeply immutable view for a background task."""
        return RunMemorySnapshot(
            max_events=self.max_events, run_id=self.run_id,
            run_epoch=self.run_epoch, character=self.character,
            act=self.act, floor=self.floor, hp=self.hp, max_hp=self.max_hp,
            gold=self.gold, deck=tuple(_freeze(x) for x in self.deck),
            relics=tuple(_freeze(x) for x in self.relics),
            potions=tuple(_freeze(x) for x in self.potions),
            current_objective=self.current_objective,
            events=tuple(_freeze(dict(x)) for x in self.events),
            deviations=tuple(_freeze(dict(x)) for x in self.deviations),
            plan_id=self.plan_id, plan_state_id=self.plan_state_id,
            plan_reason=self.plan_reason,
        )

    def identify_run(self, game: dict) -> str:
        explicit = _get(game, "run_id", "runId", "seed", default=None)
        if explicit not in (None, ""):
            return str(explicit)
        # CommunicationMod versions without a run id still provide enough
        # stable identity for a process-local memory. Act changes reset facts.
        return f"local:{_get(game, 'character', 'class', default='')}"

    def observe(self, game: dict, *, state_id: str = "") -> str:
        """Commit a normalized state observation on the owning game thread."""
        run_id = self.identify_run(game)
        act = int(_get(game, "act", default=1) or 1)
        screen = str(_get(game, "screen_type", "screen", default="")).upper()
        if screen in {"MENU", "DEATH", "VICTORY", "GAME_OVER"}:
            # Terminal/menu polls are often repeated. Reset once on the edge,
            # not once per poll, so ``run_epoch`` remains a run identity rather
            # than a transport counter.
            if self._terminal_run_id != run_id:
                self.reset(run_id)
                self._terminal_run_id = run_id
            # Keep terminal/reset semantics compatible with older callers:
            # the next playable state starts an empty run. The owning agent may
            # separately append a RESULT_RECORD with the terminal evidence.
            return self.run_id
        if self.run_id and (run_id != self.run_id or act < self.act):
            self.reset(run_id)
        self.run_id = run_id
        self._terminal_run_id = ""
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
        # during one decision. Avoid duplicating that snapshot.
        if state_id and any(
                event.get("kind") == STATE_OBSERVED and event.get("state_id") == state_id
                for event in reversed(self.events)):
            return self.run_id
        self.append_event(STATE_OBSERVED, state_id=state_id, screen=screen,
                          act=self.act, floor=self.floor, hp=self.hp, gold=self.gold)
        return self.run_id

    def append_event(self, kind: str, *, state_id: str = "", decision_id: str = "",
                     request_id: str = "", plan_id: str = "", **fields) -> dict:
        """Append one typed event; this is the only generic mutation entry."""
        event = {"kind": str(kind), "event_type": str(kind),
                 "run_id": self.run_id, "run_epoch": self.run_epoch,
                 "state_id": str(state_id or "")}
        if decision_id:
            event["decision_id"] = str(decision_id)
        if request_id:
            event["request_id"] = str(request_id)
        if plan_id:
            event["plan_id"] = str(plan_id)
        # ``result`` is accepted only by result records. A decision reason is
        # never silently reclassified as an execution result.
        if kind != RESULT_RECORD:
            fields.pop("result", None)
        event.update(copy.deepcopy(fields))
        self.events.append(event)
        return event

    def commit_event(self, kind: str, **fields) -> dict:
        """Named alias used by state-update callers and replay harnesses."""
        return self.append_event(kind, **fields)

    def apply_event(self, event: dict) -> dict:
        """Apply a serialized event through the same main-loop boundary.

        Replay tools use this method instead of reaching into ``events``. It is
        deliberately small: live state normalization still belongs to
        ``observe`` and only the owning loop may call this on a mutable memory.
        """
        data = dict(event or {})
        kind = str(data.pop("kind", data.pop("event_type", STATE_OBSERVED)))
        event_run_id = str(data.pop("run_id", "") or "")
        event_epoch = data.pop("run_epoch", None)
        if event_run_id and self.run_id and event_run_id != self.run_id:
            return {"accepted": False, "stale": True, "kind": kind,
                    "reason": "run_id_mismatch"}
        if event_epoch is not None:
            try:
                if int(event_epoch) != self.run_epoch:
                    return {"accepted": False, "stale": True, "kind": kind,
                            "reason": "run_epoch_mismatch"}
            except (TypeError, ValueError):
                return {"accepted": False, "stale": True, "kind": kind,
                        "reason": "invalid_run_epoch"}
        state_id = str(data.pop("state_id", "") or "")
        decision_id = str(data.pop("decision_id", "") or "")
        request_id = str(data.pop("request_id", "") or "")
        plan_id = str(data.pop("plan_id", "") or "")
        return self.append_event(kind, state_id=state_id,
                                 decision_id=decision_id, request_id=request_id,
                                 plan_id=plan_id, **data)

    def record_advice(self, advice: Any, *, displayed: bool = True) -> dict:
        """Record a recommendation generated/displayed to the player."""
        data = advice if isinstance(advice, dict) else {
            key: getattr(advice, key, None) for key in (
                "label", "command", "reason", "source_type", "state_id",
                "decision_id", "request_id", "plan_id", "advice_revision",
            )}
        return self.append_event(
            ADVICE_HISTORY, state_id=data.get("state_id", ""),
            decision_id=data.get("decision_id", ""),
            request_id=data.get("request_id", ""), plan_id=data.get("plan_id", ""),
            advice_revision=data.get("advice_revision", 0),
            displayed=bool(displayed), advice=copy.deepcopy(data),
            reason=str(data.get("reason", "") or ""),
        )

    def record_execution_attempt(self, command: dict, *, state_id: str = "",
                                 decision_id: str = "", request_id: str = "",
                                 plan_id: str = "", status: str = "sent",
                                 reason: str = "") -> dict:
        """Record a wire/action attempt, keeping its explanation separate."""
        return self.append_event(
            EXECUTION_ATTEMPT, state_id=state_id, decision_id=decision_id,
            request_id=request_id, plan_id=plan_id, command=dict(command or {}),
            status=str(status), reason=str(reason or ""),
        )

    def record_observed_action(self, action: Any, *, state_id: str = "",
                               decision_id: str = "", evidence: dict | None = None,
                               observed_state: dict | None = None) -> dict:
        """Commit a player action inferred from a later state."""
        if hasattr(action, "__dict__") and not isinstance(action, dict):
            data = dict(action.__dict__)
        else:
            data = dict(action or {})
        return self.append_event(
            FACT_MEMORY, state_id=state_id or data.get("state_id", ""),
            decision_id=decision_id or data.get("decision_id", ""),
            action=data, evidence=copy.deepcopy(evidence or {}),
            observed_state=copy.deepcopy(observed_state) if observed_state is not None else None,
        )

    def record_observed_result(self, result: Any, *, state_id: str = "",
                               decision_id: str = "", evidence: dict | None = None) -> dict:
        """Commit a game/result observation; unlike a reason it has a result key."""
        return self.append_event(RESULT_RECORD, state_id=state_id,
                                 decision_id=decision_id, result=copy.deepcopy(result),
                                 evidence=copy.deepcopy(evidence or {}))

    def record_action(self, action: dict, *, state_id: str = "", result: str = "") -> dict:
        """Legacy alias for execution attempts.

        Older integrations passed a decision reason as ``result``. Preserve the
        call shape but store that text as ``reason`` and never as ``result``.
        """
        return self.record_execution_attempt(action, state_id=state_id, reason=result)

    def record_deviation(self, advised: str, actual: str, *, state_id: str = "",
                         decision_id: str = "", evidence: dict | None = None,
                         observed_state: dict | None = None) -> dict:
        event = self.append_event(
            DEVIATION_RECORD, state_id=state_id, decision_id=decision_id,
            advised=str(advised), actual=copy.deepcopy(actual),
            evidence=copy.deepcopy(evidence or {}),
            observed_state=copy.deepcopy(observed_state) if observed_state is not None else None,
        )
        self.deviations.append({
            "state_id": state_id, "decision_id": decision_id,
            "advised": str(advised), "actual": copy.deepcopy(actual),
            "evidence": copy.deepcopy(evidence or {}),
        })
        return event

    def record_unobserved(self, *, state_id: str = "", decision_id: str = "",
                          evidence: dict | None = None, reason: str = "",
                          observed_state: dict | None = None) -> dict:
        return self.append_event(
            UNOBSERVED_RECORD, state_id=state_id, decision_id=decision_id,
            evidence=copy.deepcopy(evidence or {}), reason=str(reason or ""),
            observed_state=(copy.deepcopy(observed_state)
                            if observed_state is not None else None),
        )

    def set_plan(self, plan) -> None:
        """Commit a validated strategic plan on the owning game thread."""
        self.plan_id = str(getattr(plan, "plan_id", "") or "")
        self.plan_state_id = str(getattr(plan, "state_id", "") or "")
        self.current_objective = str(getattr(plan, "current_objective", "") or "")
        self.plan_reason = str(getattr(plan, "reason", "") or "")

    def reset(self, run_id: str = "") -> None:
        self.run_epoch += 1
        self.run_id = str(run_id or "")
        self._terminal_run_id = ""
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

    def mark_terminal(self, run_id: str = "") -> None:
        """Mark a terminal/menu edge after a coordinated plan reset."""
        self._terminal_run_id = str(run_id or self.run_id or "")

    def digest(self, limit: int | None = None) -> dict:
        return self.snapshot().digest(limit)
