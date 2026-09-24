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
from typing import Mapping

from spirebrain.environment import EnvironmentAdapter, host_environment

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
def find_steam_libraries(environment: EnvironmentAdapter | Mapping[str, str] | None = None) -> list[Path]:
    """Every Steam library root we can find, from the vdf files that list them."""
    roots: list[Path] = []
    env = host_environment(environment)
    candidates = list(env.steam_roots)
    if not candidates:
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


def check_game(rep: Report, environment: EnvironmentAdapter | Mapping[str, str] | None = None) -> tuple[Path | None, list[Path]]:
    """Returns the game directory and whatever jars are in its mods\\ folder."""
    try:
        from spirebrain import gamedata

        game = gamedata.find_game_dir(environment)
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
                   local_jar: bool = False,
                   environment: EnvironmentAdapter | Mapping[str, str] | None = None) -> Path | None:
    """The subscription-vs-download distinction, which is the whole point.

    `local_jar` says a CommunicationMod jar is already installed by other means
    (the game's `mods\\` folder). In that case the Workshop copy being absent is
    not a blocker, and reporting it as one sends the user off to fix a
    non-problem — which is what this check did until 2026-09-21.
    """
    libraries = find_steam_libraries(environment)
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


def find_communicationmod(mods_jars: list[Path],
                          environment: EnvironmentAdapter | Mapping[str, str] | None = None) -> list[Path]:
    """Every CommunicationMod jar we can find, best candidate first.

    Order matters for what the report *says*: the game's own `mods\\` folder is the
    installation we told the user to make, the Workshop folder is the alternative,
    and a disk-wide search is the last resort (it is slow, and on this machine it
    turned up a stray copy sitting in `D:\\` that the report had been presenting as
    the installation).
    """
    found: list[Path] = [p for p in mods_jars if "CommunicationMod" in p.name]
    env = host_environment(environment)
    roots = list(env.steam_roots) or [Path(f"{drive}:\\") for drive in ("C", "D", "E", "F")]
    for root in roots:
        content_roots = (
            root / "steamapps" / "workshop" / "content" / "646570" / "2131373661",
            root / "LeStoreDownload" / "steam" / "steamapps" / "workshop" /
            "content" / "646570" / "2131373661",
        )
        for content in content_roots:
            try:
                found.extend(sorted(content.glob("CommunicationMod*.jar")))
            except OSError:
                pass
    if found:
        return found
    for base in roots:
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


def check_mod_config(rep: Report,
                     environment: EnvironmentAdapter | Mapping[str, str] | None = None) -> None:
    """Judge every CommunicationMod-family config, not just the official one.

    `SpireConfig` is keyed on the mod name, so the official mod reads
    `ModTheSpire\\CommunicationMod\\config.properties` and the CJK fork reads
    `ModTheSpire\\CommunicationModCJK\\config.properties`. Writing one and hoping
    the player ticked the matching mod fails totally and silently when they did
    not: an empty `command=` launches no agent at all. Measured 2026-09-22, one
    hour lost, so every existing config is checked here.
    """
    try:
        from spirebrain.install_mod_config import check_argv, family_config_paths, parse_config
    except Exception as exc:  # noqa: BLE001
        rep.add(WARN, "CommunicationMod configs", f"cannot inspect: {exc}")
        return

    existing = [p for p in family_config_paths(environment) if p.exists()]
    if not existing:
        rep.add(WARN, "CommunicationMod config", "not created yet",
                "the mod creates it on its first load. Either start the game once with"
                " the mod enabled, or let us write it now:"
                " `python -m spirebrain.install_mod_config --write`")
        return

    # Which config is the LIVE one is decided by which mod is ticked, and
    # mod_lists.json knows. Without it, every existing config has to be treated as
    # possibly-live, which is why an empty one is a FAIL rather than a note.
    ticked = enabled_mod_jars(environment) or []
    required = None
    for jar in ticked:
        if jar.lower().startswith("communicationmod"):
            required = Path(jar).stem  # CommunicationModCJK.jar -> CommunicationModCJK
            break

    for cfg in existing:
        name = cfg.parent.name
        text = cfg.read_text(encoding="latin-1")   # the encoding the mod itself uses
        # Decode the way the mod does before judging the paths, or every \uXXXX
        # escape looks like a broken path. One implementation of the escaping lives
        # in install_mod_config, so doctor can never disagree with the writer.
        parsed = parse_config(text)
        command = parsed.get("command", "")
        is_live = required is None or required == name
        if not command.strip():
            # An empty command= means the game starts NO agent, and says nothing
            # about it. That is the failure this whole check exists for: 2026-09-22,
            # the player had ticked the fork while the command lived in the official
            # mod's config, and one hour went into finding it.
            status = FAIL if is_live else WARN
            why = ("this is the ticked mod, so nothing will run"
                   if is_live else
                   f"not the ticked mod ({required} is) — harmless right now")
            rep.add(status, f"Config ({name}): command=", f"missing or empty — {why}",
                    "run `python -m spirebrain.install_mod_config --write` — it writes"
                    " every config in the family, so it no longer matters which"
                    " CommunicationMod is ticked")
            continue
        if "run_agent.py" not in command and "stdio.py" in command:
            rep.add(FAIL if is_live else WARN, f"Config ({name}): command=",
                    "points at the module file",
                    "point it at run_agent.py: running a module file cannot import the"
                    " package")
            continue
        ok = "run_agent.py" in command
        label = f"Config ({name}{' = ticked' if is_live and required else ''})"
        rep.add(PASS if ok else WARN, label, command[:110],
                "" if ok else "expected run_agent.py; anything else needs a reason")
        argv = check_argv(command)
        print(f"          argv ({argv['argv_count']}): " + " | ".join(argv["argv"]))
        for problem in argv["problems"]:
            rep.add(FAIL, f"Config ({name}): command line", problem,
                    "fix it with `python -m spirebrain.install_mod_config --write`")


COMMUNICATIONMOD_FAMILY = ("CommunicationMod", "CommunicationModCJK")


def enabled_mod_jars(environment: EnvironmentAdapter | Mapping[str, str] | None = None) -> list[str] | None:
    """The mod jars ModTheSpire has TICKED, from its own saved list — or None.

    `%LOCALAPPDATA%\\ModTheSpire\\mod_lists.json` holds the selection the player
    actually launches with (`lists.<defaultList>` is a list of jar file names).
    Reading it is the difference between "two CommunicationMods are on disk" and
    "two are going to run", and only the second one is a problem: a jar sitting
    unticked in the Workshop folder does nothing.

    None means "no saved list" (ModTheSpire has never been launched, or the file
    moved), in which case the caller falls back to judging what is installed and
    says so rather than pretending to know.
    """
    local = host_environment(environment).local_appdata
    if local is None:
        return None
    path = local / "ModTheSpire" / "mod_lists.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        lists = data.get("lists") or {}
        jars = lists.get(data.get("defaultList")) or lists.get("<Default>") or []
        return [str(j) for j in jars] if isinstance(jars, list) else None
    except Exception:  # noqa: BLE001 - a corrupt list means "unknown", not "fail"
        return None


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
                                  mods_jars: list[Path],
                                  environment: EnvironmentAdapter | Mapping[str, str] | None = None) -> None:
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

    # Ask ModTheSpire what it is actually going to LOAD before judging what is on
    # disk. "Two jars installed" is not a problem; "two mods ticked" is, and only
    # the second one costs a player anything.
    ticked = enabled_mod_jars(environment)
    if ticked is not None:
        family = [j for j in ticked if j.lower().startswith("communicationmod")]
        overlay = [j for j in ticked if "spirebrain" in j.lower()]
        if not family:
            rep.add(FAIL, "CommunicationMod ticked", "none",
                    "tick ONE CommunicationMod in ModTheSpire. It is the pipe: without"
                    " it the agent receives no game state, so there is nothing to advise"
                    " on and the panel stays empty.")
        elif len(family) == 1:
            rep.add(PASS, "CommunicationMod ticked", family[0])
        else:
            rep.add(FAIL, "Two CommunicationMods are ticked", ", ".join(family),
                    "untick one in ModTheSpire. Each of them spawns its own agent"
                    " against the same game, so one agent's command lands on the NEXT"
                    " screen, and each doubles the JEV calls. Keep EITHER one - the"
                    " installer writes the config for both, so neither is special."
                    " Confirm the fix: the agent's banner in"
                    " communication_mod_errors.log appears once per launch, not twice.")
        if not overlay:
            rep.add(WARN, "In-game overlay not ticked",
                    "the agent runs but nothing is drawn in-game",
                    "tick SpireBrainOverlay.jar in ModTheSpire's mod list")
        else:
            rep.add(PASS, "In-game overlay ticked", overlay[0])
        return

    # No saved selection to read (ModTheSpire has never been launched, or the list
    # moved), so judge what is installed and say that is what this is judging.
    if game is not None:
        roots.append(game / "mods")
    # ModTheSpire auto-loads Workshop items too, so a second CommunicationMod
    # living there is just as active as one copied into mods\.
    for library in find_steam_libraries(environment):
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
                f"exactly one mod installed: {modid}"
                + (f" ({len(copies)} copies on disk; ModTheSpire dedupes by modid)"
                   if len(copies) > 1 else ""))
        return

    # Two DISTINCT modids installed, with no saved list to prove which is ticked.
    # A WARN, not a FAIL: grouping by modid already rules out the harmless case
    # (the same jar in mods\ and the Workshop folder is one mod to ModTheSpire),
    # but without mod_lists.json this genuinely cannot say whether both will load.
    names = ", ".join(sorted(by_modid))
    rep.add(WARN, "Two CommunicationMods installed", names,
            "launch ModTheSpire once so it saves the mod list, then re-run this"
            " check; if both are ticked, untick one (they each spawn an agent, and"
            " the second agent's command lands on the NEXT screen)")
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


def check_gamedata(rep: Report,
                   environment: EnvironmentAdapter | Mapping[str, str] | None = None) -> None:
    try:
        from spirebrain import gamedata

        gd = gamedata.get(environment=environment)
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

    from spirebrain.runtime_config import resolve_runtime_config

    runtime = resolve_runtime_config(ROOT, strategy=strategy)
    strategic_backend = runtime.brain_backend
    if str(strategic_backend).lower() in {
        "openai", "gpt", "deepseek", "qwen", "tongyi", "zhipu", "glm",
        "moonshot", "kimi", "siliconflow", "doubao", "openai-compatible",
        "openai_compatible", "compatible", "local", "vllm", "ollama",
    }:
        env_file = ROOT / ".env"
        has_key = bool(os.environ.get("BRAIN_API_KEY") or os.environ.get("OPENAI_API_KEY"))
        if not has_key and env_file.exists():
            has_key = any((line.strip().startswith("OPENAI_API_KEY=")
                           or line.strip().startswith("BRAIN_API_KEY="))
                          and line.split("=", 1)[1].strip().strip('"').strip("'")
                          for line in env_file.read_text(encoding="utf-8", errors="replace").splitlines()
                          if "=" in line)
        if has_key:
            rep.add(PASS, "Strategic GPT brain",
                    f"backend={strategic_backend} source={runtime.sources['brain_backend']} "
                    f"config={runtime.config_id}")
        else:
            rep.add(WARN, "Strategic GPT brain", "no strategic brain API key; JEV/rules fallback will be used",
                    "put BRAIN_API_KEY=... (or OPENAI_API_KEY=...) in .env, or set BRAIN_BACKEND=mock/disabled")
    else:
        rep.add(PASS, "Strategic brain",
                f"backend={strategic_backend} source={runtime.sources['brain_backend']} "
                f"config={runtime.config_id}")


def check_backend(rep: Report, live: bool) -> None:
    from spirebrain.runtime_config import load_dotenv, resolve_runtime_config

    load_dotenv(ROOT / ".env")
    runtime = resolve_runtime_config(ROOT)
    backend = runtime.jev_backend
    key_names = {
        "openrouter": "OPENROUTER_API_KEY", "llm": "OPENROUTER_API_KEY",
        "official": "TYPESAFE_API_KEY", "cloudflare": "CLOUDFLARE_API_TOKEN",
    }
    key_name = key_names.get(backend)
    has_key = bool(os.environ.get(key_name, "")) if key_name else True
    if key_name and has_key:
        rep.add(PASS, "JEV key", f"present for {backend} (not printed)")
    elif key_name:
        rep.add(WARN, "JEV key", f"no {key_name}",
                f"configure a credential for the selected {backend} provider, or select mock")
    rep.add(PASS, "Effective JEV backend",
            f"{backend} source={runtime.sources['jev_backend']} config={runtime.config_id}")

    if not live:
        return
    if key_name and not has_key:
        rep.add(WARN, "Live JEV probe", "skipped — selected provider key is missing")
        return
    try:
        from spirebrain.jev_brain.client import NoulSpec, get_client

        client = get_client(backend)
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
    environment = host_environment()
    print(f"Jev Spire Brain — environment check (repo: {ROOT})")
    print(f"live JEV probe: {'on' if live else 'off (pass --live to include)'}\n")

    rep = Report()
    check_python(rep)
    check_repo(rep)
    game, mods_jars = check_game(rep, environment)
    # Find the jar before judging the Workshop, so a locally installed mod is not
    # reported as a missing mandatory download.
    jars = find_communicationmod(mods_jars, environment)
    content = check_workshop(rep, local_jar=bool(jars), environment=environment)
    have_cm = check_communicationmod(rep, jars)
    check_single_communicationmod(rep, game, mods_jars, environment)
    check_overlay_jar(rep, game)
    check_mod_config(rep, environment)
    check_gamedata(rep, environment)
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
