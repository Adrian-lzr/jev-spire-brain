import json
from pathlib import Path


ROOT = Path(__file__).parent


def test_synthetic_replay_keeps_advise_commands_safe():
    from spirebrain.analysis.replay import run_fixture

    result = run_fixture(ROOT / "fixtures" / "replay" / "advisor_states.json")
    assert result["fixture_type"] == "synthetic"
    assert result["passed"] is True
    assert result["invalid_advise_commands"] == []
    assert set(result["advise_commands"]) <= {"wait", "state"}


def test_metrics_contract_counts_provider_request_and_unknown_outcome():
    from spirebrain.analysis.metrics import load_records, summarize

    path = ROOT / "fixtures" / "metrics" / "decision_trace.jsonl"
    report = summarize(load_records(path), source=str(path))
    assert report["provider_calls"] == 1
    assert report["decision_count"] == 2
    assert report["legal_action_rate"] == 0.5
    assert report["unobserved_ratio"] == 1.0
    assert report["real_outcomes"] == "unknown"
    assert report["latency_ms"]["request_p50"] == 120
    assert report["latency_ms"]["decision_p50"] == 112.5
    assert report["latency_ms"]["player_display_latency"] == "not_collected"
    # Metrics must never echo request bodies or credentials.  This fixture is
    # intentionally the only source rendered by the assertion.
    assert "Authorization" not in json.dumps(report)


def test_metrics_cli_rejects_missing_and_empty_inputs(tmp_path, capsys):
    from spirebrain.analysis.metrics import main

    missing = tmp_path / "missing.jsonl"
    assert main(["--input", str(missing)]) == 2
    assert '"error": "input_missing"' in capsys.readouterr().out
    empty = tmp_path / "empty.jsonl"
    empty.write_text("", encoding="utf-8")
    assert main(["--input", str(empty)]) == 2
    assert '"error": "no_records"' in capsys.readouterr().out


def test_advice_trace_measures_first_decision_and_feed_publish_without_sleep(tmp_path):
    from spirebrain.driver.advisor import AdviseSession
    from spirebrain.driver.agent import SpireBrainAgent
    from spirebrain.driver.decision_state import recommendation_key

    class Clock:
        value = 12.0

        def __call__(self):
            return self.value

    class Feed:
        def __init__(self, clock):
            self.clock = clock
            self.events = []

        def publish(self, kind, payload):
            self.events.append((kind, payload))
            if kind == "advice":
                self.clock.value += 0.007

    clock = Clock()
    feed = Feed(clock)
    agent = SpireBrainAgent(jev_backend="mock", brain_backend="none",
                            log_dir=tmp_path, feed=feed)
    agent.clone_for_advice = lambda: None
    choose = agent.choose_action

    def delayed_rule_action(payload):
        command = choose(payload)
        clock.value += 0.025
        return command

    agent.choose_action = delayed_rule_action
    session = AdviseSession(
        agent, advice_path=None, poll_frames=20, warn_stream=__import__("io").StringIO(),
        ensure_offered=lambda line, *args, **kwargs: line,
        fingerprint=lambda state: recommendation_key(state), message_count=lambda: 1,
        clock=clock,
    )
    state = {"character": "IRONCLAD", "screen_type": "REST",
             "current_hp": 20, "max_hp": 80, "available_commands": ["choose", "wait"],
             "screen_state": {"rest_options": ["smith", "rest"]}}
    session.advise(state, state["available_commands"], True)
    advice = next(payload for kind, payload in feed.events if kind == "advice")
    trace_records = [json.loads(line) for line in
                     (tmp_path / "decision_trace.jsonl").read_text(encoding="utf-8").splitlines()]
    trace = next(record for record in trace_records if record["event_type"] == "advice")
    assert advice["decision_id"]
    assert advice["decision_id"] == trace["decision_id"]
    assert trace["first_advice_latency_ms"] == 25
    assert trace["decision_latency_ms"] == 25
    assert trace["publish_latency_ms"] == 7
    assert "player_display_latency_ms" not in trace
