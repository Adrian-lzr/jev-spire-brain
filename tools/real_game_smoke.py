"""Bounded real-game smoke test with backup/restore safety.

This is deliberately opt-in.  Without ``--allow-real-game`` it only prints
the required command.  A real run uses the saved ModTheSpire profile, starts
the agent with a temporary dashboard port, records a timestamp-bounded log
window, and restores CommunicationMod plus Ironclad saves in ``finally``.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import statistics
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


def _provider_health(events: list[dict]) -> str:
    if not events:
        return "not_observed"
    # Only an explicit boolean is evidence of provider health.  Older or
    # hand-written rows without ``fallback`` must not turn an incomplete log
    # into a false healthy result.
    successes = sum(1 for event in events if event.get("fallback") is False)
    failures = sum(1 for event in events if event.get("fallback") is True)
    unknown = sum(1 for event in events
                  if event.get("fallback") is not True
                  and event.get("fallback") is not False)
    if successes and (failures or unknown):
        return "degraded"
    if successes:
        return "healthy"
    return "unknown" if unknown else "unavailable"


def _request_lifecycle(trace: list[dict]) -> dict:
    """Summarize provider requests that were still open at smoke shutdown.

    A bounded smoke may stop while a network call is naturally finishing.  It
    is evidence about the run's end state, not a provider failure, so keep it
    separate from ``provider_health`` and never infer a response for it.
    """
    requests: set[str] = set()
    responses: set[str] = set()
    expired: set[str] = set()
    for event in trace:
        if not isinstance(event, dict):
            continue
        kind = str(event.get("event_type") or "")
        request_id = str(event.get("request_id") or "")
        if kind == "provider_request" and request_id:
            requests.add(request_id)
        elif kind == "provider_response" and request_id:
            responses.add(request_id)
        elif kind == "expired_result":
            if request_id:
                expired.add(request_id)
            for value in event.get("provider_request_ids") or []:
                if value:
                    expired.add(str(value))
    open_ids = sorted(requests - responses - expired)
    return {
        "provider_request_count": len(requests),
        "provider_response_count": len(responses),
        "expired_request_count": len(expired & requests),
        "in_flight_request_ids": open_ids,
        "run_ended_with_pending_request": bool(open_ids),
    }


def _latency_summary(events: list[dict]) -> dict:
    values = [float(event["latency_ms"]) for event in events
              if isinstance(event.get("latency_ms"), (int, float))
              and not isinstance(event.get("latency_ms"), bool)
              and event["latency_ms"] >= 0]
    if not values:
        return {"sample_count": 0, "p50_ms": None, "p95_ms": None}
    ordered = sorted(values)
    p95_index = max(0, math.ceil(0.95 * len(ordered)) - 1)
    return {
        "sample_count": len(ordered),
        "p50_ms": round(statistics.median(ordered), 1),
        "p95_ms": round(ordered[p95_index], 1),
    }


def _build_report(brain: list[dict], jev: list[dict], trace: list[dict],
                  backup: str = "") -> dict:
    decisions = [event for event in trace if event.get("event_type") == "decision"]
    strategic_successes = [event for event in brain if event.get("fallback") is False]
    strategic_failures = [event for event in brain if event.get("fallback") is True]
    strategic_unknown = [event for event in brain
                         if event.get("fallback") is not True
                         and event.get("fallback") is not False]
    jev_failures = [event for event in jev if event.get("fallback") is True]
    jev_unknown = [event for event in jev
                   if event.get("fallback") is not True
                   and event.get("fallback") is not False]
    expired = [event for event in trace if event.get("event_type") == "expired_result"]
    strategic_context = [event for event in trace
                         if event.get("event_type") == "strategic_context_retained"]
    expired_request_ids = {str(event.get("request_id")) for event in expired
                           if event.get("request_id")}
    expired_strategic = sum(
        1 for event in brain if str(event.get("request_id") or "") in expired_request_ids
    )
    citation_ids = sorted({str(value) for event in trace
                           for value in (event.get("knowledge_citation_ids") or [])
                           if value})
    pipeline_passed = bool(decisions)
    jev_health = _provider_health(jev)
    strategic_health = _provider_health(brain)
    lifecycle = _request_lifecycle(trace)
    strategic_usable_successes = max(0, len(strategic_successes) - expired_strategic)
    if not brain:
        strategic_status = "not_observed"
    elif strategic_usable_successes:
        strategic_status = "fresh_plan_received"
    elif strategic_successes:
        strategic_status = "connected_but_stale"
    elif strategic_failures:
        strategic_status = "provider_failed"
    else:
        strategic_status = "unknown"
    # A real-game smoke is a release check, so provider access and result
    # freshness are part of success.  Keep ``pipeline_passed`` separately for
    # diagnosing a run that reached the game while a provider was degraded.
    provider_health_passed = (jev_health == "healthy"
                              and strategic_health == "healthy")
    release_passed = pipeline_passed and provider_health_passed and not bool(expired)
    degraded = (not provider_health_passed or bool(expired))
    return {
        # ``passed`` is the release signal.  ``pipeline_passed`` preserves the
        # older diagnostic meaning: the game/agent pipeline produced a decision.
        "passed": release_passed,
        "release_passed": release_passed,
        "pipeline_passed": pipeline_passed,
        "provider_health_passed": provider_health_passed,
        "provider_health": {"jev": jev_health, "strategic": strategic_health},
        "strategic_status": strategic_status,
        "strategic_backend": next((str(event.get("backend")) for event in brain
                                    if event.get("backend")), None),
        "strategic_model": next((str(event.get("model")) for event in brain
                                  if event.get("model")), None),
        "strategic_error_kinds": sorted({str(event.get("error_kind") or "unknown")
                                          for event in strategic_failures}),
        "strategic_latency": _latency_summary(brain),
        "jev_latency": _latency_summary(jev),
        "skipped": False,
        "synthetic": False,
        "backup": backup,
        "run_ids": sorted({str(event.get("run_id")) for event in brain
                           if event.get("run_id")}),
        "decisions": len(decisions),
        "strategic_successes": len(strategic_successes),
        "strategic_fallbacks": len(strategic_failures),
        "strategic_unknown_events": len(strategic_unknown),
        "strategic_usable_successes": strategic_usable_successes,
        # A retained context proves the strategic provider returned useful
        # objective/resource information, while remaining separate from a
        # fresh executable action. It must not make a degraded release pass.
        "strategic_context_successes": len(strategic_context),
        "strategic_expired_results": expired_strategic,
        "jev_requests": len(jev),
        "jev_failures": len(jev_failures),
        "jev_unknown_events": len(jev_unknown),
        "jev_error_kinds": sorted({str(event.get("error_kind") or "unknown")
                                    for event in jev_failures}),
        "rag_citation_ids": citation_ids,
        "degraded": degraded,
        "sources": sorted({str(event.get("source_type")) for event in decisions}),
        **lifecycle,
    }


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


def _backup_files(files: dict[Path, str], backup: Path) -> dict[Path, bool]:
    """Copy existing integration files and remember which targets were absent.

    The smoke runner is allowed to use ``--setup`` because it must exercise the
    same launch path as a player.  A first run can therefore create a config
    that did not exist before the test.  Recording existence separately from
    the copied contents lets the finally block restore both cases without
    leaving a test-created configuration behind.
    """
    existed: dict[Path, bool] = {}
    for source, name in files.items():
        present = source.exists()
        existed[source] = present
        if present:
            shutil.copy2(source, backup / name)
    return existed


def _restore_files(files: dict[Path, str], backup: Path,
                   existed: dict[Path, bool]) -> None:
    """Restore exact pre-run file presence, tolerating partial game setup."""
    for source, name in files.items():
        saved = backup / name
        try:
            if existed.get(source, False):
                if saved.exists():
                    source.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(saved, source)
            elif source.exists():
                # This target was created by the opt-in smoke run.  Keep the
                # cleanup narrow: only explicit integration targets are removed.
                source.unlink()
        except OSError:
            # Cleanup must not hide the smoke result.  The backup directory is
            # returned in the report so a failed restore remains diagnosable.
            continue


def run(*, duration: float, allow_real_game: bool, mode: str = "advise") -> dict:
    if not allow_real_game:
        return {"passed": False, "skipped": True,
                "message": "真实游戏测试需要显式传入 --allow-real-game"}
    mode = str(mode or "advise").strip().lower()
    if mode not in {"advise", "play"}:
        raise ValueError("mode must be 'advise' or 'play'")
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
    existed = _backup_files(files, backup)

    started = time.time()
    agent = None
    mts = None
    try:
        start_args = [sys.executable, "-u", "start.py", "--setup",
                      "--no-browser", "--port", "0"]
        # Advice is the product default: it leaves the player in control and
        # keeps the state stable long enough to verify a strategic response.
        # Automated play remains an explicit stress mode for stale-result and
        # legality checks.
        start_args.append("--play" if mode == "play" else "--advise")
        agent = subprocess.Popen(
            start_args, cwd=root,
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
        _restore_files(files, backup, existed)

    brain = _json_events(root / "logs" / "brain_calls.jsonl", started)
    jev = _json_events(root / "logs" / "jev_calls.jsonl", started)
    trace = _json_events(root / "logs" / "decision_trace.jsonl", started)
    return _build_report(brain, jev, trace, str(backup))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-real-game", action="store_true")
    parser.add_argument("--duration", type=float, default=30.0)
    parser.add_argument("--mode", choices=("advise", "play"), default="advise",
                        help="real-game mode; advise is the safe default")
    args = parser.parse_args(argv)
    result = run(duration=args.duration, allow_real_game=args.allow_real_game,
                 mode=args.mode)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    # ``skipped`` is an intentional operator opt-in guard.  An opted-in run
    # must satisfy provider health and freshness as well as producing a game
    # decision; otherwise automation must fail loudly instead of accepting a
    # rules-only/degraded smoke as release evidence.
    return 0 if result.get("release_passed", result.get("passed")) or result.get("skipped") else 1


if __name__ == "__main__":
    raise SystemExit(main())
