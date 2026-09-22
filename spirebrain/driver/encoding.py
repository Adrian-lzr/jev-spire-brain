"""Read and write the pipes without being poisoned by them.

Two hard-won rules, both from real deaths on this machine (2026-09-22):

* **Decode stdin as UTF-8 explicitly.** Windows defaults a pipe's encoding to the
  ANSI codepage (cp936 here) while the game writes UTF-8, which turned relic
  names into mojibake (`鐕冪儳涔嬭` for 燃烧之血) and — worse — produced half
  characters that decode into lone surrogates.
* **Scrub lone surrogates wherever game data touches an encoder.** `json.loads`
  happily materialises a `\\uD8xx` escape into a lone surrogate, and the first
  `.encode("utf-8")` on one raises `UnicodeEncodeError`. That killed the agent
  twice, once mid-run, and each time the player saw nothing but a frozen panel.
"""

from __future__ import annotations

import sys


def configure_streams(stdin=None, stderr=None) -> None:
    """Pin the encoding of the pipes we do not own.

    `stdin` is the protocol stream and must be UTF-8 whatever the console locale
    is; `stderr` carries our diagnostics into the mod's log file, so it must
    survive the console codepage too. `errors="replace"` is deliberate: a
    malformed byte should degrade one character in a log line, never end the run.
    """
    for stream in (stdin if stdin is not None else sys.stdin,
                   stderr if stderr is not None else sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass


def scrub_surrogates(obj):
    """Drop the lone surrogates the CJK fork's JSON escapes can carry.

    `json.loads` happily turns a `\\uD8xx` escape into a LONE surrogate
    character (pairs are combined; anything left is alone), and the first
    `.encode("utf-8")` on it raises UnicodeEncodeError - surrogates not
    allowed. That is how one in-game state killed both agents 11 seconds into
    the 2026-09-22 20:49 session: no advice, no error anywhere the player
    could see, and the panel left saying "agent not online". Scrubbing at this
    one boundary fixes every consumer (fingerprint, witness, agent, logging)
    at once.
    """
    if isinstance(obj, str):
        try:
            obj.encode("utf-8")
            return obj
        except UnicodeEncodeError:
            return obj.encode("utf-8", "ignore").decode("utf-8")
    if isinstance(obj, dict):
        return {scrub_surrogates(k): scrub_surrogates(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [scrub_surrogates(v) for v in obj]
    return obj
