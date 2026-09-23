"""Small deterministic rule layer in front of JEV.

The JSON file contains policy, while this module only knows how to match a
state, resolve the few concrete actions that policy permits, and expose the
matched evidence to the model. Game numbers always come from the live state.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RULES = ROOT / "config" / "guide_rules.json"


def _field(obj: Any, *names: str, default: Any = None) -> Any:
    if not isinstance(obj, dict):
        return default
    for name in names:
        if obj.get(name) is not None:
            return obj[name]
    return default


@dataclass(frozen=True)
class GuideRule:
    id: str
    scenes: tuple[str, ...]
    characters: tuple[str, ...]
    priority: int
    when: dict[str, Any]
    effect: str
    reason: str
    source: str


@dataclass
class GuideResult:
    matches: list[GuideRule] = field(default_factory=list)
    command: dict | None = None
    command_rule: GuideRule | None = None
    forbidden_indices: set[int] = field(default_factory=set)

    def evidence(self) -> list[dict]:
        return [{"id": r.id, "effect": r.effect, "reason": r.reason,
                 "source": r.source, "priority": r.priority} for r in self.matches]


class GuideBook:
    """Validated rules, ordered by priority. No executable conditions in JSON."""

    def __init__(self, path: str | Path = DEFAULT_RULES) -> None:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if data.get("version") != 1 or not isinstance(data.get("rules"), list):
            raise ValueError("guide rules need version=1 and a rules list")
        rules = []
        ids = set()
        for item in data["rules"]:
            rule = GuideRule(
                id=str(item["id"]), scenes=tuple(str(s).upper() for s in item["scenes"]),
                characters=tuple(str(c).upper() for c in item["characters"]),
                priority=int(item["priority"]), when=dict(item.get("when", {})),
                effect=str(item["effect"]), reason=str(item["reason"]),
                source=str(item["source"]),
            )
            if not rule.id or rule.id in ids or rule.effect not in {
                "inform", "rest", "skip_card", "safe_map", "avoid_lethal_event"
            } or not rule.source or not rule.reason:
                raise ValueError(f"invalid or duplicate guide rule: {rule.id!r}")
            ids.add(rule.id)
            rules.append(rule)
        self.rules = sorted(rules, key=lambda r: -r.priority)

    @staticmethod
    def _facts(game: dict, remaining_budget: int) -> dict[str, float]:
        hp = float(_field(game, "current_hp", "hp", default=0) or 0)
        max_hp = float(_field(game, "max_hp", default=0) or 0)
        return {"hp": hp, "hp_ratio": hp / max_hp if max_hp else 0.0,
                "deck_size": len(_field(game, "deck", default=[]) or []),
                "gold": float(_field(game, "gold", default=0) or 0),
                "budget_remaining": remaining_budget}

    @staticmethod
    def _when_matches(when: dict, facts: dict) -> bool:
        for key, bound in when.items():
            name, sep, operator = key.partition("__")
            if name not in facts or not sep or operator not in {"lt", "lte", "gt", "gte", "eq"}:
                raise ValueError(f"unsupported guide condition {key!r}")
            actual = facts[name]
            want = float(bound)
            if not {"lt": actual < want, "lte": actual <= want,
                    "gt": actual > want, "gte": actual >= want,
                    "eq": actual == want}[operator]:
                return False
        return True

    def evaluate(self, game: dict, remaining_budget: int) -> GuideResult:
        screen = str(_field(game, "screen_type", default="")).upper()
        character = str(_field(game, "character", "class", "player_class", default="")).upper()
        facts = self._facts(game, remaining_budget)
        result = GuideResult()
        state = _field(game, "screen_state", default={}) or {}
        offered = {str(x).lower() for x in (_field(game, "available_commands", default=[]) or [])}

        for rule in self.rules:
            if screen not in rule.scenes or character not in rule.characters:
                continue
            if not self._when_matches(rule.when, facts):
                continue
            result.matches.append(rule)
            if rule.effect == "rest" and result.command is None:
                options = _field(state, "rest_options", default=[]) or []
                idx = next((i for i, o in enumerate(options) if "rest" in str(o).lower()), None)
                if idx is not None and (not offered or "choose" in offered):
                    result.command, result.command_rule = {"command": "choose", "choice": idx}, rule
            elif rule.effect == "skip_card" and result.command is None:
                if not offered or "return" in offered:
                    result.command, result.command_rule = {"command": "return"}, rule
            elif rule.effect == "safe_map":
                from spirebrain.jev_brain.state import worst_case_damage
                nodes = _field(_field(game, "map", default={}), "next_nodes", default=[]) or []
                act = int(_field(game, "act", default=1) or 1)
                costs = [worst_case_damage(str(_field(n, "symbol", default="?")), act)
                         for n in nodes]
                # Only mask expensive nodes if at least one route is within the
                # budget. Otherwise the player must still be shown a way forward.
                if costs and any(c <= remaining_budget for c in costs):
                    result.forbidden_indices.update(i for i, c in enumerate(costs)
                                                    if c > remaining_budget)
            elif rule.effect == "avoid_lethal_event":
                options = _field(state, "options", default=[]) or []
                for i, option in enumerate(options):
                    cost = _field(option, "hp_cost", "damage", default=None)
                    if isinstance(cost, (int, float)) and cost >= facts["hp"]:
                        result.forbidden_indices.add(i)
        return result


class GuideAwareClient:
    """Adds matched guide evidence to every JEV question in this state."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.active: list[dict] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)

    def ask(self, state: Any, questions: dict) -> Any:
        if self.active:
            if isinstance(state, dict):
                state = {**state, "guide_rules": self.active}
            else:
                state = {"game_state": state, "guide_rules": self.active}
        return self.inner.ask(state, questions)
