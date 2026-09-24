"""Advisor mode: recommend to the player, and adjudicate what they did.

`advise` is the default mode and the reason this project exists — the player
keeps the mouse and the keyboard, and this session is the half that watches,
recommends and keeps score. It was lifted out of `StdioTransport` (2026-09-22)
because it is a conversation with the *player* rather than with the pipe, and
because the transport file had grown past the point where anyone could safely
change it (both of that night's live failures happened in that file).

The method bodies are the code that ran inside the transport; what changed is
that the dependencies arrive as explicit arguments instead of being reached for
on `self`:

    ensure_offered  — "may we say this verb here?" (the transport's own guard)
    fingerprint     — stable hash of a state, so the brain is asked ONCE per
                      distinct state and never once per poll
    message_count   — for the advice record, so a verdict is traceable

Nothing here may raise into the pipe: advice that kills the agent is worse than
no advice, which is why every publish and every router call is fenced.
"""

from __future__ import annotations

import json
import queue
import threading
import time
from pathlib import Path
from typing import Any, Callable

from spirebrain.driver.modes import ADVISE_POLL_VERBS
from spirebrain.driver.legality import check_action
from spirebrain.driver.witness import (
    SCREEN_POINT,
    Advice,
    PlayerTracker,
    advice_key,
    label_for,
)
from spirebrain.overlay.feed import advice_event, outcome_event
from spirebrain.driver.decision_state import state_id as semantic_state_id


def screen_of(game: Any) -> str:
    """The screen's type, upper-cased, or "" — never raises on a hostile state."""
    try:
        return str((game or {}).get("screen_type", "")).upper()
    except Exception:  # noqa: BLE001
        return ""


def advice_line(command: dict) -> str:
    """ASCII one-liner for the console: the verb and its indices, no prose.

    Deliberately NOT `to_command_line`: that one applies the protocol's own
    1-indexed +1 for PLAY, which would make a console line about **card #1**
    disagree with every log and every other message in this repo.
    """
    verb = str(command.get("command", "?"))
    if verb == "play":
        card = command.get("card", command.get("card_index", "?"))
        target = command.get("target", command.get("target_index"))
        return f"play card#{card}" + (f" -> target#{target}" if target is not None else "")
    if verb == "choose":
        return f"choose {command.get('name') or command.get('choice', command.get('index', '?'))}"
    return verb


class AdviseSession:
    """One advisor conversation: poll, recommend once per state, judge the player."""

    def __init__(self, agent: Any, *, advice_path: Path | None,
                 poll_frames: int, warn_stream: Any,
                 ensure_offered: Callable[..., str],
                 fingerprint: Callable[[dict], str],
                 message_count: Callable[[], int],
                 tracker: PlayerTracker | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.agent = agent
        self.advice_path = advice_path
        self.poll_frames = poll_frames
        self.warn_stream = warn_stream
        # Injected, not inherited: the session asks the transport "may we say
        # this verb here?" and "is this state new?" rather than owning the pipe.
        self._ensure_offered = ensure_offered
        self._fingerprint = fingerprint
        self.message_count = message_count
        self._clock = clock
        #: What keeps polling from becoming a per-second JEV bill: the brain is
        #: asked once per *distinct* state, and a poll re-transmits by design.
        self.advised_fp: str | None = None
        #: Recommendations made; counted here because this is where they happen.
        self.issued = 0
        self._issued_state_ids: set[str] = set()
        self.tracker = tracker if tracker is not None else PlayerTracker()
        self._worker_agent = agent.clone_for_advice() if hasattr(agent, "clone_for_advice") else None
        self._latest_fp: str | None = None
        self._latest_state_id: str | None = None
        self._generation = 0
        self._decision_sequence = 0
        self._state_started_at: dict[str, float] = {}
        self._first_advice_latency: dict[str, int] = {}
        self._work_lock = threading.Lock()
        self._work_running = False
        self._queued_work: tuple | None = None
        self._completed: queue.Queue = queue.Queue()

    def reset_run(self) -> None:
        """A new run invalidates any advice pending from the old one.

        The state that advice was made on is gone, and adjudicating it against
        the new run's first state would score a match/mismatch that never
        happened. The tally itself survives — it is the project's metric,
        measured across runs.
        """
        self.tracker.pending = None
        self.tracker.remember_state(None)
        self.advised_fp = None
        self._issued_state_ids.clear()
        self._latest_fp = None
        self._latest_state_id = None
        self._state_started_at.clear()
        self._first_advice_latency.clear()
        self._generation += 1
        with self._work_lock:
            self._queued_work = None

    def advise(self, game: dict, available: Any, modeled: bool) -> str | None:
        """Recommend, never act. Returns the poll command to send.

        The recommendation comes from the SAME pipeline auto-play uses — the JEV
        call, the gate, the tactical layer — because advice from a different
        brain than the one we measured is advice nobody can trust. What changes
        is only where the answer goes: to the player, not to the game.

        Two things are deliberately absent here. The action limit does not apply
        (every command is a poll, see __init__). And the brain is asked once per
        *distinct* state, not once per poll: polling re-transmits the same state
        every ~1/3 second, and re-asking JEV on each one would turn a $0.001
        decision into a per-second bill for a player who is simply thinking.
        """
        # Include the legal command view in the recommendation identity.  Poll
        # metadata and animation noise are removed by the shared projection.
        fp = self._fingerprint(dict(game, available_commands=available))
        state_version = semantic_state_id(game)
        outcome = self.tracker.resolve(game)
        if outcome is not None:
            self._handle_outcome_for_strategy(outcome)
            self._publish_outcome(outcome)

        if not modeled:
            # A screen we cannot read: no advice is possible, and pressing its
            # keys/click is forbidden. Keep the pipe alive and stay quiet.
            if fp != self.advised_fp:
                self.advised_fp = fp
                self._latest_fp = fp
                self._latest_state_id = state_version
                self._begin_decision(fp, state_version)
                self._publish_status(state_version, "unavailable", "当前界面暂无可判断的操作",
                                     "请继续手动操作，进入可识别界面后建议会自动恢复。",
                                     point=SCREEN_POINT.get(screen_of(game), "navigation"))
            self._drain_completed()
            return self._poll(available)

        if fp != self.advised_fp:
            self.advised_fp = fp
            self._latest_fp = fp
            self._latest_state_id = state_version
            # A state can legitimately recur later (for example, returning to
            # the same shop after a cancel).  The generation distinguishes that
            # new request from a late result produced for the earlier visit.
            self._generation += 1
            self._begin_decision(fp, state_version)
            if self._worker_agent is None:
                self._advise_once(game, available)
            else:
                self._advise_async(game, available, fp)
        self._drain_completed()
        return self._poll(available)

    def _handle_outcome_for_strategy(self, outcome) -> None:
        """Feed a clear player deviation back into the single-run planner."""
        if outcome.verdict != "mismatch":
            return
        decision_agent = self._worker_agent or self.agent
        strategic = getattr(decision_agent, "strategic", None)
        if strategic is None:
            return
        advice = outcome.advice
        advised = advice.candidate_id or advice.label or advice.command
        actual = outcome.acted_key or outcome.acted_label or "unknown"
        try:
            strategic.memory.record_deviation(
                str(advised), str(actual), state_id=advice.state_id)
            strategic.request_replan("player_deviation")
        except Exception:  # strategy feedback is optional observability
            pass

    def _publish_status(self, state_id: str, status: str, label: str,
                        reason: str, source_type: str = "pending",
                        guide_rules: list[dict] | None = None,
                        point: str = "navigation") -> None:
        feed = getattr(self.agent, "feed", None)
        if feed is not None:
            try:
                feed.publish("advice", {"point": point, "label": label,
                                        "reason": reason, "state_id": state_id,
                                        "status": status, "source_type": source_type,
                                        "source": "", "guide_rules": guide_rules or [],
                                        "confidence": 0.0, "fallback": False})
            except Exception:  # noqa: BLE001
                pass

    def _advise_async(self, game: dict, available: Any, fp: str) -> None:
        payload = dict(game, available_commands=available)
        try:
            quick = self.agent.quick_advice(payload)
        except Exception as exc:  # noqa: BLE001
            quick = {"status": "thinking", "reason": f"本地规则暂不可用：{type(exc).__name__}"}
        if quick.get("status") == "unsupported":
            self._publish_status(semantic_state_id(game), "unsupported", quick.get("label", "角色未覆盖"),
                                 quick.get("reason", ""), "unavailable",
                                 point=SCREEN_POINT.get(screen_of(game), "navigation"))
            return
        if quick.get("command"):
            self._record_action(payload, quick["command"], self.agent, semantic_state_id(game),
                                reason_override=quick.get("reason", ""),
                                source_type=quick.get("source_type", "guide_rule"),
                                source=quick.get("source", ""),
                                guide_rules=quick.get("guide_rules", []),
                                metadata=quick,
                                provisional=True)
            if quick.get("source_type") == "guide_rule":
                return  # a hard rule has already settled this decision
        else:
            self._publish_status(semantic_state_id(game), "thinking", quick.get("label", "正在分析当前局面…"),
                                 quick.get("reason", ""), "pending",
                                 quick.get("guide_rules", []),
                                 point=SCREEN_POINT.get(screen_of(game), "navigation"))
        with self._work_lock:
            self._queued_work = (fp, payload, self._generation)
            if not self._work_running:
                self._work_running = True
                threading.Thread(target=self._work_loop, daemon=True,
                                 name="SpireBrainAdvice").start()

    def _work_loop(self) -> None:
        while True:
            with self._work_lock:
                job = self._queued_work
                self._queued_work = None
                if job is None:
                    self._work_running = False
                    return
            fp, payload, generation = job
            try:
                command = self._worker_agent.choose_action(payload)
                entry = dict(self._worker_agent.history[-1]) if self._worker_agent.history else None
                self._completed.put((fp, generation, payload, command, entry, None))
            except Exception as exc:  # noqa: BLE001 - failure becomes a panel status
                self._completed.put((fp, generation, payload, None, None, exc))

    def _begin_decision(self, fp: str, state_id: str) -> None:
        self._decision_sequence += 1
        self._state_started_at[fp] = self._clock()
        self._first_advice_latency.pop(fp, None)
        if len(self._state_started_at) > 64:
            oldest = next(iter(self._state_started_at))
            self._state_started_at.pop(oldest, None)
            self._first_advice_latency.pop(oldest, None)
        self._current_decision_id = (
            f"{self._run_id()}:{state_id[:16]}:{self._decision_sequence}"
        )

    def _drain_completed(self) -> None:
        while True:
            try:
                fp, generation, payload, command, entry, error = self._completed.get_nowait()
            except queue.Empty:
                return
            if generation != self._generation or fp != self._latest_fp:
                continue  # the player already changed the game state
            if error is not None or command is None:
                # An immediate hard-rule recommendation remains visible on API
                # failure. Otherwise show a clear unavailable status.
                if self.tracker.pending is None or self.tracker.pending.state_id != self._latest_state_id:
                    self._publish_status(self._latest_state_id or fp, "unavailable", "当前建议暂不可用",
                                         f"模型调用失败：{type(error).__name__ if error else 'unknown'}",
                                         "unavailable",
                                         point=SCREEN_POINT.get(screen_of(payload), "navigation"))
                continue
            if entry and entry.get("command") == command:
                feed = getattr(self.agent, "feed", None)
                if feed is not None:
                    try:
                        feed.publish("decision", dict(entry))
                    except Exception:  # noqa: BLE001 - diagnostics are optional
                        pass
            self._record_action(payload, command, self._worker_agent,
                                semantic_state_id(payload),
                                history_entry=entry)

    def _advise_once(self, game: dict, available: Any) -> None:
        """Ask the brain once for this state and publish what it says."""
        payload = dict(game)
        # Same plumbing as auto-play: the two-phase grid handler reads the
        # offered verbs, which live on the message rather than the state.
        payload["available_commands"] = available
        try:
            command = self.agent.choose_action(payload)
        except Exception as exc:  # noqa: BLE001 - advice must never kill the pipe
            print(f"[advise] the router raised on {screen_of(game)!r}: {exc!r}",
                  file=self.warn_stream, flush=True)
            return

        self._record_action(payload, command, self.agent, semantic_state_id(game))

    def _record_action(self, payload: dict, command: dict, decision_agent: Any,
                       state_id: str, *, reason_override: str = "",
                       source_type: str = "", source: str = "",
                       guide_rules: list[dict] | None = None,
                       history_entry: dict | None = None,
                       metadata: dict | None = None,
                       provisional: bool = False) -> None:
        game = payload
        if self._worker_agent is not None:
            legal, why_not = check_action(game, command)
            if not legal:
                self._publish_status(state_id, "unavailable", "当前一步暂无法确认",
                                     why_not, "unavailable",
                                     point=SCREEN_POINT.get(screen_of(game), "navigation"))
                return
        screen = screen_of(game)
        point = SCREEN_POINT.get(screen, "navigation")
        confidence, fallback, reason = ((0.0, True, "") if provisional else
            self._last_decision_fields(command, agent=decision_agent,
                                       history_entry=history_entry))
        entry = None if provisional else (history_entry or
            ((getattr(decision_agent, "history", None) or [None])[-1]))
        detail = dict(metadata or {})
        if entry and entry.get("command") == command:
            detail.update(entry.get("detail", {}) or {})
        fp = self._fingerprint(dict(game, available_commands=game.get("available_commands")))
        started_at = self._state_started_at.get(fp)
        decision_latency = (max(0, round((self._clock() - started_at) * 1000))
                            if started_at is not None else None)
        first_latency = self._first_advice_latency.get(fp)
        if first_latency is None and decision_latency is not None:
            first_latency = decision_latency
            self._first_advice_latency[fp] = first_latency
        advice = Advice(
            point=point, screen=screen, command=command,
            key=advice_key(payload, command, point),
            label=label_for(payload, command, point),
            reason=reason_override or reason or str(command.get("reason", "") or ""),
            confidence=confidence, fallback=fallback,
            act=int(game.get("act", 0) or 0), floor=int(game.get("floor", 0) or 0),
            message=self.message_count(),
            state_id=state_id,
            source_type=source_type or detail.get("source_type", "guide_rule"),
            source=source or detail.get("source", ""),
            guide_rules=guide_rules if guide_rules is not None else detail.get("guide_rules", []),
            strategic_goal=str(detail.get("strategic_goal", "") or ""),
            plan_id=str(detail.get("plan_id", "") or ""),
            brain_source=str(detail.get("brain_source", "") or ""),
            jev_confidence=float(detail.get("jev_confidence", 0.0) or 0.0),
            alternative_command=(dict(detail["alternative_command"])
                                 if isinstance(detail.get("alternative_command"), dict) else None),
            alternative_label=str(detail.get("alternative_label", "") or ""),
            alternative_reason=str(detail.get("alternative_reason", "") or ""),
            alternative_condition=str(detail.get("alternative_condition", "") or ""),
            uncertain=bool(detail.get("uncertain", False)),
            candidates=list(detail.get("candidates", []) or []),
            candidate_id=str(detail.get("candidate_id", "") or ""),
            long_term_goal=str(detail.get("long_term_goal", "") or ""),
            brain_backend=str(detail.get("brain_backend", "") or ""),
            brain_latency_ms=int(detail.get("brain_latency_ms", 0) or 0),
            brain_request_id=str(detail.get("brain_request_id", "") or ""),
            brain_error=str(detail.get("brain_error", "") or ""),
            status=("fast_advice" if provisional or source_type in {"guide_rule", "rule_fallback"}
                    else "model_ready"),
            decision_id=getattr(self, "_current_decision_id", ""),
            request_id=str(detail.get("brain_request_id", "") or ""),
            first_advice_latency_ms=first_latency,
            decision_latency_ms=decision_latency,
        )
        self.tracker.remember_state(game)
        self.tracker.note(advice)
        if state_id not in self._issued_state_ids:
            self._issued_state_ids.add(state_id)
            self.issued += 1
        self._publish_advice(advice)
        # One ASCII line on stderr: the live console and the mod's error log have
        # no reliable encoding for the Chinese label, so the sentence the player
        # reads lives in the advice event and this line only proves liveness.
        print(f"[advise] {point} -> {advice_line(command)}"
              f" (conf {confidence:.2f}, msg {self.message_count()})",
              file=self.warn_stream, flush=True)

    def _poll(self, available: Any) -> str | None:
        """The only thing advisor mode is allowed to send.

        If the mod offers neither `wait` nor `state` there is no harmless verb
        left, and silence is the honest answer: an advisor does not press
        buttons. Returning None costs nothing — the game is in the player's
        hands either way, and the alternative (`_ensure_offered`'s empty-string
        substitution) is falsy in `run()` but is NOT None, which is the exact
        distinction that broke the ladder's callers on 2026-09-22. Both are
        handled here so no caller has to know.
        """
        if available:
            offered = {str(a).strip().lower() for a in available}
            if not offered & set(ADVISE_POLL_VERBS):
                return None
        line = self._ensure_offered(f"wait {self.poll_frames}", available,
                                    safe=ADVISE_POLL_VERBS)
        return line or None

    def _last_decision_fields(self, command: dict, *, agent: Any = None,
                              history_entry: dict | None = None) -> tuple[float, bool, str]:
        """Confidence, fallback flag and *reason* of the decision behind `command`.

        Screens answered by navigation rules (non-decision screens, a grid with no
        pending intent) never went through `_record`, so there is no number and no
        reason to report. 0.0 means "rules, not a judgement" — which the panel
        renders as 规则判断 rather than as a 0% certainty, because those are
        different facts.

        The reason has to be read from the history rather than from the command:
        `_record` stores the judgement in `detail` and returns the wire command
        alone, so `command.get("reason")` is empty for every recorded decision —
        measured 2026-09-22 with the overlay's own poller, which showed a "why"
        line that was blank on every screen that had a reason.
        """
        history = getattr(agent or self.agent, "history", None) or []
        entry = history_entry or (history[-1] if history else None)
        if entry and entry.get("command") == command:
            detail = entry.get("detail") or {}
            gate = detail.get("gate") or {}
            reason = str(detail.get("reason") or gate.get("reason") or "")
            return (float(entry.get("confidence") or 0.0),
                    bool(entry.get("fallback")), reason)
        return 0.0, False, ""

    def _publish_advice(self, advice: Advice) -> None:
        event = {"kind": "advice", "point": advice.point,
                          "screen": advice.screen, "label": advice.label,
                          "verb": str(advice.command.get("command", "")),
                          "command": advice.command, "key": list(advice.key or ()),
                          "reason": advice.reason, "confidence": advice.confidence,
                          "fallback": advice.fallback, "state_id": advice.state_id,
                          "source_type": advice.source_type, "source": advice.source,
                          "guide_rules": advice.guide_rules,
                          "strategic_goal": advice.strategic_goal,
                          "plan_id": advice.plan_id,
                          "brain_source": advice.brain_source,
                          "alternative_command": advice.alternative_command,
                          "alternative_label": advice.alternative_label,
                          "alternative_reason": advice.alternative_reason,
                          "alternative_condition": advice.alternative_condition,
                          "uncertain": advice.uncertain,
                          "candidates": advice.candidates,
                          "candidate_id": advice.candidate_id,
                          "long_term_goal": advice.long_term_goal,
                          "brain_backend": advice.brain_backend,
                          "brain_latency_ms": advice.brain_latency_ms,
                          "brain_request_id": advice.brain_request_id,
                          "brain_error": advice.brain_error,
                          "status": advice.status,
                          "decision_id": advice.decision_id,
                          "request_id": advice.request_id or advice.brain_request_id,
                          "first_advice_latency_ms": advice.first_advice_latency_ms,
                          "decision_latency_ms": advice.decision_latency_ms}
        feed = getattr(self.agent, "feed", None)
        if feed is not None:
            started = self._clock()
            try:
                feed.publish("advice", advice_event(advice, self.tracker.tally,
                                                    self.tracker.agreement))
                advice.publish_latency_ms = max(0, round((self._clock() - started) * 1000))
            except Exception:  # noqa: BLE001 - a dead dashboard must not break the pipe
                advice.publish_latency_ms = None
        self._advice_log({**event, "publish_latency_ms": advice.publish_latency_ms})
        self._trace("advice", {
            "run_id": self._run_id(), "state_id": advice.state_id,
            "decision_id": advice.decision_id,
            "request_id": advice.request_id or advice.brain_request_id or None,
            "plan_id": advice.plan_id, "screen": advice.screen,
            "point": advice.point, "brain_backend": advice.brain_backend,
            "source_type": advice.source_type,
            "rule_ids": [rule.get("id") for rule in advice.guide_rules
                         if isinstance(rule, dict) and rule.get("id")],
            "candidates": advice.candidates,
            "selected_candidate_id": advice.candidate_id,
            "command": advice.command,
            "alternative": {"command": advice.alternative_command,
                            "label": advice.alternative_label},
            "reason": advice.reason, "confidence": advice.confidence,
            "jev_confidence": advice.jev_confidence,
            "request_latency_ms": advice.brain_latency_ms or None,
            "first_advice_latency_ms": advice.first_advice_latency_ms,
            "decision_latency_ms": advice.decision_latency_ms,
            "publish_latency_ms": advice.publish_latency_ms,
            "fallback": advice.fallback, "uncertain": advice.uncertain,
            "act": advice.act, "floor": advice.floor,
        })

    def _publish_outcome(self, outcome) -> None:
        advice = outcome.advice
        event = {"kind": "outcome", "point": advice.point,
                          "verdict": outcome.verdict,
                          "advice_label": advice.label,
                          "acted_label": outcome.acted_label,
                          "acted": list(outcome.acted_key or ()),
                          "evidence": outcome.evidence,
                          "tally": dict(self.tracker.tally),
                          "agreement": self.tracker.agreement}
        self._advice_log(event)
        self._trace("outcome", {
            "run_id": self._run_id(), "state_id": advice.state_id,
            "plan_id": advice.plan_id, "screen": advice.screen,
            "point": advice.point, "selected_candidate_id": advice.candidate_id,
            "command": advice.command,
            "player_action": {"key": list(outcome.acted_key or ()),
                              "label": outcome.acted_label},
            "verdict": outcome.verdict,
        })
        print(f"[advise] verdict {outcome.verdict}: advised "
              f"{advice_line(outcome.advice.command)} / player did "
              f"{outcome.acted_label or '?'} | agreement "
              f"{'-' if self.tracker.agreement is None else format(self.tracker.agreement, '.2f')}"
              f" over {self.tracker.judged} judged",
              file=self.warn_stream, flush=True)
        feed = getattr(self.agent, "feed", None)
        if feed is None:
            return
        try:
            feed.publish("outcome", outcome_event(outcome, self.tracker.tally,
                                                  self.tracker.agreement))
        except Exception:  # noqa: BLE001
            pass

    def _advice_log(self, event: dict) -> None:
        """Append one advice/verdict line. Failing to log must not stop advice."""
        if self.advice_path is None:
            return
        try:
            self.advice_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.advice_path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps({"ts": time.time(), **event},
                                        ensure_ascii=False, default=str) + "\n")
        except OSError:
            pass

    def _run_id(self) -> str:
        strategic = getattr(self.agent, "strategic", None)
        return str(getattr(getattr(strategic, "memory", None), "run_id", "") or "")

    def _trace(self, event_type: str, event: dict) -> None:
        trace = getattr(self.agent, "trace", None)
        if trace is not None:
            trace.record(event_type, event)

