"""Environment check — one command that says what is missing and how to fix it.

Written because "is the mod installed?" turned out to need six separate manual
checks: the workshop content folder, Steam's own ledger, a disk-wide file search,
the game install, the mod config, and the game data. Doing that by hand once is
fine; doing it after every attempt is how people give up. This does all of it.

    python -m spirebrain.doctor                # offline checks
    python -m spirebrain.doctor --live         # + one real JEV call

Exit code is 1 if anything is FAIL, so it can gate a script. Stdlib only.

The Steam ledger check is the one that matters most and is the least obvious: a
Workshop **subscription** and a Workshop **download** are different states in
`appworkshop_<appid>.acf`. The id appearing under `WorkshopItemDetails` means Steam
knows you subscribed; only `WorkshopItemsInstalled` means the files are on disk.
We hit exactly that with CommunicationMod.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# What the project needs, and why. Ordered: a missing later item is not fatal on
# its own, a missing earlier one means nothing else can work.
WORKSHOP_EXPECTED = {
    "1605060445": ("ModTheSpire", True, "the mod loader — nothing loads without it"),
    "1605833019": ("BaseMod", True, "most mods depend on it"),
    "1609158507": ("StSLib", False, "library StSLib-based mods need; CommunicationMod does not"),
    "2131373661": ("CommunicationMod", True, "the pipe: game state out, commands in"),
}

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"


class Report:
    def __init__(self) -> None:
        self.lines: list[tuple[str, str, str]] = []

    def add(self, status: str, what: str, detail: str = "", fix: str = "") -> None:
        self.lines.append((status, what, detail))
        marker = {"PASS": "  ok  ", "WARN": " warn ", "FAIL": " FAIL "}[status]
        print(f"[{marker}] {what}" + (f" — {detail}" if detail else ""))
        if fix and status != PASS:
            print(f"          fix: {fix}")

    @property
    def failed(self) -> int:
        return sum(1 for s, _, _ in self.lines if s == FAIL)


# --------------------------------------------------------------------------- #
# Steam: the ledger and the content folder
# --------------------------------------------------------------------------- #
def find_steam_libraries() -> list[Path]:
    """Every Steam library root we can find, from the vdf files that list them."""
    roots: list[Path] = []
    candidates = []
    for drive in ("C", "D", "E", "F"):
        for base in (f"{drive}:\\Program Files (x86)\\Steam", f"{drive}:\\Steam",
                     f"{drive}:\\SteamLibrary", f"{drive}:\\Steam\\SteamApps",
                     f"{drive}:\\LeStoreDownload\\steam"):
            candidates.append(Path(base))
    for cand in candidates:
        for vdf in (cand / "config" / "libraryfolders.vdf", cand / "steamapps" / "libraryfolders.vdf"):
            if vdf.exists():
                try:
                    text = vdf.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                for m in re.finditer(r'"path"\s+"([^"]+)"', text):
                    p = Path(m.group(1).replace("\\\\", "\\"))
                    if p not in roots:
                        roots.append(p)
                if cand not in roots:
                    roots.append(cand)
    return roots


def read_workshop_ledger(library: Path, appid: str) -> dict:
    """Parse `appworkshop_<appid>.acf` into installed / subscribed-details sets."""
    acf = library / "steamapps" / "workshop" / f"appworkshop_{appid}.acf"
    out: dict = {"path": acf, "exists": acf.exists(), "installed": set(),
                 "details": set(), "needs_download": None}
    if not acf.exists():
        return out
    text = acf.read_text(encoding="utf-8", errors="replace")
    nd = re.search(r'"NeedsDownload"\s+"(\d+)"', text)
    out["needs_download"] = nd.group(1) if nd else None
    for block, key in (("WorkshopItemsInstalled", "installed"),
                       ("WorkshopItemDetails", "details")):
        m = re.search(rf'"{block}"\s*\{{(.*?)\n\t\}}', text, re.S)
        if m:
            # Only lines that are JUST a quoted number are item ids. A looser
            # pattern also catches `"manifest" "4757618196275874364"` and the
            # account id in `"subscribedby"`, which then look like phantom
            # subscriptions — found by running this against a real acf.
            out[key] = set(re.findall(r'(?m)^\s*"(\d{6,})"\s*$', m.group(1)))
    return out


def workshop_content_dir(library: Path, appid: str) -> Path:
    return library / "steamapps" / "workshop" / "content" / appid


# --------------------------------------------------------------------------- #
# Individual checks
# --------------------------------------------------------------------------- #
def check_python(rep: Report) -> None:
    v = sys.version_info
    if v >= (3, 10):
        rep.add(PASS, "Python version", f"{v.major}.{v.minor}.{v.micro}")
    else:
        rep.add(FAIL, "Python version", f"{v.major}.{v.minor} is too old",
                "install Python 3.10+")


def check_repo(rep: Report) -> None:
    needed = {
        "run_agent.py": "the launcher the game spawns",
        "config/strategy.json": "the strategy layer",
        "spirebrain/driver/stdio.py": "the protocol transport",
        "spirebrain/jev_brain/decisions.py": "the decision modules",
    }
    missing = [name for name in needed if not (ROOT / name).exists()]
    if missing:
        rep.add(FAIL, "Repository files", f"missing {', '.join(missing)}",
                "run from the repo root, or re-clone")
    else:
        rep.add(PASS, "Repository files", f"{len(needed)} key files present")


def check_game(rep: Report) -> tuple[Path | None, list[Path]]:
    """Returns the game directory and whatever jars are in its mods\\ folder."""
    try:
        from spirebrain import gamedata

        game = gamedata.find_game_dir()
    except Exception as exc:  # noqa: BLE001
        rep.add(FAIL, "Game install", f"gamedata could not load: {exc}", "")
        return None, []
    if game is None:
        rep.add(FAIL, "Game install", "desktop-1.0.jar not found",
                "set STS_GAME_DIR to your SlayTheSpire folder")
        return None, []
    rep.add(PASS, "Game install", str(game))
    java = game / "jre" / "bin" / "java.exe"
    if java.exists():
        rep.add(PASS, "Bundled Java", str(java))
    else:
        rep.add(WARN, "Bundled Java", "jre\\bin\\java.exe not found",
                "ModTheSpire needs a Java 8 runtime")
    mods_dir = game / "mods"
    jars: list[Path] = []
    if mods_dir.exists():
        jars = sorted(mods_dir.glob("*.jar"))
        rep.add(PASS, "mods\\ folder", f"{len(jars)} jar(s): "
                                       f"{', '.join(p.name for p in jars) or 'empty'}")
    else:
        rep.add(WARN, "mods\\ folder", "does not exist",
                "not required if the mods come from the Workshop, but needed for the"
                " manual-jar fallback")
    return game, jars


def check_workshop(rep: Report, appid: str = "646570",
                   local_jar: bool = False) -> Path | None:
    """The subscription-vs-download distinction, which is the whole point.

    `local_jar` says a CommunicationMod jar is already installed by other means
    (the game's `mods\\` folder). In that case the Workshop copy being absent is
    not a blocker, and reporting it as one sends the user off to fix a
    non-problem — which is what this check did until 2026-09-21.
    """
    libraries = find_steam_libraries()
    if not libraries:
        rep.add(WARN, "Steam libraries", "no libraryfolders.vdf found",
                "Steam may be installed in an unusual place")
        return None
    rep.add(PASS, "Steam libraries", ", ".join(str(p) for p in libraries))

    found_dir: Path | None = None
    for lib in libraries:
        ledger = read_workshop_ledger(lib, appid)
        if not ledger["exists"]:
            continue
        content = workshop_content_dir(lib, appid)
        on_disk = {p.name for p in content.iterdir() if p.is_dir()} if content.exists() else set()
        found_dir = found_dir or (content if content.exists() else None)

        for item_id, (name, required, why) in WORKSHOP_EXPECTED.items():
            if item_id in on_disk:
                jar = next(iter((content / item_id).glob("*.jar")), None)
                rep.add(PASS, f"Workshop: {name}", f"downloaded ({jar.name if jar else 'no jar?'})")
            elif item_id in ledger["details"]:
                if item_id == "2131373661" and local_jar:
                    rep.add(PASS, f"Workshop: {name}",
                            "not from the Workshop, but installed in the game's mods\\ folder")
                else:
                    rep.add(FAIL, f"Workshop: {name}",
                            f"SUBSCRIBED BUT NOT DOWNLOADED (id {item_id}, {why})",
                            "open the item in the Steam client and press Download, or"
                            " restart the Steam client, or launch the game once — a"
                            " subscription is not an install")
            elif required:
                rep.add(FAIL, f"Workshop: {name}", f"not subscribed (id {item_id}, {why})",
                        f"subscribe at steamcommunity.com/sharedfiles/filedetails/?id={item_id}")
            else:
                rep.add(WARN, f"Workshop: {name}", f"not present (id {item_id}, optional)",
                        "subscribe only if another mod requires it")

        extra = on_disk - set(WORKSHOP_EXPECTED)
        if extra:
            rep.add(WARN, "Workshop: other items", f"{len(extra)} unrecognised: {', '.join(sorted(extra))}",
                    "fine — just not used by this project")

        for other in ledger["details"] - set(WORKSHOP_EXPECTED) - on_disk:
            rep.add(WARN, "Workshop: unidentified subscription", f"id {other}",
                    "subscribed, not downloaded, not needed by this project")

        missing_required = [i for i in WORKSHOP_EXPECTED
                            if WORKSHOP_EXPECTED[i][1] and i not in on_disk]
        if ledger["needs_download"] == "1" and missing_required:
            rep.add(WARN, "Steam download queue",
                    f"NeedsDownload=1 in {ledger['path'].name}",
                    "Steam still has workshop content pending")
        break
    return found_dir


def find_communicationmod(mods_jars: list[Path]) -> list[Path]:
    """Every CommunicationMod jar we can find, best candidate first.

    Order matters for what the report *says*: the game's own `mods\\` folder is the
    installation we told the user to make, the Workshop folder is the alternative,
    and a disk-wide search is the last resort (it is slow, and on this machine it
    turned up a stray copy sitting in `D:\\` that the report had been presenting as
    the installation).
    """
    found: list[Path] = [p for p in mods_jars if "CommunicationMod" in p.name]
    for drive in ("C:", "D:", "E:", "F:"):
        content = Path(drive + "\\") / "LeStoreDownload" / "steam" / "steamapps" / "workshop" \
            / "content" / "646570" / "2131373661"
        try:
            found.extend(sorted(content.glob("CommunicationMod*.jar")))
        except OSError:
            pass
    if found:
        return found
    for drive in ("C:", "D:", "E:", "F:"):
        base = Path(drive + "\\")
        if not base.exists():
            continue
        try:
            found.extend(p for p in base.rglob("CommunicationMod*.jar") if len(p.parts) < 12)
        except (OSError, PermissionError):
            continue
    return sorted({p.resolve() for p in found})


def check_communicationmod(rep: Report, found: list[Path]) -> bool:
    """Report where the mod actually is, and where stray copies are."""
    if not found:
        rep.add(FAIL, "CommunicationMod jar", "no file found on any drive",
                "open github.com/ForgottenArbiter/CommunicationMod/releases/tag/v1.2.1"
                " and save CommunicationMod.jar (343,522 bytes) into the game's mods\\ folder"
                " — create it if needed")
        return False
    primary = found[0]
    rep.add(PASS, "CommunicationMod jar", str(primary))
    if len(found) > 1:
        rep.add(WARN, "CommunicationMod extra copies",
                ", ".join(str(p) for p in found[1:]),
                "harmless, but a jar outside the game's mods\\ folder and the Workshop"
                " folder does nothing — delete it if you did not mean to keep it")
    return True


def check_mod_config(rep: Report) -> None:
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        rep.add(WARN, "CommunicationMod config", "%LOCALAPPDATA% is not set")
        return
    cfg = Path(local) / "ModTheSpire" / "CommunicationMod" / "config.properties"
    if not cfg.exists():
        rep.add(WARN, "CommunicationMod config", f"not created yet ({cfg})",
                "the mod creates it on its first load. Either start the game once with"
                " the mod enabled, or let us write it now:"
                " `python -m spirebrain.install_mod_config --write`")
        return
    text = cfg.read_text(encoding="latin-1")   # the encoding the mod itself uses
    rep.add(PASS, "CommunicationMod config", str(cfg))

    # Decode the way the mod does before judging the paths, or every \uXXXX escape
    # looks like a broken path. One implementation of the escaping lives in
    # install_mod_config, so doctor can never disagree with the writer.
    from spirebrain.install_mod_config import check_argv, parse_config

    parsed = parse_config(text)
    command = parsed.get("command", "")
    if not command.strip():
        rep.add(FAIL, "Config: command=",
                "missing or empty",
                "run `python -m spirebrain.install_mod_config --write`")
        return
    if "run_agent.py" in command:
        rep.add(PASS, "Config: command=", command[:120])
    elif "stdio.py" in command:
        rep.add(FAIL, "Config: command=", "points at the module file",
                "point it at run_agent.py: running a module file cannot import the"
                " package")
    else:
        rep.add(WARN, "Config: command=", command[:120],
                "expected run_agent.py; anything else needs a reason")

    argv = check_argv(command)
    print(f"          argv ({argv['argv_count']}): " + " | ".join(argv["argv"]))
    for problem in argv["problems"]:
        rep.add(FAIL, "Config: command line", problem,
                "fix it with `python -m spirebrain.install_mod_config --write`")


COMMUNICATIONMOD_FAMILY = ("CommunicationMod", "CommunicationModCJK")


def _declared_modid(jar: Path) -> str | None:
    """The `modid` a jar declares, or None. Never raises: a corrupt jar is data."""
    import json as _json
    import zipfile

    try:
        with zipfile.ZipFile(jar) as z:
            entry = next((n for n in z.namelist()
                          if n.lower().endswith("modthespire.json")), None)
            if entry is None:
                return None
            data = _json.loads(z.read(entry).decode("utf-8", "replace"))
            modid = str(data.get("modid", "")).strip()
            return modid or None
    except Exception:  # noqa: BLE001 - a broken jar must not kill the doctor
        return None


def check_single_communicationmod(rep: Report, game: Path | None,
                                  mods_jars: list[Path]) -> None:
    """Exactly ONE CommunicationMod may be installed: it spawns the agent process.

    Measured 2026-09-22 on this machine, and nothing in the game reports it: with
    both the official mod and the CJK fork installed, ModTheSpire loads both
    (each in its own classloader — they declare 242 class files in common), and
    **each spawns its own agent process**. The evidence was two `Ready` handshakes
    162 ms apart in `mts_process_launch.log`, and every startup banner pair in
    `communication_mod_errors.log`.

    Why it matters: in play mode, two agents answer the same state, so the second
    command lands on the *next* screen (a `choose 0` meant for the map gets
    applied in the fight that followed). In advice mode the agent calls JEV twice
    per state, so the bill doubles. A jar is "active" if it is in the game's
    `mods\\` folder *or* anywhere in Steam's Workshop content for this game —
    ModTheSpire auto-loads Workshop items too.
    """
    by_modid: dict[str, list[Path]] = {}
    roots: list[Path] = []
    if game is not None:
        roots.append(game / "mods")
    # ModTheSpire auto-loads Workshop items too, so a second CommunicationMod
    # living there is just as active as one copied into mods\.
    for library in find_steam_libraries():
        content = workshop_content_dir(library, "646570")
        if content.exists():
            roots.extend(d for d in sorted(content.iterdir()) if d.is_dir())

    scanned: set[Path] = set()
    for root in roots:
        if not root.exists():
            continue
        for jar in sorted(root.glob("*.jar")):
            resolved = jar.resolve()
            if resolved in scanned:
                continue
            scanned.add(resolved)
            modid = _declared_modid(jar)
            if modid and modid.lower().startswith("communicationmod"):
                by_modid.setdefault(modid, []).append(jar)

    if not by_modid:
        return  # check_communicationmod already reports a missing jar
    if len(by_modid) == 1:
        modid = next(iter(by_modid))
        copies = by_modid[modid]
        rep.add(PASS, "CommunicationMod instances",
                f"exactly one mod active: {modid}"
                + (f" ({len(copies)} copies on disk; ModTheSpire dedupes by modid)"
                   if len(copies) > 1 else ""))
        return

    # Two DISTINCT modids is the failing case. Grouping by modid matters: the
    # same jar in mods\ and in the Workshop folder is one mod to ModTheSpire
    # (measured: its mod list showed CommunicationMod once), so complaining about
    # that would send the player chasing a non-problem.
    names = ", ".join(sorted(by_modid))
    rep.add(FAIL, "Two CommunicationMods are active", names,
            "untick one of them in ModTheSpire's mod list before launching (or"
            " unsubscribe the one you do not want in Steam's Workshop). Two of"
            " them each spawn their own agent against the same game, so one"
            " agent's command lands on the NEXT screen. Keep ONE: the CJK fork"
            " (CommunicationModCJK) if your game runs in Chinese/Japanese/Korean,"
            " the official CommunicationMod otherwise. To confirm the fix: the"
            " agent's banner in communication_mod_errors.log must appear once per"
            " launch, not twice.")
    for modid, jars in sorted(by_modid.items()):
        for jar in jars:
            print(f"          - {modid}: {jar}")


def check_overlay_jar(rep: Report, game: Path | None) -> None:
    """Is the in-game panel installed, and which version?

    The panel is the surface a player actually reads, so "the mod is missing" must
    be a reported fact rather than something noticed mid-run when the advice never
    appears. Version matters more than presence: an old jar still loads and still
    shows *something*, so a stale copy looks like a working panel that never
    learned to show the advice.
    """
    if game is None:
        return
    jar = game / "mods" / "SpireBrainOverlay.jar"
    if not jar.exists():
        rep.add(WARN, "In-game overlay", "not installed — advice only shows in the browser",
                "python java/build.py --install")
        return
    version = None
    try:
        import zipfile

        with zipfile.ZipFile(jar) as z:
            import json as _json

            version = _json.loads(z.read("ModTheSpire.json")
                                  .decode("utf-8", "replace")).get("version")
    except Exception:  # noqa: BLE001 - an unreadable jar is data, not a crash
        pass
    if version:
        rep.add(PASS, "In-game overlay", f"SpireBrainOverlay {version} in mods\\")
    else:
        rep.add(WARN, "In-game overlay", str(jar),
                "the jar has no readable ModTheSpire.json; rebuild it with"
                " `python java/build.py --install`")


def check_gamedata(rep: Report) -> None:
    try:
        from spirebrain import gamedata

        gd = gamedata.get()
        if not gd.loaded:
            rep.add(WARN, "Game data", "not loaded — card text will be missing",
                    "set STS_GAME_DIR, or ignore: the agent degrades to names only")
            return
        sizes = gd.stats()["sizes"]
        rep.add(PASS, "Game data", f"cards {sizes.get('cards', 0)}, relics "
                                   f"{sizes.get('relics', 0)}, potions {sizes.get('potions', 0)}")
    except Exception as exc:  # noqa: BLE001
        rep.add(WARN, "Game data", f"unavailable: {exc}", "")


def check_brain_config(rep: Report) -> None:
    strategy_file = ROOT / "config" / "strategy.json"
    try:
        strategy = json.loads(strategy_file.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        rep.add(FAIL, "Strategy config", f"unreadable: {exc}", "restore config/strategy.json")
        return
    from spirebrain.jev_brain.decisions import SCORE_ACCEPTANCE, VALID_SCORE_ACCEPTANCE

    configured = strategy.get("jev", {}).get("score_acceptance")
    if configured not in VALID_SCORE_ACCEPTANCE:
        rep.add(FAIL, "Score gate", f"invalid value {configured!r}",
                f"use one of {VALID_SCORE_ACCEPTANCE}")
    elif configured != SCORE_ACCEPTANCE:
        rep.add(WARN, "Score gate", f"config={configured} but code default={SCORE_ACCEPTANCE}",
                "harmless — the config wins — but the two are meant to agree")
    else:
        rep.add(PASS, "Score gate", f"{configured} (the experimentally selected one)")


def check_backend(rep: Report, live: bool) -> None:
    env_file = ROOT / ".env"
    key_in_env = bool(os.environ.get("OPENROUTER_API_KEY"))
    key_in_file = env_file.exists() and "OPENROUTER_API_KEY" in env_file.read_text(
        encoding="utf-8", errors="replace")
    # The .env file is what a real launch reads (run_agent.py loads it), so the
    # probe must read it too — checking for its existence is not the same as
    # having the key in this process. Mirrors run_agent._load_dotenv, minimal.
    if key_in_file and not key_in_env:
        for raw in env_file.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw.strip()
            if line.startswith("OPENROUTER_API_KEY=") and not key_in_env:
                os.environ["OPENROUTER_API_KEY"] = line.split("=", 1)[1].strip().strip('"').strip("'")
                key_in_env = True
    backend = os.environ.get("JEV_BACKEND") or "mock"
    if key_in_env or key_in_file:
        where = "process environment" if key_in_env else ".env"
        rep.add(PASS, "JEV key", f"present in {where} (not printed)")
    else:
        rep.add(WARN, "JEV key", "no OPENROUTER_API_KEY",
                "offline mock backend will be used; put the key in .env for real JEV")
    rep.add(PASS, "Default backend", backend)

    if not live:
        return
    if not (key_in_env or key_in_file):
        rep.add(WARN, "Live JEV probe", "skipped — no key")
        return
    try:
        # The class lives in client_real.py; openrouter_client holds the
        # LLM-structured stand-in. Wrong module here made the probe fail with
        # an ImportError that looked like a key problem (found 2026-09-22).
        from spirebrain.jev_brain.client_real import OpenRouterJevClient

        client = OpenRouterJevClient()
        resp = client.ask("A test message.", {"ok": __import__(
            "spirebrain.jev_brain.client", fromlist=["NoulSpec"]).NoulSpec(
            instructions="Is this a test message?")})
        answer = resp.answers["ok"]
        rep.add(PASS, "Live JEV probe", f"answered p={answer.value:.3f} via {resp.backend} "
                                        f"({resp.latency_ms} ms, ${resp.cost_usd:.8f})")
    except Exception as exc:  # noqa: BLE001
        rep.add(FAIL, "Live JEV probe", f"{type(exc).__name__}: {exc}",
                "check the key, quota, and the provider policy on the account")


def main(argv: list[str]) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    live = "--live" in argv
    print(f"Jev Spire Brain — environment check (repo: {ROOT})")
    print(f"live JEV probe: {'on' if live else 'off (pass --live to include)'}\n")

    rep = Report()
    check_python(rep)
    check_repo(rep)
    game, mods_jars = check_game(rep)
    # Find the jar before judging the Workshop, so a locally installed mod is not
    # reported as a missing mandatory download.
    jars = find_communicationmod(mods_jars)
    content = check_workshop(rep, local_jar=bool(jars))
    have_cm = check_communicationmod(rep, jars)
    check_single_communicationmod(rep, game, mods_jars)
    check_overlay_jar(rep, game)
    check_mod_config(rep)
    check_gamedata(rep)
    check_brain_config(rep)
    check_backend(rep, live)

    print()
    if rep.failed:
        print(f"{rep.failed} FAIL, {sum(1 for s, _, _ in rep.lines if s == WARN)} warn. "
              "Fix the FAILs above; each one names its fix.")
        if game and not have_cm:
            print("\nBlocking item: CommunicationMod. Nothing in Phase 1 can be tested "
                  "until its jar is on disk.")
        return 1
    print("No FAILs. Remaining warnings are optional or informative.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
