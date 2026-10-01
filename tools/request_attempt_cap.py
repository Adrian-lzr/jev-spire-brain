"""Process-tree request-attempt cap for isolated live-game smoke runs.

The cap is installed through ``sitecustomize`` in the copied project.  A small
locked counter file makes the limit survive CommunicationMod restarting the
Agent process and keeps concurrent provider workers under one shared budget.
"""

from __future__ import annotations

import os
import threading
import urllib.request
from urllib.parse import urlsplit
from pathlib import Path
from typing import Mapping


CAP_ENV = "SPIREBRAIN_MAX_REQUEST_ATTEMPTS"
COUNTER_ENV = "SPIREBRAIN_REQUEST_ATTEMPT_FILE"
_PROCESS_LOCK = threading.Lock()


class RequestAttemptLimitExceeded(OSError):
    """Raised before a network request when the smoke-run cap is exhausted."""


def _locked_counter_file(path: Path, cap: int) -> tuple[int, bool]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        if os.name == "nt":
            import msvcrt

            stream.seek(0, os.SEEK_END)
            if stream.tell() == 0:
                stream.write(b"0")
                stream.flush()
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
            unlock = lambda: msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            unlock = lambda: fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        try:
            stream.seek(0)
            raw = stream.read().decode("ascii", "ignore").strip()
            used = int(raw or "0") if raw.isdigit() else 0
            if used >= cap:
                return used, False
            used += 1
            stream.seek(0)
            stream.truncate()
            stream.write(str(used).encode("ascii"))
            stream.flush()
            os.fsync(stream.fileno())
            return used, True
        finally:
            stream.seek(0)
            unlock()


def _locked_counter(path: Path, cap: int) -> tuple[int, bool]:
    # Windows byte-range locks do not reliably serialize sibling threads in
    # the same process. The local lock covers threads; the file lock covers
    # separate Agent processes/restarts.
    with _PROCESS_LOCK:
        return _locked_counter_file(path, cap)


def read_attempt_count(path: str | Path) -> int:
    try:
        raw = Path(path).read_text(encoding="ascii").strip()
        return int(raw) if raw.isdigit() else 0
    except (OSError, ValueError):
        return 0


def _is_external_http_request(args: tuple, kwargs: dict) -> bool:
    request = args[0] if args else kwargs.get("url")
    url = getattr(request, "full_url", request)
    if not isinstance(url, str):
        return False
    parsed = urlsplit(url)
    if parsed.scheme.lower() not in {"http", "https"}:
        return False
    return (parsed.hostname or "").lower() not in {
        "localhost", "127.0.0.1", "::1",
    }


def install_urlopen_guard(*, environ: Mapping[str, str] | None = None,
                          request_module=urllib.request) -> bool:
    """Wrap urllib's opener when an isolated smoke cap is configured.

    Returns ``False`` when no cap is configured, preserving normal launches.
    A malformed or incomplete cap configuration fails closed.
    """
    env = os.environ if environ is None else environ
    raw_cap = str(env.get(CAP_ENV, "") or "").strip()
    raw_path = str(env.get(COUNTER_ENV, "") or "").strip()
    if not raw_cap and not raw_path:
        return False
    if not raw_cap.isdigit() or int(raw_cap) < 1 or not raw_path:
        raise RuntimeError("invalid isolated request-attempt cap configuration")
    cap = int(raw_cap)
    counter = Path(raw_path)
    original = request_module.urlopen
    if getattr(original, "_spirebrain_request_guard", False):
        return True

    def capped_urlopen(*args, **kwargs):
        if _is_external_http_request(args, kwargs):
            used, allowed = _locked_counter(counter, cap)
            if not allowed:
                raise RequestAttemptLimitExceeded(
                    f"isolated model request-attempt cap reached ({cap})")
        return original(*args, **kwargs)

    capped_urlopen._spirebrain_request_guard = True
    request_module.urlopen = capped_urlopen
    return True

