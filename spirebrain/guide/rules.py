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

from spirebrain.redaction import redact_text

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
    """Adds matched guide evidence and approved experience as advisory context."""

    MAX_EXPERIENCES = 3
    MAX_EXPERIENCE_CONTEXT_CHARS = 4000

    @staticmethod
    def _is_replay_validated_active(experience: Any) -> bool:
        if not isinstance(experience, dict):
            return False
        validation = experience.get("replay_validation")
        if not isinstance(validation, dict):
            return False
        counts = (
            validation.get("illegal_action_count"),
            validation.get("candidate_set_change_count"),
            validation.get("hard_constraint_violation_count"),
        )
        sample_count = validation.get("sample_count")
        experience_id = str(experience.get("experience_id") or "")
        fixture_hash = str(validation.get("fixture_hash") or "")
        return (
            experience.get("status") == "active"
            and experience.get("policy_type") == "soft_preference"
            and validation.get("passed") is True
            and validation.get("fixture_type") == "fixed_provider_responses"
            and bool(experience_id)
            and validation.get("experience_id") == experience_id
            and validation.get("experience_applied") is True
            and len(fixture_hash) == 71
            and fixture_hash.startswith("sha256:")
            and all(char in "0123456789abcdef" for char in fixture_hash[7:])
            and type(sample_count) is int and sample_count > 0
            and all(type(value) is int and value == 0 for value in counts)
        )

    @classmethod
    def _experience_context(cls, experiences: Any) -> list[dict] | None:
        """Build a small prompt-only projection; never forward ledger records."""
        if not isinstance(experiences, (list, tuple)):
            return None

        items = []
        total_chars = 0
        for experience in experiences:
            if not cls._is_replay_validated_active(experience):
                continue
            character = str(experience.get("character") or "").upper()
            scenes = experience.get("scenes")
            acts = experience.get("acts")
            if character != "IRONCLAD" or not isinstance(scenes, list) or not scenes \
                    or not isinstance(acts, list) or not acts:
                continue

            def text(value: Any, limit: int) -> str:
                return redact_text(value, limit=limit).strip()

            prerequisites = experience.get("prerequisites", [])
            exceptions = experience.get("exceptions", [])
            if not isinstance(prerequisites, (list, tuple)):
                prerequisites = []
            if not isinstance(exceptions, (list, tuple)):
                exceptions = []

            # Copy only bounded, human-reviewable prose. IDs and replay proof
            # stay in the ledger; model output never becomes an executable rule.
            item = {
                "experience_id": text(experience.get("experience_id"), 80),
                "policy_type": "soft_preference",
                "priority": "optional; below current state and legal question options",
                "guidance": (
                    "Do not treat as a rule or command, or permission to select an "
                    "unavailable action. Ignore when current facts or legal options conflict."
                ),
                "title": text(experience.get("title"), 120),
                "soft_preference": text(experience.get("claim"), 500),
                "prerequisites": [text(value, 160) for value in prerequisites[:6]
                                  if isinstance(value, str) and value.strip()],
                "exceptions": [text(value, 160) for value in exceptions[:6]
                               if isinstance(value, str) and value.strip()],
                "scope": {
                    "character": "IRONCLAD",
                    "scenes": [text(value, 40).upper() for value in scenes[:6]
                               if isinstance(value, str)],
                    "acts": [value for value in acts[:3]
                             if type(value) is int and 1 <= value <= 3],
                },
            }
            if not item["experience_id"] or not item["title"] or not item["soft_preference"]:
                continue
            item_chars = sum(len(value) for value in (
                item["title"], item["soft_preference"], *item["prerequisites"],
                *item["exceptions"],
            ))
            if total_chars + item_chars > cls.MAX_EXPERIENCE_CONTEXT_CHARS:
                break
            items.append(item)
            total_chars += item_chars
            if len(items) >= cls.MAX_EXPERIENCES:
                break

        if not items:
            return None
        return items

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.active: list[dict] = []
        self.experiences: list[dict] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)

    def ask(self, state: Any, questions: dict) -> Any:
        experience_context = self._experience_context(self.experiences)
        if isinstance(state, dict):
            # The wrapper may be called repeatedly by adapters that reuse a
            # state object. Remove our prior projection before adding the
            # current one, so a revoked or out-of-scope item cannot linger.
            state = dict(state)
            state.pop("verified_experiences", None)
            if self.active:
                state["guide_rules"] = self.active
            if experience_context:
                state["verified_experiences"] = experience_context
        elif self.active or experience_context:
            state = {"game_state": state}
            if self.active:
                state["guide_rules"] = self.active
            if experience_context:
                state["verified_experiences"] = experience_context
        return self.inner.ask(state, questions)
