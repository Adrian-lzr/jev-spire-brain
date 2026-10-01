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
        brain=[{"request_id": "brain-old", "backend": "hiyo-codex",
                 "fallback": False}],
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
        brain=[{"request_id": "brain-1", "backend": "hiyo-codex",
                 "fallback": False}],
        jev=[{"request_id": "jev-1", "backend": "openrouter",
              "fallback": False}],
        trace=[{"event_type": "decision", "source_type": "gpt_strategy"}],
    )

    assert report["pipeline_passed"] is True
    assert report["provider_health"] == {"jev": "healthy", "strategic": "healthy"}
    assert report["provider_health_passed"] is True
    assert report["release_passed"] is True
    assert report["passed"] is True
    assert report["degraded"] is False
    assert report["run_ended_with_pending_request"] is False
    assert report["strategic_status"] == "fresh_plan_received"
    assert report["strategic_latency"] == {"sample_count": 0, "p50_ms": None, "p95_ms": None}


def test_smoke_report_marks_request_left_open_at_shutdown():
    from tools.real_game_smoke import _build_report

    report = _build_report(
        brain=[{"request_id": "brain-1", "backend": "hiyo-codex",
                 "fallback": False}],
        jev=[{"request_id": "jev-1", "backend": "openrouter",
              "fallback": False}],
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
        brain=[{"request_id": "brain-1", "backend": "hiyo-codex",
                 "fallback": False}],
        jev=[{"request_id": "jev-1", "backend": "openrouter",
              "fallback": False}],
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
    assert report["strategic_status"] == "connected_but_stale"
    assert report["release_passed"] is False


def test_smoke_report_classifies_provider_failure_and_latency_percentiles():
    from tools.real_game_smoke import _build_report

    report = _build_report(
        brain=[
            {"request_id": "brain-1", "fallback": True, "error_kind": "timeout",
             "latency_ms": 20},
            {"request_id": "brain-2", "fallback": True, "error_kind": "auth",
             "latency_ms": 80},
        ],
        jev=[],
        trace=[{"event_type": "decision", "source_type": "rule_fallback"}],
    )

    assert report["strategic_status"] == "provider_failed"
    assert report["strategic_error_kinds"] == ["auth", "timeout"]
    assert report["strategic_latency"] == {"sample_count": 2, "p50_ms": 50.0, "p95_ms": 80.0}


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


def test_smoke_report_never_counts_mock_provider_as_live_health():
    from tools.real_game_smoke import _build_report

    report = _build_report(
        brain=[{"backend": "hiyo-codex", "fallback": True,
                "error_kind": "missing_key"}],
        jev=[{"backend": "mock", "fallback": False}],
        trace=[{"event_type": "decision", "source_type": "rule_fallback"}],
    )

    assert report["provider_health"] == {
        "jev": "mock", "strategic": "unavailable"}
    assert report["provider_health_passed"] is False
    assert report["release_passed"] is False


def test_real_game_smoke_never_touches_source_saves_or_appdata_configs(tmp_path):
    # The runner must not manage source configs at all; its outputs belong only
    # to the isolated workspace. Kept as a named regression for the old bug.
    from tools.real_game_smoke import _create_isolated_workspace

    project = tmp_path / "project-source"
    game = tmp_path / "game-source"
    workshop = tmp_path / "workshop-source"
    workspace = tmp_path / "smoke-workspace"
    for directory in (project / "spirebrain", project / "config",
                      game / "jre" / "bin", game / "mods",
                      game / "saves", game / "preferences", workshop / "mts",
                      workshop / "base"):
        directory.mkdir(parents=True, exist_ok=True)
    (project / "start.py").write_text("pass\n", encoding="utf-8")
    (project / "run_agent.py").write_text("pass\n", encoding="utf-8")
    (project / "spirebrain" / "module.py").write_text("pass\n", encoding="utf-8")
    (project / "config" / "strategy.json").write_text("{}\n", encoding="utf-8")
    (game / "jre" / "bin" / "java.exe").write_bytes(b"java-fixture")
    (game / "saves" / "IRONCLAD.autosave").write_text("keep", encoding="utf-8")
    (game / "preferences" / "prefs").write_text("keep", encoding="utf-8")
    (game / "mods" / "CommunicationMod.jar").write_bytes(b"comm")
    (workshop / "mts" / "ModTheSpire.jar").write_bytes(b"mts")
    (workshop / "base" / "BaseMod.jar").write_bytes(b"base")
    original_mod_config = tmp_path / "original-appdata" / "CommunicationMod" / "config.properties"
    original_mod_config.parent.mkdir(parents=True)
    original_mod_config.write_text("command=player-config\n", encoding="utf-8")
    original_before = original_mod_config.read_bytes()
    source_game_before = {
        path.relative_to(game): path.read_bytes()
        for path in game.rglob("*") if path.is_file()
    }

    layout = _create_isolated_workspace(
        project, game, game / "jre" / "bin" / "java.exe",
        workshop / "mts" / "ModTheSpire.jar", workshop, workspace,
    )

    assert (game / "saves" / "IRONCLAD.autosave").read_text(encoding="utf-8") == "keep"
    assert (game / "preferences" / "prefs").read_text(encoding="utf-8") == "keep"
    assert source_game_before == {
        path.relative_to(game): path.read_bytes()
        for path in game.rglob("*") if path.is_file()
    }
    assert original_mod_config.read_bytes() == original_before
    assert not (layout.game / "saves").exists()
    assert not (layout.game / "preferences").exists()
    assert not (layout.project / "logs").exists()
    assert (layout.local_appdata / "ModTheSpire" / "mod_lists.json").exists()
    assert set(layout.mod_jar_names) == {
        "ModTheSpire.jar", "BaseMod.jar", "CommunicationMod.jar"}

    overlapping_workspace = game / "smoke-output"
    try:
        _create_isolated_workspace(
            project, game, game / "jre" / "bin" / "java.exe",
            workshop / "mts" / "ModTheSpire.jar", workshop,
            overlapping_workspace,
        )
    except ValueError as exc:
        assert "must not overlap" in str(exc)
    else:
        raise AssertionError("workspace inside source game must be rejected")
    assert not overlapping_workspace.exists()


def test_isolated_copy_skips_dotenv_keys_logs_and_untrusted_mods(tmp_path):
    from tools.real_game_smoke import _create_isolated_workspace

    project = tmp_path / "project-source"
    game = tmp_path / "game-source"
    workshop = tmp_path / "workshop-source"
    workspace = tmp_path / "smoke-workspace"
    for directory in (project / "config", project / "logs", game / "jre" / "bin",
                      game / "mods", game / "saves", workshop / "mts",
                      workshop / "base"):
        directory.mkdir(parents=True, exist_ok=True)
    (project / ".env").write_text("BRAIN_API_KEY=never-copy\n", encoding="utf-8")
    (project / ".env.example").write_text("BRAIN_BACKEND=openai\n", encoding="utf-8")
    (project / "credentials.json").write_text('{"token":"never-copy"}', encoding="utf-8")
    (project / "logs" / "decision_trace.jsonl").write_text("private-log", encoding="utf-8")
    (project / "config" / "strategy.json").write_text("{}\n", encoding="utf-8")
    (game / "jre" / "bin" / "java.exe").write_bytes(b"java-fixture")
    (game / "steam_appid.txt").write_text("646570", encoding="ascii")
    (game / "config.json").write_text('{"user":"source"}', encoding="utf-8")
    (game / "info.displayconfig").write_text("display", encoding="utf-8")
    (game / "twitchconfig.txt").write_text("twitch", encoding="utf-8")
    (game / "communication_mod_errors.log").write_text("log", encoding="utf-8")
    (game / "saves" / "IRONCLAD.autosave").write_text("save", encoding="utf-8")
    (game / "mods" / "CommunicationMod.jar").write_bytes(b"comm")
    (game / "mods" / "UnrelatedMod.jar").write_bytes(b"unrelated")
    (workshop / "mts" / "ModTheSpire.jar").write_bytes(b"mts")
    (workshop / "base" / "BaseMod.jar").write_bytes(b"base")
    game_before = {path.relative_to(game): path.read_bytes()
                   for path in game.rglob("*") if path.is_file()}

    layout = _create_isolated_workspace(
        project, game, game / "jre" / "bin" / "java.exe",
        workshop / "mts" / "ModTheSpire.jar", workshop, workspace,
    )

    assert not (layout.project / ".env").exists()
    assert not (layout.project / "credentials.json").exists()
    assert not (layout.project / "logs").exists()
    assert (layout.project / ".env.example").is_file()
    assert not (layout.game / "steam_appid.txt").exists()
    assert not (layout.game / "config.json").exists()
    assert not (layout.game / "info.displayconfig").exists()
    assert not (layout.game / "twitchconfig.txt").exists()
    assert not (layout.game / "communication_mod_errors.log").exists()
    assert not (layout.game / "saves").exists()
    assert not (layout.game / "mods" / "UnrelatedMod.jar").exists()
    assert game_before == {path.relative_to(game): path.read_bytes()
                           for path in game.rglob("*") if path.is_file()}


def test_isolated_environment_and_generated_commands_are_bounded(tmp_path):
    from tools.real_game_smoke import (
        IsolatedWorkspace,
        MAX_SMOKE_DURATION_SECONDS,
        _bounded_actions,
        _bounded_duration,
        _bounded_request_attempts,
        _isolated_environment,
        _launch_commands,
    )

    root = tmp_path / "workspace"
    project = root / "project"
    game = root / "game"
    user = root / "user"
    for path in (project, game / "jre" / "bin", game / "mods", user,
                 root / "roaming", root / "local", root / "temp", root / "prefs"):
        path.mkdir(parents=True, exist_ok=True)
    (project / "start.py").write_text("pass\n", encoding="utf-8")
    layout = IsolatedWorkspace(
        root, project, game, user, root / "roaming", root / "local", root / "temp",
        root / "prefs", root / "temp" / "request-attempts.count",
        ("ModTheSpire.jar", "BaseMod.jar", "CommunicationMod.jar"), (),
    )

    env = _isolated_environment(layout, request_attempt_cap=17, max_actions=99,
                                base={"LOCALAPPDATA": "original-appdata", "PATH": "safe"},
                                jev_backend="openrouter",
                                brain_backend="openai", brain_model="gpt6sol-test",
                                reasoning_effort="max")
    agent, java = _launch_commands(
        layout, mode="advise", strategy_planning_enabled=False,
        brain_backend="openai", brain_model="gpt6sol-test")

    assert _bounded_duration(10000) == MAX_SMOKE_DURATION_SECONDS
    assert _bounded_duration(0) == 5
    assert _bounded_actions(99) == 99
    assert _bounded_request_attempts(17) == 17
    assert env["LOCALAPPDATA"] == str(root / "local")
    assert env["STS_GAME_DIR"] == str(game)
    assert env["JEVBRAIN_MAX_ACTIONS"] == "99"
    assert env["SPIREBRAIN_MAX_REQUEST_ATTEMPTS"] == "17"
    assert env["JEV_BACKEND"] == "openrouter"
    assert env["BRAIN_MODEL"] == "gpt6sol-test"
    assert env["BRAIN_REASONING_EFFORT"] == "max"
    assert "--advise" in agent and "--play" not in agent
    assert "--backend" in agent
    assert agent[agent.index("--backend") + 1] == "openrouter"
    assert "--jev-only" in agent
    assert "--brain-model" in agent and "gpt6sol-test" in agent
    assert java[-1] == "--skip-launcher"
    assert str(game) in java[-2]
    assert all(str(root) not in arg or "original" not in arg for arg in agent + java)

    try:
        _bounded_actions(20_001)
    except ValueError:
        pass
    else:
        raise AssertionError("smoke action limit must reject values above the hard cap")
    try:
        _bounded_request_attempts(0)
    except ValueError:
        pass
    else:
        raise AssertionError("request-attempt cap must be finite and positive")


def test_real_game_smoke_rejects_mock_jev_before_path_discovery(monkeypatch):
    import tools.real_game_smoke as smoke

    def paths_must_not_be_looked_up():
        raise AssertionError("invalid provider must be rejected before touching game paths")

    monkeypatch.setattr(smoke, "_find_paths", paths_must_not_be_looked_up)
    try:
        smoke.run(duration=0, allow_real_game=True, jev_backend="mock")
    except ValueError as exc:
        assert "live JEV backend" in str(exc)
    else:
        raise AssertionError("an opted-in real-game smoke cannot use the offline mock")
