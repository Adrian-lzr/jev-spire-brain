"""Local dashboard server — SSE stream + one static page, stdlib only.

Phase 1.5. Serves three things:

    GET  /                  the dashboard page (dashboard.html, from disk)
    GET  /events            the live feed as Server-Sent Events
    POST /publish           cross-process publishing (kind + JSON body)

Why SSE and not WebSocket: SSE is one HTTP response the server never closes, so
`http.server` can do it with zero dependencies — and the agent process may be
spawned by the game with whatever Python it finds, where a websocket library
may not exist. The page connects with `EventSource`, which every browser has.

Why POST /publish: the live agent runs *inside the game's process tree* on
stdio; it cannot also bind a port without risking a collision with the game.
So the split is: the dashboard server is a separate little process, and any
agent process can publish into it with one HTTP call (see `publish_to_url`).
In-process (simulator, demo, replay), the feed object is shared directly and
this endpoint is unused.

The whole thing follows the project's failure rules: a subscriber that dies is
dropped, a client that walks away is forgotten, and nothing here can crash the
run it is watching.
"""

from __future__ import annotations

import json
import queue
import sys
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from spirebrain.overlay.feed import EVENT_KINDS, DecisionFeed

ROOT = Path(__file__).resolve().parents[2]
DASHBOARD = Path(__file__).resolve().parent / "dashboard.html"


class _Client:
    """One SSE subscriber: an unbounded queue plus a liveness flag."""

    def __init__(self) -> None:
        self.q: queue.Queue = queue.Queue()
        self.alive = True

    def put(self, event: dict, block: bool = False) -> None:
        if self.alive:
            self.q.put(event, block=block)

    def sse_lines(self):
        """Yield the backlog, then live events, as SSE frames."""
        try:
            while self.alive:
                try:
                    event = self.q.get(timeout=15)
                except queue.Empty:
                    yield ": keep-alive\n\n"  # a quiet proxy kills idle streams
                    continue
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        finally:
            self.alive = False


def _state_snapshot(feed: DecisionFeed) -> dict:
    """A bounded JSON view for pollers (the in-game mod): latest decision + run state.

    SSE is for the browser; a Java 8 HttpURLConnection has no event-source
    support and an infinite stream would hold a game thread hostage. One GET,
    one small JSON body, done.

    Advisor mode added `last_advice` and `last_outcome` to this payload: the
    in-game overlay is the one surface that reaches a player *without* a second
    window, so a recommendation that never left the browser tab would be advice
    nobody reads.
    """
    events = feed.history()
    run_state = None
    last_decision = None
    last_advice = None
    last_outcome = None
    agent_state = None
    for event in reversed(events):
        kind = event.get("kind")
        if last_decision is None and kind == "decision":
            detail = event.get("detail") or {}
            gate = detail.get("gate") or {}
            last_decision = {
                "point": event.get("point"),
                "value": event.get("value"),
                "confidence": event.get("confidence"),
                "fallback": event.get("fallback"),
                "reason": detail.get("reason") or gate.get("reason") or "",
                "gate": gate or None,
            }
        if last_advice is None and kind == "advice":
            last_advice = {
                "point": event.get("point"),
                "label": event.get("label"),
                # `verb` + `command` travel to the in-game overlay so it can draw
                # an ASCII form of the same advice. Without them a non-CJK install
                # shows a blank line where the one actionable sentence should be —
                # found 2026-09-22 by running the mod's own poller against /state.
                "verb": event.get("verb"),
                "command": event.get("command"),
                "reason": event.get("reason") or "",
                "confidence": event.get("confidence"),
                "fallback": event.get("fallback"),
                "act": event.get("act"),
                "floor": event.get("floor"),
                "agreement": event.get("agreement"),
                "tally": event.get("tally"),
                "state_id": event.get("state_id"),
                "status": event.get("status", "ready"),
                "source_type": event.get("source_type", ""),
                "source": event.get("source", ""),
                "guide_rules": event.get("guide_rules") or [],
            }
        if last_outcome is None and kind == "outcome":
            last_outcome = {
                "point": event.get("point"),
                "verdict": event.get("verdict"),
                "advice_label": event.get("advice_label"),
                "acted_label": event.get("acted_label"),
                # The action key, for the overlay's ASCII fallback.
                "acted": event.get("acted"),
                "agreement": event.get("agreement"),
                "tally": event.get("tally"),
            }
        if run_state is None and kind == "run_state":
            run_state = {k: v for k, v in event.items()
                         if k not in ("seq", "ts", "kind")}
        if agent_state is None and kind == "agent_state":
            agent_state = {
                "state": event.get("state"),
                "detail": event.get("detail") or "",
            }
        # All four, not three. Stopping at "decision + advice + run_state" was a
        # real bug, found by reading /state against a live advisor session
        # (2026-09-22): the agent's own observe() publishes a run_state BETWEEN
        # the verdict and the recommendation, so the scan hit that third field
        # and stopped one event short of the outcome — the panel showed a verdict
        # while /state reported none. The bound still holds: a feed keeps 2000
        # events and this is a dict lookup each.
        if run_state and last_decision and last_advice and last_outcome:
            break
    return {"last_decision": last_decision, "last_advice": last_advice,
            "last_outcome": last_outcome, "run_state": run_state,
            "agent_state": agent_state,
            "current_state_id": last_advice.get("state_id") if last_advice else None}


class PortInUse(RuntimeError):
    """The dashboard port is taken, and the message says what to do about it.

    Its own exception type rather than a bare OSError because the *fix* is worth
    naming: a listener on 8787 is usually a stale dashboard from an earlier
    session (or the player's own, still running), and either way the answer is
    "close it, or use --port", never "try again later".
    """

    def __init__(self, port: int, cause: BaseException) -> None:
        self.port = port
        self.cause = cause
        super().__init__(
            f"port {port} is already in use — another dashboard is running"
            f" (often a stale one from an earlier session). Close it, or start this"
            f" one elsewhere with `--port {port + 1}`. Underlying error: {cause}"
        )


class _ExclusiveHTTPServer(ThreadingHTTPServer):
    """A server that refuses to share its port.

    `socketserver` sets `SO_REUSEADDR` by default, and on **Windows** that lets a
    second process bind a port the first is still listening on. The failure is
    silent and nasty: connections are handed to whichever socket the kernel
    picks, so three stale dashboard processes ended up sharing 8787 on 2026-09-22
    and answered the in-game overlay's `/state` with `404` from old code — the
    panel stayed empty and nothing anywhere said why.

    Turning reuse off means the second `start.py` fails loudly instead, and
    `main()` explains how to fix it. A clear error beats a haunted port.
    """

    allow_reuse_address = False
    daemon_threads = True


class DashboardServer:
    """Bind a port, hold one DecisionFeed, serve the page and the stream."""

    def __init__(self, feed: DecisionFeed | None = None,
                 port: int = 8787, page_path: str | Path | None = None) -> None:
        # `is None`, not `or`: DecisionFeed defines __len__, so an empty (but
        # real) feed is falsy and `feed or DecisionFeed()` would silently swap
        # in a second feed — the one the agent publishes to would then not be
        # the one the server serves. Found by the overlay tests, 2026-09-22.
        self.feed = feed if feed is not None else DecisionFeed()
        self.port = port
        self.page_path = Path(page_path) if page_path else DASHBOARD
        self._handler = _make_handler(self.feed, self.page_path)
        try:
            self.httpd = _ExclusiveHTTPServer(("127.0.0.1", port), self._handler)
        except OSError as exc:
            raise PortInUse(port, exc) from exc
        self.port = self.httpd.server_address[1]  # port=0 -> the OS's choice

    def serve_forever(self) -> None:
        print(f"[dashboard] http://127.0.0.1:{self.port}", file=sys.stderr, flush=True)
        self.httpd.serve_forever()

    def start_background(self) -> threading.Thread:
        t = threading.Thread(target=self.serve_forever, daemon=True)
        t.start()
        return t

    def shutdown(self) -> None:
        self.httpd.shutdown()


def _make_handler(feed: DecisionFeed, page_path: Path):
    class Handler(BaseHTTPRequestHandler):
        # Quiet by default: the game may inherit this process's console, and a
        # log line per event would be noise. Errors still surface.
        def log_message(self, fmt, *args):  # noqa: N802 - stdlib signature
            pass

        def do_GET(self):  # noqa: N802 - stdlib name
            path = self.path.split("?", 1)[0]
            if path == "/events":
                self._serve_events()
            elif path in ("/", "/index.html"):
                self._serve_page()
            elif path == "/health":
                self._send_json(200, {"ok": True, "events": len(feed)})
            elif path == "/state":
                self._send_json(200, _state_snapshot(feed))
            else:
                self._send_json(404, {"error": f"no route {path!r}"})

        def do_POST(self):  # noqa: N802 - stdlib name
            path = self.path.split("?", 1)[0]
            if path != "/publish":
                self._send_json(404, {"error": f"no route {path!r}"})
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                kind = str(body.get("kind", ""))
                if kind not in EVENT_KINDS:
                    raise ValueError(f"kind must be one of {EVENT_KINDS}")
                payload = body.get("payload") or {}
                event = feed.publish(kind, payload)
                self._send_json(200, {"ok": True, "seq": event["seq"]})
            except Exception as exc:  # noqa: BLE001 - report, keep serving
                self._send_json(400, {"error": str(exc)})

        def _serve_events(self):
            client = _Client()
            feed.subscribe(client)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            try:
                for chunk in client.sse_lines():
                    self.wfile.write(chunk.encode("utf-8"))
                    self.wfile.flush()
            except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
                pass  # the tab was closed; normal
            finally:
                feed.unsubscribe(client)

        def _serve_page(self):
            try:
                html = page_path.read_bytes()
            except OSError as exc:
                self._send_json(500, {"error": f"dashboard page missing: {exc}"})
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html)))
            self.end_headers()
            self.wfile.write(html)

        def _send_json(self, code: int, obj: dict) -> None:
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return Handler


def publish_to_url(kind: str, payload: dict, url: str = "http://127.0.0.1:8787/publish",
                   timeout: float = 1.0) -> bool:
    """Publish from another process (the live agent) into a running dashboard.

    Best-effort by design: if the dashboard is not up, this returns False and
    the caller carries on. A missing spectator must never stall a run.
    """
    body = json.dumps({"kind": kind, "payload": payload}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status == 200
    except Exception:  # noqa: BLE001 - nobody watching is not an error
        return False


class BridgeFeed:
    """A DecisionFeed look-alike that forwards every publish over HTTP.

    This is the live-pipe half of the interaction layer: the agent is spawned
    by the game on stdio and must not bind ports (a collision would be fatal
    inside the game's process tree), so it gets this proxy instead. It satisfies
    exactly the surface the agent uses — `publish(kind, payload)` — and drops
    events silently when no dashboard is listening, by the same rule as
    `publish_to_url`: nobody watching is not an error.

    Deliberately NOT a subclass: the agent duck-types its feed, and subclassing
    would tempt inheritance of behaviour the bridge must not have (no journal,
    no local history, no subscribers — the server owns all of that).
    """

    def __init__(self, url: str = "http://127.0.0.1:8787/publish",
                 timeout: float = 0.5) -> None:
        self.url = url
        self.timeout = timeout
        self.published = 0   # diagnostics: what the agent tried to send
        self.dropped = 0     # diagnostics: what no dashboard received

    def publish(self, kind: str, payload: dict) -> dict:
        if publish_to_url(kind, payload, url=self.url, timeout=self.timeout):
            self.published += 1
        else:
            self.dropped += 1
        # Return value mirrors a stored event closely enough for the agent,
        # which ignores it anyway — the run never reads the feed's answer.
        return {"kind": kind, "seq": -1, **payload}

    # Compatibility shims so a BridgeFeed can sit anywhere a DecisionFeed can:
    # the agent never calls these, but tests and demo code might probe them.
    def history(self, limit=None):
        return []

    def subscribe(self, sink):
        return sink

    def unsubscribe(self, sink):
        pass

    def __len__(self):
        # 0 means "unknown/none of your business" — and falsy, which is exactly
        # why DashboardServer must use `is None` (see the comment there).
        return 0
