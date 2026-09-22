"""Demo driver — a full simulated run, published live to the dashboard.

Watch the brain think without the game and without an API key. This reuses the
*real* offline harness (`sim.run_offline.run_one_simulation`), the *real*
decision modules and the *real* mock JEV client — the only difference from a
real ascent is who answers the questions. That is deliberate: a demo that used
canned events would demo the wrong thing.

    python -m spirebrain.overlay.demo                 # start server + run
    python -m spirebrain.overlay.demo --optimistic    # confident JEV (JEE acts)
    python -m spirebrain.overlay.demo --backend openrouter   # REAL JEV answers

The pessimistic default is the more honest demo: every module walks its
fallback path, so the dashboard shows amber "rule takeover" cards and the
*reasons* — which is the behaviour the whole architecture is built around.

How the events flow: the server holds one DecisionFeed; the simulation runs in
a thread with the same feed; the page subscribes and replays history on
connect, so refreshing the browser mid-demo never loses the plot.
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
from pathlib import Path

from spirebrain.overlay.server import DashboardServer

ROOT = Path(__file__).resolve().parents[2]


def run_demo(*, optimistic: bool = False, backend: str | None = None,
             seed: int = 42, port: int = 8787, open_browser: str | None = None) -> dict:
    """One ascent, published event by event. Returns the harness's outcome dict."""
    from spirebrain.overlay.feed import DecisionFeed
    from spirebrain.sim import run_offline

    feed = DecisionFeed(journal_path=ROOT / "logs" / "dashboard_feed.jsonl")
    server = DashboardServer(feed=feed, port=port)
    url = f"http://127.0.0.1:{server.port}"
    print(f"[demo] dashboard: {url}  (Ctrl+C to stop)", file=sys.stderr, flush=True)
    if open_browser:
        import webbrowser
        webbrowser.open(url)
    server.start_background()

    # A tiny preamble event so a fresh page shows where this came from.
    feed.publish("run_state", {"act": 1, "floor": 0, "character": "Ironclad",
                               "hp": 80, "max_hp": 80, "gold": 150,
                               "reserved": 24, "budget_remaining": 56,
                               "deck_size": 10, "relics": ["Burning Blood"]})

    outcome_box: dict = {}

    def _run():
        try:
            outcome_box["result"] = run_offline.run_one_simulation(
                seed=seed, confidence=0.90 if optimistic else None,
                backend=backend, feed=feed)
        except Exception as exc:  # noqa: BLE001 - report and keep the server up
            print(f"[demo] simulation failed: {exc}", file=sys.stderr, flush=True)
            outcome_box["error"] = str(exc)
        finally:
            if "result" in outcome_box:
                r = outcome_box["result"]
                feed.publish("run_end", {"summary": (
                    f"seed={r['seed']}  JEV 调用 {r['jev_calls']} 次  "
                    f"卡组 {r['deck_size_start']}→{r['deck_end']}  "
                    f"拿卡 {r.get('cards_taken', 0)} 张  "
                    f"费用 ${r['jev_cost_usd']:.6f}"
                )})
            print("[demo] simulation finished; the server keeps running so you "
                  "can read the stream. Ctrl+C to exit.", file=sys.stderr, flush=True)

    worker = threading.Thread(target=_run, daemon=True)
    worker.start()

    try:
        while worker.is_alive():
            worker.join(timeout=0.5)
        # Keep serving after the run ends so the page stays readable.
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        print("\n[demo] bye", file=sys.stderr)
    finally:
        server.shutdown()
    return outcome_box.get("result", {})


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="JEV Spire Brain dashboard demo")
    ap.add_argument("--optimistic", action="store_true",
                    help="confident mock: JEV's answers steer the run")
    ap.add_argument("--backend", default=None,
                    help='real answers, e.g. "openrouter" (needs OPENROUTER_API_KEY)')
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--open", action="store_true", help="open the browser automatically")
    args = ap.parse_args()
    run_demo(optimistic=args.optimistic, backend=args.backend, seed=args.seed,
             port=args.port, open_browser=True if args.open else None)
