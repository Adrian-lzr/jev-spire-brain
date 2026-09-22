"""How the agent behaves: `advise` (default) or `play`.

`advise` is the default because this tool exists to make a human play better;
an agent that silently plays the game for you is the wrong default for that job.
The mode governs which verbs may ever reach the game, so it lives next to its
own constants rather than inside the transport.
"""

from __future__ import annotations

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
