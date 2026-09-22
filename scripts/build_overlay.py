"""Build the in-game overlay mod and install it into the game's mods folder.

    python scripts/build_overlay.py                 # compile to java/target/SpireBrainOverlay.jar
    python scripts/build_overlay.py --install       # + copy into the game's mods\\ folder

Needs: a JDK (javac) on PATH - the game's bundled JRE alone is not enough to
compile. The build uses javac --release 8 so the mod runs on the game's
bundled Java 8 runtime even though the compiler itself is much newer.

Why plain javac instead of Maven: the mod is three source files with two
dependencies (desktop-1.0.jar, BaseMod.jar) that already exist on this
machine. Maven would spend more of the user's time downloading itself than
the build takes.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))  # scripts/ sits below the repo root

JAVA_DIR = ROOT / "java"
OUT_DIR = JAVA_DIR / "target" / "classes"
JAR_PATH = JAVA_DIR / "target" / "SpireBrainOverlay.jar"


def find_jar(name_fragment: str) -> Path | None:
    """Locate a mod jar in the Workshop content dirs the doctor already knows."""
    from spirebrain.doctor import find_steam_libraries

    ids = {"BaseMod": "1605833019", "ModTheSpire": "1605060445"}
    item_id = ids[name_fragment]
    for lib in find_steam_libraries():
        content = lib / "steamapps" / "workshop" / "content" / "646570" / item_id
        if content.exists():
            for jar in sorted(content.glob("*.jar")):
                if name_fragment.lower() in jar.name.lower():
                    return jar
        # also the game's own mods folder
        game_mods = lib / "steamapps" / "common" / "SlayTheSpire" / "mods"
        if game_mods.exists():
            for jar in sorted(game_mods.glob("*.jar")):
                if name_fragment.lower() in jar.name.lower():
                    return jar
    return None


def find_game_dir() -> Path | None:
    from spirebrain import gamedata

    return gamedata.find_game_dir()


def needs_shading() -> bool:
    """True when org.json is not already provided by the game (it never is)."""
    return True


def download_org_json(dest: Path) -> Path | None:
    """Fetch org.json (one file, no deps) for shading; cached after the first run."""
    import urllib.request

    url = "https://repo1.maven.org/maven2/org/json/json/20240303/json-20240303.jar"
    try:
        urllib.request.urlretrieve(url, dest)
        return dest
    except Exception as exc:  # noqa: BLE001 - offline build should still try
        print(f"[build] could not fetch org.json ({exc}); building without it")
        return None


def compile_mod(game_jar: Path, basemod_jar: Path, mts_jar: Path,
                orgjson_jar: Path | None) -> int:
    sources = sorted((JAVA_DIR / "src" / "main" / "java").rglob("*.java"))
    resources = JAVA_DIR / "src" / "main" / "resources"
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    cp = os_pathsep().join(str(p) for p in [game_jar, basemod_jar, mts_jar]
                           + ([orgjson_jar] if orgjson_jar else []))
    cmd = [
        "javac", "--release", "8", "-encoding", "UTF-8",
        "-classpath", cp, "-d", str(OUT_DIR),
        *[str(s) for s in sources],
    ]
    print("[build] compiling:", " ".join(cmd[:6]), "…")
    # errors="replace": javac's stderr is console-encoded (GBK on this machine),
    # and letting subprocess decode it as UTF-8 crashes the reader thread.
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                            errors="replace")
    if result.returncode != 0:
        print(result.stderr)
        return result.returncode

    # copy resources (ModTheSpire.json, badge art)
    if resources.exists():
        for res in resources.rglob("*"):
            if res.is_file():
                rel = res.relative_to(resources)
                dest = OUT_DIR / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(res, dest)

    # package the jar, shading org.json classes in
    JAR_PATH.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(JAR_PATH, "w", zipfile.ZIP_DEFLATED) as jar:
        for f in sorted(OUT_DIR.rglob("*")):
            if f.is_file():
                jar.write(f, f.relative_to(OUT_DIR).as_posix())
        if orgjson_jar:
            with zipfile.ZipFile(orgjson_jar) as src:
                for name in src.namelist():
                    if name.startswith("org/json/") and name.endswith(".class") \
                            and "$" not in name.split("/")[-1]:
                        jar.writestr(name, src.read(name))
    print(f"[build] wrote {JAR_PATH}")
    return 0


def os_pathsep() -> str:
    return ";" if sys.platform == "win32" else ":"


def install(game_dir: Path) -> int:
    mods = game_dir / "mods"
    mods.mkdir(parents=True, exist_ok=True)
    dest = mods / JAR_PATH.name
    shutil.copy2(JAR_PATH, dest)
    print(f"[install] copied to {dest}")
    print("[install] done. Enable 'SpireBrain Overlay' in the ModTheSpire mod list "
          "alongside BaseMod, and start the agent (python start.py).")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="build (and optionally install) the overlay mod")
    ap.add_argument("--install", action="store_true", help="also copy into the game's mods folder")
    args = ap.parse_args(argv)

    game_dir = find_game_dir()
    if game_dir is None:
        print("[build] game not found (set STS_GAME_DIR)", file=sys.stderr)
        return 1
    game_jar = game_dir / "desktop-1.0.jar"
    basemod = find_jar("BaseMod")
    mts = find_jar("ModTheSpire")
    missing = [name for name, p in [("desktop-1.0.jar", game_jar),
                                    ("BaseMod.jar", basemod),
                                    ("ModTheSpire.jar", mts)] if p is None or not p.exists()]
    if missing:
        print(f"[build] missing dependencies: {missing}", file=sys.stderr)
        return 1

    cache = JAVA_DIR / "target"
    cache.mkdir(parents=True, exist_ok=True)
    orgjson_jar = cache / "json-20240303.jar"
    if not orgjson_jar.exists():
        got = download_org_json(orgjson_jar)
        if got is None:
            orgjson_jar = None

    code = compile_mod(game_jar, basemod, mts, orgjson_jar)
    if code != 0:
        return code
    if args.install:
        return install(game_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
