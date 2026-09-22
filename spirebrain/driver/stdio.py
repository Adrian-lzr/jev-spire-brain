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
#    Proceed, shops are asked once per floor) — listed here so all three guard
#    names are documented in one place.
# 1000, not jespire's 200: the cap exists to stop a *runaway* (a confused
# loop burning API money), but this agent sends one command per wire action —
# play, choose, and every combat card — so a healthy full ascent spends
# 500-700. Measured 2026-09-22: a live run died at 200 in the error-loop
# below, but even a clean run would have hit it mid-Act 2. 1000 still turns
# a true runaway into a visible stop, ~10x over what a good run needs.
DEFAULT_MAX_ACTIONS = 1000
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
PROTOCOL_VERBS = frozenset({
    "start", "potion", "play", "end", "choose", "proceed", "return", "key",
    "click", "wait", "state",
})

# Our internal intent -> protocol verb. `skip` and `leave` are ours; the game
# only knows RETURN.
INTENT_ALIASES = {
    "skip": "return",
    "leave": "return",
    "cancel": "return",
    "confirm": "proceed",
    "purge": "choose",   # card-removal is one of the shop's choices
    "smith": "choose",   # upgrading is one of the rest site's choices
    "rest": "choose",
    "buy": "choose",
}

# If our chosen verb is not in `available_commands`, prefer these, in order.
# STATE is last because it re-transmits without advancing anything — correct
# when we are confused, but it does not move the run forward.
SAFE_VERBS = ("proceed", "return", "wait", "state")


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

    if verb == "key":
        keyname = command.get("key", command.get("keyname"))
        if not keyname:
            raise ValueError("key needs 'key'")
        timeout = command.get("timeout")
        return f"key {keyname}" + (f" {int(timeout)}" if timeout is not None else "")

    return verb  # end / proceed / return / state / click


def _verb_of(line: str) -> str:
    return line.split(" ", 1)[0].strip().lower()


class StdioTransport:
    """Reads game messages, asks the agent, writes one command back."""

    def __init__(self, agent, *, log_path: str | Path | None | object = DEFAULT_LOG,
                 auto_start: bool = False, player_class: str = "IRONCLAD",
                 ascension: int = 0, max_commands: int | None = None,
                 stall_limit: int = DEFAULT_STALL_LIMIT,
                 warn_stream=None) -> None:
        self.agent = agent
        # None disables logging; omitting the argument uses the default path. See
        # DEFAULT_LOG for why those cannot be the same thing.
        if log_path is DEFAULT_LOG:
            self.log_path: Path | None = ROOT / "logs" / "pipe.jsonl"
        else:
            self.log_path = Path(log_path) if log_path else None
        self.auto_start = auto_start
        self.player_class = player_class
        self.ascension = ascension
        # Action limit (guard #2). An explicit argument wins; otherwise the env
        # var, then the default. 0 or a negative value means unlimited — for a
        # long soak test, not for a stranger's first run.
        if max_commands is None:
            env = os.environ.get("JEVBRAIN_MAX_ACTIONS", "").strip()
            max_commands = int(env) if env.lstrip("-").isdigit() else DEFAULT_MAX_ACTIONS
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
        self.substitutions: list[dict] = []
        # Guard warnings go here (default stderr). Injectable because stderr is
        # how the tests check that a guard *said something* when it fired.
        self.warn_stream = warn_stream if warn_stream is not None else sys.stderr
        self._acted_fingerprint: str | None = None
        self._stuck_seen = 0

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
            if not self.auto_start:
                if self.menu_idle == 1:
                    # Say so out loud, once. Measured 2026-09-21: a real launch with
                    # auto_start off looks *exactly like a dead agent* from the
                    # outside — the game sent one menu state (22:28:39), we stayed
                    # silent by design, and nothing else happened for a minute until
                    # the player quit. Silence in the pipe is indistinguishable from
                    # the absence of a process unless it is recorded somewhere.
                    print("[stdio] at the main menu and --auto-start is off: staying "
                          "silent so the player keeps control. Start a run in-game, or "
                          "pass --auto-start and the agent starts one itself.",
                          file=sys.stderr, flush=True)
                return None
            return self._ensure_offered(
                to_command_line({"command": "start", "player_class": self.player_class,
                                 "ascension": self.ascension}),
                message.get("available_commands"))

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

        fp = self._fingerprint(game)
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

        line = to_command_line(self.agent.choose_action(game))
        line = self._ensure_offered(line, message.get("available_commands"))
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

    def _ensure_offered(self, line: str, available: Any) -> str:
        """Never send a verb the game did not advertise.

        The substitute is built as a full command line, not echoed as a verb
        name: `wait` needs its frame argument, and echoing the bare verb here
        is exactly the bug that burned 995 errors in the 2026-09-22 live run —
        the substitution path bypassed to_command_line, where the default
        lives.
        """
        if not available:
            return line  # build does not advertise; trust the agent
        offered = {str(a).strip().lower() for a in available}
        verb = _verb_of(line)
        if verb in offered:
            return line
        for candidate in SAFE_VERBS:
            if candidate in offered:
                sent = (f"wait {DEFAULT_WAIT_FRAMES}" if candidate == "wait"
                        else candidate)
                self.substitutions.append({"wanted": line, "sent": sent})
                return sent
        self.substitutions.append({"wanted": line, "sent": ""})
        return ""

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
    """
    transport = StdioTransport(agent, log_path=log_path)
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
    # No log_path argument: live mode wants the default file, and that is exactly
    # what DEFAULT_LOG distinguishes from None.
    transport = StdioTransport(agent,
                               auto_start="--auto-start" in argv,
                               player_class=value("class") or "IRONCLAD",
                               ascension=int(value("ascension") or 0))
    if log_path:
        transport.log_path = Path(log_path)
    # ASCII only in every stderr line - see _announce_stall.
    print(f"[stdio] ready - backend={backend or 'mock'} "
          f"acceptance={acceptance or 'margin'}; waiting for state on stdin",
          file=sys.stderr)
    transport.run(sys.stdin, sys.stdout)
    print(f"[stdio] stdin closed after {transport.messages} messages, "
          f"{transport.commands} commands, {transport.errors} errors, "
          f"{transport.menu_idle} menu states, "
          f"{len(transport.substitutions)} substitutions, "
          f"{transport.stuck_events} stall-guard trips"
          + (" (action limit hit)" if transport.action_limit_hit else ""),
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
