"""Root launcher for the live agent.

Point CommunicationMod's `command=` at **this file**, not at
`spirebrain/driver/stdio.py`. Reason: running a script puts *that script's*
directory first on `sys.path`, so running the module file directly would make
`import spirebrain` fail from inside the package. This file sits at the repo
root, so `import spirebrain` resolves with no PYTHONPATH required — which matters
because the game launches us with its own working directory and environment.

It also loads `.env` from the repo root, because a process spawned by the game
inherits the game's environment, not your shell's. Without this the agent would
start with no API key and silently fall back on the mock.

Nothing here prints to stdout: that stream is the protocol. `stdio` sends
`Ready` and commands, and keeps its own diagnostics on stderr.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def _load_dotenv(path: Path) -> int:
    """Minimal KEY=VALUE loader — stdlib only, no python-dotenv dependency.

    Deliberately simple: `#` comments, blank lines, optional `export`, and
    surrounding quotes stripped. It does not interpolate, and it will not
    override a variable that is already set in the real environment (the
    environment wins, which is what you want when debugging by hand).
    """
    if not path.exists():
        return 0
    loaded = 0
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value
            loaded += 1
    return loaded


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    _load_dotenv(ROOT / ".env")

    from spirebrain.driver.stdio import main as stdio_main

    return stdio_main(argv)


if __name__ == "__main__":
    sys.exit(main())
