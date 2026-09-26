"""Bounded real-game smoke test with backup/restore safety.

This is deliberately opt-in.  Without ``--allow-real-game`` it only prints
the required command.  A real run uses the saved ModTheSpire profile, starts
the agent with a temporary dashboard port, records a timestamp-bounded log
window, and restores CommunicationMod plus Ironclad saves in ``finally``.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path


def _find_paths() -> tuple[Path, Path, Path]:
    game = Path(os.environ.get(
        "STS_GAME_ROOT",
        r"E:\LeStoreDownload\steam\steamapps\common\SlayTheSpire",
    ))
    workshop = Path(os.environ.get(
        "STS_WORKSHOP_ROOT",
        r"E:\LeStoreDownload\steam\steamapps\workshop\content\646570",
    ))
    java = game / "jre" / "bin" / "java.exe"
    jars = list(workshop.glob("*/ModTheSpire.jar"))
    if not java.exists() or not jars:
        raise FileNotFoundError("未找到游戏 JRE 或 ModTheSpire.jar")
    return game, java, jars[0]


def _json_events(path: Path, since: float) -> list[dict]:
    if not path.exists():
        return []
    events = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            event = json.loads(line)
            if float(event.get("ts", event.get("timestamp", 0)) or 0) >= since:
                events.append(event)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
    return events


def _stop_process_tree(process) -> None:
    if process is None or process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       check=False)
    else:
        process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()


def run(*, duration: float, allow_real_game: bool) -> dict:
    if not allow_real_game:
        return {"passed": False, "skipped": True,
                "message": "真实游戏测试需要显式传入 --allow-real-game"}
    root = Path(__file__).resolve().parents[1]
    game, java, mts_jar = _find_paths()
    local_app = Path(os.environ.get("LOCALAPPDATA", ""))
    save_dir = game / "saves"
    files = {
        local_app / "ModTheSpire" / "CommunicationMod" / "config.properties": "config.properties",
        local_app / "ModTheSpire" / "CommunicationModCJK" / "config.properties": "config-cjk.properties",
        save_dir / "IRONCLAD.autosave": "ironclad.autosave",
        save_dir / "IRONCLAD.autosave.backUp": "ironclad.autosave.backUp",
        save_dir / "steam_autocloud.vdf": "steam_autocloud.vdf",
    }
    backup = Path(os.environ.get("TEMP", str(root / "tmp"))) / (
        "jev-spire-real-smoke-" + time.strftime("%Y%m%d-%H%M%S"))
    backup.mkdir(parents=True, exist_ok=True)
    for source, name in files.items():
        if source.exists():
            shutil.copy2(source, backup / name)

    started = time.time()
    agent = None
    mts = None
    try:
        agent = subprocess.Popen(
            [sys.executable, "-u", "start.py", "--play", "--setup",
             "--no-browser", "--port", "0"], cwd=root,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        time.sleep(3)
        mts = subprocess.Popen(
            [str(java), "-jar", str(mts_jar), "--skip-launcher"], cwd=game,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + max(5.0, duration)
        while time.monotonic() < deadline:
            if agent.poll() is not None:
                break
            time.sleep(1)
    finally:
        for process in (mts, agent):
            _stop_process_tree(process)
        for source, name in files.items():
            saved = backup / name
            if saved.exists():
                shutil.copy2(saved, source)

    brain = _json_events(root / "logs" / "brain_calls.jsonl", started)
    trace = _json_events(root / "logs" / "decision_trace.jsonl", started)
    decisions = [event for event in trace if event.get("event_type") == "decision"]
    strategic = [event for event in brain if not event.get("fallback")]
    return {
        "passed": bool(decisions),
        "skipped": False,
        "synthetic": False,
        "backup": str(backup),
        "run_ids": sorted({str(event.get("run_id")) for event in brain if event.get("run_id")}),
        "decisions": len(decisions),
        "strategic_successes": len(strategic),
        "strategic_fallbacks": len(brain) - len(strategic),
        "sources": sorted({str(event.get("source_type")) for event in decisions}),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-real-game", action="store_true")
    parser.add_argument("--duration", type=float, default=30.0)
    args = parser.parse_args(argv)
    result = run(duration=args.duration, allow_real_game=args.allow_real_game)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("passed") or result.get("skipped") else 1


if __name__ == "__main__":
    raise SystemExit(main())
