"""One command to watch the brain play. Built for people who skip READMEs.

    python start.py

That is the whole interface. It:

  1. runs the environment doctor (and says exactly what to fix, if anything);
  2. starts the local dashboard and opens your browser;
  3. prints the one line to paste into CommunicationMod's config (or writes it
     with --setup), so the game spawns the agent with the dashboard already
     wired — every decision streams to the page with no further steps.

Everything is optional:

    python start.py --setup            # also write the mod config (backs up)
    python start.py --backend mock     # force the offline brain (default auto)
    python start.py --port 9000        # move the dashboard
    python start.py --no-browser       # do not open a browser tab
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
    """Start the dashboard server, open a browser, return (server, url)."""
    from spirebrain.overlay.feed import DecisionFeed
    from spirebrain.overlay.server import DashboardServer

    server = DashboardServer(
        feed=DecisionFeed(journal_path=ROOT / "logs" / "dashboard_feed.jsonl"),
        port=port)
    server.start_background()
    url = f"http://127.0.0.1:{server.port}"
    print(f"\n[2/3] dashboard running: {url}")
    if open_browser:
        webbrowser.open(url)
        print("      opened it in your browser (it reconnects by itself, refresh anytime)")
    else:
        print(f"      open {url} in your browser")
    return server, url


def mod_config_command(url: str, backend: str, auto_start: bool) -> str:
    """The exact command line CommunicationMod should launch, with the dashboard wired."""
    from spirebrain.install_mod_config import default_command

    command = default_command(backend, auto_start=auto_start)
    return f"{command} --dashboard-url {url}/publish"


def write_mod_config(command: str) -> bool:
    """Write CommunicationMod's config. Returns True on success."""
    from spirebrain.install_mod_config import main as modcfg_main

    code = modcfg_main([f"--command={command}", "--auto-start", "--write"])
    if code == 0:
        # One `--setup` is forever: the marker makes every later plain
        # `python start.py` keep the config fresh without the flag. The config
        # embeds this repo's absolute path, so a moved repo needs one more
        # `--setup` — which the doctor's path checks would surface anyway.
        try:
            (ROOT / "config" / ".setup-done").write_text(
                "written by start.py --setup; delete to re-prompt\n", encoding="utf-8")
        except OSError:
            pass
    return code == 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass

    demo = "--demo" in argv
    do_setup = "--setup" in argv or do_setup_saved()
    open_browser = "--no-browser" not in argv
    port = int(_value(argv, "port") or DEFAULT_PORT)
    backend = _value(argv, "backend") or detect_backend()

    print("Jev Spire Brain — start\n")

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

    command = mod_config_command(url, backend, auto_start=True)
    print("\n[3/3] tell the game to launch the brain:")
    print(f"      {command}")
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
    """openrouter when a key exists, mock otherwise — never ask the user to choose."""
    import os

    if os.environ.get("OPENROUTER_API_KEY"):
        return "openrouter"
    env_file = ROOT / ".env"
    if env_file.exists() and "OPENROUTER_API_KEY" in env_file.read_text(
            encoding="utf-8", errors="replace"):
        return "openrouter"
    return "mock"


def do_setup_saved() -> bool:
    """Did the user ask for setup previously? A marker file remembers one `--setup`.

    Mass-audience rule: a choice made once should not have to be made again.
    """
    return (ROOT / "config" / ".setup-done").exists()


if __name__ == "__main__":
    sys.exit(main())
