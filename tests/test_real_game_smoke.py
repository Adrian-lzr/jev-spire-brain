import json


def test_real_game_smoke_without_opt_in_is_explicitly_skipped():
    from tools.real_game_smoke import run

    result = run(duration=0, allow_real_game=False)

    assert result["passed"] is False
    assert result["skipped"] is True
    assert "--allow-real-game" in result["message"]


def test_real_game_smoke_rejects_unknown_mode_before_launch():
    from tools.real_game_smoke import run

    try:
        run(duration=0, allow_real_game=True, mode="unknown")
    except ValueError as exc:
        assert "advise" in str(exc) and "play" in str(exc)
    else:
        raise AssertionError("invalid mode must not launch a real game")


def test_real_game_smoke_cli_defaults_to_advise(monkeypatch):
    import tools.real_game_smoke as smoke

    seen = {}

    def fake_run(**kwargs):
        seen.update(kwargs)
        return {"passed": False, "release_passed": False, "skipped": True}

    monkeypatch.setattr(smoke, "run", fake_run)
    assert smoke.main(["--allow-real-game", "--duration", "0"]) == 0
    assert seen["mode"] == "advise"


def test_real_game_smoke_result_schema_keeps_degradation_observable():
    # Keep this test independent of a local game installation.  It documents
    # the fields emitted by the real runner without launching a process.
    expected = {
        "strategic_successes", "strategic_fallbacks", "strategic_expired_results",
        "strategic_context_successes",
        "jev_requests", "jev_failures", "jev_error_kinds", "rag_citation_ids",
        "degraded",
    }
    from tools.real_game_smoke import run
    result = run(duration=0, allow_real_game=False)
    # The opt-in guard returns before runtime fields; this is intentional and
    # prevents a skipped test from fabricating a real-game report.
    assert not expected.issubset(result)


def test_smoke_report_separates_pipeline_activity_from_provider_health():
    from tools.real_game_smoke import _build_report

    report = _build_report(
        brain=[{"request_id": "brain-old", "fallback": False}],
        jev=[{"fallback": True, "error_kind": "network_error"}],
        trace=[
            {"event_type": "decision", "source_type": "rule_fallback"},
            {"event_type": "expired_result", "request_id": "brain-old"},
        ],
    )

    # A decision proves that the game/agent pipe ran, but a fallback provider
    # must not be reported as a successful real-game release smoke.
    assert report["passed"] is False
    assert report["release_passed"] is False
    assert report["pipeline_passed"] is True
    assert report["provider_health_passed"] is False
    assert report["provider_health"] == {
        "jev": "unavailable", "strategic": "healthy"}
    assert report["strategic_usable_successes"] == 0
    assert report["strategic_expired_results"] == 1
    assert report["degraded"] is True


def test_smoke_report_passes_only_with_healthy_fresh_provider_results():
    from tools.real_game_smoke import _build_report

    report = _build_report(
        brain=[{"request_id": "brain-1", "fallback": False}],
        jev=[{"request_id": "jev-1", "fallback": False}],
        trace=[{"event_type": "decision", "source_type": "gpt_strategy"}],
    )

    assert report["pipeline_passed"] is True
    assert report["provider_health"] == {"jev": "healthy", "strategic": "healthy"}
    assert report["provider_health_passed"] is True
    assert report["release_passed"] is True
    assert report["passed"] is True
    assert report["degraded"] is False
    assert report["run_ended_with_pending_request"] is False


def test_smoke_report_marks_request_left_open_at_shutdown():
    from tools.real_game_smoke import _build_report

    report = _build_report(
        brain=[{"request_id": "brain-1", "fallback": False}],
        jev=[{"request_id": "jev-1", "fallback": False}],
        trace=[
            {"event_type": "provider_request", "request_id": "brain-1"},
            {"event_type": "provider_response", "request_id": "jev-1"},
            {"event_type": "decision", "source_type": "gpt_strategy"},
        ],
    )

    assert report["run_ended_with_pending_request"] is True
    assert report["in_flight_request_ids"] == ["brain-1"]


def test_smoke_report_rejects_expired_provider_result_even_when_healthy():
    from tools.real_game_smoke import _build_report

    report = _build_report(
        brain=[{"request_id": "brain-1", "fallback": False}],
        jev=[{"request_id": "jev-1", "fallback": False}],
        trace=[
            {"event_type": "decision", "source_type": "gpt_strategy"},
            {"event_type": "expired_result", "request_id": "brain-1"},
        ],
    )

    assert report["provider_health_passed"] is True
    assert report["strategic_expired_results"] == 1
    assert report["release_passed"] is False
    assert report["degraded"] is True


def test_smoke_reports_retained_strategy_context_separately_from_fresh_plans():
    from tools.real_game_smoke import _build_report

    report = _build_report(
        brain=[{"request_id": "brain-1", "fallback": False}],
        jev=[{"request_id": "jev-1", "fallback": False}],
        trace=[
            {"event_type": "decision", "source_type": "rule_fallback"},
            {"event_type": "expired_result", "request_id": "brain-1"},
            {"event_type": "strategic_context_retained", "plan_id": "p1",
             "context_only": True},
        ],
    )

    assert report["strategic_context_successes"] == 1
    assert report["strategic_usable_successes"] == 0
    assert report["release_passed"] is False


def test_real_game_smoke_cli_fails_for_degraded_opted_in_report(monkeypatch, capsys):
    import tools.real_game_smoke as smoke

    monkeypatch.setattr(smoke, "run", lambda **_: {
        "passed": False,
        "release_passed": False,
        "pipeline_passed": True,
        "provider_health_passed": False,
        "degraded": True,
    })

    assert smoke.main(["--allow-real-game", "--duration", "0"]) == 1
    assert '"release_passed": false' in capsys.readouterr().out


def test_smoke_report_treats_malformed_fallback_as_unknown_not_healthy():
    from tools.real_game_smoke import _build_report

    report = _build_report(
        brain=[{"fallback": {"unexpected": True}}],
        jev=[{"fallback": ["unexpected"]}],
        trace=[{"event_type": "decision", "source_type": "rule_fallback"}],
    )

    assert report["provider_health"] == {"jev": "unknown", "strategic": "unknown"}
    assert report["provider_health_passed"] is False
    assert report["release_passed"] is False


def test_real_game_smoke_restores_preexisting_files_and_removes_created_targets(tmp_path):
    from tools.real_game_smoke import _backup_files, _restore_files

    existing = tmp_path / "existing.properties"
    existing.write_text("custom=true\n", encoding="utf-8")
    created = tmp_path / "created.properties"
    files = {existing: "existing", created: "created"}
    backup = tmp_path / "backup"
    backup.mkdir()

    existed = _backup_files(files, backup)
    existing.write_text("smoke-overwrite=true\n", encoding="utf-8")
    created.write_text("smoke-created=true\n", encoding="utf-8")
    _restore_files(files, backup, existed)

    assert existed == {existing: True, created: False}
    assert existing.read_text(encoding="utf-8") == "custom=true\n"
    assert not created.exists()


def test_real_game_smoke_restore_does_not_create_missing_targets(tmp_path):
    from tools.real_game_smoke import _backup_files, _restore_files

    target = tmp_path / "missing.properties"
    files = {target: "missing"}
    backup = tmp_path / "backup"
    backup.mkdir()

    existed = _backup_files(files, backup)
    _restore_files(files, backup, existed)

    assert existed[target] is False
    assert not target.exists()
