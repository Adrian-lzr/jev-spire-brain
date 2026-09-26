"""Run a bounded, credential-free end-to-end smoke test.

This intentionally exercises the real launcher and dashboard process, but uses
the synthetic demo and the mock strategic provider.  It never writes game
configuration, contacts a provider, or starts ModTheSpire.  Real-game runs
remain an explicit operator action because they require a visible game and
restorable save/config backups.
"""

from __future__ import annotations

import argparse
import json
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen


def _get_json(url: str) -> dict:
    with urlopen(url, timeout=1.0) as response:
        value = json.loads(response.read().decode("utf-8"))
    return value if isinstance(value, dict) else {}


def run(*, timeout: float = 8.0) -> dict:
    root = Path(__file__).resolve().parents[1]
    command = [
        sys.executable, "-u", str(root / "start.py"), "--demo", "--backend", "mock",
        "--brain-backend", "mock", "--no-browser", "--port", "0",
    ]
    process = subprocess.Popen(
        command, cwd=root, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace",
    )
    output: list[str] = []
    lines: queue.Queue[str] = queue.Queue()

    def read_output() -> None:
        if process.stdout is None:
            return
        for line in process.stdout:
            lines.put(line.rstrip())

    reader = threading.Thread(target=read_output, daemon=True)
    reader.start()
    started = time.monotonic()
    dashboard_url = ""
    state: dict = {}
    try:
        while time.monotonic() - started < timeout:
            while True:
                try:
                    line = lines.get_nowait()
                except queue.Empty:
                    break
                output.append(line)
                marker = "dashboard running: http://127.0.0.1:"
                if marker in line:
                    dashboard_url = line[line.index("http://127.0.0.1:"):].strip()
            if dashboard_url:
                try:
                    state = _get_json(dashboard_url + "/state")
                except (URLError, TimeoutError, OSError, json.JSONDecodeError):
                    pass
            if process.poll() is not None:
                break
            time.sleep(0.05)
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
        reader.join(timeout=0.5)
        while True:
            try:
                output.append(lines.get_nowait())
            except queue.Empty:
                break

    result = {
        "passed": bool(dashboard_url and state),
        "dashboard_url": dashboard_url,
        "state_received": bool(state),
        "process_returncode": process.returncode,
        "synthetic": True,
        "network_provider_called": False,
        "output_tail": output[-20:],
    }
    if not result["passed"]:
        raise RuntimeError(json.dumps(result, ensure_ascii=False, indent=2))
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=float, default=8.0)
    args = parser.parse_args(argv)
    result = run(timeout=max(2.0, args.timeout))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
