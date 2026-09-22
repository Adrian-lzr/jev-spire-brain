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


def run_advise_demo(*, backend: str | None = None, port: int = 8787,
                    open_browser: str | None = None, pace: float = 1.6) -> dict:
    """A scripted ascent where the PLAYER plays and the brain advises.

    The point of this path is that the panel is the product: a player should be
    able to see what the advisor looks like — and whether its advice is worth
    anything — **before** wiring up the game, and without an API key.

    Nothing is faked except the game. The scripted states go through the *real*
    transport in `advise` mode, which runs the *real* router, the *real* mock JEV
    client and the *real* witness layer; the "player" is a second script that
    sometimes follows the advice and sometimes does its own thing, so the verdict
    column shows both answers instead of flattering itself.

    Returns a small summary dict (recommendations, judged, agreement).
    """
    from spirebrain.driver.agent import SpireBrainAgent
    from spirebrain.driver.stdio import StdioTransport
    from spirebrain.overlay.feed import DecisionFeed

    feed = DecisionFeed(journal_path=ROOT / "logs" / "dashboard_feed.jsonl")
    server = DashboardServer(feed=feed, port=port)
    url = f"http://127.0.0.1:{server.port}"
    print(f"[advise-demo] dashboard: {url}  (Ctrl+C to stop)", file=sys.stderr, flush=True)
    if open_browser:
        import webbrowser
        webbrowser.open(url)
    server.start_background()

    agent = SpireBrainAgent(jev_backend=backend or "mock",
                            log_dir=ROOT / "logs", feed=feed)
    # log_path=None and a temp advice path: an offline demo must not append to
    # `logs/pipe.jsonl`, which is the record of what the real game sent us.
    transport = StdioTransport(agent, log_path=None, mode="advise",
                               advice_path=ROOT / "logs" / "advise_demo.jsonl")

    steps = _advise_demo_states()
    result: dict = {}

    # Say up front what is being shown, because a player's first impression of
    # the advisor IS this panel: the offline brain walks its fallback paths (it
    # has no key to ask), so its advice is deliberately dull. Without this note
    # the dullness reads as "the advisor is bad" instead of "this is the mock".
    feed.publish("run_end", {"summary": (
        f"军师模式演示：脚本扮演玩家，链路是真的（真 transport、真路由、真判定）。"
        f"大脑后端 = {backend or 'mock（离线，无 API key）——建议会偏保守平淡'}。"
        "想听真 JEV 的建议：python run_dashboard.py --advise-demo --backend openrouter。"
        "另外注意：演示全程发给游戏只有 wait 轮询，没有任何推进命令。")})

    def _run():
        try:
            for narration, state in steps:
                feed.publish("run_state", {
                    "act": state.get("act", 1), "floor": state.get("floor", 0),
                    "character": "Ironclad", "hp": state.get("current_hp", 80),
                    "max_hp": state.get("max_hp", 80),
                    "gold": state.get("gold", 99), "reserved": 24,
                    "budget_remaining": 56, "deck_size": len(state.get("deck") or []),
                    "relics": ["Burning Blood"],
                })
                transport.handle_message(_advise_demo_message(state))
                time.sleep(pace)
            feed.publish("run_end", {"summary": (
                f"军师模式演示结束：给出 {transport.advice_issued} 条建议，"
                f"可判定 {transport.tracker.judged} 条，"
                f"命中率 {'—' if transport.tracker.agreement is None else format(transport.tracker.agreement, '.0%')}"
                f"（采纳 {transport.tracker.tally['match']} / 未采纳 {transport.tracker.tally['mismatch']}"
                f" / 无法判定 {transport.tracker.tally['unobserved']}）。"
                "注意：全程没有向游戏发出任何推进命令，只有轮询。")})
        except Exception as exc:  # noqa: BLE001 - report, keep the page readable
            print(f"[advise-demo] failed: {exc}", file=sys.stderr, flush=True)
        finally:
            result.update(advice=transport.advice_issued,
                          judged=transport.tracker.judged,
                          agreement=transport.tracker.agreement,
                          tally=dict(transport.tracker.tally))
            print("[advise-demo] finished; the server keeps running so you can "
                  "read the panel. Ctrl+C to exit.", file=sys.stderr, flush=True)

    worker = threading.Thread(target=_run, daemon=True)
    worker.start()
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        print("\n[advise-demo] bye", file=sys.stderr)
    finally:
        server.shutdown()
    return result


# --------------------------------------------------------------------------- #
# The scripted game: real-shaped states, a player with a mind of its own
# --------------------------------------------------------------------------- #
STRIKE_R = {"id": "Strike_R", "name": "Strike", "type": "ATTACK", "cost": 1, "damage": 6}
DEFEND_R = {"id": "Defend_R", "name": "Defend", "type": "SKILL", "cost": 1, "block": 5}
BASH = {"id": "Bash", "name": "Bash", "type": "ATTACK", "cost": 2, "damage": 8}
DEMO_DECK = [STRIKE_R, STRIKE_R, STRIKE_R, STRIKE_R, STRIKE_R,
             DEFEND_R, DEFEND_R, DEFEND_R, DEFEND_R, BASH]


def _combat(turn, hand, energy, cultist_hp, worm_hp=44, floor=3):
    return {
        "screen_type": "COMBAT", "act": 1, "floor": floor, "turn": turn,
        "current_hp": 70, "max_hp": 80, "gold": 99, "deck": list(DEMO_DECK),
        "combat": {
            "player": {"energy": energy, "current_hp": 70, "block": 0},
            "hand": list(hand),
            "monsters": [{"name": "Cultist", "current_hp": cultist_hp,
                          "intent": "attack", "move_adjusted_damage": 6},
                         {"name": "Jaw Worm", "current_hp": worm_hp,
                          "intent": "attack", "move_adjusted_damage": 11}],
        },
    }


def _advise_demo_message(state: dict) -> dict:
    return {"in_game": True, "ready_for_command": True,
            "available_commands": ["play", "end", "choose", "proceed", "return",
                                   "wait", "state", "potion", "key", "click"],
            "game_state": state}


def _advise_demo_states() -> list[tuple[str, dict]]:
    """The scripted ascent: (what is happening, the state the mod would send).

    Deliberately includes a player who FOLLOWS the advice and a player who does
    not. A demo where every verdict read "match" would be a demo of a rigged
    measurement.
    """
    return [
        ("开局遭遇战：手牌 3 张，能量 3。",
         _combat(1, [STRIKE_R, DEFEND_R, BASH], 3, 48)),
        ("玩家听了建议：打出一张攻击牌，邪教徒掉血。",
         _combat(1, [DEFEND_R, BASH], 2, 42)),
        ("玩家这次自己拿主意：直接结束回合。",
         _combat(2, [DEFEND_R, BASH], 3, 42)),
        ("战斗结束，进入卡牌奖励。",
         {"screen_type": "CARD_REWARD", "act": 1, "floor": 3, "current_hp": 70,
          "max_hp": 80, "gold": 99, "deck": list(DEMO_DECK),
          "screen_state": {"cards": [
              {"id": "Pommel Strike", "name": "Pommel Strike", "type": "ATTACK",
               "cost": 1, "damage": 9},
              {"id": "Clothesline", "name": "Clothesline", "type": "ATTACK",
               "cost": 2, "damage": 12}]}}),
        ("玩家拿走了其中一张。",
         {"screen_type": "CARD_REWARD", "act": 1, "floor": 3, "current_hp": 70,
          "max_hp": 80, "gold": 99,
          "deck": list(DEMO_DECK) + [{"id": "Clothesline", "name": "Clothesline",
                                      "type": "ATTACK", "cost": 2, "damage": 12}],
          "screen_state": {"cards": [
              {"id": "Pommel Strike", "name": "Pommel Strike", "type": "ATTACK",
               "cost": 1, "damage": 9},
              {"id": "Clothesline", "name": "Clothesline", "type": "ATTACK",
               "cost": 2, "damage": 12}]}}),
        ("岔路：一条普通战斗，一条精英。",
         {"screen_type": "MAP", "act": 1, "floor": 4, "current_hp": 70, "max_hp": 80,
          "gold": 99, "deck": list(DEMO_DECK),
          "map": {"next_nodes": [{"x": 1, "y": 1, "symbol": "M"},
                                  {"x": 3, "y": 1, "symbol": "E"}]}}),
        ("玩家选了精英那条路，进入精英战。",
         {"screen_type": "COMBAT", "act": 1, "floor": 5, "turn": 1,
          "current_hp": 70, "max_hp": 80, "gold": 99, "deck": list(DEMO_DECK),
          "combat": {"player": {"energy": 3, "current_hp": 70},
                     "hand": [STRIKE_R, STRIKE_R, DEFEND_R],
                     "monsters": [{"name": "Gremlin Nob", "current_hp": 82,
                                   "intent": "attack", "move_adjusted_damage": 16}]}}),
        ("篝火：牌组里还有没升级的牌。",
         {"screen_type": "REST", "act": 1, "floor": 8, "current_hp": 52, "max_hp": 80,
          "gold": 99, "deck": list(DEMO_DECK),
          "screen_state": {"rest_options": ["rest", "smith"]}}),
        ("玩家选择升级一张牌。",
         {"screen_type": "REST", "act": 1, "floor": 8, "current_hp": 52, "max_hp": 80,
          "gold": 99,
          "deck": [{"id": "Bash", "name": "Bash", "type": "ATTACK", "cost": 2,
                    "damage": 10, "upgrades": 1}] + list(DEMO_DECK[1:]),
          "screen_state": {"rest_options": ["rest", "smith"]}}),
    ]


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="JEV Spire Brain dashboard demo")
    ap.add_argument("--optimistic", action="store_true",
                    help="confident mock: JEV's answers steer the run")
    ap.add_argument("--advise", action="store_true",
                    help="advisor-mode demo: the script plays, the brain advises")
    ap.add_argument("--backend", default=None,
                    help='real answers, e.g. "openrouter" (needs OPENROUTER_API_KEY)')
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--pace", type=float, default=1.6,
                    help="seconds between scripted screens (advise demo)")
    ap.add_argument("--open", action="store_true", help="open the browser automatically")
    args = ap.parse_args()
    if args.advise:
        run_advise_demo(backend=args.backend, port=args.port, pace=args.pace,
                        open_browser=True if args.open else None)
    else:
        run_demo(optimistic=args.optimistic, backend=args.backend, seed=args.seed,
                 port=args.port, open_browser=True if args.open else None)
