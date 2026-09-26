"""Decision feed — the brain's inner monologue, as an event stream.

Phase 1.5, the interaction layer. Until now every decision (probability,
confidence, fallback reason) landed in `logs/*.jsonl` for *after* the run; this
module publishes the same facts *while* the run happens, to anyone watching —
which is the difference between a black box and a demonstrable one.

Design rules, inherited from the rest of the project:

* **The feed can never break the run.** Publishing is wrapped so any subscriber
  failure is swallowed after being reported once. A dashboard that dies must
  not take the agent with it — the same reasoning that puts `try/except` around
  every JEV call in `decisions.py`.
* **Stdlib only.** The brain is stdlib-only by policy (`README.md`); the feed
  and its server keep that true, so the game can spawn us with no venv.
* **Append-only history + live subscribers.** A dashboard connecting mid-run
  replays the whole history first (so it can render HP and deck context), then
  receives events live. Both come from the same lock-guarded list.

Three event kinds, chosen to be exactly what a spectator needs:

    run_state   — act / floor / HP / budget / gold / deck size (from observe())
    decision    — one Decision: value, confidence, probabilities, fallback, why
    run_end     — summary line when a transport finishes or a sim run ends

Advisor mode adds two more, because "I suggest X" and "you did Y" are two facts
and a player needs both:

    advice      — a recommendation: what to play, why, how sure, in Chinese
    outcome     — what the player actually did, and whether it matched

Nothing here knows about HTTP or the game; `overlay/server.py` and
`driver/agent.py` are the two ends.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

# Event kinds the dashboard understands. Kept as a tuple so tests can assert
# the vocabulary the same way PROTOCOL_VERBS pins the game's verb set.
EVENT_KINDS = ("run_state", "decision", "advice", "outcome", "run_end", "agent_state")


class DecisionFeed:
    """Thread-safe append-only event log with live subscriber fan-out.

    `subscribe()` returns a queue-like object (any object with `.put()`); a
    failed subscriber is dropped after one strike rather than retried forever —
    a dead browser tab must not accumulate unbounded memory in a live run.
    """

    def __init__(self, max_history: int = 2000,
                 journal_path: str | Path | None = None) -> None:
        self._events: list[dict] = []
        self._subs: list[tuple[object, bool]] = []  # (sink, warned)
        self._lock = threading.Lock()
        self._max_history = max_history
        self.journal_path = Path(journal_path) if journal_path else None
        self.seq = 0

    # -- publishing -------------------------------------------------------- #
    def publish(self, kind: str, payload: dict) -> dict:
        """Add one event and fan it out. Returns the stored event (with seq/ts)."""
        if kind not in EVENT_KINDS:
            raise ValueError(f"unknown event kind: {kind!r} (have {EVENT_KINDS})")
        with self._lock:
            self.seq += 1
            event = {"seq": self.seq, "ts": time.time(), "kind": kind, **payload}
            self._events.append(event)
            if len(self._events) > self._max_history:
                # Drop the oldest half, not one: amortised O(1), and a dashboard
                # reconnecting still gets plenty of context.
                del self._events[: len(self._events) // 2]
            subs = list(self._subs)
        for sink, warned in subs:
            try:
                sink.put(event, block=False)
            except Exception:  # noqa: BLE001 - a dead subscriber is not our problem
                self._strike(sink, warned)
        self._journal(event)
        return event

    def _strike(self, sink, warned: bool) -> None:
        """Drop a failed subscriber once, loudly (to stderr, not to the pipe)."""
        with self._lock:
            try:
                self._subs = [(s, w) for (s, w) in self._subs if s is not sink]
            except Exception:  # noqa: BLE001
                pass
        if not warned:
            import sys
            print("[feed] dropped a dead subscriber", file=sys.stderr, flush=True)

    def _journal(self, event: dict) -> None:
        """Optional JSONL mirror of the feed — the stream itself, on disk."""
        if self.journal_path is None:
            return
        try:
            self.journal_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.journal_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(event, ensure_ascii=False) + "\n")
        except OSError:
            pass  # a full disk must not kill the run (same rule as stdio._log)

    # -- consuming --------------------------------------------------------- #
    def subscribe(self, sink) -> object:
        """Register a queue-like sink; it immediately receives the full history."""
        with self._lock:
            self._subs.append((sink, False))
            backlog = list(self._events)
        for event in backlog:
            try:
                sink.put(event, block=False)
            except Exception:  # noqa: BLE001
                self._strike(sink, False)
                break
        return sink

    def unsubscribe(self, sink) -> None:
        with self._lock:
            self._subs = [(s, w) for (s, w) in self._subs if s is not sink]

    def history(self, limit: int | None = None) -> list[dict]:
        with self._lock:
            events = list(self._events)
        return events[-limit:] if limit else events

    def __len__(self) -> int:
        with self._lock:
            return len(self._events)


# --------------------------------------------------------------------------- #
# Payload builders — the one place that knows what each event looks like
# --------------------------------------------------------------------------- #
def run_state_event(agent) -> dict:
    """Snapshot of where the run stands, from the agent's own model of the world.

    Reads the attributes `observe()` maintains; every read is defensive so a
    partially-initialised agent still yields a publishable state.
    """
    hp = agent.hp
    run = agent.run
    state: dict = {}
    if hp is not None:
        state.update({
            "act": hp.act,
            "hp": hp.current_hp,
            "max_hp": hp.max_hp,
            "reserved": hp.reserved_hp,
            "budget_remaining": hp.remaining_budget,
        })
    if run is not None:
        state.update({
            "floor": run.floor,
            "character": run.character,
            "gold": run.gold,
            "deck_size": len(run.deck or []),
            # Values have already passed through GameSnapshot normalization;
            # keep them as Unicode strings so the browser never has to guess
            # which legacy code page produced a relic name.
            "relics": [_display_name(name) for name in (run.relics or [])],
        })
    strategic = getattr(agent, "strategic", None)
    if strategic is not None:
        try:
            detail = strategic.current_detail()
            observed_state = getattr(agent, "_observed_state_id", "")
            if (observed_state and getattr(strategic, "last_state_id", "")
                    and observed_state != getattr(strategic, "last_state_id", "")):
                detail = {**detail, "strategic_goal": "", "long_term_goal": "",
                          "plan_id": "", "brain_source": "pending"}
            state.update({
                "state_id": observed_state
                or getattr(strategic, "last_state_id", ""),
                "strategic_goal": detail.get("strategic_goal", ""),
                "long_term_goal": detail.get("long_term_goal", ""),
                "plan_id": detail.get("plan_id", ""),
                "brain_source": detail.get("brain_source", ""),
                "brain_backend": detail.get("brain_backend", ""),
            })
        except Exception:  # optional observability must never affect gameplay
            pass
    runtime = getattr(agent, "runtime_config", None)
    session = getattr(agent, "run_session", None)
    state.update({
        "mode": getattr(session, "mode", "advise") or "advise",
        "config_id": getattr(runtime, "config_id", "") if runtime else "",
        "dashboard_status": "connected" if getattr(agent, "feed", None) is not None else "disabled",
        "game_connection_status": "connected" if getattr(agent, "_active_game", None) is not None else "waiting",
        "state_receiver_status": "receiving" if getattr(agent, "_observed_state_id", "") else "waiting",
        "model_connection_status": ("ready" if strategic and getattr(strategic, "backend_name", "") not in {"disabled", "unavailable"}
                                     else "fallback"),
    })
    return state


def _display_name(value) -> str:
    """Return a stable, readable label even when CM supplied mojibake."""
    if isinstance(value, dict):
        name = str(value.get("name") or "")
        identity = str(value.get("id") or value.get("relic_id") or "")
        if "\ufffd" in name or name == "文本不可用" or not name.strip():
            return identity or "未知遗物"
        return name
    text = str(value)
    if "\ufffd" in text or text == "文本不可用":
        return "未知遗物"
    return text


def _safe_display_text(value: object, fallback: str = "文本缺失") -> str:
    text = str(value or "")
    if "\ufffd" in text or "文本不可用" in text:
        return text.replace("文本不可用", fallback).replace("\ufffd", fallback)
    return text


def decision_event(decision, command: dict) -> dict:
    """One Decision + the command it became, flattened for the dashboard.

    `detail` carries the interesting internals — Score distributions, Noul probe
    tables, gate breakdowns, fallback reasons — and is passed through verbatim:
    the dashboard decides how to render each shape, the feed does not guess.
    """
    detail = _jsonable(decision.detail)
    return {
        "point": decision.point,
        "value": decision.value,
        "confidence": round(float(decision.confidence), 4),
        "fallback": bool(decision.used_fallback),
        "detail": detail,
        "raw_model_confidence": detail.get("raw_model_confidence"),
        "local_confidence": detail.get("local_confidence"),
        "selection_basis": detail.get("selection_basis", ""),
        "combat_facts": detail.get("combat_facts", {}),
        "command": command,
    }


def advice_event(advice, tally: dict | None = None,
                 agreement: float | None = None) -> dict:
    """One recommendation, as the player sees it.

    `label` is the sentence the panel shows large ("出「痛击」→ 咔咔"); `reason`
    is the why, straight from the decision that produced it. `confidence` is 0.0
    for screens answered by navigation rules rather than the model, which is why
    the dashboard renders 0 as "规则" and not as a 0% certainty.
    """
    status = getattr(advice, "status", "ready") or "ready"
    display_status = {
        "fast_advice": "local_advice",
        "model_ready": "complete_advice",
        "thinking": "model_analysis",
        "unavailable": "fallback",
        "expired": "expired",
    }.get(status, status)
    return {
        "record_kind": "advice_history",
        "point": advice.point,
        "screen": advice.screen,
        "label": _safe_display_text(advice.label, "目标信息缺失"),
        "verb": str((advice.command or {}).get("command", "")),
        "command": advice.command,
        "key": list(advice.key) if advice.key else None,
        "reason": _safe_display_text(advice.reason),
        "confidence": round(float(advice.confidence or 0.0), 4),
        "raw_model_confidence": (round(float(advice.raw_model_confidence), 4)
                                  if advice.raw_model_confidence is not None else None),
        "local_confidence": (round(float(advice.local_confidence), 4)
                              if advice.local_confidence is not None else None),
        "selection_basis": advice.selection_basis,
        "fallback": bool(advice.fallback),
        "act": advice.act,
        "floor": advice.floor,
        "tally": dict(tally or {}),
        "agreement": agreement,
        "state_id": advice.state_id,
        "run_id": advice.run_id,
        "run_epoch": advice.run_epoch,
        "advice_revision": advice.advice_revision,
        "decision_id": advice.decision_id,
        "request_id": advice.request_id or advice.brain_request_id,
        "request_latency_ms": advice.brain_latency_ms or None,
        "first_advice_latency_ms": advice.first_advice_latency_ms,
        "decision_latency_ms": advice.decision_latency_ms,
        "status": status,
        "display_status": display_status,
        "source_type": advice.source_type,
        "source": advice.source,
        "guide_rules": advice.guide_rules,
        "strategic_goal": advice.strategic_goal,
        "plan_id": advice.plan_id,
        "brain_source": advice.brain_source,
        "jev_confidence": round(float(advice.jev_confidence or 0.0), 4),
        "alternative_command": advice.alternative_command,
        "alternative_label": _safe_display_text(advice.alternative_label, "目标信息缺失"),
        "alternative_reason": _safe_display_text(advice.alternative_reason),
        "alternative_condition": _safe_display_text(advice.alternative_condition),
        "uncertain": bool(advice.uncertain),
        "candidates": _jsonable(advice.candidates),
        "candidate_id": advice.candidate_id,
        "long_term_goal": advice.long_term_goal,
        "brain_backend": advice.brain_backend,
        "brain_latency_ms": advice.brain_latency_ms,
        "brain_request_id": advice.brain_request_id,
        "brain_error": advice.brain_error,
        "combat_facts": _jsonable(advice.combat_facts),
    }


def outcome_event(outcome, tally: dict | None = None,
                  agreement: float | None = None) -> dict:
    """What the player did with the advice, and the running agreement rate.

    `evidence` travels verbatim: it is the proof of the inference, and the only
    defence against a verdict nobody can audit. `acted` is None when no single
    action could be identified — the honest case, rendered as "没看出来".
    """
    return {
        "record_kind": ("unobserved_record" if outcome.verdict == "unobserved"
                         else "deviation_record" if outcome.verdict == "mismatch"
                         else "fact_memory"),
        "point": outcome.advice.point,
        "verdict": outcome.verdict,
        "plan_deviation": outcome.verdict == "mismatch",
        "advice_label": outcome.advice.label,
        "acted_label": outcome.acted_label,
        "acted": list(outcome.acted_key) if outcome.acted_key else None,
        "evidence": _jsonable(outcome.evidence),
        "tally": dict(tally or {}),
        "agreement": agreement,
    }


def _jsonable(obj):
    """Best-effort conversion so one odd value cannot poison the whole event."""
    try:
        json.dumps(obj, ensure_ascii=False)
        return obj
    except (TypeError, ValueError):
        if isinstance(obj, dict):
            return {str(k): _jsonable(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple, set)):
            return [_jsonable(v) for v in obj]
        return str(obj)
