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
from spirebrain.driver.stdio import StdioTransport as _StdioTransport
from spirebrain.overlay.feed import DecisionFeed


def _play(agent, **kwargs) -> "_StdioTransport":
    """An AUTO-PLAY transport — what every test in this file is about.

    The transport's default mode is `advise` (recommendation only, polls the
    game instead of acting on it), because that is the project's purpose. These
    guards exist for the mode that *acts*, so each construction says `play`
    explicitly: a guard test that silently changed meaning with a default would
    be worse than one that fails. `advice_path=None` keeps the advice journal
    out of the repo, the same reason `log_path=None` is passed everywhere here.
    """
    kwargs.setdefault("mode", "play")
    kwargs.setdefault("advice_path", None)
    return _StdioTransport(agent, **kwargs)


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
        transport = _play(agent, log_path=None, stall_limit=2)
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
        transport = _play(agent, log_path=None, stall_limit=2)
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
        transport = _play(agent, log_path=None, stall_limit=2)
        same = _msg(_state())
        for _ in range(5):
            assert transport.handle_message(json.loads(same)) == "wait 20"
        assert transport.stalled is False and transport.stuck_events == 0


def test_fingerprint_changes_when_state_changes():
    fp = _StdioTransport._fingerprint
    assert fp(_state()) != fp(_state(current_hp=70))
    assert fp(_state()) == fp(_state())  # stable across calls, key order included
    assert fp({"a": 1, "b": 2}) == fp({"b": 2, "a": 1})


def test_stall_guard_tells_the_feed():
    with tempfile.TemporaryDirectory() as tmp:
        feed = DecisionFeed()
        agent = _StubAgent(feed=feed)
        transport = _play(agent, log_path=None, stall_limit=2)
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


def test_default_action_limit_is_5000():
    """5000, user call 2026-09-22 evening. The budget is now per run (reset on
    every menu->in_game edge) and the unmodeled-screen ladder removed the only
    loop that ever reached the cap, so the cap is backstop duty and can be
    generous. History of this number: jespire's 200 killed a healthy run
    mid-Act 2 (2026-09-22 15:20); 1000 was the fix until the budget-reset and
    the ladder changed what the cap protects."""
    saved = _clean_env()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            transport = _play(_StubAgent(), log_path=None)
            assert transport.max_commands == 5000
    finally:
        if saved is not None:
            os.environ["JEVBRAIN_MAX_ACTIONS"] = saved


def test_env_var_overrides_action_limit():
    saved = _clean_env()
    os.environ["JEVBRAIN_MAX_ACTIONS"] = "5"
    try:
        with tempfile.TemporaryDirectory() as tmp:
            transport = _play(_StubAgent(), log_path=None)
            assert transport.max_commands == 5
    finally:
        if saved is not None:
            os.environ["JEVBRAIN_MAX_ACTIONS"] = saved
        else:
            os.environ.pop("JEVBRAIN_MAX_ACTIONS", None)


def test_action_limit_stops_the_run_loop_with_a_warning():
    with tempfile.TemporaryDirectory() as tmp:
        err = io.StringIO()
        transport = _play(_StubAgent(), log_path=None, max_commands=3,
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
            transport = _play(_StubAgent(), log_path=None)
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
# Guard 4: the unmodeled-screen ladder
# --------------------------------------------------------------------------- #
def _unmodeled_msg(state: dict | None = None) -> str:
    """A screen the mod cannot act on: no advancing verb is offered.

    Shaped exactly like the live payload of 2026-09-22 18:08 (after Neow's
    reward): available_commands is [key, click, wait, state] only.
    """
    return _msg(state if state is not None else {"screen_type": "NONE",
                                                 "screen_state": {}},
                available_commands=["key", "click", "wait", "state"])


def _unmodeled(**over) -> dict:
    state = {"screen_type": "NONE", "screen_state": {}}
    state.update(over)
    return state


def test_unmodeled_screen_gets_waits_then_keys_then_click():
    """The ladder, rung by rung: 2 waits -> SPACE -> ESCAPE -> click, then the
    cycle repeats. Every rung must be a well-formed line the game accepts."""
    with tempfile.TemporaryDirectory() as tmp:
        agent = _StubAgent()
        transport = _play(agent, log_path=None)
        same = _unmodeled_msg()
        sequence = [transport.handle_message(json.loads(same)) for _ in range(10)]
        assert sequence[0] == "wait 20"
        assert sequence[1] == "wait 20"
        assert sequence[2] == "key SPACE"
        assert sequence[3] == "key ESCAPE"
        assert sequence[4] == "click 960 540"
        assert sequence[5] == "wait 20"  # a second cycle starts
        assert transport.stalled is False

def test_unmodeled_screen_stops_loudly_after_three_cycles():
    with tempfile.TemporaryDirectory() as tmp:
        err = io.StringIO()
        feed = DecisionFeed()
        agent = _StubAgent(feed=feed)
        transport = _play(agent, log_path=None, warn_stream=err)
        same = _unmodeled_msg()
        # 3 cycles x 5 rungs = 15 rungs; the 16th message stops loudly.
        for _ in range(15):
            assert transport.handle_message(json.loads(same)) is not None
        assert transport.ladder_stopped is False  # the last rung is not the stop
        assert transport.handle_message(json.loads(same)) is None  # msg 16 stops
        assert transport.ladder_stopped is True
        assert transport.ladder_events == 1
        assert "UNMODELED SCREEN" in err.getvalue()
        ends = [e for e in feed.history() if e["kind"] == "run_end"]
        assert ends and ends[-1]["reason"] == "unmodeled_screen"
        # Latched: no more messages are answered on this fingerprint...
        assert transport.handle_message(json.loads(same)) is None
        assert transport.ladder_events == 1  # ...and the alarm does not re-fire


def test_unmodeled_screen_auto_resumes_when_a_modeled_screen_returns():
    """A new modeled screen clears the latch by itself - a human (or an
    animation) resolved the screen, and the agent must come back to life
    without a game restart."""
    with tempfile.TemporaryDirectory() as tmp:
        err = io.StringIO()
        agent = _StubAgent()
        transport = _play(agent, log_path=None, warn_stream=err)
        stuck = _unmodeled_msg()
        for _ in range(15):
            assert transport.handle_message(json.loads(stuck)) is not None
        assert transport.ladder_stopped is False  # 15 rungs are the last cycle
        assert transport.handle_message(json.loads(stuck)) is None  # msg 16 stops
        assert transport.ladder_stopped is True
        assert transport.ladder_events == 1  # announced exactly once
        assert "UNMODELED SCREEN" in err.getvalue()
        # The game moved on: a real, actionable screen arrives.
        assert transport.handle_message(json.loads(_msg(_state()))) == "choose 0"
        assert transport.ladder_stopped is False
        assert transport._ladder_fp is None
        # And a LATER unmodeled screen gets a fresh ladder, not the old latch.
        assert transport.handle_message(json.loads(stuck)) == "wait 20"


def test_unmodeled_ladder_never_asks_the_agent():
    """The ladder is navigation, not a decision: the model is never consulted
    for a screen nobody can act on."""
    with tempfile.TemporaryDirectory() as tmp:
        agent = _StubAgent()
        transport = _play(agent, log_path=None)
        same = _unmodeled_msg()
        for _ in range(14):
            transport.handle_message(json.loads(same))
        assert agent.seen == []


def test_run_budget_resets_on_a_new_run():
    """The action budget is per run, not per process: a menu -> in_game edge
    refills it. The 18:08 session's third run died ~50 commands in because
    the counter carried its predecessors' debt."""
    with tempfile.TemporaryDirectory() as tmp:
        err = io.StringIO()
        transport = _play(_StubAgent(), log_path=None, max_commands=5,
                                   warn_stream=err)
        # Run 1: hit the cap.
        lines = [_msg(_state(floor=i)) for i in range(10)]
        assert transport.run(iter(lines), _NoopStream(), send_ready=False) == 5
        assert transport.action_limit_hit is True
        # Back to the menu, then a new run starts.
        menu = json.loads(_msg(None, in_game=False))
        transport.handle_message(menu)  # in_game False: no budget reset yet
        assert transport.commands == 5  # unchanged while at the menu
        new_run = json.loads(_msg(_state(floor=0)))
        transport.handle_message(new_run)
        assert transport.commands == 1  # refilled
        assert transport.action_limit_hit is False


def test_unmodeled_screen_waits_cost_far_under_the_cap():
    """The whole reason guard 4 exists: the identical failure on the old code
    burned 991 waits on ONE screen. The ladder must spend an order less."""
    with tempfile.TemporaryDirectory() as tmp:
        agent = _StubAgent()
        transport = _play(agent, log_path=None)
        same = _unmodeled_msg()
        for _ in range(60):  # six times the cycles the ladder allows
            transport.handle_message(json.loads(same))
        assert transport.commands <= 20
        assert transport.ladder_stopped is True


def test_ladder_survives_a_churning_state():
    """The 18:54 death, part 2: the unmodeled screen's state mutates every few
    messages (uuids, animation counters). A ladder keyed on the full state
    hash restarts mid-climb forever and never reaches its own stop. The rung
    count must key on the SCREEN, not the state."""
    with tempfile.TemporaryDirectory() as tmp:
        err = io.StringIO()
        agent = _StubAgent()
        transport = _play(agent, log_path=None, warn_stream=err)
        for i in range(16):
            # Same screen, same verbs, churning internals - like the live pipe.
            msg = _msg({"screen_type": "NONE", "screen_state": {}, "tick": i},
                       available_commands=["key", "click", "wait", "state"])
            transport.handle_message(json.loads(msg))
        assert transport.ladder_stopped is True, "churning state must not reset the ladder"
        assert "UNMODELED SCREEN" in err.getvalue()


# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"ok {fn.__name__}")
    print(f"\n{len(fns)} tests passed")
