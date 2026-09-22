"""Tests for the overlay layer: feed, agent mounting, server, demo.

The interaction layer gets the same treatment as everything else: the feed can
never break a run, a dead subscriber is dropped rather than retried, and the
whole loop is proven offline. HTTP is exercised for real over 127.0.0.1 on an
OS-picked port, so these tests need no network access.
"""

from __future__ import annotations

import json
import queue
import sys
import tempfile
import threading
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from spirebrain.driver.agent import SpireBrainAgent
from spirebrain.overlay.demo import run_demo
from spirebrain.overlay.feed import DecisionFeed, decision_event, run_state_event
from spirebrain.overlay.server import DashboardServer, publish_to_url

DECK_10 = [{"name": "Strike", "type": "Attack", "cost": 1}] * 5 + \
          [{"name": "Defend", "type": "Skill", "cost": 1}] * 4 + \
          [{"name": "Bash", "type": "Attack", "cost": 2}]


class Sink(queue.Queue):
    def put(self, event, block=False):  # match DecisionFeed's calling convention
        super().put(event, block=block)


# --------------------------------------------------------------------------- #
# Feed
# --------------------------------------------------------------------------- #
def test_feed_publishes_and_replays_history():
    with tempfile.TemporaryDirectory() as tmp:
        feed = DecisionFeed(journal_path=Path(tmp) / "feed.jsonl")
        feed.publish("run_state", {"act": 1})
        feed.publish("decision", {"point": "map", "value": "n0"})
        assert len(feed) == 2
        assert feed.history()[0]["kind"] == "run_state"
        assert feed.history()[1]["seq"] == 2  # monotone sequence
        # The journal mirrors the stream on disk.
        lines = (Path(tmp) / "feed.jsonl").read_text(encoding="utf-8").splitlines()
        assert [json.loads(l)["kind"] for l in lines] == ["run_state", "decision"]


def test_feed_rejects_unknown_kinds():
    feed = DecisionFeed()
    try:
        feed.publish("gossip", {})
        assert False, "unknown kind must raise"
    except ValueError:
        pass


def test_feed_new_subscriber_receives_backlog_then_live():
    feed = DecisionFeed()
    feed.publish("run_state", {"act": 1})
    sink = Sink()
    feed.subscribe(sink)
    assert sink.qsize() == 1  # backlog first
    feed.publish("decision", {"point": "map", "value": "n1"})
    assert sink.qsize() == 2  # then live


def test_feed_drops_dead_subscribers_without_breaking_publish():
    class Dead:
        def put(self, event, block=False):
            raise RuntimeError("tab closed")

    feed = DecisionFeed()
    dead = Dead()
    feed.subscribe(dead)
    feed.publish("run_state", {"act": 1})  # must not raise
    assert all(s is not dead for s, _ in feed._subs)


# --------------------------------------------------------------------------- #
# Payload builders
# --------------------------------------------------------------------------- #
def test_decision_event_flattens_and_rounds():
    from spirebrain.jev_brain.decisions import Decision

    d = Decision("map", "n0", 0.51234, True, {"reason": "r", "odd": {1, 2}})
    ev = decision_event(d, {"command": "choose", "choice": 0})
    assert ev["point"] == "map" and ev["confidence"] == 0.5123
    assert ev["fallback"] is True
    assert isinstance(ev["detail"]["odd"], list)  # unserialisable values str/list-ed


def test_run_state_event_reads_agent_defensively():
    with tempfile.TemporaryDirectory() as tmp:
        agent = SpireBrainAgent(jev_backend="mock", log_dir=tmp)
        ev = run_state_event(agent)  # nothing observed yet: still publishable
        assert isinstance(ev, dict)


# --------------------------------------------------------------------------- #
# Agent mounting
# --------------------------------------------------------------------------- #
def _base(**over) -> dict:
    game = {"act": 1, "max_hp": 80, "current_hp": 80, "gold": 200, "deck": DECK_10}
    game.update(over)
    return game


def test_agent_without_feed_behaves_exactly_as_before():
    """The feed is optional; None must mean zero behavioural change."""
    with tempfile.TemporaryDirectory() as tmp:
        agent = SpireBrainAgent(jev_backend="mock", log_dir=tmp)
        cmd = agent.choose_action(_base(screen_type="MAP", map={"next_nodes": [
            {"x": 0, "y": 4, "symbol": "M"}]}))
        assert cmd["command"] == "choose"
        assert agent.feed is None


def test_agent_publishes_state_and_decisions_to_feed():
    with tempfile.TemporaryDirectory() as tmp:
        feed = DecisionFeed()
        sink = Sink()
        feed.subscribe(sink)
        agent = SpireBrainAgent(jev_backend="mock", log_dir=tmp, feed=feed)
        agent.choose_action(_base(screen_type="CARD_REWARD", screen_state={
            "cards": [{"name": "Inflame", "description": "gain strength"}]}))
        kinds = []
        while True:
            try:
                kinds.append(sink.get_nowait()["kind"])
            except queue.Empty:
                break
        assert "run_state" in kinds and "decision" in kinds
        # The decision event carries what a spectator needs.
        feed_events = [e for e in feed.history() if e["kind"] == "decision"]
        assert feed_events and feed_events[0]["point"] == "card_reward"
        assert "confidence" in feed_events[0] and "detail" in feed_events[0]


def test_agent_survives_a_hostile_feed():
    """A dashboard that throws must never take the run down."""
    class Hostile:
        def publish(self, kind, payload):
            raise RuntimeError("no")

    with tempfile.TemporaryDirectory() as tmp:
        agent = SpireBrainAgent(jev_backend="mock", log_dir=tmp, feed=Hostile())
        cmd = agent.choose_action(_base(screen_type="MAP", map={"next_nodes": [
            {"x": 0, "y": 4, "symbol": "M"}]}))
        assert cmd["command"] == "choose"  # the run carried on


# --------------------------------------------------------------------------- #
# Server (real HTTP on 127.0.0.1)
# --------------------------------------------------------------------------- #
def _drain(sink: Sink, want: int, timeout: float = 5.0) -> list[dict]:
    events = []
    while len(events) < want:
        events.append(sink.get(timeout=timeout))
    return events


def test_server_serves_page_health_and_publish():
    feed = DecisionFeed()
    server = DashboardServer(feed=feed, port=0)
    server.start_background()
    try:
        base = f"http://127.0.0.1:{server.port}"
        with urllib.request.urlopen(base + "/health", timeout=5) as r:
            assert json.loads(r.read())["ok"] is True
        with urllib.request.urlopen(base + "/", timeout=5) as r:
            html = r.read().decode("utf-8")
            assert "JEV" in html and "EventSource" in html

        # Cross-process publishing lands in subscribers...
        sink = Sink()
        feed.subscribe(sink)
        ok = publish_to_url("run_state", {"act": 2}, url=base + "/publish")
        assert ok is True
        # publish() spreads the payload into the event's top level
        assert _drain(sink, 1)[0]["act"] == 2

        # ...and unknown kinds are rejected with a 400.
        body = json.dumps({"kind": "gossip", "payload": {}}).encode()
        req = urllib.request.Request(base + "/publish", data=body,
                                     headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=5)
            assert False, "bad kind must 400"
        except urllib.error.HTTPError as e:
            assert e.code == 400
    finally:
        server.shutdown()


def test_server_sse_stream_delivers_events():
    import http.client

    feed = DecisionFeed()
    server = DashboardServer(feed=feed, port=0)
    server.start_background()
    conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=10)
    try:
        conn.request("GET", "/events")
        resp = conn.getresponse()
        assert resp.status == 200
        assert resp.getheader("Content-Type").startswith("text/event-stream")

        # Publish from "the agent side" while the stream is open.
        feed.publish("run_state", {"act": 1})
        feed.publish("decision", {"point": "map", "value": "n0"})

        got = ""
        while got.count("\n\n") < 2:  # two complete SSE frames
            chunk = resp.read(1)
            if not chunk:
                break
            got += chunk.decode("utf-8")
        frames = [f for f in got.split("\n\n") if f.startswith("data: ")]
        events = [json.loads(f[len("data: "):]) for f in frames]
        assert [e["kind"] for e in events] == ["run_state", "decision"]  # backlog replays
    finally:
        conn.close()
        server.shutdown()


# --------------------------------------------------------------------------- #
# Demo (the full loop, offline)
# --------------------------------------------------------------------------- #
def test_demo_publishes_a_full_run_to_the_feed():
    """The demo reuses the real harness; every decision point must appear."""
    import time as _t

    with tempfile.TemporaryDirectory() as tmp:
        feed = DecisionFeed(journal_path=Path(tmp) / "feed.jsonl")
        # Reuse run_demo but on port 0 would close over DashboardServer; simpler:
        # drive the same path it drives (harness + feed) and assert on the feed.
        from spirebrain.sim import run_offline
        result = run_offline.run_one_simulation(seed=1, feed=feed)
        events = feed.history()
        kinds = {e["kind"] for e in events}
        assert kinds == {"run_state", "decision"}
        points = {e["point"] for e in events if e["kind"] == "decision"}
        assert points == {"map", "card_reward", "event", "rest", "shop",
                          "boss_relic", "combat_risk"}
        # Every fallback carries a reason a spectator can read.
        fb = [e for e in events if e.get("fallback")]
        assert fb and all(e.get("detail", {}).get("reason") or
                          e.get("detail", {}).get("gate") for e in fb)
        # And the harness result is untouched by the feed.
        assert result["jev_calls"] > 0


def test_demo_module_smoke():
    """The demo entry point itself: server up, run published, journal written.

    run_demo blocks after the simulation (the server stays up so the stream
    stays readable), so this asserts on what it leaves behind: the JSONL
    journal of the feed, written as the run happened. The journal is pointed
    into a temp dir so the repo's own logs/ stays untouched by tests.
    """
    import threading as _th
    import time as _time

    with tempfile.TemporaryDirectory() as tmp:
        import spirebrain.overlay.demo as demo_mod

        box: dict = {}
        journal = Path(tmp) / "logs" / "dashboard_feed.jsonl"
        original_root = demo_mod.ROOT

        def target():
            try:
                demo_mod.ROOT = Path(tmp)  # run_demo derives the journal path from ROOT
                box["result"] = run_demo(seed=3, port=0)
            finally:
                demo_mod.ROOT = original_root

        t = _th.Thread(target=target, daemon=True)
        t.start()
        deadline = _time.time() + 90
        while _time.time() < deadline:
            if journal.exists() and len(journal.read_text(encoding="utf-8").splitlines()) >= 40:
                break  # a full simulated run worth of events
            _time.sleep(0.5)
        lines = journal.read_text(encoding="utf-8").splitlines()
        kinds = {json.loads(l)["kind"] for l in lines}
        assert kinds == {"run_state", "decision", "run_end"}


# --------------------------------------------------------------------------- #
# BridgeFeed — the live-pipe half (agent process -> dashboard process)
# --------------------------------------------------------------------------- #
def test_bridge_feed_forwards_to_a_running_server():
    from spirebrain.overlay.server import BridgeFeed

    feed = DecisionFeed()
    server = DashboardServer(feed=feed, port=0)
    server.start_background()
    try:
        bridge = BridgeFeed(url=f"http://127.0.0.1:{server.port}/publish")
        bridge.publish("run_state", {"act": 1})
        bridge.publish("decision", {"point": "map", "value": "n0"})
        assert bridge.published == 2 and bridge.dropped == 0
        events = feed.history()
        assert [e["kind"] for e in events] == ["run_state", "decision"]
        assert events[1]["point"] == "map"  # payload spread to the top level
    finally:
        server.shutdown()


def test_bridge_feed_is_silent_when_no_dashboard_is_up():
    """A missing spectator must never be an error — and must never hang."""
    from spirebrain.overlay.server import BridgeFeed

    # Port 1 on 127.0.0.1: nothing listens there; a 0.2 s timeout keeps the
    # test fast while proving the drop path.
    bridge = BridgeFeed(url="http://127.0.0.1:1/publish", timeout=0.2)
    event = bridge.publish("run_state", {"act": 1})
    assert bridge.published == 0 and bridge.dropped == 1
    assert event["kind"] == "run_state"  # the call still "succeeds" for the agent


def test_bridge_feed_satisfies_the_agent_publish_surface():
    """The agent duck-types its feed; the bridge must fit the same hole."""
    from spirebrain.overlay.server import BridgeFeed

    class AgentShaped:
        def __init__(self, feed):
            self.feed = feed

        def do(self):
            self.feed.publish("decision", {"point": "rest", "value": "rest"})
            return self.feed.history() if hasattr(self.feed, "history") else []

    bridge = BridgeFeed(url="http://127.0.0.1:1/publish", timeout=0.2)
    assert AgentShaped(bridge).do() == []  # works with a real feed, too


def test_stdio_builds_agent_with_bridge_when_dashboard_url_given():
    """--dashboard-url must reach the agent as a BridgeFeed."""
    from spirebrain.overlay.server import BridgeFeed
    from spirebrain.driver.stdio import _build_agent

    with tempfile.TemporaryDirectory() as tmp:
        agent = _build_agent(None, "mock", None, dashboard_url="http://127.0.0.1:1/publish")
        assert isinstance(agent.feed, BridgeFeed)
        # And the flag off (the default) leaves the agent feedless, as before.
        plain = _build_agent(None, "mock", None)
        assert plain.feed is None


def test_end_to_end_bridge_agent_to_server_to_subscriber():
    """The full live-pipe story: agent-side bridge -> HTTP -> dashboard feed."""
    with tempfile.TemporaryDirectory() as tmp:
        feed = DecisionFeed()
        server = DashboardServer(feed=feed, port=0)
        server.start_background()
        try:
            from spirebrain.driver.stdio import _build_agent

            agent = _build_agent(None, "mock", None,
                                 dashboard_url=f"http://127.0.0.1:{server.port}/publish")
            sink = Sink()
            feed.subscribe(sink)
            agent.choose_action(_base(screen_type="BOSS_REWARD", screen_state={
                "relics": [{"name": "Runic Dome", "description": "energy, no intents"}]}))
            kinds = []
            while True:
                try:
                    kinds.append(sink.get_nowait()["kind"])
                except queue.Empty:
                    break
            assert "decision" in kinds
            decision = next(e for e in feed.history() if e["kind"] == "decision")
            assert decision["point"] == "boss_relic"
        finally:
            server.shutdown()


def test_state_snapshot_carries_the_advice_for_pollers():
    """The in-game overlay reads /state, not SSE.

    A recommendation that only ever reached the browser tab would be advice
    nobody reads while playing, so the snapshot has to carry it — including the
    verdict on the player's last choice and the running agreement rate.
    """
    from spirebrain.overlay.feed import DecisionFeed
    from spirebrain.overlay.server import _state_snapshot

    feed = DecisionFeed()
    feed.publish("run_state", {"act": 1, "floor": 3, "hp": 70, "max_hp": 80,
                               "gold": 99, "relics": []})
    feed.publish("advice", {"point": "combat", "label": "出「痛击」 → 咔咔",
                            "reason": "对手在下回合打 12", "confidence": 0.82,
                            "fallback": False, "act": 1, "floor": 3,
                            "tally": {"match": 0, "mismatch": 0, "unobserved": 0},
                            "agreement": None})
    feed.publish("outcome", {"point": "combat", "verdict": "match",
                             "advice_label": "出「痛击」 → 咔咔",
                             "acted_label": "出「痛击」 → 咔咔",
                             "tally": {"match": 1, "mismatch": 0, "unobserved": 0},
                             "agreement": 1.0})

    snap = _state_snapshot(feed)
    assert snap["last_advice"]["label"] == "出「痛击」 → 咔咔"
    assert snap["last_advice"]["confidence"] == 0.82
    assert snap["last_outcome"]["verdict"] == "match"
    assert snap["last_outcome"]["agreement"] == 1.0
    assert snap["run_state"]["floor"] == 3


def test_state_snapshot_is_empty_but_shaped_before_anything_happens():
    from spirebrain.overlay.feed import DecisionFeed
    from spirebrain.overlay.server import _state_snapshot

    snap = _state_snapshot(DecisionFeed())
    assert snap == {"last_decision": None, "last_advice": None,
                    "last_outcome": None, "run_state": None}


def test_state_snapshot_finds_the_outcome_past_the_agents_own_run_state():
    """The live ordering, pinned — this is where the first version broke.

    In a real advisor session the agent's `observe()` publishes a `run_state`
    *between* the verdict and the recommendation (verdict -> observe -> decision
    -> advice). A scan that stopped as soon as it had decision+advice+run_state
    therefore stopped one event short of the outcome: the browser panel showed a
    verdict while /state — the surface the in-game overlay reads — reported none.
    Found 2026-09-22 by reading /state of a running demo, not by a unit test.
    """
    from spirebrain.overlay.feed import DecisionFeed
    from spirebrain.overlay.server import _state_snapshot

    feed = DecisionFeed()
    feed.publish("run_state", {"act": 1, "floor": 3})            # step 1
    feed.publish("advice", {"point": "combat", "label": "出「打击」",
                            "confidence": 0.7, "tally": {}, "agreement": None})
    feed.publish("run_state", {"act": 1, "floor": 3})            # demo's own
    feed.publish("outcome", {"point": "combat", "verdict": "match",
                             "advice_label": "出「打击」", "acted_label": "出「打击」",
                             "tally": {"match": 1}, "agreement": 1.0})
    feed.publish("run_state", {"act": 1, "floor": 3})            # agent's observe()
    feed.publish("decision", {"point": "combat", "value": "end",
                              "confidence": 0.4, "detail": {}})
    feed.publish("advice", {"point": "combat", "label": "结束回合",
                            "confidence": 0.4, "tally": {"match": 1},
                            "agreement": 1.0})                   # newest

    snap = _state_snapshot(feed)
    assert snap["last_advice"]["label"] == "结束回合"   # newest advice wins
    assert snap["last_outcome"] is not None, "scan stopped before the verdict"
    assert snap["last_outcome"]["verdict"] == "match"


def test_state_snapshot_carries_every_field_the_in_game_overlay_reads():
    """A cross-language contract, pinned on the Python side.

    The in-game overlay (`java/src/main/java/io/github/adrianlzr/spirebrain/
    FeedClient.java`) parses this payload by key. A field renamed here fails no
    Java test — it silently blanks one line of the panel inside the game, which
    is the most expensive kind of regression to notice: you have to be mid-run to
    see it, and it looks like the agent's fault.

    Found exactly that way on 2026-09-22, by running the mod's own poller against
    a live `/state` (`python java/build.py --check http://127.0.0.1:8801`): the
    Chinese label arrived and the ASCII fallback came back empty, because
    `verb`/`command`/`acted` were never in the snapshot. On a non-CJK install that
    is a blank line where the one actionable sentence should be.

    A list of names, not a full comparison on purpose: this is the contract, not
    the payload.
    """
    from spirebrain.overlay.feed import DecisionFeed
    from spirebrain.overlay.server import _state_snapshot

    feed = DecisionFeed()
    feed.publish("run_state", {"act": 1, "floor": 3, "hp": 70, "max_hp": 80,
                               "reserved": 24, "budget_remaining": 28, "relics": []})
    feed.publish("advice", {"point": "combat", "label": "出「痛击」→ 咔咔",
                            "verb": "play", "command": {"command": "play", "card": 1,
                                                        "target": 0},
                            "reason": "lethal this turn", "confidence": 0.82,
                            "fallback": False, "act": 1, "floor": 3,
                            "tally": {"match": 0, "mismatch": 0, "unobserved": 0},
                            "agreement": None})
    feed.publish("outcome", {"point": "combat", "verdict": "match",
                             "advice_label": "出「痛击」→ 咔咔",
                             "acted_label": "出「痛击」→ 咔咔",
                             "acted": ["play", "bash", "cultist"],
                             "tally": {"match": 1, "mismatch": 0, "unobserved": 0},
                             "agreement": 1.0})

    snap = _state_snapshot(feed)
    advice, outcome = snap["last_advice"], snap["last_outcome"]

    required_advice = {"point", "label", "verb", "command", "reason",
                       "confidence", "fallback", "act", "floor", "tally", "agreement"}
    missing = required_advice - set(advice)
    assert not missing, f"the overlay reads these and they are gone: {sorted(missing)}"
    assert isinstance(advice["command"], dict) and advice["command"].get("command")
    assert advice["verb"] == "play"      # asciiCommand() switches on the verb

    required_outcome = {"point", "verdict", "advice_label", "acted_label",
                        "acted", "tally", "agreement"}
    missing = required_outcome - set(outcome)
    assert not missing, f"the overlay reads these and they are gone: {sorted(missing)}"
    assert isinstance(outcome["acted"], list)   # asciiKey() iterates it


def test_the_dashboard_refuses_to_share_its_port():
    """Two dashboards on one port is a haunted port, not a convenience.

    `socketserver` sets SO_REUSEADDR, and on Windows that lets a second process
    bind a port another one is listening on. On 2026-09-22 three stale demo
    servers shared 8787 and answered the in-game overlay with `404` from old code
    (an old build has no `/state`), so the panel stayed empty with nothing
    anywhere explaining why. The second bind now fails, loudly, with the fix.
    """
    from spirebrain.overlay.server import DashboardServer, PortInUse

    first = DashboardServer(port=0)
    try:
        try:
            DashboardServer(port=first.port)
            raise AssertionError("a second dashboard bound the same port")
        except PortInUse as exc:
            assert str(first.port) in str(exc)
            assert "--port" in str(exc)   # the message carries the fix
    finally:
        first.httpd.server_close()


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all overlay tests passed")
