"""Bounded real-game smoke test using an isolated copy of the installation.

The source game and user profile are read-only inputs.  The smoke copies the
game runtime, a small explicit set of Mod JARs, and the current project into a
task-scoped temporary workspace.  Saves, AppData, Java preferences, logs and
CommunicationMod configuration are created only inside that workspace.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.request_attempt_cap import read_attempt_count


MAX_SMOKE_DURATION_SECONDS = 600.0
MAX_SMOKE_ACTIONS = 20_000
DEFAULT_REQUEST_ATTEMPT_CAP = 100
MAX_REQUEST_ATTEMPT_CAP = 1_000
PROJECT_EXCLUDED_DIRS = {
    ".git", ".venv", "venv", "env", ".pytest_cache", ".playwright-cli",
    "__pycache__", "logs", "tmp", "tmp-log", "node_modules", "dist", "build",
    "target", ".idea", ".vscode",
}
GAME_EXCLUDED_DIRS = {
    "mods", "saves", "logs", "runs", "preferences", "betapreferences",
    "screenshots", "sendtodevs",
    "crash-reports", "crashreports", "config", "tmp", "temp",
}
_SECRET_NAME = re.compile(
    r"(^|[._-])(secret|secrets|credential|credentials|api[-_]?key|token|"
    r"private[-_]?key)([._-]|$)", re.IGNORECASE,
)
_SECRET_SUFFIXES = {".pem", ".key", ".p12", ".pfx", ".jks"}


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
    jars = sorted(workshop.glob("*/ModTheSpire.jar"), key=lambda p: str(p).lower())
    if not java.exists() or not jars:
        raise FileNotFoundError("未找到游戏 JRE 或 ModTheSpire.jar")
    return game, java, jars[0]


@dataclass(frozen=True)
class IsolatedWorkspace:
    root: Path
    project: Path
    game: Path
    user_profile: Path
    appdata: Path
    local_appdata: Path
    temp: Path
    java_preferences: Path
    request_counter: Path
    mod_jar_names: tuple[str, ...]
    source_paths: tuple[Path, ...]


def _is_secret_path(relative: Path) -> bool:
    for part in relative.parts:
        lower = part.lower()
        if lower == ".env" or (lower.startswith(".env.") and lower != ".env.example"):
            return True
        if lower in {"secrets", "credentials", "private_keys"}:
            return True
        if _SECRET_NAME.search(part):
            return True
        if Path(lower).suffix in _SECRET_SUFFIXES or lower.startswith("id_rsa"):
            return True
    return False


def _copy_filtered_tree(source: Path, destination: Path, *, excluded_dirs: set[str],
                        game_files: bool = False) -> int:
    """Copy ordinary files only; skip symlinks and known user/runtime material."""
    source = source.resolve()
    destination = destination.resolve()
    if source == destination or source in destination.parents or destination in source.parents:
        raise ValueError("copy destination must be independent of its source")
    if destination.exists() and any(destination.iterdir()):
        raise FileExistsError(f"copy destination must be empty: {destination}")
    if not source.is_dir():
        raise FileNotFoundError(f"source directory does not exist: {source}")
    destination.mkdir(parents=True, exist_ok=True)
    copied_count = 0
    excluded_dirs = {item.lower() for item in excluded_dirs}

    for current, dirs, files in os.walk(source, topdown=True, followlinks=False):
        current_path = Path(current)
        relative_dir = current_path.relative_to(source)
        kept_dirs = []
        for name in dirs:
            child = current_path / name
            relative = relative_dir / name
            if (name.lower() in excluded_dirs or _is_secret_path(relative)
                    or child.is_symlink()):
                continue
            kept_dirs.append(name)
        dirs[:] = kept_dirs
        target_dir = destination / relative_dir
        target_dir.mkdir(parents=True, exist_ok=True)
        for name in files:
            file_path = current_path / name
            relative = relative_dir / name
            lower = name.lower()
            if file_path.is_symlink() or _is_secret_path(relative):
                continue
            if game_files and (
                lower == "steam_appid.txt"
                or lower == "mod_lists.json"
                or lower == "config.properties"
                or lower in {"config.json", "info.displayconfig", "twitchconfig.txt"}
                or lower.endswith((".autosave", ".save", ".log", ".log.gz", ".vdf"))
            ):
                continue
            target = target_dir / name
            shutil.copy2(file_path, target)
            copied_count += 1
    return copied_count


def _named_jar(roots: tuple[Path, ...], names: tuple[str, ...]) -> Path | None:
    accepted = {name.lower() for name in names}
    for root in roots:
        if not root.is_dir():
            continue
        candidates = sorted((p for p in root.rglob("*.jar")
                             if p.name.lower() in accepted),
                            key=lambda p: (len(p.parts), str(p).lower()))
        if candidates:
            return candidates[0]
    return None


def _select_mod_sources(game: Path, workshop: Path, project: Path,
                        mts_jar: Path) -> dict[str, Path]:
    local_mods = game / "mods"
    roots = (local_mods, workshop)
    base_mod = _named_jar(roots, ("BaseMod.jar",))
    comm_mod = _named_jar(roots, ("CommunicationMod.jar",))
    if comm_mod is None:
        comm_mod = _named_jar(roots, ("CommunicationModCJK.jar",))
    overlay = _named_jar((local_mods, project / "java" / "target"),
                         ("SpireBrainOverlay.jar",))
    missing = [name for name, value in (("BaseMod.jar", base_mod),
                                        ("CommunicationMod.jar", comm_mod))
               if value is None]
    if missing:
        raise FileNotFoundError("未找到隔离测试所需 Mod: " + ", ".join(missing))
    return {
        "ModTheSpire.jar": mts_jar,
        "BaseMod.jar": base_mod,
        comm_mod.name: comm_mod,
        **({"SpireBrainOverlay.jar": overlay} if overlay else {}),
    }


def _write_isolated_profile(layout_root: Path, mod_jar_names: tuple[str, ...]) -> Path:
    profile = layout_root / "user" / "AppData" / "Local" / "ModTheSpire" / "mod_lists.json"
    enabled = [name for name in mod_jar_names if name != "ModTheSpire.jar"]
    profile.parent.mkdir(parents=True, exist_ok=True)
    comm_name = next((name for name in enabled
                      if name.lower().startswith("communicationmod")), None)
    config_mod_name = ("CommunicationModCJK" if comm_name
                       and comm_name.lower() == "communicationmodcjk.jar"
                       else "CommunicationMod")
    (profile.parent / config_mod_name).mkdir(parents=True, exist_ok=True)
    profile.write_text(json.dumps({
        "defaultList": "<Default>",
        "lists": {"<Default>": enabled},
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return profile


def _install_request_cap_bootstrap(project: Path) -> None:
    """Install only in the copied project; source files are never changed."""
    path = project / "sitecustomize.py"
    path.write_text(
        "import importlib.util as _u\n"
        "from pathlib import Path as _P\n"
        "_p = _P(__file__).parent / 'tools' / 'request_attempt_cap.py'\n"
        "_s = _u.spec_from_file_location('_spirebrain_request_attempt_cap', _p)\n"
        "_m = _u.module_from_spec(_s)\n"
        "_s.loader.exec_module(_m)\n"
        "_m.install_urlopen_guard()\n",
        encoding="utf-8",
    )


def _create_isolated_workspace(root: Path, source_game: Path, source_java: Path,
                               source_mts: Path, source_workshop: Path,
                               workspace: Path) -> IsolatedWorkspace:
    workspace = workspace.resolve()
    for source in (root.resolve(), source_game.resolve()):
        if workspace == source or source in workspace.parents or workspace in source.parents:
            raise ValueError("isolated workspace must not overlap the project or game source")
    if workspace.exists() and any(workspace.iterdir()):
        raise FileExistsError(f"isolated workspace must be empty: {workspace}")
    workspace.mkdir(parents=True, exist_ok=True)
    project_copy = workspace / "project"
    game_copy = workspace / "game"
    _copy_filtered_tree(
        root, project_copy, excluded_dirs=PROJECT_EXCLUDED_DIRS,
    )
    _install_request_cap_bootstrap(project_copy)
    _copy_filtered_tree(
        source_game, game_copy, excluded_dirs=GAME_EXCLUDED_DIRS, game_files=True,
    )
    selected = _select_mod_sources(source_game, source_workshop, root, source_mts)
    mods_dir = game_copy / "mods"
    mods_dir.mkdir(parents=True, exist_ok=True)
    for target_name, source_path in selected.items():
        if source_path is None:
            continue
        shutil.copy2(source_path, mods_dir / target_name)
    copied_java = game_copy / "jre" / "bin" / source_java.name
    if not copied_java.is_file():
        raise FileNotFoundError(f"isolated game copy has no bundled Java: {copied_java}")

    user = workspace / "user"
    appdata = user / "AppData" / "Roaming"
    local_appdata = user / "AppData" / "Local"
    temporary = workspace / "temp"
    java_preferences = workspace / "java-preferences"
    for directory in (appdata, local_appdata, temporary, java_preferences):
        directory.mkdir(parents=True, exist_ok=True)
    _write_isolated_profile(workspace, tuple(selected))
    return IsolatedWorkspace(
        root=workspace,
        project=project_copy,
        game=game_copy,
        user_profile=user,
        appdata=appdata,
        local_appdata=local_appdata,
        temp=temporary,
        java_preferences=java_preferences,
        request_counter=workspace / "temp" / "request-attempts.count",
        mod_jar_names=tuple(selected),
        source_paths=tuple([source_game, source_java, source_mts, *selected.values()]),
    )


def _bounded_duration(value: float) -> float:
    return min(MAX_SMOKE_DURATION_SECONDS, max(5.0, float(value)))


def _bounded_actions(value: int) -> int:
    actions = int(value)
    if not 1 <= actions <= MAX_SMOKE_ACTIONS:
        raise ValueError(f"max_actions must be between 1 and {MAX_SMOKE_ACTIONS}")
    return actions


def _bounded_request_attempts(value: int) -> int:
    attempts = int(value)
    if not 1 <= attempts <= MAX_REQUEST_ATTEMPT_CAP:
        raise ValueError(f"request_attempt_cap must be between 1 and {MAX_REQUEST_ATTEMPT_CAP}")
    return attempts


def _isolated_environment(layout: IsolatedWorkspace, *, request_attempt_cap: int,
                          max_actions: int, base: Mapping[str, str] | None = None,
                          jev_backend: str | None = None,
                          brain_backend: str | None = None, brain_model: str | None = None,
                          brain_endpoint: str | None = None,
                          reasoning_effort: str | None = None) -> dict[str, str]:
    env = dict(os.environ if base is None else base)
    env.update({
        "APPDATA": str(layout.appdata),
        "LOCALAPPDATA": str(layout.local_appdata),
        "USERPROFILE": str(layout.user_profile),
        "HOME": str(layout.user_profile),
        "TEMP": str(layout.temp),
        "TMP": str(layout.temp),
        "XDG_CONFIG_HOME": str(layout.user_profile / ".config"),
        "XDG_DATA_HOME": str(layout.user_profile / ".local" / "share"),
        "XDG_CACHE_HOME": str(layout.user_profile / ".cache"),
        "STS_GAME_DIR": str(layout.game),
        "STS_GAME_ROOT": str(layout.game),
        "STS_WORKSHOP_ROOT": str(layout.root / "empty-workshop"),
        "PYTHONPATH": str(layout.project),
        "JEVBRAIN_MAX_ACTIONS": str(_bounded_actions(max_actions)),
        "SPIREBRAIN_MAX_REQUEST_ATTEMPTS": str(
            _bounded_request_attempts(request_attempt_cap)),
        "SPIREBRAIN_REQUEST_ATTEMPT_FILE": str(layout.request_counter),
        # Do not let Steam attach this temporary install to the user's cloud
        # save slot. The source game and its saves are never launched.
        "SteamAppId": "0",
        "SteamGameId": "0",
    })
    drive, tail = os.path.splitdrive(str(layout.user_profile))
    if drive:
        env["HOMEDRIVE"] = drive
        env["HOMEPATH"] = tail or "\\"
    for name in ("JAVA_TOOL_OPTIONS", "_JAVA_OPTIONS", "JDK_JAVA_OPTIONS",
                 "PYTHONHOME",
                 "STEAM_COMPAT_DATA_PATH", "STEAM_COMPAT_CLIENT_INSTALL_PATH",
                 "STEAM_COMPAT_INSTALL_PATH"):
        env.pop(name, None)
    for name, value in (("JEV_BACKEND", jev_backend),
                        ("BRAIN_BACKEND", brain_backend),
                        ("BRAIN_MODEL", brain_model),
                        ("BRAIN_ENDPOINT", brain_endpoint),
                        ("BRAIN_REASONING_EFFORT", reasoning_effort)):
        if value is not None:
            if not str(value).strip() or "\n" in str(value) or "\r" in str(value):
                raise ValueError(f"{name} override must be a non-empty single line")
            if name in {"BRAIN_BACKEND", "BRAIN_MODEL", "BRAIN_REASONING_EFFORT"} \
                    and any(character.isspace() for character in str(value)):
                raise ValueError(f"{name} override cannot contain whitespace")
            if name == "BRAIN_ENDPOINT":
                endpoint = urlsplit(str(value).strip())
                if (endpoint.scheme not in {"http", "https"} or not endpoint.netloc
                        or endpoint.username or endpoint.password
                        or endpoint.query or endpoint.fragment):
                    raise ValueError(
                        "BRAIN_ENDPOINT override must be an HTTP(S) URL without credentials/query")
            env[name] = str(value).strip()
    return env


def _launch_commands(layout: IsolatedWorkspace, *, mode: str,
                     jev_backend: str | None = "openrouter",
                     brain_backend: str | None = None, brain_model: str | None = None,
                     brain_endpoint: str | None = None,
                     strategy_planning_enabled: bool = True) -> tuple[list[str], list[str]]:
    if mode not in {"advise", "play"}:
        raise ValueError("mode must be 'advise' or 'play'")
    agent = [sys.executable, "-u", "start.py", "--setup", "--no-browser",
             "--port", "0", f"--{mode}"]
    # Pin a live JEV implementation on the command line, so a sanitized
    # isolated environment cannot silently resolve to the offline mock.
    agent.extend(("--backend", str(jev_backend or "openrouter")))
    if not strategy_planning_enabled:
        agent.append("--jev-only")
    for flag, value in (("--brain-backend", brain_backend),
                        ("--brain-model", brain_model),
                        ("--brain-endpoint", brain_endpoint)):
        if value is not None:
            agent.extend((flag, str(value)))
    java = [str(layout.game / "jre" / "bin" / "java.exe"),
            f"-Duser.home={layout.user_profile}",
            f"-Djava.util.prefs.userRoot={layout.java_preferences}",
            f"-Djava.util.prefs.systemRoot={layout.java_preferences / 'system'}",
            "-jar", str(layout.game / "mods" / "ModTheSpire.jar"),
            "--skip-launcher"]
    return agent, java


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
    # A successful mock response proves the pipe ran, not that a live model
    # provider is usable. Missing backend identity is also insufficient for a
    # real-game release signal.
    mock_backends = {"mock", "offline-mock", "offline_mock", "stub", "test"}
    successes = sum(
        1 for event in events
        if event.get("fallback") is False
        and str(event.get("backend") or "").strip().lower() not in mock_backends
        and str(event.get("backend") or "").strip()
    )
    mock_successes = sum(
        1 for event in events
        if event.get("fallback") is False
        and str(event.get("backend") or "").strip().lower() in mock_backends
    )
    failures = sum(1 for event in events if event.get("fallback") is True)
    unknown = sum(1 for event in events
                  if event.get("fallback") is not True
                  and event.get("fallback") is not False)
    unknown_identity = sum(
        1 for event in events
        if event.get("fallback") is False
        and not str(event.get("backend") or "").strip()
    )
    if successes and (failures or unknown or mock_successes or unknown_identity):
        return "degraded"
    if successes:
        return "healthy"
    if mock_successes:
        return "degraded" if failures or unknown or unknown_identity else "mock"
    return "unknown" if unknown or unknown_identity else "unavailable"


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
                  backup: str = "", *, strategy_enabled: bool = True) -> dict:
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
    # A process can be stopped while its strategic request is still in flight,
    # before brain_calls.jsonl gets its response row. The trace's explicit
    # strategic scope/fingerprint marks those requests without confusing JEV,
    # which shares the generic provider_request event name.
    brain_request_ids = {str(event.get("request_id")) for event in brain
                         if event.get("request_id")}
    strategic_trace_requests = [
        event for event in trace
        if event.get("event_type") == "provider_request"
        and (event.get("strategic_scope_id") or event.get("strategy_fingerprint"))
    ]
    strategic_trace_request_ids = {str(event.get("request_id"))
                                   for event in strategic_trace_requests
                                   if event.get("request_id")}
    anonymous_strategic_trace_requests = sum(
        1 for event in strategic_trace_requests if not event.get("request_id"))
    strategic_request_count = (len(brain_request_ids | strategic_trace_request_ids)
                               + anonymous_strategic_trace_requests
                               + sum(1 for event in brain if not event.get("request_id")))
    pipeline_passed = bool(decisions)
    jev_health = _provider_health(jev)
    strategic_activity = bool(strategic_request_count)
    strategic_health = (_provider_health(brain) if strategy_enabled
                        else ("unexpected_activity" if strategic_activity
                              else "disabled_by_design"))
    lifecycle = _request_lifecycle(trace)
    strategic_usable_successes = max(0, len(strategic_successes) - expired_strategic)
    if not strategy_enabled:
        strategic_status = ("unexpected_activity" if strategic_activity
                            else "disabled_by_design")
    elif not brain:
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
    provider_health_passed = (jev_health == "healthy" and
                              ((strategic_health == "healthy") if strategy_enabled
                               else strategic_health == "disabled_by_design"))
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
        "strategy_enabled_during_run": bool(strategy_enabled),
        "strategic_requests_during_run": strategic_request_count,
        "strategic_status": strategic_status,
        "strategic_backend": next((str(event.get("backend")) for event in brain
                                    if event.get("backend")), None),
        "jev_backend": next((str(event.get("backend")) for event in jev
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


def run(*, duration: float, allow_real_game: bool, mode: str = "advise",
        keep_workspace: bool = False, request_attempt_cap: int = DEFAULT_REQUEST_ATTEMPT_CAP,
        max_actions: int = MAX_SMOKE_ACTIONS, jev_backend: str | None = None,
        brain_backend: str | None = None,
        brain_model: str | None = None, brain_endpoint: str | None = None,
        reasoning_effort: str | None = None) -> dict:
    if not allow_real_game:
        return {"passed": False, "skipped": True,
                "message": "真实游戏测试需要显式传入 --allow-real-game"}
    mode = str(mode or "advise").strip().lower()
    if mode not in {"advise", "play"}:
        raise ValueError("mode must be 'advise' or 'play'")
    duration = _bounded_duration(duration)
    request_attempt_cap = _bounded_request_attempts(request_attempt_cap)
    max_actions = _bounded_actions(max_actions)
    # Real gameplay smoke intentionally never enables the strategic provider.
    # It runs JEV (defaulting to the live OpenRouter adapter) plus local rules.
    jev_backend = str(jev_backend or "openrouter").strip().lower()
    if jev_backend not in {"openrouter", "official", "cloudflare"}:
        raise ValueError(
            "real-game smoke requires a live JEV backend: openrouter, official, or cloudflare")
    strategy_planning_enabled = False
    root = PROJECT_ROOT
    game, java, mts_jar = _find_paths()
    workshop = Path(os.environ.get(
        "STS_WORKSHOP_ROOT",
        r"E:\LeStoreDownload\steam\steamapps\workshop\content\646570",
    ))
    requested_temp = Path(os.environ.get("TEMP") or tempfile.gettempdir()).expanduser()
    temp_root = requested_temp if requested_temp.is_dir() else Path(tempfile.gettempdir())
    temp_root = temp_root.resolve()
    for source in (root.resolve(), game.resolve()):
        if temp_root == source or source in temp_root.parents:
            raise ValueError("TEMP must not point inside the project or source game")
    temp_context = tempfile.TemporaryDirectory(
        prefix="jev-spire-real-smoke-", dir=temp_root)
    workspace = Path(temp_context.name)
    agent = None
    mts = None
    completed = False
    started = time.time()
    try:
        layout = _create_isolated_workspace(root, game, java, mts_jar,
                                            workshop, workspace)
        env = _isolated_environment(
            layout, request_attempt_cap=request_attempt_cap, max_actions=max_actions,
            jev_backend=jev_backend,
            brain_backend=brain_backend, brain_model=brain_model,
            brain_endpoint=brain_endpoint, reasoning_effort=reasoning_effort,
        )
        agent_args, java_args = _launch_commands(
            layout, mode=mode, jev_backend=jev_backend,
            strategy_planning_enabled=strategy_planning_enabled,
            brain_backend=brain_backend,
            brain_model=brain_model, brain_endpoint=brain_endpoint,
        )
        # This is the only project/config pair started by the test. start.py's
        # explicit --setup writes only under the isolated LOCALAPPDATA.
        agent = subprocess.Popen(
            agent_args, cwd=layout.project, env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        time.sleep(3)
        if agent.poll() is None:
            mts = subprocess.Popen(
                java_args, cwd=layout.game, env=env,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        deadline = time.monotonic() + duration
        while time.monotonic() < deadline:
            if agent.poll() is not None or (mts is not None and mts.poll() is not None):
                break
            time.sleep(min(1.0, max(0.0, deadline - time.monotonic())))
        for process in (mts, agent):
            _stop_process_tree(process)

        logs = layout.project / "logs"
        brain = _json_events(logs / "brain_calls.jsonl", started)
        jev = _json_events(logs / "jev_calls.jsonl", started)
        trace = _json_events(logs / "decision_trace.jsonl", started)
        report = _build_report(brain, jev, trace,
                               strategy_enabled=strategy_planning_enabled)
        attempts_used = read_attempt_count(layout.request_counter)
        report.update({
            "isolated": True,
            "workspace_retained": bool(keep_workspace),
            "workspace": str(workspace) if keep_workspace else None,
            "source_paths": sorted({str(path) for path in layout.source_paths}),
            "mod_jars_copied": list(layout.mod_jar_names),
            "mode": mode,
            "duration_limit_seconds": duration,
            "max_actions": max_actions,
            "request_attempt_cap": request_attempt_cap,
            "request_attempts_used": attempts_used,
            "request_attempt_cap_reached": attempts_used >= request_attempt_cap,
            "provider_overrides": {
                "jev_backend": jev_backend,
                "brain_backend": brain_backend,
                "brain_model": brain_model,
                "reasoning_effort": reasoning_effort,
            },
        })
        if keep_workspace:
            temp_context._finalizer.detach()
        completed = True
        return report
    finally:
        for process in (mts, agent):
            _stop_process_tree(process)
        if not keep_workspace or not completed:
            temp_context.cleanup()


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-real-game", action="store_true")
    parser.add_argument("--duration", type=float, default=30.0)
    parser.add_argument("--mode", choices=("advise", "play"), default="advise",
                        help="real-game mode; advise is the safe default")
    parser.add_argument("--keep-workspace", action="store_true",
                        help="retain the isolated temporary copy for inspection")
    parser.add_argument("--max-actions", type=int, default=MAX_SMOKE_ACTIONS,
                        help=f"hard Agent command limit (1-{MAX_SMOKE_ACTIONS})")
    parser.add_argument("--request-attempt-cap", type=int,
                        default=DEFAULT_REQUEST_ATTEMPT_CAP,
                        help=f"shared GPT/JEV HTTP attempt limit (1-{MAX_REQUEST_ATTEMPT_CAP})")
    parser.add_argument("--jev-backend", help="temporary JEV provider override")
    parser.add_argument("--brain-backend", help="temporary strategic provider override")
    parser.add_argument("--brain-model", help="temporary strategic model override")
    parser.add_argument("--brain-endpoint", help="temporary strategic endpoint override")
    parser.add_argument("--reasoning-effort",
                        help="temporary strategic reasoning-effort override")
    args = parser.parse_args(argv)
    result = run(
        duration=args.duration,
        allow_real_game=args.allow_real_game,
        mode=args.mode,
        keep_workspace=args.keep_workspace,
        max_actions=args.max_actions,
        request_attempt_cap=args.request_attempt_cap,
        jev_backend=args.jev_backend,
        brain_backend=args.brain_backend,
        brain_model=args.brain_model,
        brain_endpoint=args.brain_endpoint,
        reasoning_effort=args.reasoning_effort,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    # ``skipped`` is an intentional operator opt-in guard.  An opted-in run
    # must satisfy provider health and freshness as well as producing a game
    # decision; otherwise automation must fail loudly instead of accepting a
    # rules-only/degraded smoke as release evidence.
    return 0 if result.get("release_passed", result.get("passed")) or result.get("skipped") else 1


if __name__ == "__main__":
    raise SystemExit(main())
