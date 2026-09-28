"""Small, failure-tolerant helpers for bounded local JSONL logs."""

from __future__ import annotations

import os
import threading
from pathlib import Path
import re


def _int_env(name: str, default: int, *, minimum: int = 0) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, value)


def log_max_bytes() -> int:
    """Maximum active JSONL size; zero disables rotation."""
    return _int_env("SPIREBRAIN_LOG_MAX_BYTES", 32 * 1024 * 1024)


def log_backup_count() -> int:
    return _int_env("SPIREBRAIN_LOG_BACKUPS", 2, minimum=0)


def rotated_log_paths(path: str | Path) -> list[Path]:
    """Return retained JSONL files oldest-first, then the active file."""
    target = Path(path)
    backups = []
    pattern = re.compile(re.escape(target.name) + r"\.(\d+)$")
    try:
        for candidate in target.parent.glob(target.name + ".*"):
            match = pattern.fullmatch(candidate.name)
            if match:
                backups.append((int(match.group(1)), candidate))
    except OSError:
        pass
    return [candidate for _, candidate in sorted(backups, reverse=True)] + [target]


def append_bounded(path: str | Path, text: str, *, max_bytes: int | None = None,
                   backup_count: int | None = None, lock: threading.Lock | None = None) -> bool:
    """Append text and rotate old JSONL files before they exceed the limit.

    Rotation is best-effort and deliberately side-channel safe: permission,
    encoding and rename errors return ``False`` rather than affecting the game
    decision. A single oversized line is retained intact so trace records are
    never silently truncated.
    """
    target = Path(path)
    limit = log_max_bytes() if max_bytes is None else max(0, int(max_bytes))
    backups = log_backup_count() if backup_count is None else max(0, int(backup_count))
    encoded = str(text).encode("utf-8")

    def write() -> bool:
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            if limit and target.exists():
                try:
                    current_size = target.stat().st_size
                except OSError:
                    current_size = 0
                if current_size and current_size + len(encoded) > limit:
                    overflow = target.with_name(target.name + f".{backups + 1}")
                    if overflow.exists():
                        overflow.unlink()
                    for index in range(backups - 1, 0, -1):
                        source = target.with_name(target.name + f".{index}")
                        destination = target.with_name(target.name + f".{index + 1}")
                        if source.exists():
                            source.replace(destination)
                    if backups:
                        target.replace(target.with_name(target.name + ".1"))
                    else:
                        target.unlink()
            with target.open("a", encoding="utf-8") as stream:
                stream.write(str(text))
            return True
        except (OSError, UnicodeError, TypeError, ValueError):
            return False

    if lock is None:
        return write()
    with lock:
        return write()
