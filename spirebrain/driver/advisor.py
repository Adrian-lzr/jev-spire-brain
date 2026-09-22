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
import time
from pathlib import Path
from typing import Any, Callable

from spirebrain.driver.modes import ADVISE_POLL_VERBS
from spirebrain.driver.witness import (
    SCREEN_POINT,
    Advice,
    PlayerTracker,
    advice_key,
    label_for,
)
from spirebrain.overlay.feed import advice_event, outcome_event


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
                 tracker: PlayerTracker | None = None) -> None:
        self.agent = agent
        self.advice_path = advice_path
        self.poll_frames = poll_frames
        self.warn_stream = warn_stream
        # Injected, not inherited: the session asks the transport "may we say
        # this verb here?" and "is this state new?" rather than owning the pipe.
        self._ensure_offered = ensure_offered
        self._fingerprint = fingerprint
        self.message_count = message_count
        #: What keeps polling from becoming a per-second JEV bill: the brain is
        #: asked once per *distinct* state, and a poll re-transmits by design.
        self.advised_fp: str | None = None
        #: Recommendations made; counted here because this is where they happen.
        self.issued = 0
        self.tracker = tracker if tracker is not None else PlayerTracker()

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
        outcome = self.tracker.resolve(game)
        if outcome is not None:
            self._publish_outcome(outcome)

        if not modeled:
            # A screen we cannot read: no advice is possible, and pressing its
            # keys/click is forbidden. Keep the pipe alive and stay quiet.
            return self._poll(available)

        fp = self._fingerprint(game)
        if fp != self.advised_fp:
            self.advised_fp = fp
            self._advise_once(game, available)
        return self._poll(available)

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

        screen = screen_of(game)
        point = SCREEN_POINT.get(screen, "navigation")
        confidence, fallback, reason = self._last_decision_fields(command)
        advice = Advice(
            point=point, screen=screen, command=command,
            key=advice_key(payload, command, point),
            label=label_for(payload, command, point),
            reason=reason or str(command.get("reason", "") or ""),
            confidence=confidence, fallback=fallback,
            act=int(game.get("act", 0) or 0), floor=int(game.get("floor", 0) or 0),
            message=self.message_count(),
        )
        self.tracker.remember_state(game)
        self.tracker.note(advice)
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

    def _last_decision_fields(self, command: dict) -> tuple[float, bool, str]:
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
        history = getattr(self.agent, "history", None) or []
        if history and history[-1].get("command") == command:
            entry = history[-1]
            detail = entry.get("detail") or {}
            gate = detail.get("gate") or {}
            reason = str(detail.get("reason") or gate.get("reason") or "")
            return (float(entry.get("confidence") or 0.0),
                    bool(entry.get("fallback")), reason)
        return 0.0, False, ""

    def _publish_advice(self, advice: Advice) -> None:
        self._advice_log({"kind": "advice", "point": advice.point,
                          "screen": advice.screen, "label": advice.label,
                          "verb": str(advice.command.get("command", "")),
                          "command": advice.command, "key": list(advice.key or ()),
                          "reason": advice.reason, "confidence": advice.confidence,
                          "fallback": advice.fallback})
        feed = getattr(self.agent, "feed", None)
        if feed is None:
            return
        try:
            feed.publish("advice", advice_event(advice, self.tracker.tally,
                                                self.tracker.agreement))
        except Exception:  # noqa: BLE001 - a dead dashboard must not break the pipe
            pass

    def _publish_outcome(self, outcome) -> None:
        self._advice_log({"kind": "outcome", "point": outcome.advice.point,
                          "verdict": outcome.verdict,
                          "advice_label": outcome.advice.label,
                          "acted_label": outcome.acted_label,
                          "acted": list(outcome.acted_key or ()),
                          "evidence": outcome.evidence,
                          "tally": dict(self.tracker.tally),
                          "agreement": self.tracker.agreement})
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

