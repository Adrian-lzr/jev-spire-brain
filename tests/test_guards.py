"""Tests for the three runaway guards ported from Ethics03/jevspire.

The guards exist because the same failure modes burned that project's author:

1. STALL GUARD — the same game state coming back playable after we already
   sent a command for it means the command did not take effect; retrying
   against an unchanged state burns API money in a loop.
2. ACTION LIMIT — a process-level cap on commands (default 200, env
   JEVBRAIN_MAX_ACTIONS, <=0 unlimited).
3. NAVIGATION — non-decision screens and a shop's post-purchase return are
   answered by the transport/agent without consulting the model. The agent
   half lives in test_agent_router.py; this file covers the shop-floor rule.
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from spirebrain.driver.agent import SpireBrainAgent
from spirebrain.driver.stdio import StdioTransport
from spirebrain.overlay.feed import DecisionFeed


class _StubAgent:
    """Records what it was asked and returns a fixed command."""

    def __init__(self, command: dict | None = None, feed: DecisionFeed | None = None) -> None:
        self.command = command or {"command": "choose", "choice": 0}
        self.seen: list[dict] = []
        self.feed = feed

    def choose_action(self, game):
        self.seen.append(game)
        return self.command


def _msg(state: dict | None = None, **over) -> str:
    message = {"in_game": True, "ready_for_command": True,
               "available_commands": ["play", "end", "choose", "proceed", "return", "wait",
                                      "state", "potion", "key", "click", "start"],
               "game_state": state if state is not None else {"screen_type": "MAP"}}
    message.update(over)
    return json.dumps(message)


def _state(**over) -> dict:
    state = {"screen_type": "MAP", "act": 1, "floor": 5, "current_hp": 80, "max_hp": 80}
    state.update(over)
    return state


# --------------------------------------------------------------------------- #
# Guard 1: the stall guard
# --------------------------------------------------------------------------- #
def test_stall_guard_stops_after_repeated_identical_state():
    with tempfile.TemporaryDirectory() as tmp:
        agent = _StubAgent()
        transport = StdioTransport(agent, log_path=None, stall_limit=2)
        same = _msg(_state())

        assert transport.handle_message(json.loads(same)) == "choose 0"   # acted
        assert transport.handle_message(json.loads(same)) == "choose 0"   # 1st repeat
        asked_after_first = len(agent.seen)
        assert transport.handle_message(json.loads(same)) is None         # 2nd repeat -> latch
        assert transport.stalled is True and transport.stuck_events == 1
        # Latched: the model is not asked again while the state is unchanged.
        assert transport.handle_message(json.loads(same)) is None
        assert len(agent.seen) == asked_after_first


def test_stall_guard_resumes_on_a_new_state():
    with tempfile.TemporaryDirectory() as tmp:
        agent = _StubAgent()
        transport = StdioTransport(agent, log_path=None, stall_limit=2)
        same = _msg(_state())
        for _ in range(3):  # act, repeat, latch
            transport.handle_message(json.loads(same))
        assert transport.stalled is True

        fresh = _msg(_state(current_hp=70))  # something actually happened
        assert transport.handle_message(json.loads(fresh)) == "choose 0"
        assert transport.stalled is False


def test_state_and_wait_commands_never_arm_the_stall_guard():
    """`state` re-transmits on purpose and `wait` waits on purpose: the state
    coming back unchanged is what they are for, not a sign of a stuck run."""
    with tempfile.TemporaryDirectory() as tmp:
        agent = _StubAgent(command={"command": "wait"})
        transport = StdioTransport(agent, log_path=None, stall_limit=2)
        same = _msg(_state())
        for _ in range(5):
            assert transport.handle_message(json.loads(same)) == "wait"
        assert transport.stalled is False and transport.stuck_events == 0


def test_fingerprint_changes_when_state_changes():
    fp = StdioTransport._fingerprint
    assert fp(_state()) != fp(_state(current_hp=70))
    assert fp(_state()) == fp(_state())  # stable across calls, key order included
    assert fp({"a": 1, "b": 2}) == fp({"b": 2, "a": 1})


def test_stall_guard_tells_the_feed():
    with tempfile.TemporaryDirectory() as tmp:
        feed = DecisionFeed()
        agent = _StubAgent(feed=feed)
        transport = StdioTransport(agent, log_path=None, stall_limit=2)
        same = _msg(_state())
        for _ in range(3):
            transport.handle_message(json.loads(same))
        ends = [e for e in feed.history() if e["kind"] == "run_end"]
        assert ends and ends[-1]["reason"] == "stall_guard"


# --------------------------------------------------------------------------- #
# Guard 2: the action limit
# --------------------------------------------------------------------------- #
def _clean_env(monkeypatch_target: str = "JEVBRAIN_MAX_ACTIONS") -> dict:
    saved = os.environ.pop(monkeypatch_target, None)
    return saved


def test_default_action_limit_is_200():
    saved = _clean_env()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            transport = StdioTransport(_StubAgent(), log_path=None)
            assert transport.max_commands == 200
    finally:
        if saved is not None:
            os.environ["JEVBRAIN_MAX_ACTIONS"] = saved


def test_env_var_overrides_action_limit():
    saved = _clean_env()
    os.environ["JEVBRAIN_MAX_ACTIONS"] = "5"
    try:
        with tempfile.TemporaryDirectory() as tmp:
            transport = StdioTransport(_StubAgent(), log_path=None)
            assert transport.max_commands == 5
    finally:
        if saved is not None:
            os.environ["JEVBRAIN_MAX_ACTIONS"] = saved
        else:
            os.environ.pop("JEVBRAIN_MAX_ACTIONS", None)


def test_action_limit_stops_the_run_loop_with_a_warning():
    with tempfile.TemporaryDirectory() as tmp:
        err = io.StringIO()
        transport = StdioTransport(_StubAgent(), log_path=None, max_commands=3,
                                   warn_stream=err)
        lines = [_msg(_state(floor=i)) for i in range(10)]  # fresh states: stall guard stays quiet
        sent = transport.run(iter(lines), _NoopStream(), send_ready=False)
        assert sent == 3 and transport.action_limit_hit is True
        assert "ACTION LIMIT" in err.getvalue()


def test_zero_action_limit_means_unlimited():
    saved = _clean_env()
    os.environ["JEVBRAIN_MAX_ACTIONS"] = "0"
    try:
        with tempfile.TemporaryDirectory() as tmp:
            transport = StdioTransport(_StubAgent(), log_path=None)
            assert transport.max_commands == 0
            lines = [_msg(_state(floor=i)) for i in range(10)]
            sent = transport.run(iter(lines), _NoopStream(), send_ready=False)
            assert sent == 10 and transport.action_limit_hit is False
    finally:
        if saved is not None:
            os.environ["JEVBRAIN_MAX_ACTIONS"] = saved
        else:
            os.environ.pop("JEVBRAIN_MAX_ACTIONS", None)


class _NoopStream:
    """Swallow stdout writes (the command stream under test)."""

    def write(self, text: str) -> None:
        pass

    def flush(self) -> None:
        pass


# --------------------------------------------------------------------------- #
# Guard 3: navigation without the model (agent side)
# --------------------------------------------------------------------------- #
def _agent(tmp: str) -> SpireBrainAgent:
    return SpireBrainAgent(jev_backend="mock", log_dir=tmp)


def _shop(floor: int) -> dict:
    # A stocked shelf, so the shop decision is a real JEV question: an empty
    # one short-circuits to "leave" without consulting the model, which would
    # make the call-count assertions below vacuous.
    return {"in_game": True, "act": 1, "floor": floor, "max_hp": 80, "current_hp": 80,
            "gold": 200, "deck": [{"name": f"C{i}"} for i in range(10)],
            "screen_type": "SHOP_SCREEN",
            "screen_state": {"cards": [{"name": "Ornamental Fan", "price": 10,
                                        "description": "block"}],
                             "relics": [], "potions": []}}


def test_shop_second_visit_same_floor_leaves_without_a_jev_call():
    """After a decision this floor, the shop screen that comes back is the same
    room wanting an exit — a second JEV question re-derives 'leave' for money."""
    with tempfile.TemporaryDirectory() as tmp:
        agent = _agent(tmp)
        first = agent.choose_action(_shop(floor=5))
        calls_after_first = agent.jev.calls
        assert calls_after_first > 0  # the first visit is a real question

        second = agent.choose_action(_shop(floor=5))
        assert second["command"] == "return"
        assert second["reason_source"] == "navigation"
        assert agent.jev.calls == calls_after_first  # the second visit is not
        assert first is not None


def test_shop_rule_resets_on_a_new_floor():
    with tempfile.TemporaryDirectory() as tmp:
        agent = _agent(tmp)
        agent.choose_action(_shop(floor=5))
        calls_on_floor5 = agent.jev.calls
        again = agent.choose_action(_shop(floor=6))  # a different room, a real question
        assert agent.jev.calls > calls_on_floor5
        assert again.get("reason_source") != "navigation"


# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"ok {fn.__name__}")
    print(f"\n{len(fns)} tests passed")
