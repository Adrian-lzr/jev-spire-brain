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

import json
import sys
import time
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[2]

READY = "Ready"

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
        return "wait" if frames is None else f"wait {int(frames)}"

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

    def __init__(self, agent, *, log_path: str | Path | None = None,
                 auto_start: bool = False, player_class: str = "IRONCLAD",
                 ascension: int = 0, max_commands: int | None = None) -> None:
        self.agent = agent
        self.log_path = Path(log_path) if log_path else ROOT / "logs" / "pipe.jsonl"
        self.auto_start = auto_start
        self.player_class = player_class
        self.ascension = ascension
        self.max_commands = max_commands
        self.messages = 0
        self.commands = 0
        self.errors = 0
        self.skipped_not_ready = 0
        self.substitutions: list[dict] = []

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
            if not self.auto_start:
                return None
            return to_command_line({"command": "start", "player_class": self.player_class,
                                    "ascension": self.ascension})

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

        line = to_command_line(self.agent.choose_action(game))
        line = self._ensure_offered(line, message.get("available_commands"))
        self.commands += 1
        return line

    def _ensure_offered(self, line: str, available: Any) -> str:
        """Never send a verb the game did not advertise."""
        if not available:
            return line  # build does not advertise; trust the agent
        offered = {str(a).strip().lower() for a in available}
        verb = _verb_of(line)
        if verb in offered:
            return line
        for candidate in SAFE_VERBS:
            if candidate in offered:
                self.substitutions.append({"wanted": line, "sent": candidate})
                return candidate
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
            if self.max_commands is not None and self.commands >= self.max_commands:
                break
        return self.commands

    def _log(self, raw: str, command: str | None) -> None:
        """Every message and answer, for post-mortem. Never stdout."""
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
def _build_agent(strategy_path: str | None, backend: str | None, acceptance: str | None):
    """Construct the router lazily so imports stay cheap for --replay."""
    from spirebrain.driver.agent import SpireBrainAgent

    return SpireBrainAgent(jev_backend=backend or "mock",
                           strategy_path=strategy_path, acceptance=acceptance)


def replay(paths: list[Path], agent, *, log_path: str | Path | None,
           outstream) -> StdioTransport:
    """Feed recorded state files through the same code path as the live pipe.

    This is how the transport is tested, and how a recorded live session can be
    re-decided offline without touching the game.
    """
    transport = StdioTransport(agent, log_path=log_path)
    transport.run((p.read_text(encoding="utf-8") for p in paths), outstream,
                  send_ready=False)
    return transport


def main(argv: list[str]) -> int:
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

    if replay_dir:
        agent = _build_agent(strategy_path, backend, acceptance)
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
    agent = _build_agent(strategy_path, backend, acceptance)
    transport = StdioTransport(agent, log_path=log_path,
                               auto_start="--auto-start" in argv,
                               player_class=value("class") or "IRONCLAD",
                               ascension=int(value("ascension") or 0))
    print(f"[stdio] ready — backend={backend or 'mock'} "
          f"acceptance={acceptance or 'margin'}; waiting for state on stdin",
          file=sys.stderr)
    transport.run(sys.stdin, sys.stdout)
    print(f"[stdio] stdin closed after {transport.messages} messages, "
          f"{transport.commands} commands, {transport.errors} errors",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
