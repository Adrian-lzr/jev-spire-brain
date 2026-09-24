"""One command to watch the brain play — or to have it coach you. Built for people
who skip READMEs.

    python start.py

That is the whole interface. It:

  1. runs the environment doctor (and says exactly what to fix, if anything);
  2. starts the local event server for the in-game panel;
  3. prints the one line to paste into CommunicationMod's config (or writes it
     with --setup), so the game spawns the agent with the dashboard already
     wired — every decision streams to the page with no further steps.

**Two ways to use it, and the default is the one that helps you play:**

    python start.py                 # 军师模式 / advice: it recommends, YOU play
    python start.py --play          # auto-play: the agent plays the run itself

The advice mode is the default because this project exists to make the player
better, not to replace them: an agent that quietly plays the game for you is the
wrong default for a coach. In advice mode the agent sends the game nothing but
polls — no play, no choose, no proceed — and it will not even start a run for
you. Advice appears on the dashboard and the in-game overlay; what you actually
did, and whether you followed it, lands in `logs/advice.jsonl`.

Everything is optional:

    python start.py --setup            # also write the mod config (backs up)
    python start.py --play             # auto-play instead of advising
    python start.py --backend mock     # force the offline brain (default auto)
    python start.py --port 9000        # move the dashboard
    python start.py --browser          # optionally open the detailed browser log
    python start.py --demo             # no game needed: a simulated run plays

Design rule for a mass audience: every failure must name its fix in one line,
and no step may require reading a README. The doctor already enforces that for
the environment; this file extends the same discipline to the workflow.
"""

from __future__ import annotations

import sys
import threading
import time
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_PORT = 8787


def _value(argv: list[str], name: str) -> str | None:
    prefix = f"--{name}="
    hit = next((a.split("=", 1)[1] for a in argv if a.startswith(prefix)), None)
    if hit is None and f"--{name}" in argv:
        hit = argv[argv.index(f"--{name}") + 1]
    return hit


def run_doctor(live: bool = False) -> bool:
    """The environment check. Returns True when nothing blocks playing."""
    from spirebrain.doctor import main as doctor_main

    code = doctor_main(["--live"] if live else [])
    return code == 0


def start_dashboard(port: int, open_browser: bool) -> tuple[object, str]:
    """Start the event server; the browser view is optional."""
    from spirebrain.overlay.feed import DecisionFeed
    from spirebrain.overlay.server import DashboardServer, PortInUse

    try:
        server = DashboardServer(
            feed=DecisionFeed(journal_path=ROOT / "logs" / "dashboard_feed.jsonl"),
            port=port)
    except PortInUse as exc:
        # The one case where a previous run is the most likely explanation, so say
        # it here rather than letting a raw OSError reach a player. Two dashboards
        # on one port used to be possible (Windows allows the bind) and the game's
        # panel would then read /state from whichever one the kernel picked —
        # including an old build that has no /state at all.
        print("\n[2/3] the dashboard port is taken")
        print(f"      {exc}")
        print("      most likely: an earlier `start.py` (or a demo) is still running.")
        print("      Close that window, or run:  python start.py --port %d" % (port + 1))
        raise SystemExit(2)
    url = f"http://127.0.0.1:{server.port}"
    server.start_background()
    print(f"\n[2/3] dashboard running: {url}")
    if open_browser:
        webbrowser.open(url)
        print("      opened it in your browser (it reconnects by itself, refresh anytime)")
    else:
        print("      advice appears in the in-game overlay; browser log is optional")
    return server, url


def mod_config_command(url: str, backend: str, auto_start: bool, mode: str,
                       brain_backend: str | None = None) -> str:
    """The exact command line CommunicationMod should launch, with the dashboard wired.

    The mode is part of the command, never left implicit: this string is the only
    record of what a launched agent will do, and a player who later wonders why
    the game is (or is not) playing itself should find the answer in their own mod
    config rather than in a default that changed in a commit.
    """
    from spirebrain.install_mod_config import default_command

    # The /publish endpoint, not the page: the agent POSTs events to it, and
    # default_command appends it when given the bare base URL.
    command = default_command(backend, brain_backend=brain_backend,
                              auto_start=auto_start, mode=mode,
                              dashboard_url=url, open_dashboard=True)
    return command


def write_mod_config(command: str) -> bool:
    """Write CommunicationMod's config. Returns True on success."""
    from spirebrain.install_mod_config import main as modcfg_main

    code = modcfg_main([f"--command={command}", "--auto-start", "--write"])
    return code == 0


def communication_config_status(command: str) -> dict:
    """Read-only status of the external CommunicationMod configuration."""
    from spirebrain.install_mod_config import family_config_paths, parse_config

    paths = family_config_paths()
    result = {"valid": False, "paths": [str(p) for p in paths],
              "issues": [], "configured": []}
    for path in paths:
        if not path.exists():
            result["issues"].append(f"配置不存在: {path}")
            continue
        try:
            values = parse_config(path.read_bytes().decode("latin-1"))
        except (OSError, UnicodeError) as exc:
            result["issues"].append(f"配置无法读取: {path} ({type(exc).__name__})")
            continue
        actual = values.get("command", "")
        result["configured"].append({"path": str(path), "command": actual})
        if not actual:
            result["issues"].append(f"配置缺少 command: {path}")
        elif actual != command:
            result["issues"].append(f"配置已过期或参数不一致: {path}")
    result["valid"] = bool(result["configured"]) and not result["issues"]
    return result


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass

    demo = "--demo" in argv
    # Only an explicit --setup may write the user's game configuration.
    do_setup = "--setup" in argv
    # The dashboard is the low-friction control window: open it by default so a
    # player launching the game can configure providers without memorising a URL.
    # --no-browser remains available for headless/CI runs.
    open_browser = "--no-browser" not in argv
    port = int(_value(argv, "port") or DEFAULT_PORT)
    from spirebrain.runtime_config import resolve_runtime_config

    runtime = resolve_runtime_config(ROOT, cli={
        "backend": _value(argv, "backend"),
        "brain_backend": _value(argv, "brain-backend"),
    })
    backend = runtime.jev_backend
    brain_backend = runtime.brain_backend
    mode = ("play" if "--play" in argv else
            "advise" if "--advise" in argv else (_value(argv, "mode") or "advise"))
    if mode not in ("advise", "play"):
        print(f"unknown mode {mode!r}; using 'advise' (have: advise, play)")
        mode = "advise"
    # Advisor mode never auto-starts a run: picking class/ascension/seed is the
    # player's call. Kept as a local so the printed command and the written config
    # cannot disagree with what the agent then does.
    auto_start = mode == "play"

    print("Jev Spire Brain — start\n")
    print(f"providers: JEV={backend} ({runtime.sources['jev_backend']}), "
          f"brain={brain_backend} ({runtime.sources['brain_backend']}); "
          f"config={runtime.config_id}")
    if mode == "advise":
        print("mode: ADVISE (军师模式) - the agent will recommend, never play:")
        print("      it sends the game only polls, and it will not start a run for you.")
        print("      Your advice appears in the game; what you did lands in")
        print("      logs/advice.jsonl. Use --play for the auto-player.\n")
    else:
        print("mode: PLAY (代打模式) - the agent plays the run itself.\n")

    # [1/3] environment ------------------------------------------------------- #
    print("[1/3] checking the environment (details: python -m spirebrain.doctor)")
    if demo:
        print("      --demo: skipping the game checks, no game needed")
    else:
        ok = run_doctor(live=False)
        if not ok:
            print("\nThe checks above name what is missing and how to fix it.")
            print("You can still watch a simulated run right now: python start.py --demo")
            if "--setup" not in argv and not do_setup_saved():
                print("(this does not block starting the dashboard below)")
        print()

    # [2/3] dashboard --------------------------------------------------------- #
    server, url = start_dashboard(port, open_browser)

    # [3/3] the game side ----------------------------------------------------- #
    if demo:
        print("\n[3/3] --demo: playing a simulated run on the dashboard above "
              "(mock brain unless --backend names a real one)")
        from spirebrain.overlay.feed import DecisionFeed
        from spirebrain.sim import run_offline

        feed = server.feed
        feed.publish("run_end", {"summary": "--demo 模式：模拟局即将开始（无游戏、无 API 调用）"})

        def _play():
            try:
                run_offline.run_one_simulation(
                    seed=int(_value(argv, "seed") or 7),
                    confidence=0.90 if "--optimistic" in argv else None,
                    # Demo rule: the offline brain unless the user names a real
                    # one. detect_backend() would pick openrouter on a machine
                    # with a key and then fail in this thread (run_agent.py's
                    # .env loader is what makes the key visible, and demo does
                    # not run through it) — measured, not assumed, 2026-09-22.
                    backend=None if _value(argv, "backend") in (None, "mock") else backend,
                    feed=feed)
            except Exception as exc:  # noqa: BLE001 - report, keep the page usable
                print(f"\n[demo] simulation failed: {exc}", file=sys.stderr)
            finally:
                feed.publish("run_end", {"summary":
                    "模拟局结束。面板保持打开，随时刷新；Ctrl+C 退出。"})

        threading.Thread(target=_play, daemon=True).start()
        print("      watch it play on the dashboard. Ctrl+C here to stop.")
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            print("\nbye")
        finally:
            server.shutdown()
        return 0

    command = mod_config_command(url, backend, auto_start, mode, brain_backend)
    config_status = communication_config_status(command)
    print("\n[3/3] tell the game to launch the brain:")
    print(f"      {command}")
    if mode == "advise":
        print("      (advisor mode: no --auto-start - starting a run stays yours)")
    if not config_status["valid"]:
        print("      setup status: needs setup (read-only check)")
        for issue in config_status["issues"][:6]:
            print(f"      - {issue}")
    else:
        print("      setup status: current CommunicationMod config matches this launch")
    if do_setup:
        print("      --setup: writing this into CommunicationMod's config (backs up the old one)…")
        if write_mod_config(command):
            print("      written. Start the game through ModTheSpire with the mods enabled — "
                  "that is the last step.")
        else:
            print("      could not write it (see above); paste the command manually with "
                  "python -m spirebrain.install_mod_config")
    else:
        print("      or let us write it for you:  python start.py --setup")

    if mode == "advise":
        print("\nLeave this window open and keep the dashboard in view. In the game you")
        print("play exactly as you normally would; the panel on the left tells you what")
        print("the brain would do and why, and then shows what you actually did.")
    else:
        print("\nLeave this window open. Every decision the brain makes appears on the "
              "dashboard as it happens.")
    print("Press Ctrl+C here to stop the dashboard.")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        print("\nbye")
    finally:
        server.shutdown()
    return 0


def detect_backend() -> str:
    """Compatibility helper returning the same resolved JEV provider as startup."""
    from spirebrain.runtime_config import resolve_runtime_config

    return resolve_runtime_config(ROOT).jev_backend


def do_setup_saved() -> bool:
    """Legacy compatibility helper; startup no longer trusts this marker."""
    return (ROOT / "config" / ".setup-done").exists()


if __name__ == "__main__":
    sys.exit(main())
