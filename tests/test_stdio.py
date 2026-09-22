"""Tests for the CommunicationMod stdio transport.

Every case here corresponds to a documented way an unattended run dies: no
`Ready` handshake (the game waits ten seconds and kills the process), a command
outside `available_commands`, a verb the protocol does not have, or two commands
for one state. The protocol details were verified against the CommunicationMod
README on 2026-09-21 — see the module docstring of `spirebrain/driver/stdio.py`.
"""

from __future__ import annotations

import io
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from spirebrain.driver.agent import PROTOCOL_VERBS
from spirebrain.driver.stdio import (
    StdioTransport,
    replay,
    to_command_line,
)


class _StubAgent:
    """Records what it was asked and returns a fixed command."""

    def __init__(self, command: dict | None = None) -> None:
        self.command = command or {"command": "choose", "choice": 0}
        self.seen: list[dict] = []

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


# --------------------------------------------------------------------------- #
# Command formatting
# --------------------------------------------------------------------------- #
def test_play_is_one_indexed_on_the_wire():
    # README: "CardIndex is 1-indexed to match up with the card numbers in game."
    assert to_command_line({"command": "play", "card": 0}) == "play 1"
    assert to_command_line({"command": "play", "card": 0, "target": 2}) == "play 1 2"
    assert to_command_line({"command": "play", "card_index": 4, "target_index": 1}) == "play 5 1"


def test_skip_and_leave_are_return():
    # README: RETURN is "Equivalent to SKIP, CANCEL, and LEAVE".
    assert to_command_line({"command": "skip"}) == "return"
    assert to_command_line({"command": "leave"}) == "return"
    assert to_command_line({"command": "return"}) == "return"


def test_choose_accepts_index_or_name():
    assert to_command_line({"command": "choose", "choice": 0}) == "choose 0"
    assert to_command_line({"command": "choose", "choice": 3}) == "choose 3"
    assert to_command_line({"command": "choose", "name": "rest"}) == "choose rest"


def test_potion_needs_use_or_discard_and_a_slot():
    assert to_command_line({"command": "potion", "slot": 0}) == "potion use 0"
    assert to_command_line({"command": "potion", "action": "discard", "slot": 1, "target": 0}) \
        == "potion discard 1 0"


def test_start_argument_order_is_class_then_ascension_then_seed():
    # README: START PlayerClass [AscensionLevel] [Seed]. The order is NOT
    # (seed, ascension) — getting it wrong starts a different game silently.
    assert to_command_line({"command": "start", "player_class": "IRONCLAD"}) == "start IRONCLAD"
    assert to_command_line({"command": "start", "player_class": "IRONCLAD",
                            "ascension": 20}) == "start IRONCLAD 20"
    assert to_command_line({"command": "start", "player_class": "IRONCLAD",
                            "ascension": 1, "seed": "ABC123"}) == "start IRONCLAD 1 ABC123"


def test_simple_verbs_and_wait_and_key():
    for verb in ("end", "proceed", "state"):
        assert to_command_line({"command": verb}) == verb
    # Bare `wait` is rejected by the live game ("Argument missing in command
    # \"wait\"." — measured 2026-09-22); it always carries a frame count.
    assert to_command_line({"command": "wait"}) == "wait 20"
    assert to_command_line({"command": "wait", "frames": 30}) == "wait 30"
    assert to_command_line({"command": "key", "key": "End_Turn"}) == "key End_Turn"
    assert to_command_line({"command": "key", "key": "Map", "timeout": 50}) == "key Map 50"


def test_every_formattable_verb_is_a_protocol_verb():
    for verb in ("play", "end", "choose", "proceed", "return", "wait", "state",
                 "start", "potion", "key"):
        assert verb in PROTOCOL_VERBS


def test_malformed_commands_fail_loudly():
    for bad in ({"command": "play"}, {"command": "choose"}, {"command": "start"},
                {"command": "potion"}, {"command": "key"}, {}):
        try:
            to_command_line(bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"{bad!r} should have raised")


# --------------------------------------------------------------------------- #
# The stream
# --------------------------------------------------------------------------- #
def test_ready_is_sent_before_anything_else():
    agent = _StubAgent()
    out = io.StringIO()
    transport = StdioTransport(agent, log_path=None)
    transport.run([_msg()], out)
    assert out.getvalue().startswith("Ready\n"), "the game hangs 10s without this"


def test_replay_mode_does_not_send_ready():
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "001.json"
        p.write_text(_msg(), encoding="utf-8")
        out = io.StringIO()
        replay([p], _StubAgent(), log_path=None, outstream=out)
        assert "Ready" not in out.getvalue()


def test_non_json_lines_are_ignored_not_fatal():
    agent = _StubAgent()
    out = io.StringIO()
    transport = StdioTransport(agent, log_path=None)
    transport.run(["ModTheSpire booting...\n", "\n", "not json\n", _msg()], out)
    assert agent.seen, "the JSON line after the noise must still be handled"
    assert out.getvalue().count("\n") == 2  # Ready + one command


def test_one_command_per_message_never_two():
    agent = _StubAgent()
    out = io.StringIO()
    transport = StdioTransport(agent, log_path=None)
    transport.run([_msg(), _msg()], out)
    lines = [ln for ln in out.getvalue().splitlines() if ln]
    assert lines[0] == "Ready"
    assert len(lines) == 3  # Ready + exactly one command per message


def test_not_ready_is_met_with_silence():
    agent = _StubAgent()
    out = io.StringIO()
    transport = StdioTransport(agent, log_path=None)
    transport.run([_msg(ready_for_command=False)], out)
    assert out.getvalue() == "Ready\n"
    assert transport.skipped_not_ready == 1
    assert not agent.seen


def test_an_error_message_asks_for_state_instead_of_stalling():
    # The game waits for input after an error; silence would hang the run.
    agent = _StubAgent()
    out = io.StringIO()
    transport = StdioTransport(agent, log_path=None)
    transport.run([json.dumps({"error": "invalid command", "ready_for_command": True})], out)
    assert out.getvalue() == "Ready\nstate\n"
    assert transport.errors == 1
    assert not agent.seen


def test_out_of_run_is_silent_unless_auto_start():
    agent = _StubAgent()
    game = {"screen_type": "NONE"}
    with tempfile.TemporaryDirectory() as tmp:
        silent = StdioTransport(agent, log_path=Path(tmp) / "a.jsonl")
        assert silent.handle_message({"in_game": False}) is None
        assert not agent.seen

        starter = StdioTransport(_StubAgent(), log_path=Path(tmp) / "b.jsonl",
                                 auto_start=True, player_class="SILENT", ascension=5)
        assert starter.handle_message({"in_game": False}) == "start SILENT 5"


def test_a_command_outside_available_commands_is_substituted_and_recorded():
    agent = _StubAgent({"command": "end"})  # END is not offered in this message
    transport = StdioTransport(agent, log_path=None)
    line = transport.handle_message(json.loads(
        _msg(available_commands=["choose", "proceed"])))
    assert line == "proceed"
    assert transport.substitutions == [{"wanted": "end", "sent": "proceed"}]


def test_wait_substitution_carries_its_frame_argument():
    """The live run on 2026-09-22 burned 995 errors here: on a NONE screen the
    navigation Proceed was substituted to the bare verb `wait`, which the game
    rejects (`Argument missing in command "wait".`). The substitution path
    must not echo verb names - it builds a command line."""
    transport = StdioTransport(_StubAgent({"command": "choose", "choice": 0}),
                               log_path=None)
    line = transport.handle_message(json.loads(
        _msg(available_commands=["play", "end", "key", "click", "wait", "state"])))
    assert line == "wait 20"
    assert transport.substitutions[-1]["sent"] == "wait 20"


def test_no_safe_substitute_means_no_command_at_all():
    transport = StdioTransport(_StubAgent({"command": "end"}), log_path=None)
    line = transport.handle_message(json.loads(_msg(available_commands=["play"])))
    assert line == ""
    assert transport.substitutions[-1]["sent"] == ""


def test_a_ready_message_without_state_asks_for_state():
    transport = StdioTransport(_StubAgent(), log_path=None)
    assert transport.handle_message({"ready_for_command": True, "in_game": True}) == "state"


def test_the_pipe_log_records_both_sides():
    with tempfile.TemporaryDirectory() as tmp:
        log = Path(tmp) / "pipe.jsonl"
        transport = StdioTransport(_StubAgent(), log_path=log)
        transport.run([_msg()], io.StringIO())
        rec = json.loads(log.read_text(encoding="utf-8").splitlines()[0])
        assert rec["sent"] == "choose 0"
        assert "game_state" in rec["msg"]


# --------------------------------------------------------------------------- #
# Log hygiene
# --------------------------------------------------------------------------- #
# `logs/pipe.jsonl` is the record of what the REAL game sent us. It briefly got
# 44 KB of synthetic states appended by this very test file, because `log_path=None`
# meant "use the default path" instead of "do not log". Fake states that look like a
# working pipe are worse than no log at all: they were nearly read as a successful
# live smoke test. These tests exist so that cannot recur.
ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LOG = ROOT / "logs" / "pipe.jsonl"


def _size(path: Path) -> int:
    return path.stat().st_size if path.exists() else -1


def test_log_path_none_means_no_logging_at_all():
    before = _size(DEFAULT_LOG)
    transport = StdioTransport(_StubAgent(), log_path=None)
    assert transport.log_path is None
    transport.run([_msg(), _msg()], io.StringIO())
    assert _size(DEFAULT_LOG) == before, (
        "a transport with logging disabled must not touch the live pipe log"
    )


def test_omitting_log_path_uses_the_default_file():
    """Only an explicit omission chooses the default; that is what DEFAULT_LOG is for."""
    from spirebrain.driver.stdio import DEFAULT_LOG as SENTINEL

    transport = StdioTransport(_StubAgent())
    assert transport.log_path == DEFAULT_LOG
    assert SENTINEL is not None and SENTINEL is not None


def test_replay_does_not_log_by_default():
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "001.json"
        p.write_text(_msg(), encoding="utf-8")
        before = _size(DEFAULT_LOG)
        transport = replay([p], _StubAgent(), outstream=io.StringIO())
        assert transport.log_path is None
        assert _size(DEFAULT_LOG) == before


def test_this_test_file_does_not_write_to_the_repo_logs():
    """Run the whole file's worth of transport activity and check the live log."""
    before = _size(DEFAULT_LOG)
    with tempfile.TemporaryDirectory() as tmp:
        for i, msg in enumerate((_msg(), json.dumps({"error": "x"}), _msg())):
            StdioTransport(_StubAgent(), log_path=None).run([msg], io.StringIO())
    assert _size(DEFAULT_LOG) == before


def test_replay_decides_recorded_states_offline():
    with tempfile.TemporaryDirectory() as tmp:
        files = []
        for i, screen in enumerate(("MAP", "REST", "COMBAT")):
            p = Path(tmp) / f"{i:03d}.json"
            p.write_text(_msg({"screen_type": screen}), encoding="utf-8")
            files.append(p)
        agent = _StubAgent()
        out = io.StringIO()
        transport = replay(files, agent, log_path=None, outstream=out)
        assert transport.messages == 3 and transport.commands == 3
        assert [g["screen_type"] for g in agent.seen] == ["MAP", "REST", "COMBAT"]



# --------------------------------------------------------------------------- #
# The main menu: the state that cost an evening (measured 2026-09-21)
# --------------------------------------------------------------------------- #
def test_menu_without_auto_start_stays_silent_but_says_so():
    """A real launch idled here and looked exactly like a dead agent.

    The game sent one menu state (22:28:39), this transport correctly sent
    nothing, and from the outside there was no way to tell that from a process
    that never started at all. The silence stays — the player may want the menu —
    but it must leave a trace on stderr.
    """
    menu = json.dumps({"available_commands": ["start", "state"],
                       "ready_for_command": True, "in_game": False})
    transport = StdioTransport(_StubAgent(), log_path=None)
    err = io.StringIO()
    real, sys.stderr = sys.stderr, err
    try:
        assert transport.handle_message(json.loads(menu)) is None
    finally:
        sys.stderr = real
    assert transport.menu_idle == 1
    assert "--auto-start" in err.getvalue()


def test_menu_with_auto_start_starts_the_run():
    menu = {"available_commands": ["start", "state"], "ready_for_command": True,
            "in_game": False}
    transport = StdioTransport(_StubAgent(), log_path=None, auto_start=True,
                               player_class="IRONCLAD", ascension=0)
    assert transport.handle_message(menu) == "start IRONCLAD 0"


def test_menu_start_is_dropped_when_the_game_does_not_offer_it():
    """Every emitted verb must be advertised, including on the menu path."""
    menu = {"available_commands": ["state"], "ready_for_command": True, "in_game": False}
    transport = StdioTransport(_StubAgent(), log_path=None, auto_start=True)
    line = transport.handle_message(menu)
    assert line == "state"
    assert transport.substitutions[0]["wanted"] == "start IRONCLAD 0"



def test_confirm_and_cancel_are_sent_verbatim_not_aliased():
    """The 18:54 death, pinned.

    GRID screens are two-phase: choose N, then the SAME screen returns offering
    [confirm, cancel, ...] and the game waits to be CONFIRMED. We used to alias
    `confirm`->proceed and `cancel`->return in INTENT_ALIASES (leftovers from
    when "cancel" meant our own intent "leave this screen"); proceed was not
    offered, so SAFE_VERBS degraded the line to `wait 20` and the run idled on a
    screen that had already been answered. Aliases are for words the game does
    not know.
    """
    from spirebrain.driver.stdio import to_command_line, INTENT_ALIASES
    assert "confirm" not in INTENT_ALIASES and "cancel" not in INTENT_ALIASES
    assert to_command_line({"command": "confirm"}) == "confirm"
    assert to_command_line({"command": "cancel"}) == "cancel"


def test_grid_confirm_phase_survives_the_transport_end_to_end():
    """MAP -> GRID(choose) -> GRID(confirm), with the REAL router on the wire.

    Before the fix the third state produced `wait 20`; the acceptance bar is that
    no substitution and no ladder trip happens at all. The router's own
    two-phase logic is covered in test_agent_router.py; this case exists to pin
    the TRANSPORT half of the chain (verb -> line -> offered check).
    """
    from spirebrain.driver.agent import SpireBrainAgent
    with tempfile.TemporaryDirectory() as tmp:
        agent = SpireBrainAgent(jev_backend="mock", log_dir=tmp)
        transport = StdioTransport(agent, log_path=None)
        deck = [{"cost": 1, "name": "Strike", "id": "Strike_R", "type": "ATTACK"},
                {"cost": 1, "name": "Defend", "id": "Defend_R", "type": "SKILL"},
                {"cost": 2, "name": "Bash", "id": "Bash", "type": "ATTACK"}]
        grid = {"screen_type": "GRID", "screen_state": {"cards": deck}}
        steps = [
            ({"screen_type": "MAP", "screen_state": {"next_nodes": [{"symbol": "M"}]}},
             ["choose", "return", "key", "click", "wait", "state"]),
            (grid, ["choose", "return", "key", "click", "wait", "state"]),
            (grid, ["confirm", "cancel", "key", "click", "wait", "state"]),
        ]
        sent = [transport.handle_message({"in_game": True, "ready_for_command": True,
                                          "available_commands": avail,
                                          "game_state": dict(state, act=1, floor=0,
                                                             current_hp=80, max_hp=80,
                                                             gold=99, deck=deck)})
                for state, avail in steps]
        assert sent[1] == "choose 0"
        assert sent[2] == "confirm", sent
        assert transport.substitutions == []
        assert transport.ladder_events == 0


if __name__ == "__main__":
    import traceback

    passed = failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                passed += 1
                print(f"ok   {name}")
            except Exception:
                failed += 1
                print(f"FAIL {name}")
                traceback.print_exc()
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
