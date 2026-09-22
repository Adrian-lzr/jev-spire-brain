"""Advisor mode: the agent recommends, the player chooses.

The project exists to help a human play, not to play for them, so `advise` is the
transport's default mode and these tests pin the one rule that makes it mean
anything: **an advisor never acts.** Every command it sends must be a poll
(`wait`/`state`), on every screen, including the ones where auto-play would have
pressed keys.

The second half is the measurement the project needs to say anything honest about
its own advice: the player's choice is inferred from the difference between two
states, and the inference is only counted when the evidence singles out an
action. Both halves are asserted here — what we say, and whether we can tell what
came back.

Every construction asks for `mode="advise"` explicitly, both because a test that
silently changed meaning with a default would be worse than one that fails, and
because `advice_path=None` is what keeps the journal out of the repo.
"""

from __future__ import annotations

import io
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from spirebrain.driver.stdio import (
    ADVISE_POLL_VERBS,
    DEFAULT_MODE,
    StdioTransport,
)
from spirebrain.driver.witness import MATCH, MISMATCH, UNOBSERVED, PlayerTracker
from spirebrain.overlay.feed import DecisionFeed


class _StubAgent:
    """A router with a fixed answer, so the transport is what is under test."""

    def __init__(self, command: dict | None = None, feed=None) -> None:
        self.command = command or {"command": "choose", "choice": 0}
        self.seen: list[dict] = []
        self.feed = feed
        self.history: list[dict] = []

    def choose_action(self, game):
        self.seen.append(game)
        return dict(self.command)


def _advise(agent, **kwargs) -> StdioTransport:
    kwargs.setdefault("mode", "advise")
    kwargs.setdefault("advice_path", None)
    kwargs.setdefault("log_path", None)
    return StdioTransport(agent, **kwargs)


def _msg(state: dict, available=None, **over) -> dict:
    message = {
        "in_game": True,
        "ready_for_command": True,
        "available_commands": available if available is not None else
        ["play", "end", "choose", "proceed", "return", "wait", "state", "potion"],
        "game_state": state,
    }
    message.update(over)
    return message


ALL_VERBS = ["start", "potion", "play", "end", "choose", "confirm", "cancel",
             "proceed", "return", "key", "click", "wait", "state"]


def _verb(line: str | None) -> str:
    return (line or "").split(" ", 1)[0].strip().lower()


def _combat(hand=("Strike_R", "Defend_R"), energy=3, hp=(48, 44), turn=1):
    return {
        "screen_type": "COMBAT", "act": 1, "floor": 3, "turn": turn,
        "current_hp": 70, "max_hp": 80, "gold": 99,
        "deck": [{"id": "Strike_R"}, {"id": "Defend_R"}, {"id": "Bash"}],
        "combat": {
            "player": {"energy": energy, "current_hp": 70},
            "hand": [{"id": h} for h in hand],
            "monsters": [{"name": "Cultist", "current_hp": hp[0]},
                         {"name": "Jaw Worm", "current_hp": hp[1]}],
        },
    }


# --------------------------------------------------------------------------- #
# The hard rule: an advisor never acts
# --------------------------------------------------------------------------- #
def test_advise_is_the_default_mode():
    """The product decision, pinned: the tool helps you play, it does not play.

    A default that auto-plays is the wrong default for something whose job is to
    make the player better, and the mode is a real choice — so it is asserted,
    not assumed.
    """
    assert DEFAULT_MODE == "advise"
    agent = _StubAgent()
    transport = StdioTransport(agent, log_path=None, advice_path=None)
    assert transport.mode == "advise"
    assert transport.max_commands == 0  # polls are a clock, not a budget


def test_advise_never_sends_an_advancing_verb_on_any_screen():
    """The whole point. MAP, GRID (both phases), EVENT, REST, SHOP, COMBAT.

    Auto-play would send `choose`/`play`/`confirm` on these; advisor mode must
    answer every one of them with a poll, because the player is holding the
    mouse. One advancing verb anywhere in here and the agent has taken a
    decision that was not its to take.
    """
    screens = [
        ({"screen_type": "MAP", "map": {"next_nodes": [{"x": 1, "y": 0, "symbol": "M"}]}},
         None),
        ({"screen_type": "GRID", "screen_state": {"cards": [{"id": "Strike_R"}]}}, None),
        ({"screen_type": "GRID", "screen_state": {"cards": [{"id": "Strike_R"}]}},
         ["confirm", "cancel", "wait", "state"]),
        ({"screen_type": "EVENT", "screen_state": {"body": "e", "options": [{"label": "a"}]}}, None),
        ({"screen_type": "REST", "screen_state": {"rest_options": ["rest", "smith"]}}, None),
        ({"screen_type": "SHOP_SCREEN", "gold": 200, "screen_state": {
            "cards": [{"id": "Bash", "price": 90}], "relics": [], "potions": []}}, None),
        ({"screen_type": "BOSS_REWARD", "screen_state": {"relics": [{"id": "Sozu"}]}}, None),
        (_combat(), None),
    ]
    agent = _StubAgent({"command": "play", "card": 0, "target": 0})  # worst case
    transport = _advise(agent)
    sent = []
    for state, available in screens:
        state = dict(state, act=1, floor=3, current_hp=70, max_hp=80, gold=99,
                     deck=[{"id": "Strike_R"}])
        sent.append(transport.handle_message(_msg(state, available)))
        # Also with every verb the mod has ever advertised: a wider menu must not
        # tempt the advisor into using one of them.
        sent.append(transport.handle_message(_msg(dict(state, floor=4), ALL_VERBS)))

    assert sent, "no commands at all: the advisor must still keep the pipe alive"
    for line in sent:
        assert line is not None
        assert _verb(line) in ADVISE_POLL_VERBS, f"advisor sent {line!r}"
    assert transport.substitutions == [], transport.substitutions


def test_advise_never_climbs_the_unmodeled_ladder():
    """The 18:08 screen, under advice.

    Auto-play answers `[key, click, wait, state]` with keys and clicks, because
    its job is to force the game forward. An advisor's job is not, so it polls —
    and the ladder must never fire, or the agent is pressing buttons in a game
    the player is playing.
    """
    unmodeled = {"screen_type": "NONE", "screen_state": {},
                 "act": 1, "floor": 0, "current_hp": 80, "max_hp": 80}
    agent = _StubAgent()
    transport = _advise(agent)
    sent = [transport.handle_message(
        _msg(unmodeled, ["key", "click", "wait", "state"])) for _ in range(100)]

    assert set(_verb(s) for s in sent) == {"wait"}
    assert transport.ladder_events == 0
    assert transport.ladder_stopped is False
    assert agent.seen == [], "no advice is possible on a screen we cannot read"
    assert transport.stuck_events == 0 and transport.stalled is False


def test_advise_polls_but_asks_the_brain_once_per_state():
    """Polling must not become a per-second JEV bill.

    The player is allowed to think for a minute; the pipe still needs an answer,
    so we poll. Asking the model on every poll would turn a $0.001 decision into
    a running charge against a motionless screen — the same money-burning loop
    the stall guard exists to stop, arriving through a different door.
    """
    state = dict(_combat(), act=1, floor=3, current_hp=70, max_hp=80, gold=99,
                 deck=[{"id": "Strike_R"}])
    agent = _StubAgent({"command": "play", "card": 0, "target": 0})
    transport = _advise(agent)
    for _ in range(40):
        assert transport.handle_message(_msg(state)) == "wait 20"

    assert len(agent.seen) == 1, f"asked the brain {len(agent.seen)}x for one state"
    assert transport.advice_issued == 1


def test_advise_poll_falls_back_to_state_when_wait_is_missing():
    state = {"screen_type": "NONE", "screen_state": {}}
    transport = _advise(_StubAgent())
    assert transport.handle_message(_msg(state, ["state", "key"])) == "state"
    # Neither poll verb offered: silence is the honest answer. An advisor does not
    # press buttons to keep a conversation going.
    assert transport.handle_message(_msg(state, ["key", "click"])) is None


# --------------------------------------------------------------------------- #
# Saying it to the player
# --------------------------------------------------------------------------- #
def test_advise_publishes_a_recommendation_in_chinese():
    state = dict(_combat(), act=1, floor=3, current_hp=70, max_hp=80, gold=99,
                 deck=[{"id": "Strike_R"}])
    feed = DecisionFeed()
    agent = _StubAgent({"command": "play", "card": 0, "target": 0}, feed=feed)
    transport = _advise(agent)
    transport.handle_message(_msg(state))

    advice = [e for e in feed.history() if e["kind"] == "advice"]
    assert len(advice) == 1
    event = advice[0]
    assert event["point"] == "combat"
    assert event["label"] == "出「Strike_R」 → Cultist"
    assert event["agreement"] is None       # nothing judged yet, and it says so
    assert event["tally"][MATCH] == 0


def test_advise_journals_advice_and_verdicts_to_its_own_file():
    with tempfile.TemporaryDirectory() as tmp:
        journal = Path(tmp) / "advice.jsonl"
        state = dict(_combat(), act=1, floor=3, current_hp=70, max_hp=80, gold=99,
                     deck=[{"id": "Strike_R"}])
        agent = _StubAgent({"command": "play", "card": 0, "target": 0})
        transport = _advise(agent, advice_path=journal)
        transport.handle_message(_msg(state))
        transport.handle_message(_msg(dict(state, combat={
            "player": {"energy": 2, "current_hp": 70},
            "hand": [{"id": "Defend_R"}],
            "monsters": [{"name": "Cultist", "current_hp": 42},
                         {"name": "Jaw Worm", "current_hp": 44}],
        })))

        lines = [json.loads(l) for l in journal.read_text(encoding="utf-8").splitlines()]
        # advice -> what the player did -> the next recommendation for the state
        # their action created. All three are the record a later analysis needs.
        assert [l["kind"] for l in lines] == ["advice", "outcome", "advice"]
        assert lines[1]["verdict"] == MATCH
        assert lines[1]["acted_label"] == "出「Strike_R」 → Cultist"
        assert lines[1]["tally"] == {"match": 1, "mismatch": 0, "unobserved": 0}
        assert lines[2]["label"] == "出「Defend_R」 → Cultist"


# --------------------------------------------------------------------------- #
# What the player did
# --------------------------------------------------------------------------- #
def test_advise_records_a_match_when_the_player_follows_it():
    state = dict(_combat(), act=1, floor=3, current_hp=70, max_hp=80, gold=99,
                 deck=[{"id": "Strike_R"}])
    feed = DecisionFeed()
    agent = _StubAgent({"command": "play", "card": 0, "target": 0}, feed=feed)
    transport = _advise(agent)
    transport.handle_message(_msg(state))
    # The player played Strike at the Cultist: the card left the hand, energy
    # went 3 -> 2, and only the Cultist lost HP.
    transport.handle_message(_msg(dict(state, combat={
        "player": {"energy": 2, "current_hp": 70},
        "hand": [{"id": "Defend_R"}],
        "monsters": [{"name": "Cultist", "current_hp": 42},
                     {"name": "Jaw Worm", "current_hp": 44}],
    })))

    outcomes = [e for e in feed.history() if e["kind"] == "outcome"]
    assert len(outcomes) == 1
    assert outcomes[0]["verdict"] == MATCH
    assert outcomes[0]["acted_label"] == "出「Strike_R」 → Cultist"
    assert outcomes[0]["evidence"]["hand_lost"] == {"Strike_R": 1}
    assert transport.tracker.agreement == 1.0


def test_advise_records_a_mismatch_when_the_player_plays_something_else():
    state = dict(_combat(), act=1, floor=3, current_hp=70, max_hp=80, gold=99,
                 deck=[{"id": "Strike_R"}])
    agent = _StubAgent({"command": "play", "card": 0, "target": 0})  # Strike
    transport = _advise(agent)
    transport.handle_message(_msg(state))
    # The player played the DEFEND instead: a different card left the hand.
    transport.handle_message(_msg(dict(state, combat={
        "player": {"energy": 2, "current_hp": 70},
        "hand": [{"id": "Strike_R"}],
        "monsters": [{"name": "Cultist", "current_hp": 48},
                     {"name": "Jaw Worm", "current_hp": 44}],
    })))

    outcome = transport.tracker.history[-1]
    assert outcome.verdict == MISMATCH
    assert outcome.acted_label == "出「Defend_R」"
    assert transport.tracker.agreement == 0.0
    assert transport.tracker.judged == 1


def test_advise_keeps_waiting_instead_of_scoring_a_player_who_has_not_moved():
    """A poll is not an answer. Neither is a state that changed for animation.

    The expensive mistake this prevents: counting "no action yet" as a mismatch.
    An advisor whose measured agreement drops because the player took three
    seconds to think is measuring its own impatience.
    """
    state = dict(_combat(), act=1, floor=3, current_hp=70, max_hp=80, gold=99,
                 deck=[{"id": "Strike_R"}])
    transport = _advise(_StubAgent({"command": "play", "card": 0, "target": 0}))
    transport.handle_message(_msg(state))
    for _ in range(10):
        transport.handle_message(_msg(state))          # identical re-transmission

    assert transport.tracker.judged == 0
    assert transport.tracker.tally[UNOBSERVED] == 0
    assert transport.tracker.pending is not None


def test_advise_marks_a_screen_change_it_cannot_read_as_unobserved():
    """Honest accounting: the question ended, we never saw the answer."""
    state = dict(_combat(), act=1, floor=3, current_hp=70, max_hp=80, gold=99,
                 deck=[{"id": "Strike_R"}])
    transport = _advise(_StubAgent({"command": "play", "card": 0, "target": 0}))
    transport.handle_message(_msg(state))
    # Straight to a card reward with the deck untouched: the fight ended but no
    # play is visible in this transition, so there is nothing to score.
    transport.handle_message(_msg({"screen_type": "CARD_REWARD",
                                   "screen_state": {"cards": [{"id": "Bash"}]},
                                   "deck": [{"id": "Strike_R"}]}))

    assert transport.tracker.tally[UNOBSERVED] == 1
    assert transport.tracker.judged == 0
    assert transport.tracker.agreement is None


def test_advise_survives_a_router_that_raises():
    """Advice is a side channel: a broken recommendation must not kill the pipe."""
    class _Boom:
        history: list = []

        def choose_action(self, game):
            raise RuntimeError("jev exploded")

    err = io.StringIO()
    transport = _advise(_Boom(), warn_stream=err)
    line = transport.handle_message(_msg({"screen_type": "MAP", "screen_state": {}}))
    assert line == "wait 20"
    assert "jev exploded" in err.getvalue()


# --------------------------------------------------------------------------- #
# The tracker, in isolation
# --------------------------------------------------------------------------- #
def test_tracker_reports_no_agreement_rather_than_zero():
    """`None` and `0.0` are different facts and must not render the same."""
    tracker = PlayerTracker()
    assert tracker.agreement is None
    assert tracker.judged == 0


def test_unknown_evidence_never_manufactures_a_mismatch():
    """`None` in a key means "unknown", not "different".

    The player played the right card at a target we could not identify (two
    monsters lost HP at once). Scoring that as a mismatch would punish the
    player for our blindness — so the target slot compares equal to anything.
    """
    prev = _combat()
    cur = _combat(hand=("Defend_R",), energy=2, hp=(42, 38))
    tracker = PlayerTracker()
    tracker.remember_state(prev)
    from spirebrain.driver.witness import Advice, advice_key
    command = {"command": "play", "card": 0, "target": 0}
    tracker.note(Advice(point="combat", screen="COMBAT", command=command,
                        key=advice_key(prev, command)))
    outcome = tracker.resolve(cur)
    assert outcome.verdict == MATCH
    assert outcome.evidence.get("ambiguous") == "several monsters lost HP"


def test_a_restated_advice_is_not_a_superseded_one():
    """Polling re-states the same advice; that is not a new question."""
    from spirebrain.driver.witness import Advice
    tracker = PlayerTracker()
    advice = Advice(point="combat", screen="COMBAT", command={"command": "end"},
                    key=("end",))
    for _ in range(20):
        tracker.note(advice)
    assert tracker.superseded == 0

    tracker.note(Advice(point="combat", screen="COMBAT",
                        command={"command": "play", "card": 1},
                        key=("play", "defend_r", None)))
    assert tracker.superseded == 1


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
