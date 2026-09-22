"""Regression: the 20:49 death — one poisoned message must not kill the agent.

The CJK fork's state JSON can carry `\\uD8xx` escapes that json.loads turns into
LONE SURROGATE characters. The first `.encode("utf-8")` on such a string raises
UnicodeEncodeError, and on 2026-09-22 20:49 that killed BOTH agents 11 seconds
into a live session: the panel then said "agent not online" forever, which is
how this bug was found.

Three layers of defence, each pinned here:
  1. `_scrub_surrogates` strips lone surrogates from any parsed message;
  2. `handle_message` isolates per-message failures (traceback to stderr, reply
     `state`, keep breathing) so no single state can end the agent;
  3. `_fingerprint` encodes with errors="replace" as the deep backstop.
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from spirebrain.driver.stdio import StdioTransport, _scrub_surrogates


class _Stub:
    """Decides nothing; only what the transport needs to complete a turn."""

    def decide(self, game_state):
        return {"command": "wait", "frames": 20}

    feed = None
    history = []


def _msg(game_state_extra=None):
    hand = [{"id": "Strike_R"}, {"id": "Defend_R"}]
    if game_state_extra:
        hand = game_state_extra
    return {
        "available_commands": ["play", "end", "choose", "state", "wait"],
        "ready_for_command": True,
        "in_game": True,
        "game_state": {
            "screen_type": "COMBAT", "act": 1, "floor": 3, "turn": 1,
            "current_hp": 70, "max_hp": 80, "gold": 99,
            "deck": [{"id": "Strike_R", "cost": 1, "type": "ATTACK"}],
            "combat": {"player": {"energy": 3, "current_hp": 70},
                       "hand": hand,
                       "monsters": [{"name": "Cultist", "current_hp": 48}]},
        },
    }


def _transport():
    return StdioTransport(_Stub(), log_path=None, mode="advise",
                          advice_path=None, warn_stream=io.StringIO())


def test_scrub_strips_lone_surrogates_anywhere_in_the_tree():
    poisoned = {"a": "ok", "b": "bad\uD841", "c": ["x\uD808", {"d": "\uD999"}]}
    clean = _scrub_surrogates(poisoned)
    # json.dumps is the operation that used to raise; it must now succeed
    blob = json.dumps(clean, ensure_ascii=False)
    assert "\ud841" not in blob
    assert "\ud808" not in blob
    assert "\ud999" not in blob
    assert clean["a"] == "ok"                      # untouched strings untouched
    assert _scrub_surrogates(42) == 42 and _scrub_surrogates(None) is None


def test_a_lone_surrogate_in_a_real_state_does_not_kill_the_agent():
    """The exact 20:49 crash replayed: hand card id ends with a lone surrogate."""
    t = _transport()
    bad_hand = [{"id": "Strike_R\uD841"}, {"id": "Defend_R"}]
    reply = t.handle_message(_msg(bad_hand))
    assert reply in ("wait 20", "state")            # alive and answered
    assert t.errors == 0                            # and not via the error path


def test_any_unexpected_exception_in_one_message_degrades_to_state():
    """Even a bug we never imagined must not end the agent (isolation layer).

    The advise path already degrades router failures to a poll (measured:
    `[advise] the router raised on 'COMBAT'` -> `wait 20`), and the outer
    handle_message layer backs that up. Either way the contract is the same:
    a reply that cannot advance anything, an agent that is still breathing,
    and the next message still flowing.
    """

    class Exploding(_Stub):
        def decide(self, game_state):
            raise RuntimeError("imagine any bug here")

    t = StdioTransport(Exploding(), log_path=None, mode="advise",
                       advice_path=None, warn_stream=io.StringIO())
    reply = t.handle_message(_msg())
    assert reply in ("wait 20", "state")
    # and the NEXT message still flows — the loop is not broken
    reply2 = t.handle_message(_msg())
    assert reply2 in ("wait 20", "state")


def test_fingerprint_survives_a_surrogate_that_slips_through():
    game = {"deck": [{"id": "X\uD841"}]}
    fp1 = StdioTransport._fingerprint(game)
    fp2 = StdioTransport._fingerprint(game)
    assert fp1 == fp2 and len(fp1) == 40            # a hash, not an exception


def test_handle_line_accepts_the_escaped_form_the_game_actually_sends():
    """The wire carries ESCAPE TEXT (seven ASCII chars), not the character.

    Building it with chr(92)+"u..." avoids this very test file growing a real
    surrogate that no editor can save cleanly.
    """
    esc = chr(92) + "ud841"
    line = ('{"available_commands":["state","wait"],"ready_for_command":true,'
            '"in_game":true,"game_state":{"screen_type":"COMBAT","act":1,'
            '"floor":3,"deck":[{"id":"Strike_R' + esc + '"}]}}')
    t = _transport()
    reply = t.handle_line(line)
    assert reply is not None                         # parsed, scrubbed, answered



def test_a_poisoned_line_cannot_kill_the_log_write_either():
    """The 21:31 death: the poison survived message handling and killed the
    agent inside _log, OUTSIDE every guard, after the advice had already
    published — the panel froze on one screen while the player kept playing.

    A log write must never be fatal, whatever it is asked to encode.
    """
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        log = Path(tmp) / "pipe.jsonl"
        t = StdioTransport(_Stub(), log_path=log, mode="advise",
                           advice_path=None, warn_stream=io.StringIO())
        poisoned_raw = '{"game_state":{"relics":["' + chr(0xD841) + '"]}}'
        # direct hit on the layer that died
        t._log(poisoned_raw, "wait 20")
        text = log.read_text(encoding="utf-8", errors="replace")
        assert '"sent": "wait 20"' in text          # the record EXISTS
        assert chr(0xD841) not in text              # and carries no poison


def test_the_run_loop_survives_a_poisoned_end_to_end_line():
    """run() itself may not exit on a bad line — a coach that dies mid-run is
    worse than one that skips a turn."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        log = Path(tmp) / "pipe.jsonl"
        esc = chr(92) + "ud841"
        lines = [
            '{"available_commands":["start","state"],"ready_for_command":true,"in_game":false}',
            '{"available_commands":["state","wait"],"ready_for_command":true,"in_game":true,'
            '"game_state":{"screen_type":"COMBAT","act":1,"floor":3,"deck":[{"id":"X' + esc + '"}]}}',
            '{"available_commands":["state","wait"],"ready_for_command":true,"in_game":true,'
            '"game_state":{"screen_type":"COMBAT","act":1,"floor":4,"deck":[{"id":"Y"}]}}',
        ]
        t = StdioTransport(_Stub(), log_path=log, mode="advise",
                           advice_path=None, warn_stream=io.StringIO())
        out = io.StringIO()
        n = t.run(iter(lines), out)
        replies = [l for l in out.getvalue().splitlines() if l]
        # Ready + one reply per IN-GAME state. The menu state stays silent by
        # design (an advisor never starts a run), so 1 + (len-1) lines.
        assert replies[0] == "Ready"
        assert len(replies) == len(lines)
        assert replies[1:] == ["wait 20"] * (len(lines) - 1)
        assert n >= 0                                   # and returned normally


if __name__ == "__main__":
    for name, fn in sorted({k: v for k, v in globals().items()
                            if k.startswith("test_") and callable(v)}.items()):
        fn()
        print("pass:", name)
    print("ALL PASS")
