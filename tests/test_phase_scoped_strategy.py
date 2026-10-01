from __future__ import annotations

import hashlib
import json

import pytest


def _write_experience_fixture(path, proposal_id, *, valid=True):
    payload = {
        "fixture_type": "synthetic",
        "kind": "fixed_provider_responses",
        "experience_ids": [proposal_id] if valid else [],
        "responses": [{
            "plan_id": "fixture-plan",
            "current_objective": "survive",
            "long_term_goal": "finish",
            "priority": ["survive"],
            "preferred_candidates": [], "avoid_candidates": [],
            "resource_constraints": {}, "next_steps": [],
            "replan_triggers": [], "reason": "fixture",
            "uncertainty": "", "expires_after": 1,
        }],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def test_jev_only_keeps_jev_live_and_never_calls_strategic_provider(tmp_path, monkeypatch):
    from spirebrain.brain.protocol import BrainResponse
    from spirebrain.driver import agent as agent_module
    from spirebrain.jev_brain.client import JevAnswer, JevResponse

    class StrategicSpy:
        backend_name = "strategic-spy"
        async_required = False

        def __init__(self):
            self.calls = 0

        def plan(self, payload):
            self.calls += 1
            return BrainResponse(backend=self.backend_name, fallback=True)

    class JevSpy:
        backend_name = "real-jev-test-double"

        def __init__(self):
            self.calls = 0

        def ask(self, state, questions):
            self.calls += 1
            answers = {}
            for name, spec in questions.items():
                criteria = getattr(spec, "criteria", None)
                choice = next(iter(criteria)) if isinstance(criteria, dict) else 0
                answers[name] = JevAnswer(value=choice, confidence=0.95)
            return JevResponse(answers=answers, backend=self.backend_name,
                               model="fixed-test-double")

    strategic = StrategicSpy()
    jev = JevSpy()
    monkeypatch.setattr(agent_module, "get_client", lambda *args, **kwargs: jev)
    agent = agent_module.SpireBrainAgent(
        jev_backend="openrouter", brain_backend="openai", brain_client=strategic,
        async_planning=False, strategy_planning_enabled=False, log_dir=tmp_path,
    )
    state = {
        "screen_type": "EVENT",
        "screen_state": {"body": "A test event", "options": [
            {"label": "Take gold"}, {"label": "Lose health"},
        ]},
        "character": "IRONCLAD", "run_id": "run-a", "act": 1, "floor": 1,
        "current_hp": 70, "max_hp": 80, "gold": 0,
        "available_commands": ["choose", "wait", "state"],
        "ready_for_command": True, "in_game": True,
    }

    try:
        agent.prepare_advice_strategy(state, decision_id="d1")
        command = agent.choose_action(state)

        assert command["command"] == "choose"
        assert jev.calls == 1
        assert strategic.calls == 0
        assert agent.strategic.planning_enabled is False
    finally:
        agent.close()


def test_jev_only_combat_uses_jev_without_a_strategic_plan(tmp_path, monkeypatch):
    from spirebrain.brain.protocol import BrainResponse
    from spirebrain.driver import agent as agent_module
    from spirebrain.jev_brain.client import JevAnswer, JevResponse

    class StrategicSpy:
        backend_name = "strategic-spy"
        async_required = False

        def __init__(self):
            self.calls = 0

        def plan(self, payload):
            self.calls += 1
            return BrainResponse(backend=self.backend_name, fallback=True)

    class JevSpy:
        backend_name = "real-jev-test-double"

        def __init__(self):
            self.calls = 0
            self.states = []

        def ask(self, state, questions):
            self.calls += 1
            self.states.append(state)
            answers = {}
            for name, spec in questions.items():
                criteria = getattr(spec, "criteria", None)
                choice = next(iter(criteria)) if isinstance(criteria, dict) else 0
                answers[name] = JevAnswer(value=choice, confidence=0.95)
            return JevResponse(answers=answers, backend=self.backend_name,
                               model="fixed-test-double")

    strategic = StrategicSpy()
    jev = JevSpy()
    monkeypatch.setattr(agent_module, "get_client", lambda *args, **kwargs: jev)
    agent = agent_module.SpireBrainAgent(
        jev_backend="openrouter", brain_backend="openai", brain_client=strategic,
        async_planning=False, strategy_planning_enabled=False, log_dir=tmp_path,
    )
    state = {
        "screen_type": "COMBAT", "character": "IRONCLAD", "run_id": "combat-run",
        "act": 1, "floor": 1, "current_hp": 70, "max_hp": 80,
        "available_commands": ["play", "end", "wait", "state"],
        "combat": {
            "player": {"current_hp": 70, "energy": 3, "block": 0},
            "hand": [
                {"id": "Strike_R", "name": "Strike", "cost": 1,
                 "damage": 6, "has_target": True},
                {"id": "Defend_R", "name": "Defend", "cost": 1, "block": 5},
            ],
            "monsters": [{"id": "Cultist", "instance_id": "cult-1",
                          "current_hp": 40, "intent": "ATTACK", "damage": 6}],
        },
    }

    try:
        command = agent.choose_action(state)
        assert command["command"] in {"play", "end"}
        assert jev.calls == 1
        assert "verified_experiences" not in jev.states[0] or isinstance(
            jev.states[0]["verified_experiences"], list)
        assert strategic.calls == 0
    finally:
        agent.close()


def test_jev_only_is_written_to_communication_launch_command():
    from spirebrain.install_mod_config import default_command
    from start import mod_config_command

    command = default_command("openrouter", mode="advise",
                              strategy_planning_enabled=False)

    assert "--jev-only" in command
    assert "--mode advise" in command
    assert "--jev-only" in default_command("openrouter", mode="advise")
    online = default_command("openrouter", mode="advise",
                             strategy_planning_enabled=True)
    assert "--strategic-online" in online
    assert "--jev-only" not in online
    launch = mod_config_command("http://127.0.0.1:8787", "openrouter", False,
                                 "advise", strategy_planning_enabled=False)
    assert "--backend openrouter" in launch
    assert "--jev-only" in launch


def test_install_config_jev_only_flag_builds_the_same_explicit_command(capsys, tmp_path,
                                                                        monkeypatch):
    """The operator-facing installer must not require hand-editing a command."""
    from spirebrain import install_mod_config

    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    code = install_mod_config.main(["--backend", "openrouter", "--mode", "advise",
                                    "--jev-only"])
    assert code == 0
    output = capsys.readouterr().out
    assert "--jev-only" in output
    assert "JEV-only" in output


def test_install_config_jev_only_adds_flag_to_explicit_command(capsys, tmp_path, monkeypatch):
    from spirebrain import install_mod_config

    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    code = install_mod_config.main([
        "--command", "python run_agent.py --backend openrouter --mode advise",
        "--jev-only",
    ])
    assert code == 0
    assert "--jev-only" in capsys.readouterr().out


def test_stdio_jev_only_disables_planning_but_keeps_selected_jev_backend(monkeypatch):
    import io
    from types import SimpleNamespace
    from spirebrain.driver import stdio

    seen = {}

    class Agent:
        jev = SimpleNamespace(backend_name="openrouter-jev")
        strategic = SimpleNamespace(backend_name="openai")
        runtime_config = SimpleNamespace(config_id="cfg-test")

    class Transport:
        def __init__(self, agent, **kwargs):
            self.mode = kwargs["mode"]
            self.tracker = SimpleNamespace(agreement=None, judged=0)
            for name in ("messages", "commands", "errors", "menu_idle",
                         "stuck_events", "ladder_events", "advice_issued"):
                setattr(self, name, 0)
            self.substitutions = []
            self.action_limit_hit = False

        def run(self, source, sink):
            seen["transport_ran"] = True

    def build_agent(*args, **kwargs):
        seen["backend"] = args[1]
        seen["planning_enabled"] = kwargs["strategy_planning_enabled"]
        seen["trace_source_type"] = kwargs["trace_source_type"]
        return Agent()

    stderr = io.StringIO()
    monkeypatch.setattr(stdio, "configure_streams", lambda *_: None)
    monkeypatch.setattr(stdio, "_build_agent", build_agent)
    monkeypatch.setattr(stdio, "StdioTransport", Transport)
    monkeypatch.setattr(stdio.sys, "stdin", io.StringIO())
    monkeypatch.setattr(stdio.sys, "stdout", io.StringIO())
    monkeypatch.setattr(stdio.sys, "stderr", stderr)

    assert stdio.main(["--backend", "openrouter"]) == 0
    assert seen == {"backend": "openrouter", "planning_enabled": False,
                    "trace_source_type": "real_game", "transport_ran": True}
    assert "JEV-ONLY MODE" in stderr.getvalue()
    assert "JEV=openrouter-jev brain=disabled_by_design" in stderr.getvalue()

    assert stdio.main(["--strategic-online", "--backend", "openrouter"]) == 0
    assert seen["planning_enabled"] is True


def test_stdio_replay_is_tagged_synthetic_and_cannot_look_like_real_game(
        monkeypatch, tmp_path):
    import io
    from types import SimpleNamespace
    from spirebrain.driver import stdio

    (tmp_path / "state.json").write_text("{}", encoding="utf-8")
    seen = {}

    def build_agent(*args, **kwargs):
        seen["trace_source_type"] = kwargs["trace_source_type"]
        return object()

    def replay(paths, agent, **kwargs):
        seen["replay_paths"] = [path.name for path in paths]
        return SimpleNamespace(messages=1, commands=0, skipped_not_ready=0,
                               substitutions=[])

    monkeypatch.setattr(stdio, "configure_streams", lambda *_: None)
    monkeypatch.setattr(stdio, "_build_agent", build_agent)
    monkeypatch.setattr(stdio, "replay", replay)
    monkeypatch.setattr(stdio.sys, "stdin", io.StringIO())
    monkeypatch.setattr(stdio.sys, "stdout", io.StringIO())
    monkeypatch.setattr(stdio.sys, "stderr", io.StringIO())

    assert stdio.main(["--replay", str(tmp_path)]) == 0
    assert seen == {"trace_source_type": "synthetic", "replay_paths": ["state.json"]}


def test_jev_only_smoke_requires_jev_but_rejects_unexpected_strategic_calls():
    from tools.real_game_smoke import _build_report

    report = _build_report(
        brain=[],
        jev=[{"backend": "openrouter", "fallback": False}],
        trace=[{"event_type": "decision", "source_type": "rule_fallback"}],
        strategy_enabled=False,
    )

    assert report["provider_health"]["strategic"] == "disabled_by_design"
    assert report["release_passed"] is True
    assert report["strategic_requests_during_run"] == 0

    unexpected = _build_report(
        brain=[{"backend": "openai", "fallback": False}],
        jev=[{"backend": "openrouter", "fallback": False}],
        trace=[{"event_type": "decision", "source_type": "gpt_strategy"}],
        strategy_enabled=False,
    )
    assert unexpected["release_passed"] is False
    assert unexpected["strategic_requests_during_run"] == 1

    in_flight = _build_report(
        brain=[],
        jev=[{"backend": "openrouter-jev", "fallback": False}],
        trace=[
            {"event_type": "decision", "source_type": "rule_fallback"},
            {"event_type": "provider_request", "request_id": "unfinished-brain-1",
             "provider": "openai", "strategic_scope_id": "combat:1:1"},
        ],
        strategy_enabled=False,
    )
    assert in_flight["release_passed"] is False
    assert in_flight["provider_health"]["strategic"] == "unexpected_activity"
    assert in_flight["strategic_requests_during_run"] == 1


def test_only_replay_validated_active_experiences_reach_jev(tmp_path):
    from spirebrain.analysis.experience import ExperienceLedger
    from spirebrain.guide.rules import GuideAwareClient

    review = {
        "run_id": "run-a", "run_epoch": 3,
        "source_type": "real_game", "lifecycle_complete": True,
        "terminal_result": "death",
        "observed_characters": ["IRONCLAD"], "observed_scenes": ["COMBAT"],
        "observed_acts": [1, 2],
        "evidence_event_ids": ["trace:10", "trace:11"],
    }
    raw = {
        "title": "Avoid spending the last potion slot early",
        "claim": "When the next elite is near and health is safe, prefer retaining one potion.",
        "character": "IRONCLAD", "scenes": ["COMBAT"], "acts": [1, 2],
        "prerequisites": ["potion_count >= 1", "elite_within_2_floors"],
        "exceptions": ["current turn is lethal without potion use"],
        "evidence_event_ids": ["trace:10", "trace:11"],
    }
    ledger = ExperienceLedger(tmp_path / "experiences.jsonl")
    proposal = ledger.add_pending(raw, review, provider="test", model="fixture")

    assert ledger.active_for("IRONCLAD", "COMBAT") == []
    ledger.approve(proposal["proposal_id"], reviewer="player")
    assert ledger.active_for("IRONCLAD", "COMBAT") == []
    fixture = tmp_path / "fixed.json"
    fixture_hash = _write_experience_fixture(fixture, proposal["proposal_id"])
    ledger.record_replay_validation(proposal["proposal_id"], {
        "fixture_path": str(fixture), "fixture_hash": fixture_hash,
    })
    assert ledger.active_for("IRONCLAD", "COMBAT") == []
    ledger.activate(proposal["proposal_id"])
    active = ledger.active_for("IRONCLAD", "COMBAT")
    assert len(active) == 1
    assert active[0]["experience_id"] == proposal["proposal_id"]
    assert active[0]["policy_type"] == "soft_preference"
    assert active[0]["status"] == "active"
    assert active[0]["replay_validation"]["experience_applied"] is True

    class Sink:
        def ask(self, state, questions):
            self.state = state
            return state

    sink = Sink()
    client = GuideAwareClient(sink)
    client.experiences = active
    client.ask({"legal_candidates": ["a", "b"]}, {})
    assert sink.state["legal_candidates"] == ["a", "b"]
    assert sink.state["verified_experiences"][0]["experience_id"] == proposal["proposal_id"]


def test_experience_proposal_rejects_unobserved_evidence_ids(tmp_path):
    from spirebrain.analysis.experience import ExperienceLedger

    raw = {
        "title": "Example", "claim": "Prefer X in a narrow condition.",
        "character": "IRONCLAD", "scenes": ["COMBAT"], "acts": [1],
        "prerequisites": [], "exceptions": [],
        "evidence_event_ids": ["trace:999"],
    }
    try:
        ExperienceLedger(tmp_path / "experiences.jsonl").add_pending(
            raw, {"run_id": "run-a", "run_epoch": 1,
                  "source_type": "real_game", "lifecycle_complete": True,
                  "terminal_result": "death",
                  "observed_characters": ["IRONCLAD"], "observed_scenes": ["COMBAT"],
                  "observed_acts": [1],
                  "evidence_event_ids": ["trace:1"]}, provider="test", model="fixture")
    except ValueError as exc:
        assert "evidence" in str(exc).lower()
    else:
        raise AssertionError("model claims must cite events from this run review")


def test_experience_replay_needs_bound_hash_and_explicit_zero_violation_counts(tmp_path):
    from spirebrain.analysis.experience import ExperienceLedger

    review = {"run_id": "r", "run_epoch": 1, "source_type": "real_game",
              "lifecycle_complete": True, "terminal_result": "death",
              "observed_characters": ["IRONCLAD"], "observed_scenes": ["COMBAT"],
              "observed_acts": [1],
              "evidence_event_ids": ["trace:1"]}
    raw = {"title": "Scoped", "claim": "Prefer this only under stated conditions.",
           "character": "IRONCLAD", "scenes": ["COMBAT"], "acts": [1],
           "prerequisites": [], "exceptions": [], "evidence_event_ids": ["trace:1"]}
    ledger = ExperienceLedger(tmp_path / "experiences.jsonl")
    proposal = ledger.add_pending(raw, review, provider="test", model="fixture")
    ledger.approve(proposal["proposal_id"], reviewer="player")
    fixture = tmp_path / "fixed.json"
    fixture_hash = _write_experience_fixture(fixture, proposal["proposal_id"])

    with pytest.raises(ValueError, match="sha256"):
        ledger.record_replay_validation(proposal["proposal_id"], {
            "fixture_path": str(fixture), "fixture_hash": "sha256:short",
        })
    assert ledger.active_for("IRONCLAD", "COMBAT", 1) == []

    passed = ledger.record_replay_validation(proposal["proposal_id"], {
        "fixture_path": str(fixture), "fixture_hash": fixture_hash,
    })
    assert passed["status"] == "replay_passed"
    ledger.activate(proposal["proposal_id"])
    assert len(ledger.active_for("IRONCLAD", "COMBAT", 1)) == 1


def test_experience_replay_requires_executed_fixture_and_recomputed_hash(tmp_path):
    from spirebrain.analysis.experience import ExperienceLedger

    review = {"run_id": "r", "run_epoch": 1, "source_type": "real_game",
              "lifecycle_complete": True, "terminal_result": "death",
              "observed_characters": ["IRONCLAD"], "observed_scenes": ["COMBAT"],
              "observed_acts": [1],
              "evidence_event_ids": ["trace:1"]}
    raw = {"title": "Scoped", "claim": "Prefer this only under stated conditions.",
           "character": "IRONCLAD", "scenes": ["COMBAT"], "acts": [1],
           "prerequisites": [], "exceptions": [], "evidence_event_ids": ["trace:1"]}
    ledger = ExperienceLedger(tmp_path / "experiences.jsonl")
    proposal = ledger.add_pending(raw, review, provider="test", model="fixture")
    ledger.approve(proposal["proposal_id"], reviewer="player")

    fixture = tmp_path / "fixed.json"
    fixture.write_text(json.dumps({
        "fixture_type": "synthetic", "kind": "fixed_provider_responses",
        "experience_ids": [proposal["proposal_id"]],
        "responses": [{"plan_id": "p", "current_objective": "survive",
                       "long_term_goal": "finish", "priority": ["survive"],
                       "preferred_candidates": [], "avoid_candidates": [],
                       "resource_constraints": {}, "next_steps": [],
                       "replan_triggers": [], "reason": "fixture",
                       "uncertainty": "", "expires_after": 1}],
    }), encoding="utf-8")
    digest = "sha256:" + hashlib.sha256(fixture.read_bytes()).hexdigest()

    with pytest.raises(ValueError, match="fixture_path"):
        ledger.record_replay_validation(proposal["proposal_id"], {
            "passed": True, "fixture_type": "fixed_provider_responses",
            "fixture_hash": digest, "sample_count": 1,
            "experience_id": proposal["proposal_id"], "experience_applied": True,
            "illegal_action_count": 0, "candidate_set_change_count": 0,
            "hard_constraint_violation_count": 0,
        })

    fixture.write_text(fixture.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="hash"):
        ledger.record_replay_validation(proposal["proposal_id"], {
            "fixture_path": str(fixture), "fixture_hash": digest,
        })

    fixture.write_text(fixture.read_text(encoding="utf-8").rstrip() + "\n", encoding="utf-8")
    digest = "sha256:" + hashlib.sha256(fixture.read_bytes()).hexdigest()
    passed = ledger.record_replay_validation(proposal["proposal_id"], {
        "fixture_path": str(fixture), "fixture_hash": digest,
    })
    assert passed["status"] == "replay_passed"
    validation = passed["validation"]
    assert validation["fixture_hash"] == digest
    assert validation["sample_count"] == 1
    assert validation["experience_applied"] is True


def test_experience_replay_rejects_unapplied_or_invalid_fixed_response(tmp_path):
    from spirebrain.analysis.experience import ExperienceLedger

    review = {"run_id": "r", "run_epoch": 1, "source_type": "real_game",
              "lifecycle_complete": True, "terminal_result": "death",
              "observed_characters": ["IRONCLAD"], "observed_scenes": ["COMBAT"],
              "observed_acts": [1],
              "evidence_event_ids": ["trace:1"]}
    raw = {"title": "Scoped", "claim": "Prefer this only under stated conditions.",
           "character": "IRONCLAD", "scenes": ["COMBAT"], "acts": [1],
           "prerequisites": [], "exceptions": [], "evidence_event_ids": ["trace:1"]}
    ledger = ExperienceLedger(tmp_path / "experiences.jsonl")
    proposal = ledger.add_pending(raw, review, provider="test", model="fixture")
    ledger.approve(proposal["proposal_id"], reviewer="player")
    fixture = tmp_path / "not-applied.json"
    fixture.write_text(json.dumps({
        "fixture_type": "synthetic", "kind": "fixed_provider_responses",
        "experience_ids": [], "responses": [{"plan_id": "p"}],
    }), encoding="utf-8")
    digest = "sha256:" + hashlib.sha256(fixture.read_bytes()).hexdigest()
    result = ledger.record_replay_validation(proposal["proposal_id"], {
        "fixture_path": str(fixture), "fixture_hash": digest,
    })
    assert result["status"] == "replay_failed"
    assert result["validation"]["experience_applied"] is False
