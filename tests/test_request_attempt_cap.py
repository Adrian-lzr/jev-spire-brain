import os
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from urllib.request import Request
from pathlib import Path

import pytest

from tools.request_attempt_cap import (
    CAP_ENV,
    COUNTER_ENV,
    RequestAttemptLimitExceeded,
    install_urlopen_guard,
    read_attempt_count,
)


def test_request_attempt_cap_is_shared_and_blocks_before_network(tmp_path):
    attempts = []

    def fake_urlopen(request, **kwargs):
        attempts.append((request, kwargs))
        return "response"

    module = SimpleNamespace(urlopen=fake_urlopen)
    counter = tmp_path / "request-count"
    assert install_urlopen_guard(environ={
        CAP_ENV: "2",
        COUNTER_ENV: str(counter),
    }, request_module=module)

    gpt_request = Request("https://gpt.example.invalid/v1/responses")
    jev_request = Request("https://jev.example.invalid/v1/score")
    blocked_request = Request("https://blocked.example.invalid/v1/score")
    assert module.urlopen(gpt_request, timeout=1) == "response"
    assert module.urlopen(jev_request, timeout=1) == "response"
    with pytest.raises(RequestAttemptLimitExceeded):
        module.urlopen(blocked_request, timeout=1)

    assert [request for request, _ in attempts] == [gpt_request, jev_request]
    assert read_attempt_count(counter) == 2


def test_request_attempt_cap_is_atomic_for_concurrent_provider_workers(tmp_path):
    calls = []
    module = SimpleNamespace(urlopen=lambda request, **kwargs: calls.append(request) or "ok")
    counter = tmp_path / "concurrent-count"
    install_urlopen_guard(environ={CAP_ENV: "7", COUNTER_ENV: str(counter)},
                          request_module=module)

    def request(index):
        try:
            module.urlopen(Request(f"https://provider.example.invalid/{index}"))
            return True
        except RequestAttemptLimitExceeded:
            return False

    with ThreadPoolExecutor(max_workers=12) as pool:
        allowed = list(pool.map(request, range(30)))

    assert sum(allowed) == 7
    assert len(calls) == 7
    assert read_attempt_count(counter) == 7


def test_request_attempt_cap_is_disabled_without_harness_environment():
    module = SimpleNamespace(urlopen=lambda *args, **kwargs: "ok")
    assert not install_urlopen_guard(environ={}, request_module=module)
    assert module.urlopen("request") == "ok"


@pytest.mark.parametrize("environ", [
    {CAP_ENV: "2"},
    {COUNTER_ENV: "counter"},
    {CAP_ENV: "0", COUNTER_ENV: "counter"},
    {CAP_ENV: "many", COUNTER_ENV: "counter"},
])
def test_partial_or_invalid_request_cap_fails_closed(environ):
    with pytest.raises(RuntimeError, match="request-attempt cap"):
        install_urlopen_guard(environ=environ,
                              request_module=SimpleNamespace(urlopen=lambda *a, **k: None))


def test_sitecustomize_cap_survives_agent_process_restarts(tmp_path):
    from tools.real_game_smoke import _install_request_cap_bootstrap

    project = tmp_path / "copy"
    tools_dir = project / "tools"
    tools_dir.mkdir(parents=True)
    source = Path(__file__).resolve().parents[1] / "tools" / "request_attempt_cap.py"
    shutil.copy2(source, tools_dir / source.name)
    _install_request_cap_bootstrap(project)
    counter = tmp_path / "attempts.count"
    env = dict(os.environ)
    env.update({
        "PYTHONPATH": str(project),
        CAP_ENV: "2",
        COUNTER_ENV: str(counter),
    })
    code = (
        "import urllib.request\n"
        "try:\n"
        " urllib.request.urlopen('http://127.0.0.2:1/probe', timeout=0.1)\n"
        "except OSError as exc:\n"
        " print(type(exc).__name__, str(exc))\n"
    )

    # Two provider attempts are allowed across distinct Agent processes. The
    # third must be rejected before urlopen reaches its underlying network call.
    subprocess.run([sys.executable, "-c", code], env=env, check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
    subprocess.run([sys.executable, "-c", code], env=env, check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
    blocked = subprocess.run(
        [sys.executable, "-c", code], env=env, check=False,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5,
    )

    assert b"RequestAttemptLimitExceeded" in blocked.stdout
    assert b"request-attempt cap reached (2)" in blocked.stdout
    assert read_attempt_count(counter) == 2
