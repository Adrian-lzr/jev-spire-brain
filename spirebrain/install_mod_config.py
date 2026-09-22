"""Write CommunicationMod's config file correctly — verified against its source.

Why this exists
---------------
The manual instructions were wrong, and the failure mode is silent. Reading the
mod's source (2026-09-21) settled four things the docs and forum posts get wrong:

1. **The path is `%LOCALAPPDATA%\\ModTheSpire\\CommunicationMod\\config.properties`.**
   `SpireConfig("CommunicationMod", "config", defaults)` builds
   `ConfigUtils.CONFIG_DIR + "CommunicationMod" + "config.properties"`, and
   `CONFIG_DIR` on Windows is `%LOCALAPPDATA%\\ModTheSpire`.

2. **It is read as ISO-8859-1, not UTF-8.** `SpireConfig.load()` is
   `properties.load(new FileInputStream(file))`, and `java.util.Properties` reads
   bytes as ISO-8859-1, decoding `\\uXXXX` escapes. So a path containing Chinese
   characters written as raw UTF-8 is read back as mojibake and the process launch
   fails with a confusing error. Writing `\\uXXXX` escapes — which is what
   `Properties.store()` emits itself — is the format's native form and is
   encoding-proof. This is the bug this module removes.

3. **The command is split on whitespace.** `getSubprocessCommand()` is
   `getString("command").trim().split("\\\\s+")` and the result goes to
   `ProcessBuilder`, so there is *no quoting*: any space in either path breaks the
   launch. `check_argv()` reproduces that split so the problem is visible before
   the game is started.

4. **Only four keys exist**: `command`, `runAtGameStart`, `verbose`,
   `maxInitializationTimeout` (seconds, default 10 — the window the game waits for
   our `Ready` line before killing us).

Usage
-----
    python -m spirebrain.install_mod_config                  # preview only, writes nothing
    python -m spirebrain.install_mod_config --write           # apply (backs up any existing file)
    python -m spirebrain.install_mod_config --show            # print the current file, decoded
    python -m spirebrain.install_mod_config --backend official --write

    # let the agent start the run itself instead of waiting at the menu:
    python -m spirebrain.install_mod_config --auto-start --write
    python -m spirebrain.install_mod_config --auto-start --ascension 0 --write

    # which mode the launched agent runs in. `advise` (the default) recommends
    # and never acts - it will not even start a run for you; `play` is the
    # auto-player. The mode is written into the config on purpose: the installed
    # file is the record of what a launched agent will do.
    python -m spirebrain.install_mod_config --mode advise --write
    python -m spirebrain.install_mod_config --mode play --auto-start --write

Preview is the default because this writes outside the repository, into the user's
own mod settings directory.
"""

from __future__ import annotations

import os
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

MOD_NAME = "CommunicationMod"
CONFIG_NAME = "config.properties"
KEYS = ("command", "runAtGameStart", "verbose", "maxInitializationTimeout")

DEFAULT_BACKEND = "openrouter"
DEFAULT_RUN_AT_GAME_START = True
DEFAULT_VERBOSE = True
# 30 s, not the mod's own default 10: the agent's first cold start compiles
# every .pyc and initializes the JEV client + gamedata, which measured well
# over 10 s on 2026-09-22 — the game gave up ("Agent not reachable") before
# the agent could say Ready. A longer window costs nothing when startup is
# fast, because Ready is sent the moment it is.
DEFAULT_TIMEOUT = 30


# --------------------------------------------------------------------------- #
# Java .properties escaping (the reason this module exists)
# --------------------------------------------------------------------------- #
def escape_value(value: str) -> str:
    """Encode a value so `Properties.load(InputStream)` yields it back unchanged.

    Escapes backslash, the key/value separators, and leading spaces (which are
    stripped), then writes every non-Latin-1 character as `\\uXXXX` — the same
    transform `Properties.store()` applies, which is why this is the file's native
    form rather than a workaround.
    """
    out: list[str] = []
    for i, ch in enumerate(value):
        if ch == "\\":
            out.append("\\\\")
        elif ch in ":=#!":
            out.append("\\" + ch)
        elif ch == " " and i == 0:
            out.append("\\ ")
        elif ch == "\t":
            out.append("\\t")
        elif ch in ("\n", "\r"):
            out.append("\\n")
        elif ord(ch) < 0x20 or ord(ch) > 0x7E:
            # Anything outside printable ASCII becomes an escape, so the file's
            # bytes stay pure ASCII and no encoding can reinterpret them.
            if ord(ch) > 0xFFFF:
                # Java properties do not support \\uXXXX with more than 4 digits;
                # a surrogate pair keeps the value exact.
                hi, lo = ((ord(ch) - 0x10000) >> 10) + 0xD800, ((ord(ch) - 0x10000) & 0x3FF) + 0xDC00
                out.append(f"\\u{hi:04X}\\u{lo:04X}")
            else:
                out.append(f"\\u{ord(ch):04X}")
        else:
            out.append(ch)
    return "".join(out)


def decode_value(text: str) -> str:
    """Inverse of escape_value, mirroring what java.util.Properties does.

    Used to verify a written file: decode its bytes as ISO-8859-1 (exactly what
    `Properties.load(FileInputStream)` does) and check the result equals what we
    intended. If this passes, the mod will see the right path.

    Java writes characters above U+FFFF as a surrogate pair, so the inverse has to
    recombine them — otherwise an emoji in a path would decode to two lone
    surrogates rather than one character.
    """
    out: list[str] = []
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "\\" and i + 1 < len(text):
            nxt = text[i + 1]
            if nxt == "u" and i + 5 < len(text) + 1:
                hexpart = text[i + 2:i + 6]
                try:
                    out.append(chr(int(hexpart, 16)))
                    i += 6
                    continue
                except ValueError:
                    pass
            mapped = {"n": "\n", "r": "\r", "t": "\t", "f": "\f"}
            out.append(mapped.get(nxt, nxt))
            i += 2
            continue
        out.append(ch)
        i += 1
    joined = "".join(out)
    # Recombine any UTF-16 surrogate pairs, the way a Java reader would.
    try:
        return joined.encode("utf-16", "surrogatepass").decode("utf-16")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return joined


# --------------------------------------------------------------------------- #
# What the mod will actually run
# --------------------------------------------------------------------------- #
def check_argv(command: str) -> dict:
    """Reproduce `getString("command").trim().split("\\\\s+")` and judge the result.

    `ProcessBuilder` gets that array verbatim — no shell, no quoting — so quotes
    are not stripped (they become part of the argument) and a space inside a path
    silently produces the wrong arguments. Both are checked on the *raw* string,
    because after splitting a space is indistinguishable from a separator.
    """
    trimmed = command.strip()
    argv = trimmed.split() if trimmed else []
    problems: list[str] = []
    if not trimmed:
        problems.append("command is empty — the mod will fail to start anything")
    if '"' in trimmed or "'" in trimmed:
        problems.append("the command contains a quote; the mod does not strip quotes, so it "
                        "becomes part of an argument — paths with spaces cannot work")
    if "  " in trimmed:
        problems.append("consecutive spaces will produce empty arguments")
    for arg in argv:
        looks_like_path = ("\\" in arg or "/" in arg)
        if looks_like_path and not Path(arg).exists():
            # A URL is an argument value, not a file the mod opens — the
            # splitter just hands it to our agent, which parses it itself.
            # (Found by start.py's tests: --dashboard-url .../publish tripped
            # the naive path check, 2026-09-22.)
            if arg.startswith(("http://", "https://")):
                continue
            problems.append(f"no such file: {arg}")
    return {"argv": argv, "argv_count": len(argv), "problems": problems}


def build_config(command: str, *, run_at_game_start: bool = DEFAULT_RUN_AT_GAME_START,
                 verbose: bool = DEFAULT_VERBOSE,
                 timeout: int = DEFAULT_TIMEOUT) -> str:
    """The full file text, in the format `Properties.store()` would produce."""
    lines = [
        "# Jev Spire Brain - CommunicationMod settings",
        "# Written by `python -m spirebrain.install_mod_config`.",
        "#",
        "# Read by SpireConfig.load() as ISO-8859-1, so every non-ASCII character",
        "# is \\uXXXX-escaped: raw UTF-8 in a path would be misread. The command is",
        "# split on whitespace and passed to ProcessBuilder, so no path may contain",
        "# a space - there is no quoting. (This header is plain ASCII on purpose:",
        "# the whole file must stay ASCII so no encoding can reinterpret it.)",
        f"command={escape_value(command)}",
        f"runAtGameStart={'true' if run_at_game_start else 'false'}",
        f"verbose={'true' if verbose else 'false'}",
        f"maxInitializationTimeout={int(timeout)}",
        "",
    ]
    return "\n".join(lines)


def config_path() -> Path:
    """`ConfigUtils.CONFIG_DIR` + mod name + config name, per ModTheSpire source."""
    return config_dir(MOD_NAME) / CONFIG_NAME


def config_dir(mod_name: str) -> Path:
    local = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
    if not local:
        raise RuntimeError("%LOCALAPPDATA% is not set; cannot locate ModTheSpire's config dir")
    return Path(local) / "ModTheSpire" / mod_name


# Every mod in the CommunicationMod family keeps its OWN config file, because
# `SpireConfig` is keyed on the mod name:
#
#   CommunicationMod/config.properties      the official mod
#   CommunicationModCJK/config.properties   the CJK fork (modid CommunicationModCJK)
#
# Measured 2026-09-22, and it cost an hour: the player had unticked the official
# mod and kept the fork, so the fork's own — freshly created, therefore EMPTY —
# `command=` was the one being read. No command, no agent, no advice, and nothing
# anywhere said so. Writing ONE file and hoping the player ticked the matching mod
# is a trap, so we write every config whose mod is actually installed.
FAMILY = ("CommunicationMod", "CommunicationModCJK")


def family_config_paths() -> list[Path]:
    """Every config file this project should keep in sync, official one first.

    The official path is always included (it is created on demand). A fork's path
    is included when its directory already exists — that directory is how a mod
    that has run at least once shows up, so it means "this fork is in use here".
    """
    paths = [config_dir(FAMILY[0]) / CONFIG_NAME]
    for name in FAMILY[1:]:
        directory = config_dir(name)
        if directory.exists():
            paths.append(directory / CONFIG_NAME)
    return paths


def parse_config(text: str) -> dict:
    """Minimal .properties reader for verification (not a general parser)."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("!"):
            continue
        key, _, value = stripped.partition("=")
        out[key.strip()] = decode_value(value.strip())
    return out


def default_command(backend: str = DEFAULT_BACKEND, *, auto_start: bool = False,
                   klass: str | None = None, ascension: int | None = None,
                   mode: str | None = None) -> str:
    """`<python> <repo>/run_agent.py --backend <backend> [agent flags]`.

    Never the module file: running a script puts *that script's* directory first
    on `sys.path`, so pointing at `spirebrain/driver/stdio.py` cannot import the
    package.

    `--auto-start` is the flag that decides whether the agent starts a run by
    itself when it finds the game at the main menu. Without it a live launch sits
    silently at the menu — measured 2026-09-21, and from the outside it is
    indistinguishable from a dead process — so the flag is offered here instead of
    being left to a hand edit of the config file.

    `mode` is written into the config **explicitly** rather than left to the
    agent's default, and for a reason a player can feel: this file is the only
    record of what a launched agent will do, and a behaviour that changed because
    a default changed elsewhere is a behaviour nobody agreed to. The installed
    config says which mode it is, in the file itself.
    """
    agent = f"{sys.executable} {ROOT / 'run_agent.py'} --backend {backend}"
    if klass:
        agent += f" --class {klass}"
    if ascension is not None:
        agent += f" --ascension {int(ascension)}"
    if mode:
        agent += f" --mode {mode}"
    if auto_start:
        # Never in advisor mode: starting a run is a decision (which class, which
        # ascension, which seed) and the advisor's whole rule is that decisions
        # belong to the player. Silently dropping it here means the flag can be
        # passed harmlessly instead of producing a config that contradicts itself.
        if str(mode).lower() == "advise":
            pass
        else:
            agent += " --auto-start"
    return agent


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv: list[str]) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    def value(name: str) -> str | None:
        prefix = f"--{name}="
        hit = next((a.split("=", 1)[1] for a in argv if a.startswith(prefix)), None)
        if hit is None and f"--{name}" in argv:
            hit = argv[argv.index(f"--{name}") + 1]
        return hit

    paths = family_config_paths()
    path = paths[0]

    if "--show" in argv:
        for target in paths:
            print(f"config file: {target}")
            if not target.exists():
                print("  does not exist yet — the mod creates it on its first load.")
                continue
            text = target.read_bytes().decode("latin-1")
            print("--- raw bytes, decoded as ISO-8859-1 (what the mod reads) ---")
            print(text.rstrip())
            print("--- parsed keys ---")
            for key, val in parse_config(text).items():
                print(f"  {key} = {val}")
            print()
        return 0

    backend = value("backend") or DEFAULT_BACKEND
    mode = value("mode")
    # These three were documented in the usage block but never actually read, so
    # `--auto-start --write` silently wrote a config without it. Found while
    # adding `--mode` (2026-09-22): a documented flag that does nothing is worse
    # than a missing one, because it costs a user their debugging session.
    command = value("command") or default_command(
        backend, mode=mode,
        auto_start="--auto-start" in argv,
        klass=value("class"),
        ascension=int(value("ascension")) if value("ascension") else None)
    argv_check = check_argv(command)

    print(f"repo            : {ROOT}")
    print("config files    : " + ", ".join(str(p) for p in paths))
    print(f"exists yet      : {', '.join('yes' if p.exists() else 'no' for p in paths)}")
    if mode:
        print(f"agent mode      : {mode}"
              + ("  (recommends only - you keep the mouse and the keyboard;"
                 " starting a run included)" if mode == "advise"
                 else "  (auto-play: the agent plays the run itself)"))
    print()
    print(f"command (raw)   : {command}")
    print(f"argv the mod will build ({argv_check['argv_count']} elements):")
    for i, arg in enumerate(argv_check["argv"]):
        mark = "ok " if Path(arg).exists() or not arg.endswith((".exe", ".py")) else "MISSING"
        print(f"   [{i}] {mark} {arg}")
    print()
    print("--- file content to write (non-ASCII shown escaped) ---")
    text = build_config(command)
    print(text.rstrip())
    print("--- end ---")
    print()

    problems = list(argv_check["problems"])
    if not (ROOT / "run_agent.py").exists():
        problems.append("run_agent.py is missing from the repo root")
    if "stdio.py" in command:
        problems.append("command points at the module file; it cannot import the package — "
                        "use run_agent.py")
    non_ascii = [ch for ch in command if ord(ch) > 0x7E]
    if non_ascii:
        print(f"note: the command contains {len(non_ascii)} non-ASCII character(s); "
              "they are \\uXXXX-escaped above, which is what makes the file safe.")

    if problems:
        print("PROBLEMS:")
        for p in problems:
            print(f"   - {p}")
        print()
        print("Not writing. Fix the above first (a space in a path cannot be quoted — "
              "move the repo to an ASCII, space-free path).")
        return 1

    if "--write" not in argv:
        print("Preview only. Re-run with --write to apply.")
        return 0

    # Write EVERY family config, not just the official one. Each mod in the
    # family reads its own file, so writing only one means "it works if you ticked
    # the right mod" — and the failure is total silence, which is exactly how an
    # hour went missing on 2026-09-22 (the player had ticked the CJK fork, whose
    # own config was empty).
    written: list[Path] = []
    for target in paths:
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            backup = target.with_suffix(f".properties.bak-{time.strftime('%Y%m%d-%H%M%S')}")
            shutil.copy2(target, backup)
            print(f"backed up {backup.name} (in {backup.parent.name}\\)")
        target.write_bytes(text.encode("ascii"))
        written.append(target)

    # Verification: read the bytes back the way the mod does and check the value.
    for target in written:
        readback = parse_config(target.read_bytes().decode("latin-1"))
        if readback.get("command") != command:
            print(f"FAILED verification for {target}: the file does not decode back to"
                  " the intended command.")
            print(f"  intended: {command!r}")
            print(f"  read back: {readback.get('command')!r}")
            return 1
    print(f"wrote {len(written)} config file(s); each decodes back to the intended command")
    for target in written:
        print(f"  {target}")
    missing = [k for k in KEYS if k not in readback]
    if missing:
        print(f"warning: keys missing from the file: {missing}")
    print()
    print("Next: launch the game through ModTheSpire with ONE CommunicationMod enabled. "
          "In its settings panel the '(Re)start external process' button relaunches "
          "our agent without restarting the game. Its stderr lands in "
          "communication_mod_errors.log in the game folder.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
