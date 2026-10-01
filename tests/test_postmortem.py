import json

from spirebrain.analysis.postmortem import release_report, review_decision, review_run


def _records():
    return [
        {"event_type": "state_observed", "decision_id": "d1", "run_id": "r1", "state_id": "s1"},
        {"event_type": "provider_request", "decision_id": "d1", "request_id": "q1"},
        {"event_type": "decision", "decision_id": "d1", "rule_ids": ["combat.lethal"], "uncertainty": "none"},
        {"event_type": "advice", "decision_id": "d1", "advice_revision": 1},
        {"event_type": "outcome", "decision_id": "d1", "result": "unknown"},
    ]


def test_review_reconstructs_decision_without_inventing_result():
    report = review_decision(_records(), "d1")
    assert report["final_selection"]["decision_id"] == "d1"
    assert report["advice_versions"][0]["advice_revision"] == 1
    assert report["review"]["rules_used"] == ["combat.lethal"]


def test_release_report_keeps_real_result_unknown():
    report = release_report(_records(), commit="abc", config_id="cfg")
    assert report["source"]["sample_count"] == 5
    assert report["environment"]["commit"] == "abc"
    assert report["real_game_results"] == "unknown"


def test_release_report_exposes_trace_completion_and_rag_evidence():
    records = [
        {"event_type": "provider_request", "decision_id": "d1",
         "request_id": "q1", "knowledge_citation_ids": ["source-a"]},
        {"event_type": "provider_response", "decision_id": "d1",
         "request_id": "q1"},
    ]

    report = release_report(records)

    assert report["validation"]["trace_request_completion_rate"] == 1.0
    assert report["strategy_retrieval"] == {
        "citation_ids": ["source-a"], "citation_count": 1, "quality": "unknown",
    }


def test_run_review_groups_by_run_epoch_and_includes_terminal_result_record():
    records = [
        {"trace_sequence": 1, "event_type": "run_started", "run_id": "r1",
         "run_epoch": 2, "state_id": "s1"},
        {"trace_sequence": 2, "event_type": "decision", "run_id": "r1",
         "run_epoch": 2, "decision_id": "d1", "state_id": "s1",
         "command": {"command": "end"}, "source_type": "rule_fallback"},
        {"trace_sequence": 3, "event_type": "result_record", "run_id": "r1",
         "run_epoch": 2, "state_id": "s2", "result": "victory"},
        {"trace_sequence": 4, "event_type": "decision", "run_id": "r1",
         "run_epoch": 3, "decision_id": "d2", "state_id": "s3",
         "command": {"command": "end"}, "source_type": "rule_fallback"},
    ]

    report = review_run(records, "r1", run_epoch=2)

    assert [event["trace_sequence"] for event in report["decisions"]] == [2]
    assert report["results"][0]["result"] == "victory"
    assert report["evidence_event_ids"] == ["trace:1", "trace:2", "trace:3"]
    assert report["run_epoch"] == 2


def test_release_report_counts_terminal_result_record_as_real_result():
    report = release_report([{
        "event_type": "result_record", "run_id": "r1", "run_epoch": 1,
        "result": "death",
    }])

    assert report["real_game_results"] == {"death": 1}


def test_run_review_marks_real_game_only_with_complete_source_tagged_lifecycle():
    incomplete = review_run([
        {"trace_sequence": 1, "event_type": "decision", "run_id": "r",
         "run_epoch": 1, "source_type": "real_game"},
    ], "r", run_epoch=1)
    assert incomplete["source_type"] == "recorded_unverified"
    assert incomplete["lifecycle_complete"] is False

    complete = review_run([
        {"trace_sequence": 1, "event_type": "run_started", "run_id": "r",
         "run_epoch": 1, "source_type": "real_game"},
        {"trace_sequence": 2, "event_type": "run_ended", "run_id": "r",
         "run_epoch": 1, "source_type": "real_game", "result": "victory"},
    ], "r", run_epoch=1)
    assert complete["source_type"] == "real_game"
    assert complete["lifecycle_complete"] is True

    menu_exit = review_run([
        {"trace_sequence": 1, "event_type": "run_started", "run_id": "r2",
         "run_epoch": 1, "source_type": "real_game"},
        {"trace_sequence": 2, "event_type": "run_ended", "run_id": "r2",
         "run_epoch": 1, "source_type": "real_game", "result": None,
         "terminal_screen": "MENU"},
    ], "r2", run_epoch=1)
    assert menu_exit["source_type"] == "recorded_unverified"
    assert menu_exit["lifecycle_complete"] is False
    assert menu_exit["result_status"] == "unknown"


def test_run_review_does_not_assemble_terminal_result_from_result_record():
    """A result record is evidence, never proof that a run reached a terminal state."""
    report = review_run([
        {"trace_sequence": 1, "event_type": "run_started", "run_id": "r3",
         "run_epoch": 4, "source_type": "real_game", "character": "IRONCLAD"},
        {"trace_sequence": 2, "event_type": "result_record", "run_id": "r3",
         "run_epoch": 4, "source_type": "real_game", "result": "victory"},
        {"trace_sequence": 3, "event_type": "run_ended", "run_id": "r3",
         "run_epoch": 4, "source_type": "real_game", "result": None},
    ], "r3", run_epoch=4)

    assert report["terminal_result"] is None
    assert report["result_status"] == "unknown"
    assert report["lifecycle_complete"] is False
    assert report["source_type"] == "recorded_unverified"


def test_run_review_exposes_observed_scope_for_experience_validation():
    report = review_run([
        {"trace_sequence": 1, "event_type": "run_started", "run_id": "r4",
         "run_epoch": 1, "source_type": "real_game", "character": "ironclad", "act": 1},
        {"trace_sequence": 2, "event_type": "advice", "run_id": "r4",
         "run_epoch": 1, "screen": "combat", "act": 1},
        {"trace_sequence": 3, "event_type": "run_ended", "run_id": "r4",
         "run_epoch": 1, "source_type": "real_game", "result": "death"},
    ], "r4", run_epoch=1)

    assert report["observed_characters"] == ["IRONCLAD"]
    assert report["observed_scenes"] == ["COMBAT"]
    assert report["observed_acts"] == [1]


def test_run_lifecycle_trace_preserves_character_and_terminal_screen(tmp_path):
    from spirebrain.analysis.metrics import load_records
    from spirebrain.driver.trace import DecisionTrace

    path = tmp_path / "decision_trace.jsonl"
    trace = DecisionTrace(path, config_id="offline-test")
    trace.record("run_started", {
        "run_id": "r-lifecycle", "run_epoch": 2, "state_id": "s-start",
        "character": "IRONCLAD", "act": 1, "floor": 0,
        "source_type": "real_game",
    })
    trace.record("run_ended", {
        "run_id": "r-lifecycle", "run_epoch": 2, "state_id": "s-end",
        "character": "IRONCLAD", "terminal_screen": "DEATH",
        "result": "death", "source_type": "real_game",
    })

    report = review_run(load_records(path), "r-lifecycle", run_epoch=2)

    assert report["source_type"] == "real_game"
    assert report["observed_characters"] == ["IRONCLAD"]
    assert report["events"][-1]["terminal_screen"] == "DEATH"
    assert all(not row.get("contract_errors") for row in report["events"])


def test_postmortem_rejects_experience_scope_outside_observed_run(tmp_path, monkeypatch):
    import pytest
    import spirebrain.analysis.postmortem as postmortem
    from types import SimpleNamespace

    trace_path = tmp_path / "trace.jsonl"
    rows = [
        {"trace_sequence": 1, "event_type": "run_started", "run_id": "r5",
         "run_epoch": 1, "source_type": "real_game", "character": "IRONCLAD", "act": 1},
        {"trace_sequence": 2, "event_type": "advice", "run_id": "r5",
         "run_epoch": 1, "screen": "COMBAT", "act": 1},
        {"trace_sequence": 3, "event_type": "run_ended", "run_id": "r5",
         "run_epoch": 1, "source_type": "real_game", "result": "death"},
    ]
    trace_path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")

    class Reviewer:
        def __init__(self, **kwargs):
            pass

        def review(self, report):
            return [{
                "title": "Out of scope", "claim": "Do not accept this.",
                "character": "IRONCLAD", "scenes": ["SHOP"], "acts": [2],
                "prerequisites": [], "exceptions": [], "evidence_event_ids": ["trace:2"],
            }]

    monkeypatch.setattr(postmortem, "OpenAIExperienceReviewer", Reviewer)
    monkeypatch.setattr(
        "spirebrain.runtime_config.resolve_runtime_config",
        lambda root: SimpleNamespace(openai_endpoint="https://example.invalid/v1/responses",
                                     openai_model="fixture-model", brain_api_key="test-only",
                                     brain_backend="fixture", timeout_ms=500),
    )
    with pytest.raises(SystemExit):
        postmortem.main([
            "--input", str(trace_path), "--run-id", "r5", "--run-epoch", "1",
            "--propose-experiences", "--ledger", str(tmp_path / "ledger.jsonl"),
        ])


def test_compact_review_redacts_nested_secrets_and_paths_and_pseudonyms_run_id():
    import json as json_module
    from spirebrain.analysis.postmortem import _prepare_model_review

    compact = _prepare_model_review({
        "run_id": "player-run-123", "run_epoch": 2, "source_type": "real_game",
        "character": "IRONCLAD", "terminal_result": "death", "sample_counts": {},
        "evidence_event_ids": ["trace:1"], "events": [{
            "event_type": "decision", "event_id": "trace:1",
            "command": {"raw": "D:\\Users\\Lenovo\\private-command.json"},
            "reason": {"nested": {"api_key": "secret-value",
                                    "path": "C:\\Users\\Lenovo\\secret.json"}},
            "combat_facts": {"headers": {"Authorization": "Bearer secret-token"}},
            "private_player_name": "Lenovo",
        }],
    })
    encoded = json_module.dumps(compact, ensure_ascii=False)

    assert compact["run_id"] != "player-run-123"
    assert "secret-value" not in encoded
    assert "secret-token" not in encoded
    assert "C:\\Users\\Lenovo" not in encoded
    assert "private-command" not in encoded
    assert "private_player_name" not in encoded


def test_run_review_requires_epoch_when_events_are_unscoped():
    import pytest

    rows = [
        {"event_type": "run_started", "run_id": "r", "run_epoch": 2},
        {"event_type": "decision", "run_id": "r", "decision_id": "legacy"},
    ]
    with pytest.raises(ValueError, match="specify --run-epoch"):
        review_run(rows, "r")
    assert len(review_run(rows, "r", run_epoch=2)["events"]) == 1


def test_postmortem_model_review_is_explicit_and_writes_pending_to_selected_ledger(
        tmp_path, monkeypatch, capsys):
    from types import SimpleNamespace
    import spirebrain.analysis.postmortem as postmortem
    from spirebrain.analysis.experience import ExperienceLedger

    trace_path = tmp_path / "trace.jsonl"
    rows = [
        {"trace_sequence": 1, "event_type": "run_started", "run_id": "r",
         "run_epoch": 1, "source_type": "real_game", "character": "IRONCLAD", "act": 1},
        {"trace_sequence": 2, "event_type": "advice", "run_id": "r",
         "run_epoch": 1, "decision_id": "d", "screen": "COMBAT", "act": 1},
        {"trace_sequence": 3, "event_type": "run_ended", "run_id": "r",
         "run_epoch": 1, "source_type": "real_game", "result": "death"},
    ]
    trace_path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    calls = []

    class Reviewer:
        def __init__(self, **kwargs):
            calls.append(("init", kwargs))

        def review(self, report):
            calls.append(("review", report["run_id"], report["run_epoch"]))
            return [{
                "title": "Bounded test proposal", "claim": "Prefer the scoped choice.",
                "character": "IRONCLAD", "scenes": ["COMBAT"], "acts": [1],
                "prerequisites": [], "exceptions": [],
                "evidence_event_ids": ["trace:2"],
            }]

    monkeypatch.setattr(postmortem, "OpenAIExperienceReviewer", Reviewer)
    monkeypatch.setattr(
        "spirebrain.runtime_config.resolve_runtime_config",
        lambda root: SimpleNamespace(openai_endpoint="https://example.invalid/v1/responses",
                                     openai_model="fixture-model", brain_api_key="test-only",
                                     brain_backend="fixture", timeout_ms=500),
    )
    ledger_path = tmp_path / "isolated-ledger.jsonl"
    code = postmortem.main([
        "--input", str(trace_path), "--run-id", "r",
        "--propose-experiences", "--ledger", str(ledger_path),
    ])
    output = json.loads(capsys.readouterr().out)

    assert code == 0
    assert [item[0] for item in calls] == ["init", "review"]
    assert output["postmortem_request_count"] == 1
    proposals = ExperienceLedger(ledger_path).proposals()
    assert len(proposals) == 1
    assert proposals[0]["status"] == "pending_review"


def test_postmortem_trace_links_review_attempt_and_completion(tmp_path, monkeypatch, capsys):
    import json
    import spirebrain.analysis.postmortem as postmortem
    from spirebrain.analysis.metrics import load_records
    from spirebrain.analysis.experience import ExperienceLedger
    from types import SimpleNamespace

    trace_path = tmp_path / "decision_trace.jsonl"
    trace_path.write_text("\n".join([
        json.dumps({"trace_sequence": 1, "event_type": "run_started",
                    "run_id": "review-run", "run_epoch": 4,
                    "source_type": "real_game", "state_id": "s1",
                    "character": "IRONCLAD", "act": 1}),
        json.dumps({"trace_sequence": 2, "event_type": "advice",
                    "run_id": "review-run", "run_epoch": 4,
                    "decision_id": "d1", "state_id": "s1",
                    "screen": "COMBAT", "act": 1}),
        json.dumps({"trace_sequence": 3, "event_type": "run_ended",
                    "run_id": "review-run", "run_epoch": 4,
                    "source_type": "real_game", "state_id": "s2",
                    "character": "IRONCLAD", "terminal_screen": "DEATH",
                    "result": "death"}),
    ]) + "\n", encoding="utf-8")

    class Reviewer:
        def __init__(self, **kwargs):
            pass

        def review(self, report):
            return [{
                "title": "Trace-linked proposal", "claim": "Prefer the scoped choice.",
                "character": "IRONCLAD", "scenes": ["COMBAT"], "acts": [1],
                "prerequisites": [], "exceptions": [], "evidence_event_ids": ["trace:2"],
            }]

    monkeypatch.setattr(postmortem, "OpenAIExperienceReviewer", Reviewer)
    monkeypatch.setattr(
        "spirebrain.runtime_config.resolve_runtime_config",
        lambda root: SimpleNamespace(openai_endpoint="https://example.invalid/v1/responses",
                                     openai_model="fixture-model", brain_api_key="test-only",
                                     brain_backend="fixture", timeout_ms=500),
    )
    ledger_path = tmp_path / "ledger.jsonl"
    assert postmortem.main([
        "--input", str(trace_path), "--run-id", "review-run", "--run-epoch", "4",
        "--propose-experiences", "--ledger", str(ledger_path),
    ]) == 0
    capsys.readouterr()

    events = load_records(trace_path)
    started = [event for event in events if event.get("event_type") == "review_started"]
    completed = [event for event in events if event.get("event_type") == "review_completed"]
    assert len(started) == len(completed) == 1
    assert started[0]["request_id"] == completed[0]["request_id"]
    assert started[0]["run_id"] == completed[0]["run_id"] == "review-run"
    assert started[0]["run_epoch"] == completed[0]["run_epoch"] == 4
    assert completed[0]["status"] == "completed"
    assert completed[0]["proposal_count"] == 1
    assert completed[0]["error_kind"] is None
    assert ExperienceLedger(ledger_path).proposals()[0]["status"] == "pending_review"


def test_postmortem_failure_is_traceable_without_exception_text_or_secret(
        tmp_path, monkeypatch):
    import json
    import spirebrain.analysis.postmortem as postmortem
    from spirebrain.analysis.metrics import load_records
    from types import SimpleNamespace
    import pytest

    trace_path = tmp_path / "decision_trace.jsonl"
    rows = [
        {"trace_sequence": 1, "event_type": "run_started", "run_id": "failed-review",
         "run_epoch": 1, "source_type": "real_game", "state_id": "s1",
         "character": "IRONCLAD", "act": 1},
        {"trace_sequence": 2, "event_type": "run_ended", "run_id": "failed-review",
         "run_epoch": 1, "source_type": "real_game", "state_id": "s2",
         "character": "IRONCLAD", "terminal_screen": "DEATH", "result": "death"},
    ]
    trace_path.write_text("\n".join(json.dumps(row) for row in rows) + "\n",
                          encoding="utf-8")

    class Reviewer:
        def __init__(self, **kwargs):
            pass

        def review(self, report):
            raise RuntimeError("missing_strategic_key secret-token-not-for-logs")

    monkeypatch.setattr(postmortem, "OpenAIExperienceReviewer", Reviewer)
    monkeypatch.setattr(
        "spirebrain.runtime_config.resolve_runtime_config",
        lambda root: SimpleNamespace(openai_endpoint="https://example.invalid/v1/responses",
                                     openai_model="fixture-model", brain_api_key="",
                                     brain_backend="fixture", timeout_ms=500),
    )
    with pytest.raises(SystemExit):
        postmortem.main([
            "--input", str(trace_path), "--run-id", "failed-review", "--run-epoch", "1",
            "--propose-experiences", "--ledger", str(tmp_path / "ledger.jsonl"),
        ])

    events = load_records(trace_path)
    completed = [event for event in events if event.get("event_type") == "review_completed"]
    assert len(completed) == 1
    assert completed[0]["status"] == "failed"
    assert completed[0]["error_kind"] == "missing_key"
    assert completed[0]["provider_attempted"] is False
    serialized = trace_path.read_text(encoding="utf-8")
    assert "secret-token-not-for-logs" not in serialized


def test_postmortem_does_not_call_model_without_explicit_flag(
        tmp_path, monkeypatch, capsys):
    import spirebrain.analysis.postmortem as postmortem

    trace_path = tmp_path / "trace.jsonl"
    trace_path.write_text(json.dumps({
        "event_type": "decision", "run_id": "r", "run_epoch": 1,
    }) + "\n", encoding="utf-8")

    class UnexpectedReviewer:
        def __init__(self, **kwargs):
            raise AssertionError("post-run provider must require explicit opt-in")

    monkeypatch.setattr(postmortem, "OpenAIExperienceReviewer", UnexpectedReviewer)
    assert postmortem.main(["--input", str(trace_path), "--run-id", "r",
                            "--run-epoch", "1"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["source_type"] == "recorded_unverified"
    assert "postmortem_request_count" not in report


def test_postmortem_provider_parses_fixed_json_without_leaking_authorization():
    from spirebrain.analysis.experience import OpenAIExperienceReviewer

    captured = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, limit):
            return json.dumps({"output_text": json.dumps({"proposals": [{
                "title": "Fixture proposal", "claim": "Keep the current resource.",
                "character": "IRONCLAD", "scenes": ["COMBAT"], "acts": [1],
                "prerequisites": [], "exceptions": [],
                "evidence_event_ids": ["trace:1"],
            }]})}).encode("utf-8")

    def opener(request, timeout):
        captured["url"] = request.full_url
        captured["body"] = request.data.decode("utf-8")
        captured["timeout"] = timeout
        return Response()

    reviewer = OpenAIExperienceReviewer(
        endpoint="https://example.invalid/v1/responses", model="fixture-model",
        api_key="secret-must-not-enter-body", timeout_ms=900, opener=opener,
    )
    proposals = reviewer.review({
        "run_id": "r", "run_epoch": 1, "source_type": "real_game",
        "lifecycle_complete": True, "terminal_result": "death", "events": [{
            "event_type": "result_record", "trace_sequence": 1,
            "reason": "safe", "run_id": "r", "run_epoch": 1,
            "command": {"raw": "C:\\Users\\Lenovo\\private-command.json"},
            "combat_facts": {"api_key": "nested-secret",
                             "path": "C:\\Users\\Lenovo\\secret.json"},
        }], "evidence_event_ids": ["trace:1"],
    })

    assert proposals[0]["title"] == "Fixture proposal"
    assert captured["timeout"] == 0.9
    assert "secret-must-not-enter-body" not in captured["body"]
    assert "Authorization" not in captured["body"]
    assert "nested-secret" not in captured["body"]
    assert "private-command" not in captured["body"]
