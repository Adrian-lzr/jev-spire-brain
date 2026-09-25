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
import time
from pathlib import Path

from spirebrain.jev_brain.client import ChoiceSpec, NOUL_UNCERTAIN_BAND, get_client
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
from spirebrain.brain.action_broker import build_action_candidates, potion_purchase_allowed
from spirebrain.brain.orchestrator import StrategicOrchestrator
from spirebrain.brain.protocol import (
    DecisionContext,
    DecisionProposal,
    FinalDecision,
    RunSession,
)
from spirebrain.brain.planner import stable_state_id
from spirebrain.guide.rules import GuideAwareClient, GuideBook, GuideResult
from spirebrain.overlay.feed import DecisionFeed, decision_event, run_state_event
from spirebrain.tactical.combat_greedy import Card, CombatState, play_order, recommend_action
from spirebrain.tactical.hp_budget import HPBudget
from spirebrain.driver.legality import check_action
from spirebrain.runtime_config import resolve_runtime_config
from spirebrain.runtime_budget import DecisionBudget, use_budget
from spirebrain.driver.live_state import GameSnapshot
from spirebrain.driver.scenes import SceneRouter
from spirebrain.driver.trace import DecisionTrace

ROOT = Path(__file__).resolve().parents[2]

MAP_SCREEN = "MAP"
CARD_REWARD_SCREEN = "CARD_REWARD"
EVENT_SCREEN = "EVENT"
REST_SCREEN = "REST"
SHOP_SCREEN = "SHOP_SCREEN"
SHOP = "SHOP"
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


def _as_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
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

    def __init__(self, jev_backend: str | None = None, strategy_path: str | Path | None = None,
                 log_dir: str | Path | None = None,
                 acceptance: str | None = None,
                 feed: DecisionFeed | None = None,
                 brain_backend: str | None = None,
                 brain_client=None, async_planning: bool | None = None) -> None:
        self._constructor = {"jev_backend": jev_backend, "strategy_path": strategy_path,
                              "log_dir": log_dir, "acceptance": acceptance,
                              "brain_backend": brain_backend, "brain_client": brain_client,
                              "async_planning": async_planning}
        strategy_file = Path(strategy_path) if strategy_path else ROOT / "config" / "strategy.json"
        self.strategy = json.loads(strategy_file.read_text(encoding="utf-8"))
        self.runtime_config = resolve_runtime_config(
            ROOT, cli={"backend": jev_backend, "brain_backend": brain_backend},
            strategy=self.strategy)
        jev_backend = self.runtime_config.jev_backend
        brain_backend = self.runtime_config.brain_backend
        self._constructor.update({"jev_backend": jev_backend, "brain_backend": brain_backend})
        self.goal = self.strategy.get("goal", "")
        # "margin" | "argmax" — the Score gate, see decisions.evaluate_score. The
        # default is the one the pre-registered comparison in docs/MEASUREMENTS.md
        # selected; the strategy file carries it so it stays a human choice.
        self.acceptance = acceptance or self.runtime_config.score_acceptance

        jev_kwargs = {}
        if jev_backend == "openrouter":
            jev_kwargs.update(total_budget_ms=self.runtime_config.jev_budget_ms,
                              max_retries=self.runtime_config.jev_max_retries)
        elif jev_backend == "official":
            jev_kwargs.update(total_budget_ms=self.runtime_config.jev_budget_ms,
                              max_retries=self.runtime_config.jev_max_retries)
        client = get_client(jev_backend, **jev_kwargs)
        self.guide = GuideBook()
        logged_client = LoggingJevClient(
            client, log_dir=log_dir or ROOT / self.strategy.get("jev", {}).get("log_dir", "logs"))
        self._guide_client = GuideAwareClient(logged_client)
        self.jev = self._guide_client
        self.guide_result = GuideResult()
        self.hp: HPBudget | None = None
        self.run: RunContext | None = None
        brain_cfg = self.strategy.get("brain", {}) or {}
        resolved_log_dir = Path(log_dir) if log_dir else ROOT / brain_cfg.get("log_dir", "logs")
        self.strategic = StrategicOrchestrator(
            backend=brain_backend,
            model=self.runtime_config.openai_model,
            endpoint=self.runtime_config.openai_endpoint,
            timeout_ms=self.runtime_config.timeout_ms,
            max_plan_steps=self.runtime_config.max_plan_steps,
            memory_events=self.runtime_config.memory_events,
            max_output_tokens=self.runtime_config.max_output_tokens,
            client=brain_client,
            log_dir=resolved_log_dir,
            async_planning=async_planning,
        )
        self.trace = DecisionTrace(resolved_log_dir / "decision_trace.jsonl",
                                   config_id=self.runtime_config.config_id)
        # Provider events share the same decision trace as advice and outcomes.
        logged_client.event_sink = self.trace.record
        try:
            self.strategic.provider.event_sink = self.trace.record
        except AttributeError:
            pass
        self._active_game: dict | None = None
        self._observed_state_id = ""
        self._active_candidates = []
        self._active_plan = None
        self._active_brain_response = None
        self._candidate_diagnostics: list[dict] = []
        self.snapshot: GameSnapshot | None = None
        self.scene_router = SceneRouter({
            MAP_SCREEN: self._on_map,
            CARD_REWARD_SCREEN: self._on_card_reward,
            EVENT_SCREEN: self._on_event,
            REST_SCREEN: self._on_rest,
            SHOP_SCREEN: self._on_shop,
            SHOP: self._on_shop,
            BOSS_REWARD_SCREEN: self._on_boss_reward,
            COMBAT_SCREEN: self._on_combat,
        }, GRID_SCREENS)
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
        #   - a shop state is asked once per unchanged shelf/gold snapshot;
        #     a real purchase changes the signature and is replanned.
        self._shop_decided_on_floor: int | None = None
        self._shop_state_signature: tuple | None = None
        self.history: list[dict] = []
        self.max_history = 2000
        self._decision_sequence = 0
        self._decision_started_at = 0.0
        self._active_budget: DecisionBudget | None = None
        self._traced_requests: set[str] = set()
        self.run_session = RunSession(mode="advise")
        self._last_observed_terminal = False
        self._advice_revisions: dict[str, int] = {}
        self._last_final_decision: FinalDecision | None = None
        # Phase 1.5, the interaction layer: an optional live feed of everything
        # the brain is thinking. None (the default) changes nothing — the feed
        # is an observability side-channel, never a dependency.
        self.feed = feed

    def _append_history(self, entry: dict) -> None:
        """Keep in-process replay memory bounded during long game sessions."""
        self.history.append(entry)
        if len(self.history) > self.max_history:
            del self.history[:len(self.history) - self.max_history]

    def clone_for_advice(self) -> "SpireBrainAgent":
        """A private router for the background model worker, with no live feed.

        The polling thread owns the panel and player tracker. This clone keeps
        the model's mutable run/grid state off that thread and prevents a late
        answer from publishing an old HP snapshot into the game's panel.
        """
        constructor = dict(self._constructor)
        # AdviseSession already owns a worker thread around choose_action.  Its
        # clone should synchronously finish the strategic call inside that worker
        # so the completed recommendation includes the GPT plan rather than an
        # early local fallback with no later consumer.
        constructor["async_planning"] = False
        return SpireBrainAgent(**constructor, feed=None)

    def begin_new_run(self, run_id: str = "", *, seed: str = "") -> None:
        """Reset all live-run state before the first state of a new run.

        CommunicationMod can move from its menu straight into a new run without
        sending a terminal game payload. The transport calls this boundary so a
        reused seed/run id cannot inherit a plan, pending grid choice or worker
        generation from the previous run.
        """
        self.run_session.reset(run_id, seed=seed)
        self._last_observed_terminal = False
        self._advice_revisions.clear()
        self._shop_decided_on_floor = None
        self._shop_state_signature = None
        self._pending_upgrade = None
        self._grid_picked = None
        self._active_game = None
        self._active_candidates = []
        self._active_plan = None
        self._active_brain_response = None
        self.hp = None
        try:
            self.strategic.reset(run_id=run_id)
        except Exception:
            pass

    # Explicit alias for integrations that name the boundary after its effect.
    reset_run_state = begin_new_run

    def quick_advice(self, game: dict) -> dict:
        """Immediate, model-free advice or a truthful waiting/coverage state."""
        character = str(_get(game, "character", "class", default="")).upper()
        if character and character != "IRONCLAD":
            return {"status": "unsupported", "label": "当前角色攻略尚未覆盖",
                    "reason": "首版攻略只验证了铁甲战士；不会套用铁甲战士专属规则。",
                    "source_type": "unavailable"}
        self.observe(game)
        # A provisional answer must not display the previous state's strategic
        # goal while the background planner is still running.
        # A valid short plan intentionally spans ordinary combat card steps, so
        # its state-bound request ID will differ from the latest combat snapshot.
        # Ask the orchestrator whether the plan is still usable instead of
        # clearing the strategic goal on every card animation.
        strategic_detail = self.strategic.detail_for(game)
        guide = self.guide.evaluate(game, self._budget().remaining_budget)
        if guide.command is not None and guide.command_rule is not None:
            rule = guide.command_rule
            return {"status": "ready", "command": guide.command,
                    "reason": rule.reason, "source_type": "guide_rule",
                    "source": rule.source, "guide_rules": guide.evidence(),
                    **strategic_detail}
        if str(_get(game, "screen_type", default="")).upper() == COMBAT_SCREEN:
            suggestion = recommend_action(game)
            if suggestion is not None:
                return {"status": "ready", "command": suggestion.command,
                        "reason": suggestion.reason, "source_type": "rule_fallback",
                        "source": "游戏实时战斗状态；本地战术规则",
                        "combat_facts": dict(suggestion.facts),
                        "selection_basis": suggestion.facts.get("decision_basis", "local_tactical_rule"),
                        "uncertain": bool(suggestion.uncertain),
                        **strategic_detail,
                        "guide_rules": guide.evidence()}
        return {"status": "thinking", "label": "正在分析当前局面…",
                "reason": "结合攻略规则与 JEV 判断，稍后显示当前一步。",
                "source_type": "pending", "guide_rules": guide.evidence(),
                **strategic_detail}

    # -- lifecycle --------------------------------------------------------- #
    def observe(self, game) -> None:
        """Sync our model of the world (HP budget + run context) from the game."""
        snapshot = GameSnapshot.from_communication(game)
        game = snapshot.payload
        self.snapshot = snapshot
        self._observed_state_id = stable_state_id(game)
        run_id = str(_get(game, "run_id", "runId", default="") or "")
        seed = str(_get(game, "seed", default="") or "")
        screen_name = str(_get(game, "screen_type", "screen", default="")).upper()
        terminal_screen = screen_name in {"MENU", "DEATH", "VICTORY", "GAME_OVER"}
        if terminal_screen:
            if not self._last_observed_terminal:
                self.run_session.reset(run_id, seed=seed)
            self._advice_revisions.clear()
        elif self._last_observed_terminal:
            # A new playable screen after MENU/DEATH is a new run even when
            # CommunicationMod reuses the same seed/run_id.
            self.run_session.reset(run_id, seed=seed)
        elif run_id:
            self.run_session.observe(run_id, seed=seed)
        act = max(1, _as_int(_get(game, "act", default=1), 1))
        max_hp = max(1, _as_int(_get(game, "max_hp", default=80), 80))
        current_hp = max(0, _as_int(_get(game, "current_hp", "hp", default=max_hp), max_hp))
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
            floor=_as_int(_get(game, "floor", "floor_num", default=0)),
            character=str(_get(game, "character", "class", "player_class", default="")).upper(),
            hp=current_hp,
            max_hp=max_hp,
            gold=_as_int(_get(game, "gold", default=0)),
            deck=deck,
            relics=[str(r if isinstance(r, str) else _get(r, "name", "relic_id", default=""))
                    for r in (_get(game, "relics", default=[]) or [])],
            potions=[str(p if isinstance(p, str) else _get(p, "name", "potion_id", default=""))
                     for p in (_get(game, "potions", default=[]) or [])],
            goal=self.goal,
        ).with_budget(self.hp)

        # Keep the strategic memory in sync with the same state used by JEV.
        # This is bounded and process-local; it never writes a credential or a
        # full transcript to disk.
        try:
            if self._last_observed_terminal and not terminal_screen:
                self.strategic.reset(run_id=run_id or self.run_session.run_id)
            previous_run = self.strategic.memory.run_id
            self.strategic.memory.observe(game, state_id=self._observed_state_id)
            run_changed = bool(previous_run and self.strategic.memory.run_id != previous_run)
            if not self.run_session.run_id:
                self.run_session.observe(self.strategic.memory.run_id, seed=seed)
            elif run_changed and self.run_session.run_id != self.strategic.memory.run_id:
                self.run_session.observe(self.strategic.memory.run_id, seed=seed)
            if run_changed or terminal_screen:
                self._shop_decided_on_floor = None
                self._shop_state_signature = None
                self._pending_upgrade = None
                self._grid_picked = None
            reset_strategy = (run_changed
                              or (not terminal_screen and not self.strategic.memory.events)
                              or (terminal_screen and not self._last_observed_terminal))
            if reset_strategy:
                self.strategic.reset(run_id=self.run_session.run_id
                                     if terminal_screen else self.strategic.memory.run_id)
                if not terminal_screen:
                    self.strategic.memory.observe(game, state_id=self._observed_state_id)
                else:
                    self.strategic.memory.mark_terminal(self.run_session.run_id)
            if screen_name in {"DEATH", "VICTORY", "GAME_OVER"} and not self._last_observed_terminal:
                self.record_observed_result(
                    screen_name.lower(), state_id=self._observed_state_id,
                    evidence={"run_id": self.run_session.run_id, "seed": seed},
                )
        except Exception:  # noqa: BLE001 - memory is optional observability
            pass

        self._last_observed_terminal = terminal_screen
        self._publish_state()

    def _publish_state(self) -> None:
        """Push a run snapshot to the feed, if anyone is watching."""
        if self.feed is None:
            return
        try:
            self.feed.publish("run_state", run_state_event(self))
        except Exception:  # noqa: BLE001 - the dashboard must not break the run
            pass

    # -- unified main-loop event boundary --------------------------------- #
    def record_advice_history(self, advice, *, displayed: bool = True) -> None:
        """Commit a recommendation after it has been generated/published."""
        try:
            self.strategic.memory.record_advice(advice, displayed=displayed)
        except Exception:  # observability must not break the game loop
            pass

    def record_execution_attempt(self, command: dict, *, state_id: str = "",
                                 decision_id: str = "", request_id: str = "",
                                 plan_id: str = "", status: str = "sent",
                                 reason: str = "") -> None:
        """Commit a command that actually reached the CommunicationMod wire."""
        try:
            event = self.strategic.memory.record_execution_attempt(
                command, state_id=state_id or self._observed_state_id,
                decision_id=decision_id, request_id=request_id, plan_id=plan_id,
                status=status, reason=reason,
            )
            self.trace.record("execution_attempt", {
                "record_kind": "execution_attempt",
                "run_id": self.run_session.run_id,
                "run_epoch": self.run_session.run_epoch,
                "state_id": event.get("state_id", ""),
                "decision_id": event.get("decision_id", "") or None,
                "request_id": event.get("request_id", "") or None,
                "plan_id": event.get("plan_id", "") or None,
                "command": command, "status": status, "reason": reason,
            })
        except Exception:
            pass

    def record_player_feedback(self, outcome, *, observed_state: dict | None = None) -> None:
        """Commit tracker feedback on the owning thread, then replan if needed."""
        try:
            advice = outcome.advice
            actual = outcome.acted_key or outcome.acted_label or "unknown"
            # Feedback belongs to the run that produced the advice. The
            # tracker normally clears this on a run edge, but the identity
            # check is the final guard for delayed callbacks.
            if (advice.run_id and self.run_session.run_id
                    and advice.run_id != self.run_session.run_id):
                return
            if int(advice.run_epoch or 0) != int(self.run_session.run_epoch):
                return
            action = {
                "kind": outcome.verdict,
                "key": list(outcome.acted_key or ()) if outcome.acted_key else None,
                "label": outcome.acted_label,
                "verdict": outcome.verdict,
            }
            memory = self.strategic.memory
            if outcome.verdict == "unobserved":
                event = memory.record_unobserved(
                    state_id=advice.state_id, decision_id=advice.decision_id,
                    evidence=outcome.evidence,
                    reason="无法从连续状态确定玩家行动",
                    observed_state=observed_state,
                )
                trace_kind = "unobserved"
            else:
                event = memory.record_observed_action(
                    action, state_id=advice.state_id,
                    decision_id=advice.decision_id, evidence=outcome.evidence,
                    observed_state=observed_state,
                )
                trace_kind = "fact"
                if outcome.verdict == "mismatch":
                    event = memory.record_deviation(
                        advice.candidate_id or advice.label, str(actual),
                        state_id=advice.state_id, decision_id=advice.decision_id,
                        evidence=outcome.evidence, observed_state=observed_state,
                    )
                    trace_kind = "deviation"
                    # This invalidates only pending strategic work. The player
                    # action itself remains a fact in memory.
                    self.strategic.request_replan("player_deviation")
            self.trace.record(trace_kind, {
                "record_kind": event.get("kind", ""),
                "run_id": event.get("run_id", self.run_session.run_id),
                "run_epoch": event.get("run_epoch", self.run_session.run_epoch),
                "state_id": event.get("state_id", advice.state_id),
                "decision_id": event.get("decision_id", advice.decision_id),
                "player_action": action,
                "evidence": outcome.evidence,
                "verdict": outcome.verdict,
            })
        except Exception:
            pass

    def record_observed_result(self, result, *, state_id: str = "",
                               decision_id: str = "", evidence: dict | None = None) -> None:
        try:
            event = self.strategic.memory.record_observed_result(
                result, state_id=state_id or self._observed_state_id,
                decision_id=decision_id, evidence=evidence,
            )
            self.trace.record("result", {
                "record_kind": "result_record",
                "run_id": event.get("run_id", self.run_session.run_id),
                "run_epoch": event.get("run_epoch", self.run_session.run_epoch),
                "state_id": event.get("state_id", ""),
                "decision_id": event.get("decision_id", "") or None,
                "result": result, "evidence": evidence or {},
            })
        except Exception:
            pass

    def choose_action(self, game) -> dict:
        """Return ONE CommunicationMod command for the current screen."""
        self._decision_sequence += 1
        self._decision_started_at = time.monotonic()
        # One budget covers the strategic request and all tactical provider
        # calls made while resolving this decision.  The object is shared with
        # an async strategic worker; late results are still rejected by the
        # existing state/generation checks.
        self._active_budget = DecisionBudget(
            total_ms=max(250, int(self.runtime_config.decision_budget_ms)),
            max_calls=int(self.runtime_config.max_model_calls))
        self.snapshot = GameSnapshot.from_communication(game)
        game = self.snapshot.payload
        self.observe(game)
        self.guide_result = self.guide.evaluate(game, self._budget().remaining_budget)
        self._guide_client.active = self.guide_result.evidence()
        character = str(_get(game, "character", "class", "player_class", default="")).upper()
        screen = str(_get(game, "screen_type", "screen", default="")).upper()
        self._active_game = game
        if character and character != "IRONCLAD":
            self._active_candidates = []
            self._active_plan = None
            self._active_brain_response = None
            return {"command": "state", "reason_source": "unsupported_character",
                    "reason": "当前角色尚未覆盖，暂不套用铁甲战士攻略。"}
        self._candidate_diagnostics = []
        self._active_candidates = build_action_candidates(
            game, forbidden_indices=set(self.guide_result.forbidden_indices),
            diagnostics=self._candidate_diagnostics)
        self._active_plan = None
        self._active_brain_response = None
        if self.guide_result.command is not None:
            rule = self.guide_result.command_rule
            assert rule is not None
            decision = Decision(
                {"REST": "rest", "CARD_REWARD": "card_reward"}.get(screen, screen.lower()),
                "rest" if screen == "REST" else "skip", 0.0, True,
                {"reason": rule.reason, "guide_rules": self.guide_result.evidence(),
                 "source_type": "guide_rule", "rule_id": rule.id, "source": rule.source},
            )
            return self._finalize_and_record(decision, dict(self.guide_result.command))
        handler = self.scene_router.resolve(screen)
        if handler is None and self.scene_router.is_grid(screen):
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
        try:
            trigger = "combat_start" if screen == COMBAT_SCREEN and self.strategic.last_screen != COMBAT_SCREEN else None
            with use_budget(self._active_budget):
                self._active_plan, self._active_brain_response = self.strategic.plan_for(
                    game,
                    candidates=self._active_candidates,
                    guide_rules=self.guide_result.evidence(),
                    trigger=trigger,
                    budget=self._active_budget,
                )
        except Exception as exc:  # strategic planning is a non-fatal side channel
            self._active_brain_response = None
            self._append_history({"point": screen.lower(), "fallback": True,
                                 "detail": {"source_type": "unavailable",
                                            "reason": f"战略大脑异常：{type(exc).__name__}"}})
        with use_budget(self._active_budget):
            return handler(game)

    # -- shared helpers ---------------------------------------------------- #
    def _finalize_and_record(self, decision, command: dict) -> dict:
        detail = dict(decision.detail)
        # Confidence is layered evidence, not one made-up probability.  Keep
        # provider confidence, local tactical confidence and the final basis
        # separate all the way to the trace and overlay.
        raw_model_value = detail.get("raw_model_confidence", detail.get("model_confidence"))
        try:
            raw_model_confidence = (float(raw_model_value)
                                    if raw_model_value is not None else None)
        except (TypeError, ValueError):
            raw_model_confidence = None
        local_value = detail.get("local_confidence")
        if local_value is None and not (decision.used_fallback and
                                        float(decision.confidence or 0.0) == 0.0):
            local_value = decision.confidence
        try:
            local_confidence = float(local_value) if local_value is not None else None
        except (TypeError, ValueError):
            local_confidence = None
        detail["raw_model_confidence"] = raw_model_confidence
        detail["local_confidence"] = local_confidence
        detail.setdefault("selection_basis", detail.get("source_type", "rule_fallback"))
        # A model may only select an already enumerated, legal candidate.  The
        # hard guide path and risk-posture probe are intentionally excluded.
        if (self._active_game is not None
                and self._active_candidates
                and str(command.get("command", "")).lower() not in
                    {"(posture only)", "confirm", "cancel"}
                and detail.get("source_type") != "guide_rule"):
            try:
                original_command = dict(command)
                command, execution = self.strategic.choose(
                    self._active_game,
                    command,
                    candidates=self._active_candidates,
                    reason=str(detail.get("reason", "") or ""),
                    jev_confidence=float(decision.confidence or 0.0),
                    guide_rules=self.guide_result.evidence(),
                )
                execution_detail = execution.detail()
                detail.update(execution_detail)
                detail.update(self.strategic.current_detail())
                detail["source_type"] = execution.source_type
                if execution.reason:
                    detail["reason"] = execution.reason
                if execution.source_type == "gpt_strategy":
                    detail["source"] = "GPT 战略计划；合法候选执行层"
                    detail["strategy_overrode"] = command != original_command
                elif execution.source_type == "jev_tactical":
                    detail.setdefault("source", "JEV 局部判断；GPT 战略约束")
                decision.detail = detail
            except Exception as exc:  # noqa: BLE001 - preserve the old decision
                detail.setdefault("source_type", "rule_fallback")
                detail.setdefault("reason", f"战略层未接管：{type(exc).__name__}")
        # The broker should already have filtered the command, but this final
        # check is intentionally redundant: guide rules, stale plans, and
        # legacy handlers must never put an illegal index or target on the wire.
        verb = str(command.get("command", "")).lower()
        legal_result = None
        legality_reason = ""
        if (self._active_game is not None and verb not in {"state", "wait", "(posture only)"}):
            legal, why_not = check_action(self._active_game, command)
            legal_result = bool(legal)
            legality_reason = why_not if not legal else ""
            if not legal:
                detail["source_type"] = "rule_constraint"
                detail["selection_basis"] = "hard_legality_constraint"
                detail["constraint_reason"] = why_not
                detail["reason"] = f"硬规则拦截当前动作：{why_not}"
                command = {"command": "state", "reason_source": "rule_constraint",
                           "reason": why_not}
        detail.setdefault("guide_rules", self.guide_result.evidence())
        detail.setdefault("source_type", "rule_fallback" if decision.used_fallback else "jev")
        # The broker may have replaced the proposal and its metadata.  Re-read
        # the layers after arbitration so a GPT/JEV override cannot retain the
        # old rule source or confidence by accident.
        raw_model_value = detail.get("raw_model_confidence", detail.get("model_confidence"))
        try:
            raw_model_confidence = (float(raw_model_value)
                                    if raw_model_value is not None else None)
        except (TypeError, ValueError):
            raw_model_confidence = None
        local_value = detail.get("local_confidence")
        if local_value is None and not (decision.used_fallback and
                                        float(decision.confidence or 0.0) == 0.0):
            local_value = decision.confidence
        try:
            local_confidence = float(local_value) if local_value is not None else None
        except (TypeError, ValueError):
            local_confidence = None
        detail["raw_model_confidence"] = raw_model_confidence
        detail["local_confidence"] = local_confidence
        detail.setdefault("selection_basis", detail.get("source_type", "rule_fallback"))
        decision.detail = detail
        state_for_trace = self.snapshot.state_id if self.snapshot else self._observed_state_id
        run_id = self.run_session.run_id or self.strategic.memory.run_id
        self.run_session.observe(run_id)
        decision_id = f"{run_id}:{state_for_trace[:16]}:{self._decision_sequence}"
        revision = self._advice_revisions.get(state_for_trace, 0) + 1
        self._advice_revisions[state_for_trace] = revision
        context = DecisionContext(
            run=self.run_session, state_id=state_for_trace, decision_id=decision_id,
            config_id=self.runtime_config.config_id, generation=self._decision_sequence,
            advice_revision=revision, state=dict(self._active_game or {}),
        )
        proposal = DecisionProposal(
            point=decision.point, command=dict(command), source_type=detail.get("source_type", ""),
            reason=str(detail.get("reason", "") or ""),
            rule_ids=[str(rule.get("id")) for rule in (detail.get("guide_rules") or [])
                      if isinstance(rule, dict) and rule.get("id")],
            model_confidence=raw_model_confidence,
            jev_confidence=(float(detail.get("jev_confidence"))
                           if detail.get("jev_confidence") is not None else None),
            raw_model_confidence=raw_model_confidence,
            local_confidence=local_confidence,
            selection_basis=str(detail.get("selection_basis", "") or ""),
            uncertain=bool(detail.get("uncertain", False)), detail=dict(detail),
        )
        candidate_id = str(detail.get("candidate_id", "") or "")
        candidate = next((c for c in self._active_candidates if c.candidate_id == candidate_id), None)
        alternative = None
        alternative_id = str(detail.get("alternative_candidate_id", "") or "")
        if alternative_id:
            alternative = next((c for c in self._active_candidates if c.candidate_id == alternative_id), None)
        final = FinalDecision(
            context=context, command=dict(command), proposal=proposal,
            source_type=str(detail.get("source_type", "rule_fallback")),
            reason=str(detail.get("reason", "") or ""), candidate=candidate,
            alternative_candidate=alternative, constraint_reason=str(detail.get("constraint_reason", "") or ""),
            request_id=str(detail.get("brain_request_id", "") or ""),
            plan_id=str(detail.get("plan_id", "") or ""), uncertain=bool(detail.get("uncertain", False)),
            raw_model_confidence=raw_model_confidence,
            local_confidence=local_confidence,
            selection_basis=str(detail.get("selection_basis", "") or ""),
            legal=legal_result, legality_reason=legality_reason, legacy_decision=decision,
            detail=detail,
        )
        return self._record(final)

    def _record(self, final: FinalDecision) -> dict:
        """Record-only boundary: the command and metadata are already final."""
        detail = dict(final.detail)
        decision = final.legacy_decision
        command = dict(final.command)
        request_id = final.request_id
        if request_id and request_id not in self._traced_requests:
            self._traced_requests.add(request_id)
            if len(self._traced_requests) > 4096:
                # Request IDs are only a per-process deduplication aid. Keep a
                # bounded window so a long run cannot turn this set into a leak.
                self._traced_requests = set(list(self._traced_requests)[-2048:])
            response = self._active_brain_response
            usage = getattr(response, "usage", {}) or {}
            self.trace.record("provider_request", {
                "run_id": final.run_id, "state_id": final.state_id,
                "decision_id": final.decision_id, "request_id": request_id,
                "plan_id": final.plan_id, "brain_backend": self.strategic.backend_name,
                "request_latency_ms": (int(detail["brain_latency_ms"])
                                        if detail.get("brain_latency_ms") is not None else None),
                "cost_usd": usage.get("cost_usd", usage.get("cost")),
                "fallback": bool(detail.get("brain_error")),
                "fallback_reason": detail.get("brain_error", "") or None,
                "error_kind": getattr(response, "error_kind", "") or None,
                "budget": self._active_budget.snapshot() if self._active_budget else None,
                "timeout": "timeout" in str(detail.get("brain_error", "")).lower(),
            })
        if decision is not None:
            self._append_history({
                "point": decision.point, "value": decision.value,
                "confidence": round(float(decision.confidence), 4),
                "fallback": bool(decision.used_fallback), "detail": detail, "command": command,
                "decision_id": final.decision_id, "state_id": final.state_id,
                "run_id": final.run_id, "run_epoch": final.context.run_epoch,
                "advice_revision": final.advice_revision, "source_type": final.source_type,
            })
        if self.feed is not None and decision is not None:
            try:
                self.feed.publish("decision", {**decision_event(decision, command),
                                                "decision_id": final.decision_id,
                                                "state_id": final.state_id,
                                                "run_id": final.run_id,
                                                "run_epoch": final.context.run_epoch,
                                                "advice_revision": final.advice_revision,
                                                "source_type": final.source_type,
                                                "uncertain": final.uncertain})
            except Exception:  # noqa: BLE001
                pass
        self.trace.record("state_observed", {
            "run_id": final.run_id, "run_epoch": final.context.run_epoch,
            "state_id": final.state_id, "decision_id": final.decision_id,
            "request_id": request_id or None, "plan_id": final.plan_id,
            "screen": str(_get(self._active_game, "screen_type", default="")).upper(),
            "act": _as_int(_get(self._active_game, "act", default=0)),
            "floor": _as_int(_get(self._active_game, "floor", "floor_num", default=0)),
        })
        self.trace.record("decision", {
            "run_id": final.run_id, "state_id": final.state_id,
            "decision_id": final.decision_id, "request_id": request_id or None,
            "plan_id": final.plan_id, "screen": str(_get(self._active_game, "screen_type", default="")).upper(),
            "point": decision.point if decision is not None else final.proposal.point,
            "brain_backend": self.strategic.backend_name,
            "jev_backend": str(getattr(self.jev, "backend_name", "unknown")),
            "source_type": final.source_type, "rule_ids": final.proposal.rule_ids,
            "candidates": [candidate.to_dict() for candidate in self._active_candidates],
            "filtered_candidates": self._candidate_diagnostics,
            "selected_candidate_id": final.candidate.candidate_id if final.candidate else "",
            "candidate_signature": final.candidate.candidate_signature if final.candidate else None,
            "command": command, "reason": final.reason,
            "confidence": final.proposal.model_confidence,
            "raw_model_confidence": final.raw_model_confidence,
            "local_confidence": final.local_confidence,
            "jev_confidence": final.proposal.jev_confidence,
            "selection_basis": final.selection_basis,
            "latency_ms": (int(detail["brain_latency_ms"])
                           if detail.get("brain_latency_ms") is not None else None),
            "request_latency_ms": (int(detail["brain_latency_ms"])
                                   if detail.get("brain_latency_ms") is not None else None),
            "decision_latency_ms": int((time.monotonic() - self._decision_started_at) * 1000)
            if self._decision_started_at else None,
            "fallback": bool((decision and decision.used_fallback) or detail.get("fallback")),
            "fallback_reason": detail.get("brain_error") or final.constraint_reason or None,
            "legal": final.legal, "legality_reason": final.legality_reason or None,
            "uncertain": final.uncertain,
            "act": _as_int(_get(self._active_game, "act", default=0)),
            "floor": _as_int(_get(self._active_game, "floor", "floor_num", default=0)),
        })
        if request_id:
            response = self._active_brain_response
            self.trace.record("provider_response", {
                "run_id": final.run_id, "run_epoch": final.context.run_epoch,
                "state_id": final.state_id, "decision_id": final.decision_id,
                "request_id": request_id, "plan_id": final.plan_id,
                "provider": getattr(response, "backend", self.strategic.backend_name),
                "model": getattr(response, "model", ""),
                "latency_ms": getattr(response, "latency_ms", None),
                "usage": getattr(response, "usage", {}) or {},
                "error": getattr(response, "error", "") or None,
                "error_kind": getattr(response, "error_kind", "") or None,
                "fallback": bool(getattr(response, "fallback", False)),
            })
        self._last_final_decision = final
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
        return self._finalize_and_record(d, {"command": "choose", "choice": choice})

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
            return self._finalize_and_record(d, {"command": "return"})
        return self._finalize_and_record(d, {"command": "choose", "choice": index.get(str(d.value), 0)})

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
        return self._finalize_and_record(d, {"command": "choose", "choice": index.get(str(d.value), 0)})

    # -- 4. rest site ------------------------------------------------------ #
    def _on_rest(self, game) -> dict:
        """Rest sites are a CHOOSE, not a `rest`/`smith` verb (there are neither).

        The protocol takes one command per screen, so an upgrade decision spans
        two states: this answers the rest-site screen, and `_on_grid` answers the
        card grid that follows using the intent stashed in `_pending_upgrade`.
        """
        screen = _get(game, "screen_state", "screen", default=game)
        options = [str(o) for o in (_get(screen, "rest_options", default=[]) or [])]
        if not options:
            return {"command": "state", "reason_source": "insufficient_state",
                    "reason": "篝火选项尚未同步，等待游戏状态更新。"}
        rest_i = next((i for i, o in enumerate(options) if "rest" in o.lower()), 0)
        smith_i = next((i for i, o in enumerate(options)
                        if "smith" in o.lower() or "upgrade" in o.lower()), None)
        if smith_i is None:
            # Nothing to decide: JEV is not consulted when there is no choice.
            d = RestSiteDecider(self.jev, self._budget(), self.goal,
                                run=self.run).decide(hp_ratio=self._hp_ratio(), upgradable={})
            self._pending_upgrade = None
            return self._finalize_and_record(d, {"command": "choose", "choice": rest_i})

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
                return self._finalize_and_record(d, {"command": "choose", "choice": rest_i})
            # An upgrade was chosen ON THIS SCREEN (model or deck): the command
            # is the smith slot, and the following grid gets the card's index.
            # Saying "upgrade Whirlwind" while choosing rest would be exactly
            # the kind of advice/command split a player cannot trust.
            _, index = _unique_labels(labels)
            self._pending_upgrade = index.get(str(d.value), 0)
            return self._finalize_and_record(d, {"command": "choose", "choice": smith_i})

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
        return self._finalize_and_record(d, {"command": "choose", "choice": smith_i})

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
            return self._finalize_and_record(
                Decision("grid", "confirm", 0.0, True,
                         {"reason": "已选定卡牌，确认当前选择。", "source_type": "rule_fallback"}),
                {"command": "confirm"})
        if confirm_offered:
            # A confirm-only grid we did not pick (opened by the player?).
            # Confirm is still the only way through; keep it honest in history.
            confirmed = self._finalize_and_record(
                Decision("grid", "confirm", 0.0, True,
                         {"reason": "当前界面只提供确认，继续完成选择。",
                          "source_type": "rule_fallback"}),
                {"command": "confirm"})
            confirmed.setdefault("reason", "confirm offered with no pending pick; confirming to advance")
            return confirmed

        cards = _get(_get(game, "screen_state", "screen", default={}), "cards", default=[]) or []
        if not cards:
            return {"command": "state", "reason_source": "insufficient_state",
                    "reason": "选卡列表尚未同步，等待游戏状态更新。"}

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
            picked = self._finalize_and_record(
                Decision("grid", choice, 0.0, True,
                         {"reason": reason, "source_type": "rule_fallback",
                          "local_removal": detail_note} if detail_note else
                         {"reason": reason, "source_type": "rule_fallback"}),
                out)
            if picked.get("command") == "choose" and picked.get("choice") == choice:
                picked.setdefault("reason", reason)
                if detail_note:
                    picked.setdefault("local_removal", detail_note)
            self._grid_picked = picked.get("choice") if picked.get("command") == "choose" else None
            return picked
        self._grid_picked = choice
        out = {"command": "choose", "choice": choice}
        if reason:
            out["reason"] = reason
        picked = self._finalize_and_record(
            Decision("grid", choice, 0.0, True,
                     {"reason": reason or "按当前选卡意图执行。", "source_type": "rule_fallback"}),
            out)
        if reason and picked.get("command") == "choose" and picked.get("choice") == choice:
            picked.setdefault("reason", reason)
        self._grid_picked = picked.get("choice") if picked.get("command") == "choose" else None
        return picked

    def _hp_ratio(self) -> float:
        hp = self._budget()
        return hp.current_hp / hp.max_hp if hp.max_hp else 0.0

    def _jev_tactical_pick(self, game: dict):
        """Let JEV rank a small, GPT-compatible candidate set in combat.

        JEV still receives labels, never protocol commands.  A low-confidence
        answer is deliberately ignored so the local combat policy or GPT's
        explicit preference remains the safe fallback.
        """
        if self._active_plan is None or not self._active_candidates or self.run is None:
            return None
        candidates = [c for c in self._active_candidates
                      if c.candidate_id not in self._active_plan.avoid_candidates]
        preferred = set(self._active_plan.preferred_candidates)
        if preferred:
            preferred_candidates = [c for c in candidates if c.candidate_id in preferred]
            others = [c for c in candidates if c.candidate_id not in preferred]
            candidates = preferred_candidates + others[:2]
        candidates = candidates[:6]
        if len(candidates) < 2:
            return None
        criteria = {c.candidate_id: c.label for c in candidates}
        try:
            state = self.run.digest({
                "tactical_goal": self._active_plan.current_objective,
                "candidates": [c.model_dict() for c in candidates],
                "screen": "COMBAT",
            })
            response = self.jev.ask(state, {
                "tactical": ChoiceSpec(
                    instructions=(
                        "Within the already legal candidates, which single action best serves "
                        "the strategic goal this combat turn? Choose only a candidate_id."
                    ),
                    criteria=criteria,
                )
            })
            answer = response.answers.get("tactical")
            if answer is None or float(answer.confidence or 0.0) < NOUL_UNCERTAIN_BAND[1]:
                return None
            candidate = next((c for c in candidates if c.candidate_id == str(answer.value)), None)
            return (candidate, float(answer.confidence or 0.0)) if candidate is not None else None
        except Exception:
            return None

    # -- 5. shop ----------------------------------------------------------- #
    def _on_shop(self, game) -> dict:
        """Shop: buying is CHOOSE by shelf index, leaving is RETURN.

        PHASE 1 VERIFY: CHOOSE addresses the shelves in the order cards, then
        relics, then potions (the order we build `index` in below), and the
        card-removal service is one more choice after them. Confirm on the live
        pipe — if the ordering differs, we buy the wrong item, which is visible
        and recoverable, so this stays a flagged assumption rather than a blocker.
        """
        floor = _as_int(_get(game, "floor", "floor_num", default=0))
        screen = _get(game, "screen_state", "screen", default=game)
        gold = _as_int(_get(game, "gold", default=0))
        raw = []
        for kind, key in (("card", "cards"), ("relic", "relics"), ("potion", "potions")):
            for item in (_get(screen, key, default=[]) or []):
                price_valid = True
                try:
                    raw_price = _get(item, "price", "cost", default=None)
                    price = int(raw_price) if raw_price is not None else -1
                except (TypeError, ValueError):
                    price = -1
                    price_valid = False
                unavailable = item is None or not price_valid or any(bool(_get(item, flag, default=False)) for flag in (
                    "disabled", "purchased", "sold", "removed", "unavailable"))
                unavailable = unavailable or _get(item, "available", default=True) is False \
                    or _get(item, "is_available", default=True) is False
                raw.append({"kind": kind,
                            "name": str(_get(item, "id", "card_id", "relic_id", "name", default=kind)),
                            "price": price,
                            "description": str(_get(item, "description", default="")),
                            "disabled": unavailable,
                            "potion_full": bool(_get(item, "potion_full", "potion_slots_full", default=False))})
        try:
            purge_cost = int(_get(screen, "purge_cost", default=0) or 0) or None
        except (TypeError, ValueError):
            purge_cost = None
        signature = (
            floor, gold,
            tuple((item["kind"], item["name"], item["price"], item["disabled"], item["potion_full"])
                  for item in raw),
            purge_cost,
        )
        if self._shop_decided_on_floor == floor and self._shop_state_signature == signature:
            # The exact same state is a navigation/confirmation revisit. A
            # changed shelf or gold amount is a real purchase and is replanned.
            return {"command": "return", "reason_source": "navigation",
                    "reason": "商店状态未变化，保持上次建议并等待玩家操作"}
        self._shop_decided_on_floor = floor
        self._shop_state_signature = signature
        # `shop_items` disambiguates duplicate labels; keep only affordable
        # objects so JEV cannot select a purchase the legality layer will reject.
        affordable = [item for item in raw
                      if not item["disabled"] and item["price"] <= gold
                      and (item["kind"] != "potion"
                           or potion_purchase_allowed(game, item, screen))]
        items = shop_items(affordable)
        d = ShopDecider(self.jev, goal=self.goal, run=self.run).decide(
            gold=gold, items=items,
            removal_cost=purge_cost,
            remove_candidate="a starter Strike or Defend",
        )
        if d.value == "leave":
            return self._finalize_and_record(d, {"command": "return"})
        if d.value == "remove":
            # The purge service sits after the shelves; PURGE is not a verb.
            return self._finalize_and_record(d, {"command": "choose", "choice": len(raw)})
        choice = next((i for i, r in enumerate(raw)
                       if not r["disabled"] and r["price"] <= gold
                       and r["name"] == str(d.value)), None)
        if choice is None:  # label came from a disambiguated duplicate
            choice = next((i for i, r in enumerate(raw)
                           if not r["disabled"] and r["price"] <= gold
                           and str(d.value).startswith(r["name"])), 0)
        return self._finalize_and_record(d, {"command": "choose", "choice": choice})

    # -- 6. boss relic ----------------------------------------------------- #
    def _on_boss_reward(self, game) -> dict:
        screen = _get(game, "screen_state", "screen", default=game)
        raw = _get(screen, "relics", default=[]) or []
        if not raw:
            # Some CommunicationMod versions expose an empty boss-reward list
            # while the reward animation is still resolving; CHOOSE 0 is the
            # historical harmless acknowledgement for that state.
            return {"command": "choose", "choice": 0}
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
        return self._finalize_and_record(d, {"command": "choose", "choice": index.get(str(d.value), 0)})

    # -- 7. combat --------------------------------------------------------- #
    def _on_combat(self, game) -> dict:
        def _int_value(value, default: int = 0) -> int:
            try:
                return int(value)
            except (TypeError, ValueError):
                return default

        combat = _get(game, "combat", default=game)
        player = _get(combat, "player", default=combat)
        monsters = _get(combat, "monsters", default=[]) or []
        hand_raw = _get(combat, "hand", default=[]) or []

        state = CombatState(
            player_hp=_int_value(_get(player, "current_hp", "hp", default=1), 1),
            player_block=_int_value(_get(player, "block", default=0)),
            energy=_int_value(_get(player, "energy", default=3), 3),
            hand=[Card(name=str(_get(c, "name", "card_id", default="card")),
                       type=str(_get(c, "type", default="skill")).lower(),
                       damage=_int_value(_get(c, "damage", default=0)),
                       block=_int_value(_get(c, "block", default=0)),
                       energy=max(0, _int_value(_get(c, "cost", default=1), 1)))
                  for c in hand_raw],
            enemies=[{"name": str(_get(m, "name", default="enemy")),
                      "hp": _int_value(_get(m, "current_hp", "hp", default=0)),
                      "intent": str(_get(m, "intent", default="unknown")).lower(),
                      "damage": _int_value(_get(m, "move_adjusted_damage", "damage", default=0))}
                     for m in monsters],
        )

        # JEV decides posture; the code decides the cards. The gate is only
        # consulted when the incoming damage threatens the act's HP budget, so a
        # routine turn costs zero JEV calls.
        suggestion = recommend_action(game)
        incoming = sum(max(0, e["damage"]) for e in state.enemies if "attack" in e["intent"])
        defensive_posture = False
        if incoming > self._budget().remaining_budget:
            gate = CombatRiskGate(self.jev, self._budget(), run=self.run)
            d = gate.decide(str(_get(combat, "encounter_name", default="a fight")), incoming)
            self._finalize_and_record(d, {"command": "(posture only)"})
            defensive_posture = str(d.detail.get("posture", "")) == "defensive"
            if defensive_posture:
                # Defensive posture is a risk preference, not a blanket ban on
                # attacks. Preserve a locally verified lethal action because it
                # removes the incoming threat completely; only unverified
                # attacks are excluded from the defensive candidate set.
                lethal_command = (suggestion.command if suggestion is not None
                                  and suggestion.facts.get("lethal_confirmed") else None)
                safe = [candidate for candidate in self._active_candidates
                        if candidate.kind in {"end", "potion"}
                        or "block" in candidate.goal_tags
                        or (lethal_command is not None and candidate.command == lethal_command)]
                if safe:
                    self._active_candidates = safe

        if suggestion is not None:
            if defensive_posture and str(suggestion.command.get("command", "")).lower() == "play":
                # A verified kill remains preferable to a defensive fallback.
                if suggestion.facts.get("lethal_confirmed") and any(
                        candidate.command == suggestion.command for candidate in self._active_candidates):
                    safe = next(candidate for candidate in self._active_candidates
                                if candidate.command == suggestion.command)
                else:
                    safe = next((candidate for candidate in self._active_candidates
                                 if "block" in candidate.goal_tags), None)
                safe = safe or next((candidate for candidate in self._active_candidates
                                     if candidate.kind == "potion"), None)
                safe = safe or next((candidate for candidate in self._active_candidates
                                     if candidate.kind == "end"), None)
                if safe is not None:
                    keeps_lethal = bool(suggestion.facts.get("lethal_confirmed")
                                        and safe.command == suggestion.command)
                    suggestion = type(suggestion)(
                        command=dict(safe.command),
                        reason="硬规则要求优先防守，避免本回合明确超出生命预算。",
                        uncertain=bool(safe.uncertainty),
                        facts={**suggestion.facts,
                               "lethal_confirmed": keeps_lethal,
                               "decision_basis": "defensive_constraint"},
                    )
            tactical = self._jev_tactical_pick(game)
            tactical_confidence = 0.0
            if tactical is not None:
                tactical_candidate, tactical_confidence = tactical
                suggestion = type(suggestion)(
                    command=dict(tactical_candidate.command),
                    reason=f"JEV 在 GPT 战略目标内选择：{tactical_candidate.label}。",
                    uncertain=bool(tactical_candidate.uncertainty),
                    facts={**suggestion.facts, "decision_basis": "jev_tactical_pick",
                           "jev_selected_candidate": tactical_candidate.candidate_id},
                )
            return self._finalize_and_record(Decision("combat", suggestion.command, tactical_confidence,
                                         tactical is None,
                                         {"reason": suggestion.reason,
                                          "source_type": "rule_fallback",
                                          "uncertain": suggestion.uncertain,
                                          "combat_facts": dict(suggestion.facts),
                                          "selection_basis": suggestion.facts.get(
                                              "decision_basis", "local_tactical_rule"),
                                          "local_confidence": (tactical_confidence
                                                                if tactical is not None else None),
                                          "jev_confidence": (tactical_confidence
                                                             if tactical is not None else None)}),
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
