"""Extract authoritative card metadata from the player's own game install.

Why a bytecode reader instead of a wiki table
---------------------------------------------
Every public card list on the web is tied to *some* version of the game, and
several of the popular ones are now for the sequel — a card list that is one
patch (or one game) off is worse than no list, because it looks authoritative.
The install on disk is the only list that is guaranteed to match the run the
player is in. So cost/type/rarity/base numbers are read from the card classes
in `desktop-1.0.jar` via `javap`, and the output is data, not prose.

What is read, and how
---------------------
A Slay the Spire card constructor calls
`AbstractCard.<init>(id, name, img, cost, description, type, color, rarity, target)`
and then assigns its own numbers (`baseDamage`, `baseBlock`, `baseMagicNumber`,
`magicNumber`) with `putfield`. So in the bytecode:

* the numeric push immediately before the `CardType.*` reference is the **cost**;
* the enum references give **type / color / rarity / target**;
* the push immediately before each `putfield baseDamage:I` etc. is that number.

Usage
-----
    python tools/extract_card_meta.py                    # write the data file
    python tools/extract_card_meta.py --show Bash Anger # inspect a couple

Output: `spirebrain/data/card_meta.json` — a plain data file, committed so the
agent never needs a JDK at runtime. Re-run it after a game update.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

OUT = ROOT / "spirebrain" / "data" / "card_meta.json"

#: Packages that hold real, obtainable cards. `deprecated`, `tempCards` and
#: `optionCards` are excluded on purpose: the first two are dead or generated
#: code, and option cards only exist inside a single event.
PACKAGES = ("red", "green", "blue", "purple", "colorless", "curses", "status")

#: Which character a package belongs to (None = any character can get it).
COLOR_OF_PACKAGE = {
    "red": "IRONCLAD",
    "green": "SILENT",
    "blue": "DEFECT",
    "purple": "WATCHER",
    "colorless": "COLORLESS",
    "curses": "CURSE",
    "status": "STATUS",
}

INT_PUSH = {
    "iconst_m1": -1, "iconst_0": 0, "iconst_1": 1, "iconst_2": 2,
    "iconst_3": 3, "iconst_4": 4, "iconst_5": 5,
}

#: `putfield` names worth keeping, mapped to the JSON key they become.
NUMERIC_FIELDS = {
    "baseDamage": "damage",
    "baseBlock": "block",
    "baseMagicNumber": "magic",
    "magicNumber": "magic_now",
}

INSTR = re.compile(r"^\s*(\d+):\s+(\S+)\s*(.*)$")
CONSTRUCTOR = re.compile(r"^\s*public\s+([\w.$]+)\(\);\s*$", re.M)


def find_javap() -> Path | None:
    """A `javap` that can read a Java 8 jar, wherever it lives on this machine."""
    found = shutil.which("javap")
    if found:
        return Path(found)
    for root in (Path(r"C:\Program Files\Java"), Path(r"C:\Program Files\Eclipse Adoptium"),
                 Path(r"C:\Program Files\Android\Android Studio\jbr")):
        if root.exists():
            for candidate in sorted(root.rglob("javap.exe")):
                return candidate
    return None


def card_classes(jar: Path) -> list[tuple[str, str]]:
    """[(fqcn, package)] for every obtainable card in the jar."""
    out: list[tuple[str, str]] = []
    with zipfile.ZipFile(jar) as z:
        for name in z.namelist():
            if not name.endswith(".class") or "$" in name:
                continue
            parts = name[:-6].split("/")
            if len(parts) < 5 or parts[-2] not in PACKAGES:
                continue
            out.append((".".join(parts), parts[-2]))
    return sorted(out)


def _int_at(instructions: list[tuple[str, str]], index: int) -> int | None:
    """The integer this instruction pushes, or None if it is not a push."""
    mnem, operand = instructions[index]
    if mnem in INT_PUSH:
        return INT_PUSH[mnem]
    if mnem in ("bipush", "sipush"):
        try:
            return int(operand.split()[0])
        except (ValueError, IndexError):
            return None
    if mnem in ("ldc", "ldc_w"):
        hit = re.search(r"//\s*int\s+(-?\d+)", operand)
        if hit:
            return int(hit.group(1))
    return None


def _enum(operand: str, kind: str) -> str | None:
    hit = re.search(rf"AbstractCard\${kind}\.(\w+)", operand)
    return hit.group(1) if hit else None


def _id_from_bytecode(bytecode: str) -> str | None:
    """The card's in-game ID.

    Read from the argument of `LocalizedStrings.getCardStrings("...")` in the
    static initializer, which is exactly how the game looks its own card up —
    the constructor then passes the same constant as `id`. The *class* name is
    not it (`PommelStrike` is the class, `Pommel Strike` is the ID; `Strike_Red`
    is the class, `Strike_R` is the ID), and mixing the two made every lookup
    for a multi-word card silently miss one table or the other. Caught by the
    consistency guard in `tests/test_cards.py`.
    """
    lines = bytecode.splitlines()
    for i, line in enumerate(lines):
        if "getCardStrings" not in line:
            continue
        for back in range(i - 1, max(-1, i - 4), -1):
            hit = re.search(r'ldc\s+#\d+\s+//\s+String (.+?)\s*$', lines[back])
            if hit:
                return hit.group(1)
    return None


def parse_class(bytecode: str) -> dict | None:
    """Pull cost/type/color/rarity/target and base numbers out of one class dump."""
    bodies = list(CONSTRUCTOR.finditer(bytecode))
    if not bodies:
        return None
    # The no-arg constructor is the card's; any other is a copy/upgrade overload.
    body = bodies[0]
    end = len(bytecode)
    for nxt in CONSTRUCTOR.finditer(bytecode, body.end()):
        end = nxt.start()
        break
    section = bytecode[body.end():end]

    sup = section.find('AbstractCard."<init>"')
    if sup == -1:
        return None
    before, after = section[:sup], section[sup:]

    instructions: list[tuple[str, str]] = []
    for line in before.splitlines():
        hit = INSTR.match(line)
        if hit:
            instructions.append((hit.group(2), hit.group(3).strip()))

    info: dict = {}
    found_id = _id_from_bytecode(bytecode)
    if found_id:
        info["game_id"] = found_id

    # cost: the last integer pushed before the CardType reference
    for i, (mnem, operand) in enumerate(instructions):
        if _enum(operand, "CardType"):
            for back in range(i - 1, max(-1, i - 6), -1):
                value = _int_at(instructions, back)
                if value is not None:
                    info["cost"] = value
                    break
            break

    for i, (_, operand) in enumerate(instructions):
        for kind, key in (("CardType", "type"), ("CardColor", "color"),
                          ("CardRarity", "rarity"), ("CardTarget", "target")):
            value = _enum(operand, kind)
            if value and key not in info:
                info[key] = value

    # base numbers: the push immediately before each putfield
    tail = after.splitlines()
    for i, line in enumerate(tail):
        hit = re.search(r"putfield\s+#\d+\s+//\s+Field ([\w.$]+):I", line)
        if not hit:
            continue
        field = hit.group(1).split(".")[-1]
        key = NUMERIC_FIELDS.get(field)
        if not key or key in info:
            continue
        for back in range(i - 1, max(-1, i - 5), -1):
            prev = INSTR.match(tail[back])
            if not prev:
                continue
            value = _int_at([(prev.group(2), prev.group(3).strip())], 0)
            if value is not None:
                info[key] = value
                break
    return info or None


def extract(jar: Path, only: set[str] | None = None) -> dict:
    javap = find_javap()
    if javap is None:
        raise SystemExit("no javap found; install a JDK or fold the data by hand")
    classes = card_classes(jar)
    if only:
        classes = [(fq, pkg) for fq, pkg in classes if fq.rsplit(".", 1)[1] in only]
    print(f"[extract] {len(classes)} card classes from {jar.name}")

    # One javap call for everything: a per-class process would take minutes.
    result = subprocess.run([str(javap), "-c", "-p", "-classpath", str(jar)]
                            + [fq for fq, _ in classes],
                            capture_output=True, text=True, encoding="utf-8",
                            errors="replace", timeout=900)
    if result.returncode != 0:
        raise SystemExit(f"javap failed: {(result.stderr or '')[:400]}")
    dump = result.stdout or ""

    # Split the dump back into per-class chunks by their header line.
    chunks: dict[str, str] = {}
    current = None
    for line in dump.splitlines(keepends=True):
        if line.startswith("public class ") or line.startswith("public abstract class "):
            after = line.split()[2]
            current = after
            chunks[current] = ""
        if current:
            chunks[current] += line

    cards: dict[str, dict] = {}
    for fq, pkg in classes:
        chunk = chunks.get(fq)
        if not chunk:
            continue
        info = parse_class(chunk)
        if not info:
            continue
        info["id"] = fq.rsplit(".", 1)[1]
        info["package"] = pkg
        info["character"] = COLOR_OF_PACKAGE.get(pkg)
        cards[info["id"]] = info
    return cards


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--show", nargs="*", default=None,
                    help="print these cards' parsed data instead of writing the file")
    ap.add_argument("--out", default=str(OUT))
    args = ap.parse_args(argv)

    from spirebrain.gamedata import find_game_dir

    game = find_game_dir()
    if game is None:
        print("could not find Slay the Spire; set STS_GAME_DIR")
        return 2
    jar = Path(game) / "desktop-1.0.jar"
    if not jar.exists():
        print(f"no desktop-1.0.jar in {game}")
        return 2

    if args.show is not None:
        cards = extract(jar, only=set(args.show) or None)
        if args.show:
            for name in args.show:
                print(name, "->", json.dumps(cards.get(name), ensure_ascii=False))
        else:
            print(json.dumps(cards, ensure_ascii=False, indent=1)[:2000])
        return 0

    cards = extract(jar)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(cards, ensure_ascii=False, indent=1, sort_keys=True),
                   encoding="utf-8")
    by_type: dict[str, int] = {}
    for card in cards.values():
        by_type[card.get("type", "?")] = by_type.get(card.get("type", "?"), 0) + 1
    print(f"[extract] wrote {len(cards)} cards -> {out}")
    print(f"[extract] by type: {by_type}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
