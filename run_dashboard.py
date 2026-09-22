"""Root launcher for the decision dashboard.

Phase 1.5 — the interaction layer. `run_agent.py` is the brain the game
launches; this is the window you watch it through. Two ways in:

    python run_dashboard.py --demo            # simulated run, mock JEV, no key
    python run_dashboard.py --demo --optimistic   # confident mock: JEV steers
    python run_dashboard.py --demo --backend openrouter   # REAL JEV answers
    python run_dashboard.py --port 8788       # just the server, watch a live agent

The demo path reuses the real offline harness end to end — same decision
modules, same mock client, same fallbacks — so what you see is the architecture
behaving, not a canned animation.

Nothing here touches stdout as a protocol: this process is not the game's
child, so printing is fine.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))

    def value(name: str) -> str | None:
        prefix = f"--{name}="
        hit = next((a.split("=", 1)[1] for a in argv if a.startswith(prefix)), None)
        if hit is None and f"--{name}" in argv:
            hit = argv[argv.index(f"--{name}") + 1]
        return hit

    port = int(value("port") or 8787)

    if "--demo" in argv:
        from spirebrain.overlay.demo import run_demo
        run_demo(optimistic="--optimistic" in argv, backend=value("backend"),
                 seed=int(value("seed") or 42), port=port,
                 open_browser=True if "--open" in argv else None)
        return 0

    # Server-only mode: watch a live agent that publishes into it, or just
    # leave the page open while a --replay runs elsewhere.
    from spirebrain.overlay.feed import DecisionFeed
    from spirebrain.overlay.server import DashboardServer

    server = DashboardServer(
        feed=DecisionFeed(journal_path=ROOT / "logs" / "dashboard_feed.jsonl"),
        port=port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[dashboard] bye", file=sys.stderr)
    finally:
        server.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
