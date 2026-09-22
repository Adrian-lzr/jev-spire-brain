"""Build SpireBrainOverlay.jar — no Maven required.

Why this exists instead of a `mvn package`
-------------------------------------------
The pom.xml is still the reference for *what* goes in the jar, but Maven is not
installed on the machine this was developed on, and the build has exactly three
things to get right. Doing them here in the standard library means the player (or
a future me) can rebuild the overlay with the same Python that runs the agent:

    python java/build.py            # compile + package -> java/target/SpireBrainOverlay.jar
    python java/build.py --install  # ...and copy it into the game's mods\\ folder

The three things, each of which has bitten a mod build before:

1. **Target Java 8.** The game ships its own JRE and this install's is
   `1.8.0_144`. A jar compiled for a newer JVM dies in the game with
   `UnsupportedClassVersionError` *before* any of our code runs, which looks like
   "the mod does nothing". `javac --release 8` guarantees class file version 52.
2. **Shade org.json in.** ModTheSpire gives a mod no dependency resolution: a
   jar that references a class the game does not have fails at load. org.json is
   the one external dependency, so its classes are copied into the output.
3. **Keep ModTheSpire.json at the jar root.** That file is how ModTheSpire
   identifies the mod; if it ends up under `classes/` the game lists the mod as
   broken.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

JAVA_DIR = Path(__file__).resolve().parent
ROOT = JAVA_DIR.parent
TARGET = JAVA_DIR / "target"
CLASSES = TARGET / "classes"
STAGING = TARGET / "staging"
OUT_JAR = TARGET / "SpireBrainOverlay.jar"
SOURCES = JAVA_DIR / "src" / "main" / "java"
RESOURCES = JAVA_DIR / "src" / "main" / "resources"

# Vendored: org.json, the one non-game dependency (see the pom's shade config).
JSON_JAR = TARGET / "json-20240303.jar"
GAME_APPID = "646570"


# --------------------------------------------------------------------------- #
# Finding things
# --------------------------------------------------------------------------- #
def find_javac() -> Path | None:
    """A JDK's javac, newest-first among the usual install locations.

    The `javapath` shim directory only holds java/javac wrappers on some
    installs, and Android Studio's bundled JBR is a full JDK — both are checked
    because "javac is on PATH" and "javac can target 8" are different questions.
    """
    candidates = [
        Path(r"C:\Program Files\Java\jdk-21\bin\javac.exe"),
        Path(r"C:\Program Files\Java\jdk-17\bin\javac.exe"),
        Path(r"C:\Program Files\Android\Android Studio\jbr\bin\javac.exe"),
        Path(r"C:\Program Files\Eclipse Adoptium\bin\javac.exe"),
    ]
    for cand in candidates:
        if cand.exists():
            return cand
    for cand in Path(r"C:\Program Files\Java").glob("jdk-*/bin/javac.exe"):
        return cand
    found = shutil.which("javac")
    return Path(found) if found else None


def find_jar_tool(javac: Path) -> Path | None:
    """The `jar` tool next to javac (or on PATH)."""
    sibling = javac.with_name("jar.exe" if os.name == "nt" else "jar")
    if sibling.exists():
        return sibling
    found = shutil.which("jar")
    return Path(found) if found else None


def find_java_tool(javac: Path) -> Path | None:
    """The `java` launcher next to javac (or on PATH)."""
    sibling = javac.with_name("java.exe" if os.name == "nt" else "java")
    if sibling.exists():
        return sibling
    found = shutil.which("java")
    return Path(found) if found else None


def find_game_dir() -> Path | None:
    """The Slay the Spire install, from STS_GAME_DIR or the usual Steam paths."""
    env = os.environ.get("STS_GAME_DIR")
    if env and (Path(env) / "desktop-1.0.jar").exists():
        return Path(env)
    libraries = []
    for drive in ("C", "D", "E", "F"):
        for base in (rf"{drive}:\LeStoreDownload\steam", rf"{drive}:\Program Files (x86)\Steam",
                     rf"{drive}:\Steam"):
            if Path(base).exists():
                libraries.append(Path(base))
                vdf = Path(base) / "steamapps" / "libraryfolders.vdf"
                if vdf.exists():
                    import re
                    for m in re.finditer(r'"path"\s+"([^"]+)"',
                                         vdf.read_text(encoding="utf-8", errors="replace")):
                        libraries.append(Path(m.group(1).replace("\\\\", "\\")))
    for lib in libraries:
        game = lib / "steamapps" / "common" / "SlayTheSpire"
        if (game / "desktop-1.0.jar").exists():
            return game
    return None


def workshop_mod_jar(name: str) -> Path | None:
    """A mod jar from Steam's Workshop content for this game, if subscribed."""
    for drive in ("C", "D", "E", "F"):
        content = Path(rf"{drive}:\LeStoreDownload\steam\steamapps\workshop\content") / GAME_APPID
        if not content.exists():
            continue
        for jar in content.glob(f"*/{name}"):
            return jar
    return None


# --------------------------------------------------------------------------- #
# Build
# --------------------------------------------------------------------------- #
def ensure_json_jar() -> bool:
    """Fetch org.json if it is not vendored yet.

    `java/target/` is gitignored, so a fresh clone has no org.json to shade in and
    the mod would fail to load in the game with a NoClassDefFoundError. Downloading
    one 78 KB jar from Maven Central is a smaller ask than "go find it yourself",
    and it is the only external dependency this mod has.
    """
    if JSON_JAR.exists():
        return True
    url = ("https://repo1.maven.org/maven2/org/json/json/"
           "20240303/json-20240303.jar")
    print(f"[build] fetching {JSON_JAR.name} from Maven Central (one time, 78 KB)")
    try:
        import urllib.request
        JSON_JAR.parent.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(url, timeout=30) as resp:
            data = resp.read()
        if len(data) < 10_000:
            raise ValueError(f"got {len(data)} bytes, which is not a jar")
        JSON_JAR.write_bytes(data)
        print(f"[build] saved {JSON_JAR} ({len(data)} bytes)")
        return True
    except Exception as exc:  # noqa: BLE001 - offline is a normal state
        print(f"[build] could not download it ({exc}).")
        print(f"[build] offline? put the jar at {JSON_JAR} by hand — same URL.")
        return False


def dependencies(game: Path) -> list[Path]:
    """The compile classpath: the game, BaseMod, ModTheSpire, org.json."""
    deps = [game / "desktop-1.0.jar"]
    basemod = workshop_mod_jar("BaseMod.jar")
    mts = workshop_mod_jar("ModTheSpire.jar")
    if basemod:
        deps.append(basemod)
    if mts:
        deps.append(mts)
    if JSON_JAR.exists():
        deps.append(JSON_JAR)
    return [d for d in deps if d.exists()]


def compile_sources(javac: Path, deps: list[Path], classpath_sep: str = os.pathsep) -> int:
    java_files = sorted(str(p) for p in SOURCES.rglob("*.java"))
    if not java_files:
        print(f"[build] no sources under {SOURCES}")
        return 1
    CLASSES.mkdir(parents=True, exist_ok=True)
    cmd = [str(javac), "--release", "8", "-encoding", "UTF-8",
           "-classpath", classpath_sep.join(str(d) for d in deps),
           "-d", str(CLASSES)] + java_files
    print(f"[build] javac --release 8 ({len(java_files)} source file(s))")
    result = subprocess.run(cmd, capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
    if result.returncode != 0:
        print(result.stdout or "")
        print(result.stderr or "")
        print("[build] COMPILE FAILED — nothing was written")
        return result.returncode
    for line in (result.stderr or "").splitlines():
        if "warning" in line.lower():
            print(f"        {line.strip()[:150]}")
    return 0


def stage_and_package() -> int:
    """Lay out the jar's contents, then zip them: that order is the whole method."""
    if STAGING.exists():
        shutil.rmtree(STAGING)
    STAGING.mkdir(parents=True)
    shutil.copytree(CLASSES, STAGING, dirs_exist_ok=True)
    for res in RESOURCES.iterdir():
        shutil.copy2(res, STAGING / res.name)
    # Shade org.json in: ModTheSpire does no dependency resolution.
    if JSON_JAR.exists():
        with zipfile.ZipFile(JSON_JAR) as z:
            for name in z.namelist():
                if name.endswith("/") or name.startswith("META-INF/"):
                    continue
                dest = STAGING / name
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(z.read(name))

    if not (STAGING / "ModTheSpire.json").exists():
        print("[build] ModTheSpire.json missing from the staging root — the game would"
              " not recognise the jar")
        return 1

    OUT_JAR.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(OUT_JAR, "w", zipfile.ZIP_DEFLATED) as z:
        for path in sorted(STAGING.rglob("*")):
            if path.is_file():
                z.write(path, path.relative_to(STAGING).as_posix())
    print(f"[build] wrote {OUT_JAR} ({OUT_JAR.stat().st_size} bytes)")
    return 0


def verify_jar() -> bool:
    """Read the produced jar back the way the game's JVM will.

    The class file version is the check that matters: 52 is Java 8. A jar that
    passes everything else but is version 61 will simply not load in this game,
    and the failure is invisible from the build side.
    """
    ok = True
    with zipfile.ZipFile(OUT_JAR) as z:
        names = z.namelist()
        for required in ("ModTheSpire.json",
                         "io/github/adrianlzr/spirebrain/SpireBrainOverlayMod.class",
                         "io/github/adrianlzr/spirebrain/FeedClient.class",
                         "org/json/JSONObject.class"):
            if required not in names:
                print(f"[verify] MISSING {required}")
                ok = False
        ours = [n for n in names if n.startswith("io/github/adrianlzr/") and n.endswith(".class")]
        for name in ours:
            with z.open(name) as handle:
                head = handle.read(8)
            major = int.from_bytes(head[6:8], "big")
            if major != 52:
                print(f"[verify] {name} is class file version {major}, not 52 (Java 8)")
                ok = False
            break
        print(f"[verify] {len(names)} entries, {len(ours)} of ours, "
              f"{sum(1 for n in names if n.startswith('org/json/'))} shaded org.json classes")
    return ok


def install(game: Path) -> int:
    """Copy the jar into the game's mods\\ folder, backing up what was there."""
    mods = game / "mods"
    mods.mkdir(parents=True, exist_ok=True)
    dest = mods / "SpireBrainOverlay.jar"
    if dest.exists():
        backup = mods / f"SpireBrainOverlay.jar.bak-{time.strftime('%Y%m%d-%H%M%S')}"
        shutil.copy2(dest, backup)
        print(f"[install] backed up the old jar to {backup.name}")
    if dest.exists():
        try:
            dest.unlink()
        except PermissionError:
            print("[install] the game has the jar open — close Slay the Spire and retry")
            return 1
    shutil.copy2(OUT_JAR, dest)
    print(f"[install] {dest}")
    print("[install] next launch: ModTheSpire's mod list should show "
          "SpireBrain Overlay 0.2.0")
    return 0


def check_against_dashboard(javac: Path, java_tool: Path | None, url: str) -> int:
    """Run the mod's own poller+parser against a live dashboard.

    `FeedClient` is the only part of the mod with real logic, and the only part
    that can be exercised without launching the game. Everything else is draw
    calls. Without this, the first test of the parser is a player looking at a
    wrong panel and having to guess whether the agent or the parser was at fault.
    """
    if java_tool is None:
        print("[check] no java found to run the check with")
        return 2
    tools_out = TARGET / "tools"
    tools_out.mkdir(parents=True, exist_ok=True)
    tool = JAVA_DIR / "tools" / "ParseCheck.java"
    cmd = [str(javac), "--release", "8", "-encoding", "UTF-8",
           "-classpath", str(OUT_JAR), "-d", str(tools_out), str(tool)]
    result = subprocess.run(cmd, capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
    if result.returncode != 0:
        print(result.stdout or "")
        print(result.stderr or "")
        print("[check] could not compile the check tool")
        return result.returncode
    run = subprocess.run(
        [str(java_tool), "-cp", os.pathsep.join([str(OUT_JAR), str(tools_out)]),
         "io.github.adrianlzr.spirebrain.ParseCheck", url, "5"],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    print(run.stdout or "")
    if run.stderr:
        print(run.stderr[:800])
    return run.returncode


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Build (and optionally install) the overlay mod")
    ap.add_argument("--install", action="store_true",
                    help="copy the built jar into the game's mods folder (backs up)")
    ap.add_argument("--game-dir", default=None, help="override the Slay the Spire install")
    ap.add_argument("--check", nargs="?", const="http://127.0.0.1:8787", default=None,
                    metavar="URL",
                    help="run the mod's poller+parser against a live dashboard and print"
                         " what the panel would show")
    ap.add_argument("--no-verify", action="store_true")
    args = ap.parse_args(argv)

    game = Path(args.game_dir) if args.game_dir else find_game_dir()
    if game is None:
        print("[build] could not find Slay the Spire; pass --game-dir or set STS_GAME_DIR")
        return 2
    print(f"[build] game        : {game}")

    javac = find_javac()
    if javac is None:
        print("[build] no javac found. Install a JDK (any version that can target 8) or"
              " run this on a machine that has one.")
        return 2
    print(f"[build] javac       : {javac}")

    # Before the classpath is assembled: the sources import org.json, so a missing
    # jar is a compile error, not just a packaging detail.
    if not ensure_json_jar():
        print("[build] WARNING: org.json is missing, so it will not be shaded in and the"
              " mod will fail to load in the game (NoClassDefFoundError).")

    deps = dependencies(game)
    print(f"[build] classpath   : {', '.join(d.name for d in deps)}")
    code = compile_sources(javac, deps)
    if code:
        return code
    code = stage_and_package()
    if code:
        return code
    if not args.no_verify and not verify_jar():
        return 1
    if args.check:
        return check_against_dashboard(javac, find_java_tool(javac), args.check)
    if args.install:
        return install(game)
    print("[build] not installed (pass --install to copy it into the game's mods folder)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
