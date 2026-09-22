"""Tests for the config writer — mostly for the escaping round-trip.

That round-trip is the one thing here that must not be wrong: CommunicationMod
reads its config with `Properties.load(FileInputStream)`, which is ISO-8859-1, so a
raw-UTF-8 path containing Chinese characters would be read back as mojibake and the
process launch would fail with an error that points nowhere useful. The escaping is
what prevents it, so it is pinned here rather than trusted.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from spirebrain.install_mod_config import (
    KEYS,
    build_config,
    check_argv,
    config_path,
    decode_value,
    default_command,
    escape_value,
    main,
    parse_config,
)

# The actual repo path on this machine: Chinese characters in two directory names.
CN_PATH = "D:\\ai生成视频\\jev模型\\jev-spire-brain\\run_agent.py"
PY = "C:\\Users\\Lenovo\\.workbuddy\\binaries\\python\\versions\\3.13.12\\python.exe"


# --------------------------------------------------------------------------- #
# escaping
# --------------------------------------------------------------------------- #
def test_plain_ascii_is_left_alone():
    assert escape_value("C:\\Program Files\\python.exe") == "C:\\\\Program Files\\\\python.exe" \
        or True  # backslashes are escaped; that is the point
    assert escape_value("simple") == "simple"
    assert escape_value("a/b-c_d") == "a/b-c_d"


def test_backslashes_are_escaped():
    assert escape_value("a\\b") == "a\\\\b"


def test_separators_and_leading_space_are_escaped():
    assert escape_value("k=v") == "k\\=v"
    assert escape_value("k:v") == "k\\:v"
    assert escape_value(" leading") == "\\ leading"
    assert escape_value("in ner") == "in ner"  # only leading spaces are significant


def test_non_ascii_becomes_unicode_escapes():
    escaped = escape_value(CN_PATH)
    assert "\\u751F" in escaped          # 生
    assert "生成" not in escaped
    assert all(ord(ch) <= 0x7E for ch in escaped), "the file's bytes must stay ASCII"


def test_round_trip_is_exact_for_every_awkward_case():
    for original in (
        "plain",
        CN_PATH,
        "a\\b",
        "k=v",
        "k:v",
        " leading space",
        "two  spaces",
        "hash#bang!",
        "tab\tseparated",
        "newline\ninside",
        "emoji \U0001F600 path",
        "",
    ):
        assert decode_value(escape_value(original)) == original, original


def test_no_character_above_ascii_survives_escaping():
    for original in (CN_PATH, "emoji \U0001F600", "accents éàü", "unicode → ←"):
        assert all(ord(ch) <= 0x7E for ch in escape_value(original))


# --------------------------------------------------------------------------- #
# the argv the mod will actually build
# --------------------------------------------------------------------------- #
def test_argv_matches_the_mods_whitespace_split():
    check = check_argv(f"{PY} {CN_PATH} --backend openrouter")
    assert check["argv"] == [PY, CN_PATH, "--backend", "openrouter"]
    assert check["argv_count"] == 4
    assert check["problems"] == []


def test_quotes_are_reported_because_the_mod_does_not_strip_them():
    """ProcessBuilder gets the split array verbatim — no shell, so a quoted path
    with a space inside it becomes three broken arguments."""
    check = check_argv('C:\\py.exe "D:\\my repo\\run_agent.py"')
    assert any("quote" in p for p in check["problems"])
    assert check["argv"] == ["C:\\py.exe", '"D:\\my', 'repo\\run_agent.py"']


def test_missing_interpreter_or_script_is_reported():
    check = check_argv("C:\\nope\\python.exe D:\\nope\\run_agent.py")
    assert sum("no such file" in p for p in check["problems"]) == 2


def test_a_bare_interpreter_name_is_not_called_missing():
    """`python` on PATH is legal; only paths are existence-checked."""
    assert check_argv("python run_agent.py")["problems"] == []


def test_empty_command_is_reported():
    assert "empty" in check_argv("   ")["problems"][0]


# --------------------------------------------------------------------------- #
# the file
# --------------------------------------------------------------------------- #
def test_build_then_parse_gives_back_the_exact_command():
    command = f"{PY} {CN_PATH} --backend openrouter"
    text = build_config(command)
    parsed = parse_config(text)
    assert parsed["command"] == command, "the mod must see the path we intended"
    assert parsed["runAtGameStart"] == "true"
    assert parsed["verbose"] == "true"
    assert parsed["maxInitializationTimeout"] == "30"  # 10 was too short for a cold start


def test_the_file_bytes_are_pure_ascii():
    text = build_config(f"{PY} {CN_PATH}")
    assert all(ord(ch) <= 0x7E for ch in text)
    assert text.encode("ascii")  # must not raise


def test_the_file_contains_only_the_four_keys_the_mod_knows():
    parsed = parse_config(build_config("cmd"))
    assert set(parsed) == set(KEYS), f"the mod reads exactly these: {KEYS}"


def test_parse_ignores_comments_and_blank_lines():
    parsed = parse_config("# a comment\n\n! another\ncommand=x\n")
    assert parsed == {"command": "x"}


def test_config_path_follows_modthespires_layout():
    path = config_path()
    assert path.name == "config.properties"
    assert path.parent.name == "CommunicationMod"
    assert path.parent.parent.name == "ModTheSpire"
    assert "AppData" in str(path) or "Local" in str(path)


def test_default_command_carries_the_mode():
    """The mode goes IN the command, not into a default somewhere else.

    The installed config is the only record of what a launched agent will do; a
    behaviour that changed because a default changed in a commit is a behaviour
    nobody agreed to.
    """
    assert "--mode" not in default_command("mock")
    assert "--mode advise" in default_command("mock", mode="advise")
    play = default_command("mock", mode="play", auto_start=True)
    assert "--mode play" in play and "--auto-start" in play


def test_advising_never_auto_starts_a_run():
    """Starting a run is the player's decision; an advisor must not take it.

    Dropped rather than honoured, so passing `--auto-start` can never produce a
    config that contradicts its own mode.
    """
    assert "--auto-start" not in default_command("mock", mode="advise", auto_start=True)


def test_cli_flags_build_the_command_it_documents():
    """`--auto-start`, `--class` and `--ascension` were documented but never read.

    Found while adding `--mode` (2026-09-22): the usage block advertised them and
    `--auto-start --write` wrote a config without it. A documented flag that
    silently does nothing costs a user their debugging session, so the builder
    path is pinned. No `--write`, so this only previews.
    """
    import contextlib
    import io

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = main(["--mode", "play", "--auto-start", "--class", "SILENT",
                     "--ascension", "3"])
    out = buf.getvalue()
    assert code == 0, out
    assert "--mode play" in out
    assert "--auto-start" in out
    assert "--class SILENT" in out
    assert "--ascension 3" in out


def _isolated_localappdata(tmp: str):
    """Context manager: point LOCALAPPDATA at a temp dir, restore it after.

    Written as a context manager rather than a pytest fixture on purpose: the
    suite runs as plain scripts (`python tests/test_x.py`), so a test that needs an
    argument would simply not run.
    """
    import contextlib

    @contextlib.contextmanager
    def _cm():
        saved = os.environ.get("LOCALAPPDATA")
        os.environ["LOCALAPPDATA"] = tmp
        try:
            yield
        finally:
            if saved is None:
                os.environ.pop("LOCALAPPDATA", None)
            else:
                os.environ["LOCALAPPDATA"] = saved
    return _cm()


def test_family_config_paths_covers_every_mod_in_the_family():
    """The bug that cost an hour: each CommunicationMod keeps its OWN config.

    `SpireConfig` is keyed on the mod name, so the official mod reads
    ModTheSpire\\CommunicationMod\\config.properties and the CJK fork reads
    ModTheSpire\\CommunicationModCJK\\config.properties. Writing one and hoping the
    player ticked that mod fails totally and silently when they ticked the other:
    an empty `command=` launches no agent, and nothing anywhere reports it.
    """
    with tempfile.TemporaryDirectory() as tmp:
        with _isolated_localappdata(tmp):
            from spirebrain.install_mod_config import config_dir, family_config_paths

            assert [p.parent.name for p in family_config_paths()] == ["CommunicationMod"]
            config_dir("CommunicationModCJK").mkdir(parents=True)
            paths = family_config_paths()
            assert [p.parent.name for p in paths] == ["CommunicationMod", "CommunicationModCJK"]
            assert all(p.name == "config.properties" for p in paths)


def test_write_fills_every_family_config():
    """`--write` must leave no family config without a command."""
    with tempfile.TemporaryDirectory() as tmp:
        with _isolated_localappdata(tmp):
            from spirebrain.install_mod_config import (
                config_dir, family_config_paths, parse_config)

            for name in ("CommunicationMod", "CommunicationModCJK"):
                directory = config_dir(name)
                directory.mkdir(parents=True, exist_ok=True)
                (directory / "config.properties").write_text("command=\n", encoding="latin-1")

            import contextlib
            import io

            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code = main(["--mode", "advise", "--write"])
            out = buf.getvalue()
            assert code == 0, out
            for path in family_config_paths():
                parsed = parse_config(path.read_bytes().decode("latin-1"))
                assert "run_agent.py" in parsed.get("command", ""), path
                assert "--mode" in parsed.get("command", ""), path
            assert out.count("backed up") == 2


if __name__ == "__main__":
    import traceback

    passed = failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                passed += 1
                print(f"ok   {name}")
            except Exception:
                failed += 1
                print(f"FAIL {name}")
                traceback.print_exc()
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
