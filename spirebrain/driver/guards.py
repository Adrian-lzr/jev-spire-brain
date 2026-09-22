"""The runaway guard that watches the *game's* screen, not our own output.

Extracted from `StdioTransport` (2026-09-22) because it exists because of a real
death, and a guard nobody can test in isolation is a guard nobody can trust.

**The unmodeled-screen ladder.** A screen whose `available_commands` offer no
advancing verb cannot be acted on at all, and waiting there forever is how the
18:08 run burned 991 waits in 66 seconds and died on the action cap
("agent not reachable"). The ladder tries two waits, a centre click, SPACE and
ESCAPE — at most `LADDER_CYCLES_BEFORE_STOP` cycles — then stops loudly and
resumes by itself the moment a screen we can read comes back.

Its rungs count against the *screen* signature, never the state hash: animations
churn the state every few messages, and a churning hash restarts the climb
forever (the 18:54 death, 5008 waits).

The ladder is deliberately unavailable in advisor mode. Pressing keys IS acting,
and an advisor that acts is not an advisor (see `driver/modes.py`).
"""

from __future__ import annotations

import sys
from typing import Any

from spirebrain.driver.protocol import DEFAULT_WAIT_FRAMES

#: Rungs of one ladder cycle, in order. `wait` is expanded to `wait N`; the keys
#: and the click are the only way left to tell the game "I am still here" when
#: it offers us no verb at all.
LADDER_CYCLE = ("wait", "wait", "SPACE", "ESCAPE", "click")
LADDER_WAITS_BEFORE_KEY = 2   # kept for tests/documentation of intent
#: Trip the loud-stop after this many full ladder cycles on the same unmodeled
#: screen fingerprint: 5 rungs x 3 cycles is ~15 messages - far under any cap,
#: and if none of that advanced the game, only a human can.
LADDER_CYCLES_BEFORE_STOP = 3


class Ladder:
    """Climbs an unreadable screen a few rungs at a time, then stops loudly."""

    def __init__(self, agent: Any, *, warn_stream: Any = None) -> None:
        self.agent = agent
        self.warn_stream = warn_stream if warn_stream is not None else sys.stderr
        #: The screen we are climbing (its signature, not the state hash).
        self.fp: str | None = None
        #: Rung index within the whole climb.
        self.step = 0
        #: Latched when the climb gave up; cleared by `reset()`.
        self.stopped = False
        #: How many times the ladder stopped and asked for a human.
        self.events = 0

    def reset(self) -> None:
        """A readable screen is back: whatever we were stuck on is gone.

        `step` is deliberately not cleared here — `command()` zeroes it when the
        screen signature changes, which is the same event seen from the other
        side, and clearing it twice would make the two paths disagree.
        """
        self.fp = None
        self.stopped = False

    def command(self, fp: str) -> str | None:
        """Next rung for this screen, or None once the climb has given up.

        The full cycle runs `LADDER_CYCLES_BEFORE_STOP` times; the message AFTER
        the last rung stops loudly — once, because later silent messages change
        nothing and would inflate `events` into a meaningless number.
        """
        if fp != self.fp:
            self.fp = fp
            self.step = 0
        total_rungs = len(LADDER_CYCLE) * LADDER_CYCLES_BEFORE_STOP
        if self.step >= total_rungs:
            if not self.stopped:
                self.stopped = True
                self.events += 1
                self._announce(fp)
            return None  # stay silent; a new modeled screen auto-resumes
        rung = LADDER_CYCLE[self.step % len(LADDER_CYCLE)]
        self.step += 1
        if rung == "wait":
            return f"wait {DEFAULT_WAIT_FRAMES}"
        if rung == "click":
            # Centre of the game's native 1920x1080. The live payload does not
            # carry dimensions; correct here on first live contact if it misses.
            return "click 960 540"
        return f"key {rung}"

    def _announce(self, fp: str) -> None:
        # ASCII only: CommunicationMod writes stderr into the mod's log through
        # the platform codepage, and a Chinese sentence arrives there as bytes
        # nobody can read (see the encoding notes in `driver/encoding.py`).
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
