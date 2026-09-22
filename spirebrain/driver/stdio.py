"""CommunicationMod stdio transport — the entry point the game actually launches.

`agent.py` decides *what* to do; this module is *how it is said*. Between them
they are the whole Phase 1 integration, and until now only the first half
existed (`agent.py` had no `__main__` at all, so CommunicationMod had nothing to
spawn).

Verified against the CommunicationMod README (fetched 2026-09-21). The four
details that are easy to get wrong, all now implemented, all confirmed in that
document:

1. **Send `Ready\\n` first.** "Make sure your process sends "Ready\\n" to stdout
   when it is ready to receive commands." Without it the game hangs for ten
   seconds and then the process quits — the mod's own FAQ entry.
2. **PLAY is 1-indexed.** "CardIndex is 1-indexed to match up with the card
   numbers in game." CHOOSE is 0-indexed. The agent stays 0-indexed throughout;
   the +1 happens in `to_command_line()` and nowhere else.
3. **There is no `skip` verb.** Skipping a card reward and leaving a shop are
   both `RETURN`, which the README defines as "Equivalent to SKIP, CANCEL, and
   LEAVE". `PURGE` and `SMITH` do not exist either — every screen choice is
   `CHOOSE`.
4. **Errors arrive as a message too**: `{"error": "...", "ready_for_command":
   True}`. Silently ignoring one stalls the pipe, because the game is waiting for
   input; we answer with `STATE`, which the README says is "Always available".

Also encoded here: **nothing prints to stdout except `Ready` and commands.**
stdout is captured into the game log; a stray print corrupts the stream, and the
README points users at a log file for exactly this reason.

Anything that still cannot be confirmed offline is flagged `PHASE 1 VERIFY`.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Iterable

from spirebrain.driver.witness import (
    SCREEN_POINT,
    Advice,
    PlayerTracker,
    advice_key,
    label_for,
)
from spirebrain.overlay.feed import advice_event, outcome_event

ROOT = Path(__file__).resolve().parents[2]

READY = "Ready"

# Three runaway guards, ported from Ethics03/jevspire (2026-09-22) — the only
# other CommunicationMod+JEV project, and all three of its protections exist
# because the author was burned by the same failure modes we would have been:
#
# 1. STALL GUARD (fingerprint stop): if the *same* game state comes back
#    playable after we already sent a command for it, our command did not take
#    effect. Retry-asking JEV against an unchanged state burns API money in a
#    loop; jespire latches after 2 repeats and waits for a human or a new state.
# 2. ACTION LIMIT: a process-level cap on commands (default 200, env
#    JEVBRAIN_MAX_ACTIONS). A confused agent that "plays" 5000 cards is a
#    runaway, and the cap is what turns that into a visible stop.
# 3. NAVIGATION WITHOUT THE MODEL lives in agent.py (non-decision screens get
#    Proceed, shops are asked once per floor) — listed here so the guard names
#    stay documented in one place.
# 4. UNMODELED-SCREEN LADDER (own design, 2026-09-22 evening): a screen whose
#    available_commands offer no advancing verb cannot be acted on at all.
#    Waiting forever there is how the 18:08 run burned 991 waits and died on
#    the action cap ("agent not reachable"). The ladder: two waits, a centre
#    click, SPACE, then a loud stop — with auto-resume the moment a modeled
#    screen returns.
# 1000 -> 5000 (user call, 2026-09-22 evening). Two changes the same night
# reshaped what the cap protects. (1) The budget is now PER RUN, not per
# process — _reset_run_budget() fires on every menu->in_game edge, because a
# player restarting a run in-game was spending one shared counter (the third
# run of the 18:08 session died after ~50 commands; that is why 1000 "felt
# small"). (2) The ladder removed the only loop that ever reached the cap, so
# the cap is back to pure backstop duty and can be generous.
DEFAULT_MAX_ACTIONS = 5000
DEFAULT_STALL_LIMIT = 2

# Verbs that cannot advance a run by themselves. Sending one is not "acting on
# a state", so it must not arm the stall guard: `state` re-transmits on purpose
# and `wait` waits on purpose — the state coming back unchanged is what they
# are *for*.
NON_ADVANCING_VERBS = frozenset({"state", "wait"})

# Bare `wait` is not a command, it is an error: the game rejects it with
# `Argument missing in command "wait".` (measured twice on 2026-09-22 — the
# second time because the SAFE_VERBS substitution below bypassed
# to_command_line and sent the verb name as the whole line). Every `wait`,
# wherever it originates, goes out with a frame count: 20 frames is about a
# third of a second, enough for a screen transition, invisible to a human.
DEFAULT_WAIT_FRAMES = 20

# Sentinel for "use the default log path". Distinct from None, because None must
# mean *no logging at all* — tests pass None, and when None silently meant "write
# to the repo's default file" the test suite appended 44 KB of synthetic game
# states to `logs/pipe.jsonl`. That file is the record of what the real game sent
# us, so contaminating it makes a live smoke test unreadable: the fake states were
# already being mistaken for a working pipe. Found 2026-09-21, before that
# misinterpretation reached anyone else.
DEFAULT_LOG = object()

# The complete verb set from the CommunicationMod README. Our router is not
# allowed to invent words: an unknown verb is ignored by the game, which from
# this side is indistinguishable from the pipe having died.
# CONFIRM/CANCEL joined on 2026-09-22 evening, from the live pipe: after a
# GRID selection (Neow's card removal) the same screen comes back offering
# [confirm, cancel, ...] — the mod's two-step select-then-finalize dance,
# which this set's "complete" claim had quietly omitted. Every run death
# that night started on exactly that screen.
PROTOCOL_VERBS = frozenset({
    "start", "potion", "play", "end", "choose", "confirm", "cancel",
    "proceed", "return", "key", "click", "wait", "state",
})

# Our internal intent -> protocol verb. `skip` and `leave` are ours; the game
# only knows RETURN.
#
# CONFIRM/CANCEL were aliased here until 2026-09-22 evening (to proceed/return),
# from back when "cancel" meant *our* intent "leave this screen". Once they
# turned out to be real protocol verbs — the GRID screen after a selection
# offers exactly [confirm, cancel, ...] — the alias became a silent lie: the
# router said `confirm`, this map rewrote it to `proceed`, proceed was not
# offered, SAFE_VERBS fell through to `wait`, and the game sat on a screen it
# was waiting to be CONFIRMED on. Aliases are for words the game does not know;
# never for words it does.
INTENT_ALIASES = {
    "skip": "return",
    "leave": "return",
    "purge": "choose",   # card-removal is one of the shop's choices
    "smith": "choose",   # upgrading is one of the rest site's choices
    "rest": "choose",
    "buy": "choose",
}

# If our chosen verb is not in `available_commands`, prefer these, in order.
# STATE is last because it re-transmits without advancing anything — correct
# when we are confused, but it does not move the run forward.
SAFE_VERBS = ("proceed", "return", "wait", "state")

# Verbs the ROUTER can emit that actually move a run forward. A screen whose
# available_commands contain none of these is UNMODELED: CommunicationMod has
# no API for whatever the game is showing. `key`/`click` are deliberately NOT
# in this set: the router never emits them — the only code that does is the
# unmodeled-screen ladder below, under guard #4's own budget.
# `wait`/`state` cannot advance anything. `confirm`/`cancel` belong here: the
# 18:54 run died on a GRID screen that offered exactly [confirm, cancel, key,
# click, wait, state] and the router had no word for either — the ladder
# clicked blindly while the game waited for `confirm`.
ADVANCING_VERBS = frozenset({"start", "play", "end", "choose", "confirm",
                             "cancel", "proceed", "return", "potion"})

# The ladder for unmodeled screens: one cycle is this exact rung sequence,
# tried on the same screen fingerprint. The game may ignore keys during an
# animation, so the cycle repeats; after LADDER_CYCLES_BEFORE_STOP full
# cycles, stop loudly instead of cycling forever.
LADDER_CYCLE = ("wait", "wait", "SPACE", "ESCAPE", "click")
LADDER_WAITS_BEFORE_KEY = 2   # kept for tests/documentation of intent
# Trip the loud-stop after this many full ladder cycles on the same unmodeled
# screen fingerprint: 5 rungs x 3 cycles is ~15 messages - far under any cap,
# and if none of that advanced the game, only a human can.
LADDER_CYCLES_BEFORE_STOP = 3

# --------------------------------------------------------------------------- #
# Modes
# --------------------------------------------------------------------------- #
# `advise` — the DEFAULT — recommends to the player and never acts. `play` is the
# auto-player the offline measurements needed. The default is `advise` because
# this tool exists to make a human play better: a helper that silently plays the
# game for you is the wrong default for that job, and it is the difference
# between a coach and a bot.
MODES = ("advise", "play")
DEFAULT_MODE = "advise"

# In advise mode the ONLY commands we ever send are polls: `wait` re-evaluates
# after N frames, `state` re-transmits. Both are non-advancing by definition, so
# the mouse and the keyboard stay the player's. `proceed`/`return` are
# deliberately absent even though SAFE_VERBS contains them — they move the run.
ADVISE_POLL_VERBS = ("wait", "state")

# Poll cadence in frames. 20 is ~1/3 second: fast enough that a recommendation
# lands while the decision is still live, and since it is a poll and not an
# action, being early costs a re-transmitted state, not a card played.
DEFAULT_POLL_FRAMES = 20


def to_command_line(command: dict) -> str:
    """Turn the agent's command dict into the protocol's one-line form.

    Note what is *absent*: there is no free-text path into the game. Every
    command is a verb plus integer indices the tactical layer computed — the same
    discipline as the JEV calls themselves, where the model supplies judgements
    and the code supplies anything that must be exact.
    """
    verb = str(command.get("command", "")).strip().lower()
    if not verb:
        raise ValueError(f"command dict has no 'command' key: {command!r}")
    verb = INTENT_ALIASES.get(verb, verb)

    if verb == "play":
        card = command.get("card", command.get("card_index"))
        if card is None:
            raise ValueError("play needs 'card'")
        # The one place 0-indexed becomes 1-indexed. See module docstring (2).
        target = command.get("target", command.get("target_index"))
        line = f"play {int(card) + 1}"
        return line + (f" {int(target)}" if target is not None else "")

    if verb == "choose":
        # CHOOSE takes an index OR a name; names come from the game state and
        # avoid our label->index mapping entirely when they are available.
        if command.get("name"):
            return f"choose {command['name']}"
        idx = command.get("choice", command.get("index"))
        if idx is None:
            raise ValueError("choose needs 'choice' or 'name'")
        return f"choose {int(idx)}"

    if verb == "potion":
        action = str(command.get("action", "use")).lower()
        slot = command.get("slot", command.get("choice"))
        if slot is None:
            raise ValueError("potion needs 'slot'")
        target = command.get("target")
        return f"potion {action} {int(slot)}" + (
            f" {int(target)}" if target is not None else "")

    if verb == "wait":
        frames = command.get("frames", command.get("ms"))
        return f"wait {int(frames) if frames is not None else DEFAULT_WAIT_FRAMES}"

    if verb == "start":
        # START PlayerClass [AscensionLevel] [Seed] — class is required, and the
        # order is NOT (seed, difficulty, ascension); getting it wrong silently
        # starts a different game.
        klass = command.get("player_class", command.get("class"))
        if not klass:
            raise ValueError("start needs 'player_class'")
        parts = [str(klass)]
        if command.get("ascension") is not None:
            parts.append(str(int(command["ascension"])))
            if command.get("seed") is not None:
                parts.append(str(command["seed"]))
        return " ".join(["start", *parts])

    if verb == "confirm":
        # CONFIRM/CANCEL: the second half of a grid selection. `choose N` puts
        # the card in the pick slot (measured 2026-09-22: after `choose 0` on
        # Neow's removal grid, the same screen comes back offering confirm);
        # CONFIRM finalizes it. Without this word the agent was mute on the
        # exact screen every run of that night died on.
        return "confirm"
    if verb == "cancel":
        return "cancel"

    if verb == "key":
        keyname = command.get("key", command.get("keyname"))
        if not keyname:
            raise ValueError("key needs 'key'")
        timeout = command.get("timeout")
        return f"key {keyname}" + (f" {int(timeout)}" if timeout is not None else "")

    return verb  # end / proceed / return / state / click


def _verb_of(line: str) -> str:
    return line.split(" ", 1)[0].strip().lower()


def _screen_of(game: Any) -> str:
    """The screen's type, upper-cased, or "" — never raises on a hostile state."""
    try:
        return str((game or {}).get("screen_type", "")).upper()
    except Exception:  # noqa: BLE001
        return ""


def _advice_line(command: dict) -> str:
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


class StdioTransport:
    """Reads game messages, asks the agent, writes one command back."""

    def __init__(self, agent, *, log_path: str | Path | None | object = DEFAULT_LOG,
                 auto_start: bool = False, player_class: str = "IRONCLAD",
                 ascension: int = 0, max_commands: int | None = None,
                 stall_limit: int = DEFAULT_STALL_LIMIT,
                 mode: str = DEFAULT_MODE, poll_frames: int = DEFAULT_POLL_FRAMES,
                 advice_path: str | Path | None | object = DEFAULT_LOG,
                 warn_stream=None) -> None:
        self.agent = agent
        # None disables logging; omitting the argument uses the default path. See
        # DEFAULT_LOG for why those cannot be the same thing.
        if log_path is DEFAULT_LOG:
            self.log_path: Path | None = ROOT / "logs" / "pipe.jsonl"
        else:
            self.log_path = Path(log_path) if log_path else None
        # The advice stream gets its own file. `pipe.jsonl` is the record of what
        # the game sent us (one line per message, including every poll); advice
        # and verdicts are the *other* half of the conversation — what we said
        # and what the player did — and mixing them makes both harder to read.
        if advice_path is DEFAULT_LOG:
            self.advice_path: Path | None = ROOT / "logs" / "advice.jsonl"
        else:
            self.advice_path = Path(advice_path) if advice_path else None
        self.mode = mode if mode in MODES else DEFAULT_MODE
        self.poll_frames = max(1, int(poll_frames))
        self.auto_start = auto_start
        self.player_class = player_class
        self.ascension = ascension
        # Action limit (guard #2). An explicit argument wins; otherwise the env
        # var, then the default. 0 or a negative value means unlimited — for a
        # long soak test, not for a stranger's first run.
        #
        # In advise mode the default is UNLIMITED, and that is not a loosening of
        # the runaway rule: every command we send there is a poll that cannot
        # change the game, so "commands sent" is a clock, not a budget. Counting
        # a clock against a cap would silence the advisor mid-run for no reason —
        # the failure the cap exists to prevent (an agent that plays 5000 cards
        # in a loop) cannot happen on a verb set of {wait, state}.
        if max_commands is None:
            env = os.environ.get("JEVBRAIN_MAX_ACTIONS", "").strip()
            if env.lstrip("-").isdigit():
                max_commands = int(env)
            else:
                max_commands = 0 if self.mode == "advise" else DEFAULT_MAX_ACTIONS
        self.max_commands = max_commands
        self.stall_limit = max(1, stall_limit)
        self.messages = 0
        self.commands = 0
        self.errors = 0
        self.skipped_not_ready = 0
        self.menu_idle = 0
        self.stalled = False          # latched by the stall guard, cleared on a new state
        self.stuck_events = 0         # how many times the stall guard fired
        self.action_limit_hit = False
        self.ladder_stopped = False   # latched by the unmodeled-screen ladder
        self.ladder_events = 0        # how many times the ladder stopped for a human
        self.substitutions: list[dict] = []
        # Guard warnings go here (default stderr). Injectable because stderr is
        # how the tests check that a guard *said something* when it fired.
        self.warn_stream = warn_stream if warn_stream is not None else sys.stderr
        self._acted_fingerprint: str | None = None
        self._stuck_seen = 0
        # Guard #4 (unmodeled-screen ladder) state: fingerprint of the screen
        # we are climbing on, and the rung index within the ladder cycle.
        self._ladder_fp: str | None = None
        self._ladder_step = 0
        self._in_game = False
        # Advisor mode (mode == "advise"): the player chooses, we recommend, and
        # `PlayerTracker` turns the next state change into a verdict on what they
        # did. `_advised_fp` is what keeps polling from becoming a per-second
        # JEV bill — the brain is asked once per *distinct* state, not once per
        # poll, and a poll re-transmits the same state by design.
        self.tracker = PlayerTracker()
        self._advised_fp: str | None = None
        self.advice_issued = 0

    def _reset_run_budget(self) -> None:
        """Per-run action budget: a fresh run inside the same process starts
        with a full counter. The 18:08 session's third run died ~50 commands
        in because the first two runs had already spent the process-wide cap —
        the player read that as "1000 is too small"; it was really "budget
        never resets"."""
        self.commands = 0
        self.action_limit_hit = False
        # A new run invalidates any advice pending from the old one: the state it
        # was made on is gone, and adjudicating it against the new run's first
        # state would score a match/mismatch that never happened. The tally
        # itself survives — it is the project's metric, measured across runs.
        self.tracker.pending = None
        self.tracker.remember_state(None)
        self._advised_fp = None

    def _ladder_command(self, fp: str) -> str:
        """Next rung of the unmodeled-screen ladder, tracking cycles.

        The full cycle runs LADDER_CYCLES_BEFORE_STOP times; the message AFTER
        the last rung stops loudly (once - later silent messages change
        nothing, so the announcement does not repeat and ladder_events does
        not inflate). `fp` is the SCREEN signature (screen_type + command
        list), not the full state hash - animations mutate the state every
        few messages, and a full-state fingerprint resets the ladder mid-
        climb forever, which is how the 18:54 run burned 5008 waits.
        """
        if fp != self._ladder_fp:
            self._ladder_fp = fp
            self._ladder_step = 0
        total_rungs = len(LADDER_CYCLE) * LADDER_CYCLES_BEFORE_STOP
        if self._ladder_step >= total_rungs:
            if not self.ladder_stopped:
                self.ladder_stopped = True
                self.ladder_events += 1
                self._announce_unmodeled(fp)
            return None  # stay silent; a new modeled screen auto-resumes
        rung = LADDER_CYCLE[self._ladder_step % len(LADDER_CYCLE)]
        self._ladder_step += 1
        if rung == "wait":
            return f"wait {DEFAULT_WAIT_FRAMES}"
        if rung == "click":
            # Centre of the game's native 1920x1080. The live payload does not
            # carry dimensions; correct here on first live contact if it misses.
            return "click 960 540"
        return f"key {rung}"

    def _announce_unmodeled(self, fp: str) -> None:
        # ASCII only - see _announce_stall for the codepage reason.
        print(
            f"[stdio] UNMODELED SCREEN: {LADDER_CYCLES_BEFORE_STOP} ladder cycles "
            f"(waits, click, SPACE, ESCAPE) did not advance this screen "
            f"(fingerprint {fp[:12]}). The mod offers no command for what the "
            f"game is showing - look at the game window and act by hand; the "
            f"agent resumes by itself when a known screen returns.",
            file=self.warn_stream, flush=True)
        feed = getattr(self.agent, "feed", None)
        if feed is not None:
            try:
                feed.publish("run_end", {
                    "summary": "Unmodeled screen: clicks and keys did not "
                               "advance it; pausing auto-decisions until a "
                               "known screen returns.",
                    "reason": "unmodeled_screen",
                    "fingerprint": fp,
                })
            except Exception:  # noqa: BLE001 - a dead dashboard must not break the pipe
                pass

    @staticmethod
    def _fingerprint(game: dict) -> str:
        """Stable hash of exactly the state decide() will see.

        sort_keys so dict ordering cannot fake a change; default=str so one odd
        value degrades to its repr instead of raising inside the hot loop.
        """
        try:
            blob = json.dumps(game, sort_keys=True, default=str, ensure_ascii=False)
        except (TypeError, ValueError):
            blob = repr(game)
        return hashlib.sha1(blob.encode("utf-8")).hexdigest()

    @staticmethod
    def _screen_signature(game: dict) -> str:
        """What KIND of screen this is, ignoring its churning internals.

        The ladder counts its rungs against THIS, not against the full state
        fingerprint: live screens mutate every few messages (uuids, animation
        counters, hp floats), and a full-hash ladder restarts mid-climb
        forever - the 18:54 run spent 5008 waits that way. Two fields that do
        not churn are enough to know we are on the same stuck screen.
        """
        try:
            screen = str(game.get("screen_type", "?"))
        except Exception:  # noqa: BLE001 - a hostile state degrades, never raises
            screen = "?"
        return screen

    # -- one message ------------------------------------------------------- #
    def handle_message(self, message: dict) -> str | None:
        """Return the command line to send, or None to stay silent."""
        self.messages += 1

        if message.get("error"):
            # The game is waiting for input after an error; silence would stall
            # it. STATE is documented as always available.
            self.errors += 1
            return "state"

        if not message.get("in_game", True):
            # Not in a run. Starting one is a *choice*, not a default: the player
            # may be sitting in the menu on purpose, and this agent is meant to
            # be watched while it plays.
            self.menu_idle += 1
            self._in_game = False
            if self.mode == "advise" or not self.auto_start:
                if self.menu_idle == 1:
                    # Say so out loud, once. Measured 2026-09-21: a real launch with
                    # auto_start off looks *exactly like a dead agent* from the
                    # outside — the game sent one menu state (22:28:39), we stayed
                    # silent by design, and nothing else happened for a minute until
                    # the player quit. Silence in the pipe is indistinguishable from
                    # the absence of a process unless it is recorded somewhere.
                    if self.mode == "advise":
                        # `--auto-start` is ignored here rather than honoured: which
                        # class, which ascension, which seed is the most personal
                        # decision in the run, and "advise" means the player makes
                        # the decisions. Advice starts with the run's first screen.
                        print("[stdio] advisor mode at the main menu: waiting for YOU "
                              "to start a run (class/ascension/seed are your calls). "
                              "Advice begins on the run's first screen.",
                              file=sys.stderr, flush=True)
                    else:
                        print("[stdio] at the main menu and --auto-start is off: staying "
                              "silent so the player keeps control. Start a run in-game, or "
                              "pass --auto-start and the agent starts one itself.",
                              file=sys.stderr, flush=True)
                return None
            return self._ensure_offered(
                to_command_line({"command": "start", "player_class": self.player_class,
                                 "ascension": self.ascension}),
                message.get("available_commands"))

        if not self._in_game:
            # Menu -> in_game edge: a new run has begun inside this process.
            # Give the action budget back — a restarted run is a new run, not
            # the tail of the old one (the third run of 18:08 died ~50 commands
            # in, spending its predecessors' debt).
            self._in_game = True
            self._reset_run_budget()
            self._ladder_fp = None
            self.ladder_stopped = False

        if not message.get("ready_for_command", True):
            # Absence of the flag is treated as "ready" rather than stalling
            # forever. PHASE 1 VERIFY: confirm no mid-animation states get
            # answered when the flag is present and false.
            self.skipped_not_ready += 1
            return None

        game = message.get("game_state")
        if not isinstance(game, dict):
            # A ready message with no state we can read: ask for one rather than
            # guess. PHASE 1 VERIFY: check whether this occurs in practice.
            return "state"

        available = message.get("available_commands")
        fp = self._fingerprint(game)
        modeled = isinstance(available, list) and any(
            str(a).strip().lower() in ADVANCING_VERBS for a in available)
        if self.mode == "advise":
            # Advisor mode returns HERE, before the ladder and the stall guard,
            # because both of those are auto-play tools. The ladder exists to
            # force a stuck screen forward by pressing keys — the one thing an
            # advisor must never do — and the stall guard pauses auto-decisions
            # that do not exist. Neither has anything to protect here.
            return self._advise(game, available, modeled)
        if not modeled:
            # Guard #4 (unmodeled-screen ladder): the mod offers no verb that
            # can advance anything. Waiting here forever was the 18:08 death —
            # 991 waits in 66 seconds. Climb the ladder instead. The ladder's
            # rungs count against the SCREEN signature, not the state hash:
            # animations churn the state, and a churning hash restarts the
            # ladder forever (the 18:54 death, 5008 waits).
            return self._ladder_command(self._screen_signature(game))
        if self.stalled:
            if fp != self._acted_fingerprint:
                # A state we have never acted on: whatever stuck us is gone
                # (a human pressed on, or an animation resolved). Resume.
                self.stalled = False
                self._stuck_seen = 0
            else:
                return None  # keep the seat warm; spend nothing on retries

        if fp == self._acted_fingerprint:
            # Guard #1 (jespire): we already sent an advancing command for
            # exactly this state and it came back playable, byte-identical.
            # Once is animation noise; stall_limit times is a command the game
            # ignored, and retrying is how an agent burns money in a loop.
            self._stuck_seen += 1
            if self._stuck_seen >= self.stall_limit:
                self.stalled = True
                self.stuck_events += 1
                self._announce_stall(fp)
                return None
        else:
            self._stuck_seen = 0

        # Grid screens are two-phase (choose -> confirm), and the router needs
        # the offered verbs to know which phase it is in. `available_commands`
        # lives on the message, not the game_state, so pass it down: the agent
        # reads it defensively and other screens ignore it.
        game = dict(game)
        game["available_commands"] = available
        line = to_command_line(self.agent.choose_action(game))
        line = self._ensure_offered(line, available)
        self.commands += 1
        if line and _verb_of(line) not in NON_ADVANCING_VERBS:
            # Only an advancing command counts as "acted on this state".
            # state/wait cannot change anything, so arming the guard with them
            # would fire on the first legitimate re-transmission. Note what is
            # deliberately NOT here: resetting _stuck_seen. The repeat counter
            # spans successive commands against the same state — two commands
            # that both failed to move the game are exactly the runaway this
            # guard exists to stop. Only a genuinely new state clears it.
            self._acted_fingerprint = fp
        # A modeled screen is back: whatever the ladder was stuck on is gone.
        self._ladder_fp = None
        self.ladder_stopped = False
        return line

    def _announce_stall(self, fp: str) -> None:
        # ASCII only: CommunicationMod writes stderr into
        # communication_mod_errors.log via the platform codepage, and an em
        # dash arrives there as mojibake (measured 2026-09-22).
        print(
            f"[stdio] STALL GUARD: the same game state came back {self._stuck_seen}x "
            f"after we acted on it - the last command did not take effect. "
            f"Auto-decisions are paused for this state; play on manually or let a "
            f"new state arrive to resume. (fingerprint {fp[:12]})",
            file=self.warn_stream, flush=True)
        feed = getattr(self.agent, "feed", None)
        if feed is not None:
            try:
                feed.publish("run_end", {
                    "summary": f"STALL GUARD: state repeated {self._stuck_seen}x "
                               "after our command; auto-decisions paused until the state changes.",
                    "reason": "stall_guard",
                    "fingerprint": fp,
                })
            except Exception:  # noqa: BLE001 - a dead dashboard must not break the pipe
                pass

    def _ensure_offered(self, line: str, available: Any,
                        safe: tuple[str, ...] = SAFE_VERBS) -> str:
        """Never send a verb the game did not advertise.

        The substitute is built as a full command line, not echoed as a verb
        name: `wait` needs its frame argument, and echoing the bare verb here
        is exactly the bug that burned 995 errors in the 2026-09-22 live run —
        the substitution path bypassed to_command_line, where the default
        lives.

        `safe` is the substitution ladder and it is caller-chosen: auto-play may
        fall back to `proceed`, an advisor may not. Same function, different
        definition of "harmless".
        """
        if not available:
            return line  # build does not advertise; trust the agent
        offered = {str(a).strip().lower() for a in available}
        verb = _verb_of(line)
        if verb in offered:
            return line
        for candidate in safe:
            if candidate in offered:
                sent = (f"wait {DEFAULT_WAIT_FRAMES}" if candidate == "wait"
                        else candidate)
                self.substitutions.append({"wanted": line, "sent": sent})
                return sent
        self.substitutions.append({"wanted": line, "sent": ""})
        return ""

    # -- advisor mode ------------------------------------------------------ #
    def _advise(self, game: dict, available: Any, modeled: bool) -> str | None:
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
        if fp != self._advised_fp:
            self._advised_fp = fp
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
            print(f"[advise] the router raised on {_screen_of(game)!r}: {exc!r}",
                  file=self.warn_stream, flush=True)
            return

        screen = _screen_of(game)
        point = SCREEN_POINT.get(screen, "navigation")
        confidence, fallback = self._last_confidence(command)
        advice = Advice(
            point=point, screen=screen, command=command,
            key=advice_key(payload, command, point),
            label=label_for(payload, command, point),
            reason=str(command.get("reason", "") or ""),
            confidence=confidence, fallback=fallback,
            act=int(game.get("act", 0) or 0), floor=int(game.get("floor", 0) or 0),
            message=self.messages,
        )
        self.tracker.remember_state(game)
        self.tracker.note(advice)
        self.advice_issued += 1
        self._publish_advice(advice)
        # One ASCII line on stderr: the live console and the mod's error log have
        # no reliable encoding for the Chinese label, so the sentence the player
        # reads lives in the advice event and this line only proves liveness.
        print(f"[advise] {point} -> {_advice_line(command)}"
              f" (conf {confidence:.2f}, msg {self.messages})",
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

    def _last_confidence(self, command: dict) -> tuple[float, bool]:
        """The confidence of the decision that produced `command`, if it was one.

        Screens answered by navigation rules (non-decision screens, a grid with
        no pending intent) never went through `_record`, so there is no number to
        report. 0.0 means "rules, not a judgement" — which the dashboard renders
        as 规则 rather than as a 0% certainty, because those are different facts.
        """
        history = getattr(self.agent, "history", None) or []
        if history and history[-1].get("command") == command:
            return float(history[-1].get("confidence") or 0.0), bool(history[-1].get("fallback"))
        return 0.0, False

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
              f"{_advice_line(outcome.advice.command)} / player did "
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

    # -- the stream -------------------------------------------------------- #
    def handle_line(self, line: str) -> str | None:
        """Parse one raw line and decide. Non-JSON lines are ignored, not fatal.

        CommunicationMod prints human-readable notices on the same stream, so a
        parser that assumed "every line is JSON" would die on the first greeting.
        """
        text = line.strip()
        if not text or not text.startswith("{"):
            return None
        try:
            message = json.loads(text)
        except json.JSONDecodeError:
            return None
        if not isinstance(message, dict):
            return None
        return self.handle_message(message)

    def run(self, instream: Iterable[str], outstream, *, send_ready: bool = True) -> int:
        """The real loop. Returns the number of commands sent."""
        if send_ready:
            # Point (1) of the module docstring. Without this the game waits ten
            # seconds and then kills us.
            outstream.write(READY + "\n")
            outstream.flush()
        for raw in instream:
            command = self.handle_line(raw)
            self._log(raw, command)
            if command:
                outstream.write(command + "\n")
                outstream.flush()
            if self.max_commands is not None and self.max_commands > 0 \
                    and self.commands >= self.max_commands:
                if not self.action_limit_hit:
                    # Guard #2 (jespire): say *why* the agent went quiet. A cap
                    # that trips silently looks exactly like a crashed process.
                    # ASCII only - see _announce_stall for the codepage reason.
                    self.action_limit_hit = True
                    print(f"[stdio] ACTION LIMIT: {self.max_commands} commands sent "
                          f"this process - stopping auto-decisions (runaway guard). "
                          f"Set JEVBRAIN_MAX_ACTIONS to change or 0 for unlimited.",
                          file=self.warn_stream, flush=True)
                    feed = getattr(self.agent, "feed", None)
                    if feed is not None:
                        try:
                            feed.publish("run_end", {
                                "summary": f"action limit ({self.max_commands}) reached; "
                                           "auto-decisions stopped.",
                                "reason": "action_limit",
                            })
                        except Exception:  # noqa: BLE001
                            pass
                break
        return self.commands

    def _log(self, raw: str, command: str | None) -> None:
        """Every message and answer, for post-mortem. Never stdout.

        `log_path=None` means no logging: the pipe log is the record of what the
        real game sent, and a test fixture must never land in it.
        """
        if self.log_path is None:
            return
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps({
                    "ts": time.time(),
                    "msg": raw.strip()[:4000],
                    "sent": command,
                }, ensure_ascii=False) + "\n")
        except OSError as exc:  # a full disk must not kill the run
            print(f"[stdio] could not log: {exc}", file=sys.stderr)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _build_agent(strategy_path: str | None, backend: str | None, acceptance: str | None,
                 dashboard_url: str | None = None):
    """Construct the router lazily so imports stay cheap for --replay.

    With `dashboard_url`, the agent gets a BridgeFeed: every decision is
    forwarded to a running dashboard server (run_dashboard.py) over HTTP. The
    bridge fails silently when no dashboard is up, so `--dashboard-url` can
    stay in a launch config permanently — it is observability, never a
    dependency.
    """
    from spirebrain.driver.agent import SpireBrainAgent

    feed = None
    if dashboard_url:
        from spirebrain.overlay.server import BridgeFeed
        feed = BridgeFeed(url=dashboard_url)
    return SpireBrainAgent(jev_backend=backend or "mock",
                           strategy_path=strategy_path, acceptance=acceptance,
                           feed=feed)


def replay(paths: list[Path], agent, *, log_path: str | Path | None = None,
           outstream=None) -> StdioTransport:
    """Feed recorded state files through the same code path as the live pipe.

    This is how the transport is tested, and how a recorded live session can be
    re-decided offline without touching the game.

    Logging is **off by default** here, unlike live mode: `logs/pipe.jsonl` is the
    record of what the real game sent us, so an offline replay must not append to
    it unless a path is given explicitly.

    `mode="play"` on purpose: a replay exists to re-decide recorded states, so it
    must emit the real commands. Advisor mode would answer every recorded screen
    with a poll and prove nothing.
    """
    transport = StdioTransport(agent, log_path=log_path, mode="play")
    transport.run((p.read_text(encoding="utf-8") for p in paths),
                  outstream if outstream is not None else sys.stdout,
                  send_ready=False)
    return transport


def main(argv: list[str]) -> int:
    # Our diagnostics must survive the console encoding. Windows Python defaults
    # to the locale codepage (cp936 on this machine), and the mod captures stderr
    # into communication_mod_errors.log: the em dash in our banner arrived there
    # as the two stray bytes `a1 aa`. Only stderr is reconfigured — stdout is the
    # protocol stream and is ASCII by construction, so it is left alone.
    if hasattr(sys.stderr, "reconfigure"):
        try:
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass

    def value(name: str) -> str | None:
        prefix = f"--{name}="
        hit = next((a.split("=", 1)[1] for a in argv if a.startswith(prefix)), None)
        if hit is None and f"--{name}" in argv:
            hit = argv[argv.index(f"--{name}") + 1]
        return hit

    backend = value("backend")
    acceptance = value("acceptance")
    strategy_path = value("strategy")
    log_path = value("log")
    replay_dir = value("replay")
    dashboard_url = value("dashboard-url") or value("dashboard")
    mode = ("play" if "--play" in argv else
            "advise" if "--advise" in argv else (value("mode") or DEFAULT_MODE))
    if mode not in MODES:
        print(f"[stdio] unknown --mode {mode!r}; using {DEFAULT_MODE!r} "
              f"(have {MODES})", file=sys.stderr)
        mode = DEFAULT_MODE
    poll_frames = int(value("poll-frames") or DEFAULT_POLL_FRAMES)

    if replay_dir:
        agent = _build_agent(strategy_path, backend, acceptance, dashboard_url)
        files = sorted(Path(replay_dir).glob("*.json"))
        if not files:
            print(f"[stdio] no .json messages in {replay_dir}", file=sys.stderr)
            return 2
        transport = replay(files, agent, log_path=log_path, outstream=sys.stdout)
        print(f"[stdio] replayed {transport.messages} messages, "
              f"emitted {transport.commands} commands, "
              f"skipped {transport.skipped_not_ready} not-ready, "
              f"{len(transport.substitutions)} substitutions", file=sys.stderr)
        return 0

    # Live mode: this is what CommunicationMod launches.
    agent = _build_agent(strategy_path, backend, acceptance, dashboard_url)
    if dashboard_url:
        print(f"[stdio] decisions stream to {dashboard_url} "
              f"(start run_dashboard.py to watch; silent when it is not up)",
              file=sys.stderr)
    if mode == "advise":
        # Say what this process will and will not do, in the log the mod keeps.
        # A player who launches the agent and sees the game not being played
        # needs to find out why from the artifact, not from a silent pipe.
        print("[stdio] ADVISOR MODE: the agent will only recommend - every command "
              "it sends is a poll (wait/state), never a play/choose/proceed. "
              "You keep the mouse and the keyboard. Advice appears on the "
              f"dashboard; verdicts land in logs/advice.jsonl.", file=sys.stderr)
    elif "--auto-start" in argv:
        print("[stdio] PLAY MODE (auto-play): the agent will play the run itself.",
              file=sys.stderr)
    # No log_path argument: live mode wants the default file, and that is exactly
    # what DEFAULT_LOG distinguishes from None.
    transport = StdioTransport(agent,
                               auto_start="--auto-start" in argv,
                               player_class=value("class") or "IRONCLAD",
                               ascension=int(value("ascension") or 0),
                               mode=mode, poll_frames=poll_frames)
    if log_path:
        transport.log_path = Path(log_path)
    # ASCII only in every stderr line - see _announce_stall.
    print(f"[stdio] ready - backend={backend or 'mock'} "
          f"acceptance={acceptance or 'margin'} mode={transport.mode}; "
          f"waiting for state on stdin",
          file=sys.stderr)
    transport.run(sys.stdin, sys.stdout)
    print(f"[stdio] stdin closed after {transport.messages} messages, "
          f"{transport.commands} commands, {transport.errors} errors, "
          f"{transport.menu_idle} menu states, "
          f"{len(transport.substitutions)} substitutions, "
          f"{transport.stuck_events} stall-guard trips, "
          f"{transport.ladder_events} unmodeled-screen stops, "
          f"{transport.advice_issued} recommendations, "
          f"agreement {_agreement_text(transport)}"
          + (" (action limit hit)" if transport.action_limit_hit else ""),
          file=sys.stderr)
    return 0


def _agreement_text(transport: StdioTransport) -> str:
    """One field for the closing banner: matches/verdicts, or "no verdicts yet".

    `-` rather than `0` when nothing could be judged: a player who ignored every
    recommendation and a run where no action was ever identifiable are different
    situations, and the log should not conflate them.
    """
    rate = transport.tracker.agreement
    judged = transport.tracker.judged
    if rate is None:
        return f"n/a (0 of {transport.advice_issued} recommendations judged)"
    return f"{rate:.0%} of {judged} judged"


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
