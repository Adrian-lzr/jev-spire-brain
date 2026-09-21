"""Authoritative game data, read from the player's own Slay the Spire install.

Why this module exists
----------------------
Both we and the only other JEV + Slay-the-Spire project we could find
(`Ethics03/jevspire`, 2026-09-19) were feeding the model card **names** and
letting it supply the effects from memory. That project's README is explicit
about the consequence:

    "Card text and some observations are missing from upstream snapshots."
    "Missing card effects are not filled in with fabricated numbers."

We can do better than either: Slay the Spire ships its complete localization set
inside `desktop-1.0.jar`, so the agent can read real card, relic, potion, power,
monster and keyword text from the copy already on the player's disk. No
fabrication, no hallucinated card effects, no network.

And it matters for the thing we are actually trying to fix: Score questions about
cards produced flat distributions and were always skipped, because a question
like "how much would adding this card improve the deck?" is unanswerable when
neither side knows what the card does.

Design
------
* `get()` — process-wide lazy singleton. Read-only cache, so the global is safe
  and it means every existing caller (state digests, decision modules, the live
  agent) benefits without any wiring.
* `GameData.load(game_dir=None)` — auto-detects the install, opens the jar,
  extracts the English localization JSONs, and caches them under `.cache/` so the
  ~356 MB archive is read once and re-read only when it changes.
* Lookups return `None` when unknown. Callers must render that honestly
  ("effect not found") rather than guess — the whole point of this module is to
  stop the pipeline from inventing facts about the game.
* No hard dependency: if the game is not installed, `get()` returns an empty
  GameData and every digest degrades to names only.

Note on what localization does and does not contain: it gives NAME, DESCRIPTION
and (for many cards) UPGRADE_DESCRIPTION. **Cost and card type are not in these
files** — they live in the game's code — so they must keep coming from the game
state snapshot, which is exactly what CommunicationMod provides.
"""

from __future__ import annotations

import json
import os
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CACHE_DIR = ROOT / ".cache" / "gamedata"
JAR_NAME = "desktop-1.0.jar"
LANG = "eng"

# Files we pull out of the jar, and the lookup they back.
WANTED = {
    "cards": "cards",
    "relics": "relics",
    "potions": "potions",
    "powers": "powers",
    "monsters": "monsters",
    "keywords": "keywords",
}

# Where a Slay the Spire install tends to live. STS_GAME_DIR wins over all of
# these. (The first entry is the machine this was developed on.)
CANDIDATES = [
    r"E:\LeStoreDownload\steam\steamapps\common\SlayTheSpire",
    r"D:\Steam\steamapps\common\SlayTheSpire",
    r"D:\SteamLibrary\steamapps\common\SlayTheSpire",
    r"C:\Program Files (x86)\Steam\steamapps\common\SlayTheSpire",
    r"E:\Steam\steamapps\common\SlayTheSpire",
    r"E:\SteamLibrary\steamapps\common\SlayTheSpire",
]


def find_game_dir() -> Path | None:
    """$STS_GAME_DIR first, then the usual Steam locations."""
    env = os.environ.get("STS_GAME_DIR")
    if env and (Path(env) / JAR_NAME).exists():
        return Path(env)
    for cand in CANDIDATES:
        p = Path(cand)
        if (p / JAR_NAME).exists():
            return p
    return None


# The game's own markup. Stripping it is formatting-only, never content.
_COLOR_TAGS = ("#b", "#y", "#g", "#r", "#p", "#B", "#Y", "#G", "#R", "#P")
_PLACEHOLDERS = {"!D!": "<damage>", "!M!": "<magic>", "!B!": "<block>"}

# Basic cards share a display name across characters, so the localization keys
# them by internal id: Strike_R (Ironclad), Strike_G (Silent), Strike_B (Defect),
# Strike_P (Watcher). Knowing the character disambiguates; otherwise we take the
# first match, which for basic cards is harmless (the text is identical).
CHARACTER_LETTER = {"IRONCLAD": "R", "THE_SILENT": "G", "SILENT": "G",
                    "DEFECT": "B", "WATCHER": "P"}


def normalize_text(text: str, values: dict | None = None) -> str:
    """Game markup -> readable English, never inventing anything.

    * `NL` (the game's newline) becomes a space.
    * `#b`/`#y`/... colour tags are stripped.
    * `!D!`/`!M!`/`!B!` are the damage/magic/block slots. When the caller passes
      the real numbers (`values={"D": 6}`) we substitute them; otherwise we leave
      a visibly-not-a-number marker `<damage>` rather than guess a value.
    """
    out = text.replace(" NL ", " ").replace("NL ", " ").replace("NL", " ")
    for tag in _COLOR_TAGS:
        out = out.replace(tag, "")
    for token, label in _PLACEHOLDERS.items():
        if values and token[1:-1] in values:
            out = out.replace(token, str(values[token[1:-1]]))
        else:
            out = out.replace(token, label)
    return " ".join(out.split())


@dataclass
class GameData:
    """Read-only lookups over the game's own localization tables."""

    source: str = ""                      # where it came from (jar path), for logs
    loaded: bool = False
    tables: dict[str, dict] = field(default_factory=dict)
    _name_index: dict[str, dict] = field(default_factory=dict, repr=False)

    # -- loading ----------------------------------------------------------- #
    @classmethod
    def load(cls, game_dir: str | Path | None = None, use_cache: bool = True) -> GameData:
        gd = cls()
        jar = None
        if game_dir is not None:
            cand = Path(game_dir) / JAR_NAME
            if cand.exists():
                jar = cand
        if jar is None:
            found = find_game_dir()
            if found is None:
                return gd  # empty, not an error: everything degrades gracefully
            jar = found / JAR_NAME

        # Cache key: jar size + mtime. Cheap to compute, invalidates on patch.
        stat = jar.stat()
        key = f"{stat.st_size}-{int(stat.st_mtime)}"
        meta = CACHE_DIR / "meta.json"
        if use_cache and meta.exists():
            try:
                if json.loads(meta.read_text(encoding="utf-8")).get("key") == key:
                    for name in WANTED:
                        f = CACHE_DIR / f"{name}.json"
                        if f.exists():
                            gd.tables[name] = json.loads(f.read_text(encoding="utf-8"))
                    gd.source = f"{jar} (cached)"
                    gd.loaded = True
                    return gd
            except (OSError, ValueError):
                pass  # corrupt cache: fall through and rebuild

        try:
            with zipfile.ZipFile(jar) as z:
                for name in WANTED:
                    entry = f"localization/{LANG}/{name}.json"
                    try:
                        gd.tables[name] = json.loads(z.read(entry).decode("utf-8"))
                    except KeyError:
                        gd.tables[name] = {}
        except (OSError, zipfile.BadZipFile):
            return gd

        gd.source = str(jar)
        gd.loaded = True
        if use_cache:
            try:
                CACHE_DIR.mkdir(parents=True, exist_ok=True)
                for name, table in gd.tables.items():
                    (CACHE_DIR / f"{name}.json").write_text(
                        json.dumps(table, ensure_ascii=False), encoding="utf-8")
                meta.write_text(json.dumps({"key": key, "jar": str(jar)}), encoding="utf-8")
            except OSError:
                pass  # caching is an optimisation, never a requirement
        return gd

    # -- lookups ----------------------------------------------------------- #
    def _index(self, table: str) -> dict[str, str]:
        """Display NAME -> key, for tables keyed by internal id (Strike_R, AwakenedOne).

        Built lazily and memoised; the tables are immutable once loaded.
        """
        if table not in self._name_index:
            idx: dict[str, str] = {}
            for key, entry in self.tables.get(table, {}).items():
                if isinstance(entry, dict) and entry.get("NAME"):
                    idx.setdefault(str(entry["NAME"]), key)
            self._name_index[table] = idx
        return self._name_index[table]

    def _entry(self, table: str, name: str, character: str | None = None) -> dict | None:
        """Find an entry by localization key first, then by display NAME."""
        table_data = self.tables.get(table, {})
        if character:
            letter = CHARACTER_LETTER.get(character.upper())
            if letter and f"{name}_{letter}" in table_data:
                return table_data[f"{name}_{letter}"]
        entry = table_data.get(name)
        if isinstance(entry, dict):
            return entry
        key = self._index(table).get(name)
        if key is not None:
            candidate = table_data.get(key)
            if isinstance(candidate, dict):
                return candidate
        return None

    @staticmethod
    def _render(entry: dict, *, upgraded: bool, values: dict | None = None) -> str | None:
        """Turn one localization entry into readable text, or None if it has none.

        Cards carry `DESCRIPTION` (plus `UPGRADE_DESCRIPTION`); relics, potions,
        powers and monsters carry `DESCRIPTIONS`, an ARRAY the game assembles
        around substituted values. We join its parts **in file order and never
        reorder them** — the assembly rule differs per entry, and guessing an
        order would be exactly the kind of invented fact this module exists to
        avoid. All the text is present either way; only the reading order of a
        leading "Gain [E]" clause may look odd, which is preferable to a
        confidently wrong sentence.
        """
        raw: list[str] = []
        if upgraded and entry.get("UPGRADE_DESCRIPTION"):
            raw.append(str(entry["UPGRADE_DESCRIPTION"]))
        elif entry.get("DESCRIPTION"):
            raw.append(str(entry["DESCRIPTION"]))
        elif isinstance(entry.get("DESCRIPTIONS"), list):
            raw.extend(str(part) for part in entry["DESCRIPTIONS"])
        if not raw:
            return None
        return normalize_text(" ".join(raw), values=values)

    def effect(self, table: str, name: str, *, upgraded: bool = False,
               character: str | None = None, values: dict | None = None) -> str | None:
        entry = self._entry(table, name, character=character)
        if entry is None:
            return None
        return self._render(entry, upgraded=upgraded, values=values)

    def card_effect(self, name: str, *, upgraded: bool = False,
                    character: str | None = None, values: dict | None = None) -> str | None:
        return self.effect("cards", name, upgraded=upgraded, character=character, values=values)

    def relic_effect(self, name: str) -> str | None:
        return self.effect("relics", name)

    def potion_effect(self, name: str) -> str | None:
        return self.effect("potions", name)

    def power_effect(self, name: str) -> str | None:
        return self.effect("powers", name)

    def monster_effect(self, name: str) -> str | None:
        return self.effect("monsters", name)

    def keyword(self, name: str) -> str | None:
        return self.effect("keywords", name)

    def known(self, table: str, name: str, character: str | None = None) -> bool:
        return self._entry(table, name, character=character) is not None

    def card_line(self, name: str, *, upgraded: bool = False,
                  character: str | None = None, values: dict | None = None) -> str:
        """One honest line for a card: real effect text, or an explicit admission.

        The explicit admission is the point. A model asked to rate a card it has
        no text for will answer from memory and be confidently wrong; a model told
        "effect not found in the game's card data" at least knows what it does not
        know — and the confidence floor can then do its job.
        """
        text = self.card_effect(name, upgraded=upgraded, character=character, values=values)
        if text is None:
            return f"{name}: effect not found in the game's card data"
        return f"{name}: {text}"

    def relic_line(self, name: str) -> str:
        text = self.relic_effect(name)
        if text is None:
            return f"{name}: effect not found in the game's relic data"
        return f"{name}: {text}"

    def stats(self) -> dict:
        return {
            "loaded": self.loaded,
            "source": self.source,
            "sizes": {k: len(v) for k, v in self.tables.items()},
        }


_INSTANCE: GameData | None = None


def get(reload: bool = False) -> GameData:
    """Process-wide lazy singleton. Safe because the data is read-only."""
    global _INSTANCE
    if _INSTANCE is None or reload:
        _INSTANCE = GameData.load()
    return _INSTANCE


if __name__ == "__main__":
    gd = get(reload=True)
    print(json.dumps(gd.stats(), ensure_ascii=False, indent=2))
    print("\n-- cards (real text from the player's own install) --")
    for probe in ("Strike", "Defend", "Bash", "Inflame", "Shrug It Off",
                  "Pommel Strike", "Twin Strike", "Anger"):
        print("  " + gd.card_line(probe, character="IRONCLAD"))
    print("\n-- with real numbers supplied (no guessing) --")
    print("  " + gd.card_line("Bash", character="IRONCLAD", values={"D": 8, "M": 2}))
    print("  " + gd.card_line("Defend", character="IRONCLAD", values={"B": 5}))
    print("\n-- relics --")
    for relic in ("Burning Blood", "Runic Dome", "Coffee Dripper", "Philosopher's Stone"):
        print("  " + gd.relic_line(relic))
    print("\n-- monsters --")
    for m in ("Gremlin Nob", "Cultist", "AwakenedOne"):
        print(f"  {m}: {gd.monster_effect(m)}")
