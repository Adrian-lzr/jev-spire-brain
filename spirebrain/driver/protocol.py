"""The CommunicationMod wire protocol: verbs, aliases, and command formatting.

Split out of `stdio.py` (2026-09-22) because none of it touches the transport's
state — it is a pure mapping from the router's intent to the one line the game
accepts. Keeping it separate means the "which words may we say" rules have one
home, and the transport file is about the pipe rather than about vocabulary.

Verified against the CommunicationMod README; the details that are easy to get
wrong (1-indexed PLAY, no `skip` verb, errors arrive as messages) are documented
at each constant below.
"""

from __future__ import annotations

from typing import Any

READY = "Ready"

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


def verb_of(line: str) -> str:
    return line.split(" ", 1)[0].strip().lower()
