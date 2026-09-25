"""Runtime adapter for bounded combat search.

The searcher deals in immutable combat facts and card indexes.  This adapter
binds its result to the current legal ActionCandidate set and therefore never
creates a command on its own.  Unknown effects deliberately fall back to the
existing greedy recommendation.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from spirebrain.brain.protocol import ActionCandidate
from .combat_greedy import ActionSuggestion, recommend_action
from .combat_search import search_sequences


@dataclass(frozen=True)
class CombatProposal:
    candidate: ActionCandidate | None
    alternative: ActionCandidate | None = None
    facts: dict[str, Any] = field(default_factory=dict)
    reason_code: str = "fallback"
    reason: str = ""
    uncertainty: str = ""
    confidence: str = "unsupported"
    fallback_reason: str = ""


class CombatTacticalEngine:
    """Choose one current-step combat candidate with a bounded search."""

    def __init__(self, *, depth: int = 3, node_limit: int = 64,
                 time_limit_ms: float = 8.0) -> None:
        self.depth = max(1, min(3, int(depth)))
        self.node_limit = max(1, int(node_limit))
        self.time_limit_ms = max(0.0, float(time_limit_ms))

    @staticmethod
    def _combat(game: Mapping[str, Any]) -> Mapping[str, Any]:
        return game.get("combat") or game.get("combat_state") or game

    @staticmethod
    def _matches(candidate: ActionCandidate, command: Mapping[str, Any]) -> bool:
        return dict(candidate.command) == dict(command)

    def propose(self, game: Mapping[str, Any], candidates: Sequence[ActionCandidate]) -> CombatProposal:
        legal = [c for c in candidates if c.legal]
        if not legal:
            return CombatProposal(None, reason_code="no_legal_action",
                                  reason="当前没有可确认的合法战斗动作。",
                                  uncertainty="no_legal_action", fallback_reason="no_legal_action")
        result = search_sequences(self._combat(game), depth=self.depth,
                                  node_limit=self.node_limit,
                                  time_limit_ms=self.time_limit_ms)
        sequence = tuple(result.get("sequence") or ())
        if not sequence or result.get("confidence") != "verified":
            fallback = recommend_action(dict(game))
            if fallback is not None:
                selected = next((c for c in legal if self._matches(c, fallback.command)), None)
                if selected is not None:
                    return CombatProposal(
                        selected,
                        facts={**fallback.facts, "search": result},
                        reason_code="greedy_fallback",
                        reason=fallback.reason,
                        uncertainty=fallback.facts.get("uncertainty", result.get("uncertainty", "unknown")),
                        confidence="estimated" if fallback.uncertain else "verified",
                        fallback_reason=str(result.get("uncertainty", "search_unavailable")),
                    )
            return CombatProposal(
                next((c for c in legal if c.kind == "end"), legal[0]),
                facts={"search": result}, reason_code="rule_fallback",
                reason="战斗效果未完整解析，暂不推断精确出牌。",
                uncertainty=str(result.get("uncertainty", "unknown_effect")),
                fallback_reason=str(result.get("uncertainty", "search_unavailable")),
            )

        first_index = int(sequence[0])
        target_index = result.get("target_index")
        command = {"command": "play", "card": first_index}
        hand = self._combat(game).get("hand") or []
        card = hand[first_index] if 0 <= first_index < len(hand) else {}
        targeted = bool(card.get("has_target", str(card.get("type", "")).upper() == "ATTACK"))
        if targeted and target_index is not None:
            command["target"] = int(target_index)
        selected = next((c for c in legal if self._matches(c, command)), None)
        if selected is None:
            # The search saw a card which the live legality layer no longer
            # accepts. Reconcile against current candidates instead of using a
            # stale index.
            return CombatProposal(
                next((c for c in legal if c.kind == "end"), legal[0]),
                facts={"search": result}, reason_code="candidate_invalidated",
                reason="搜索结果对应的目标或手牌已变化，已重新等待当前状态。",
                uncertainty="stale_candidate", fallback_reason="candidate_invalidated")

        facts = {}
        if result.get("facts"):
            facts = result["facts"][0].__dict__.copy()
        facts.update({k: result[k] for k in (
            "damage", "block", "lethal_confirmed", "target_index", "nodes",
            "budget_exhausted") if k in result})
        reason = ("确认击杀来袭敌人，优先消除本回合威胁。"
                  if result.get("lethal_confirmed") else
                  "当前出牌顺序可覆盖已知来袭伤害。"
                  if result.get("block", 0) else
                  "按已验证的伤害与能量收益选择当前一步。")
        alternative = next((c for c in legal if c.candidate_id != selected.candidate_id), None)
        return CombatProposal(selected, alternative, facts=facts,
                              reason_code="bounded_search", reason=reason,
                              confidence="verified", uncertainty="")

    def advise(self, game: Mapping[str, Any], candidates: Sequence[ActionCandidate]) -> ActionSuggestion | None:
        """Compatibility helper for callers that still consume ActionSuggestion."""
        proposal = self.propose(game, candidates)
        if proposal.candidate is None:
            return None
        return ActionSuggestion(command=dict(proposal.candidate.command),
                                reason=proposal.reason, uncertain=bool(proposal.uncertainty),
                                facts=proposal.facts)
