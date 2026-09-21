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


def check_game(rep: Report) -> Path | None:
    try:
        from spirebrain import gamedata

        game = gamedata.find_game_dir()
    except Exception as exc:  # noqa: BLE001
        rep.add(FAIL, "Game install", f"gamedata could not load: {exc}", "")
        return None
    if game is None:
        rep.add(FAIL, "Game install", "desktop-1.0.jar not found",
                "set STS_GAME_DIR to your SlayTheSpire folder")
        return None
    rep.add(PASS, "Game install", str(game))
    java = game / "jre" / "bin" / "java.exe"
    if java.exists():
        rep.add(PASS, "Bundled Java", str(java))
    else:
        rep.add(WARN, "Bundled Java", "jre\\bin\\java.exe not found",
                "ModTheSpire needs a Java 8 runtime")
    mods_dir = game / "mods"
    if mods_dir.exists():
        jars = sorted(p.name for p in mods_dir.glob("*.jar"))
        rep.add(PASS, "mods\\ folder", f"{len(jars)} jar(s): {', '.join(jars) or 'empty'}")
    else:
        rep.add(WARN, "mods\\ folder", "does not exist",
                "not required if the mods come from the Workshop, but needed for the"
                " manual-jar fallback")
    return game


def check_workshop(rep: Report, appid: str = "646570") -> Path | None:
    """The subscription-vs-download distinction, which is the whole point."""
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

        if ledger["needs_download"] == "1":
            rep.add(WARN, "Steam download queue",
                    f"NeedsDownload=1 in {ledger['path'].name}",
                    "Steam still has workshop content pending")
        break
    return found_dir


def check_communicationmod_anywhere(rep: Report, content: Path | None) -> bool:
    """Final authority: does a CommunicationMod jar exist on disk at all?"""
    if content is not None and (content / "2131373661").exists():
        jars = list((content / "2131373661").glob("*.jar"))
        if jars:
            rep.add(PASS, "CommunicationMod jar", str(jars[0]))
            return True
    hits: list[Path] = []
    for drive in ("C:", "D:", "E:", "F:"):
        base = Path(drive + "\\")
        if not base.exists():
            continue
        for name in ("CommunicationMod.jar", "CommunicationMod*.jar"):
            try:
                hits.extend(p for p in base.rglob(name) if len(p.parts) < 12)
            except (OSError, PermissionError):
                continue
    # compare canonical paths so the two searches above do not double-report
    uniq = {p.resolve() for p in hits}
    if uniq:
        rep.add(PASS, "CommunicationMod jar", str(sorted(uniq)[0]))
        return True
    rep.add(FAIL, "CommunicationMod jar", "no file found on any drive",
            "download it in a browser from"
            " github.com/ForgottenArbiter/CommunicationMod/releases/latest and put it"
            " in the game's mods\\ folder")
    return False


def check_mod_config(rep: Report) -> None:
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        rep.add(WARN, "CommunicationMod config", "%LOCALAPPDATA% is not set")
        return
    cfg = Path(local) / "ModTheSpire" / "CommunicationMod" / "config.properties"
    if not cfg.exists():
        rep.add(WARN, "CommunicationMod config", f"not created yet ({cfg})",
                "the mod writes it on first run — start the game once with the mod"
                " enabled, then set command= to run_agent.py")
        return
    text = cfg.read_text(encoding="utf-8", errors="replace")
    rep.add(PASS, "CommunicationMod config", str(cfg))
    m = re.search(r"(?m)^\s*command\s*=\s*(.+)$", text)
    if not m:
        rep.add(FAIL, "Config: command=", "no command line set",
                "point it at run_agent.py — see docs/SETUP.md step 2")
        return
    command = m.group(1).strip()
    if "run_agent.py" in command:
        rep.add(PASS, "Config: command=", command[:110])
    elif "stdio.py" in command:
        rep.add(FAIL, "Config: command=", "points at the module file",
                "point it at run_agent.py: running a module file cannot import the"
                " package")
    else:
        rep.add(WARN, "Config: command=", command[:110],
                "expected run_agent.py; anything else needs a reason")
    for path in re.findall(r"[A-Za-z]:\\\\[^\s]+|(?<![\\\w])[A-Za-z]:\\[^\s]+", command):
        cleaned = Path(path.replace("\\\\", "\\"))
        if cleaned.suffix.lower() in (".exe", ".py") and not cleaned.exists():
            rep.add(FAIL, "Config: path exists?", f"{cleaned} — not found",
                    "fix the path or its escaping (backslashes and colons need \\\\)")


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
        from spirebrain.jev_brain.openrouter_client import OpenRouterJevClient

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
    game = check_game(rep)
    content = check_workshop(rep)
    have_cm = check_communicationmod_anywhere(rep, content)
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
