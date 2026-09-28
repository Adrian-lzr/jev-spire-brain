"""Small, deterministic Ironclad combat simulator for replay and smoke tests.

This is deliberately a narrow model, rather than a second Slay the Spire
engine.  It handles only effects that are explicit in a CommunicationMod
snapshot (damage, block, energy, hits and vulnerable).  Unknown effects are
retained in the snapshot and make the corresponding action uncertain.

The simulator never reads credentials or game configuration and never writes
to an installed game.  ``copy_game_data_manifest`` can be used by an external
test runner to describe a read-only copy of ``desktop-1.0.jar`` without copying
saves, preferences or mods.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import zipfile
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping


SUPPORTED_CHARACTER = "IRONCLAD"
KNOWN_CARD_IDS = frozenset({"Strike_R", "Defend_R", "Bash"})


def _combat_state(state: Mapping[str, Any]) -> dict[str, Any]:
    """Return a copied combat mapping from either a raw or wrapped snapshot."""
    root = state.get("game_state") if isinstance(state.get("game_state"), Mapping) else state
    combat = root.get("combat") if isinstance(root, Mapping) else None
    if not isinstance(combat, Mapping):
        raise ValueError("snapshot has no combat object")
    return deepcopy(dict(combat))


def sanitize_snapshot(state: Mapping[str, Any], *, max_chars: int = 120_000) -> dict[str, Any]:
    """Copy only replay-safe state and remove transport/configuration metadata.

    CommunicationMod payloads vary by version, so this keeps the game state
    shape while dropping obvious transport and credential fields.  The bounded
    JSON round-trip prevents an accidentally captured object from growing a
    replay without changing the caller's input.
    """
    root = state.get("game_state") if isinstance(state.get("game_state"), Mapping) else state
    if not isinstance(root, Mapping):
        raise ValueError("snapshot must be a mapping")
    dropped = {"authorization", "api_key", "apikey", "token", "password", "headers",
               "request", "response", "timestamp", "uuid", "transport"}

    def clean(value: Any, key: str = "") -> Any:
        if key.lower() in dropped or any(part in key.lower() for part in ("api_key", "authorization")):
            return None
        if isinstance(value, Mapping):
            return {str(k): clean(v, str(k)) for k, v in value.items()
                    if str(k).lower() not in dropped}
        if isinstance(value, list):
            return [clean(v, key) for v in value]
        if isinstance(value, (str, int, float, bool)) or value is None:
            return value
        return str(value)

    result = clean(root)
    encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    if len(encoded) > max_chars:
        raise ValueError(f"snapshot exceeds replay limit ({max_chars} characters)")
    return result


def copy_game_data_manifest(game_dir: str | Path, destination: str | Path) -> dict[str, Any]:
    """Copy only the immutable game data JAR into an isolated directory.

    Saves, preferences, mods, logs and launcher files are intentionally never
    copied.  This is useful for extracting localization/card data in a test
    environment; it is not a full game clone and does not claim to simulate the
    game's Java rules.
    """
    source_root = Path(game_dir).expanduser().resolve()
    target_root = Path(destination).expanduser().resolve()
    jar = source_root / "desktop-1.0.jar"
    if not jar.is_file():
        raise FileNotFoundError(f"game data not found: {jar}")
    if target_root == source_root or source_root in target_root.parents:
        raise ValueError("destination must be outside the installed game directory")
    target_root.mkdir(parents=True, exist_ok=True)
    target = target_root / jar.name
    shutil.copy2(jar, target)
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    return {"source": str(jar), "copy": str(target), "sha256": digest,
            "included": [jar.name], "excluded": ["saves", "preferences", "mods", "logs"]}


def extract_minimal_game_snapshot(
    game_dir: str | Path,
    destination: str | Path,
    *,
    card_ids: Iterable[str] = ("Strike_R", "Defend_R", "Bash"),
    relic_ids: Iterable[str] = ("Burning Blood",),
) -> dict[str, Any]:
    """Extract a small, reproducible Ironclad data snapshot from the game JAR.

    Only localization JSON inside ``desktop-1.0.jar`` is read.  The output is
    suitable for an offline fixture and never includes the installed JAR,
    saves, preferences, mods, or request credentials.  Missing entries are
    reported in ``missing`` instead of being guessed.
    """
    source_root = Path(game_dir).expanduser().resolve()
    output = Path(destination).expanduser().resolve()
    jar = source_root / "desktop-1.0.jar"
    if not jar.is_file():
        raise FileNotFoundError(f"game data not found: {jar}")
    if output == source_root or source_root in output.parents:
        raise ValueError("destination must be outside the installed game directory")
    cards_wanted = tuple(dict.fromkeys(str(x) for x in card_ids))
    relics_wanted = tuple(dict.fromkeys(str(x) for x in relic_ids))
    with zipfile.ZipFile(jar) as archive:
        tables: dict[str, dict[str, Any]] = {}
        for table, wanted in (("cards", cards_wanted), ("relics", relics_wanted)):
            entry_name = f"localization/eng/{table}.json"
            try:
                raw = json.loads(archive.read(entry_name).decode("utf-8"))
            except (KeyError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError(f"invalid or missing {entry_name}") from exc
            tables[table] = {key: raw[key] for key in wanted if key in raw}
        missing = {
            table: [key for key in wanted if key not in tables[table]]
            for table, wanted in (("cards", cards_wanted), ("relics", relics_wanted))
        }
    digest = hashlib.sha256(jar.read_bytes()).hexdigest()
    payload = {
        "schema_version": 1,
        "fixture_type": "real_replay",
        "source_kind": "live_copy",
        "source": {"jar_name": jar.name, "sha256": digest},
        "character": SUPPORTED_CHARACTER,
        "tables": tables,
        "missing": missing,
        "excluded": ["saves", "preferences", "mods", "logs", "secrets"],
    }
    output.mkdir(parents=True, exist_ok=True)
    target = output / "ironclad_data_snapshot.json"
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {**payload, "path": str(target)}


@dataclass(frozen=True)
class SimulationResult:
    state: dict[str, Any]
    events: tuple[dict[str, Any], ...] = ()
    legal: bool = True
    uncertainty: str = "none"
    rejection: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"state": deepcopy(self.state), "events": [deepcopy(e) for e in self.events],
                "legal": self.legal, "uncertainty": self.uncertainty,
                "rejection": self.rejection}


@dataclass
class IroncladCombatSimulator:
    """Apply a bounded sequence of explicit one-turn actions to a snapshot."""

    max_actions: int = 8
    _events: list[dict[str, Any]] = field(default_factory=list, init=False)

    def run(self, snapshot: Mapping[str, Any], actions: Iterable[Mapping[str, Any]]) -> SimulationResult:
        state = _combat_state(snapshot)
        player = state.setdefault("player", {})
        hand = state.setdefault("hand", [])
        monsters = state.setdefault("monsters", [])
        root = snapshot.get("game_state") if isinstance(snapshot.get("game_state"), Mapping) else snapshot
        if str(root.get("character", SUPPORTED_CHARACTER)).upper() not in {SUPPORTED_CHARACTER, ""}:
            return SimulationResult(state, legal=False, uncertainty="unsupported_character",
                                    rejection="only Ironclad is supported")
        for index, action in enumerate(actions):
            if index >= self.max_actions:
                return SimulationResult(state, tuple(self._events), legal=False,
                                        uncertainty="action_limit", rejection="action limit exceeded")
            result = self._apply(state, action)
            if not result[0]:
                return SimulationResult(state, tuple(self._events), legal=False,
                                        uncertainty=result[2], rejection=result[1])
            if result[2] != "none":
                return SimulationResult(state, tuple(self._events), legal=True,
                                        uncertainty=result[2])
            if action.get("command") == "end":
                break
        return SimulationResult(state, tuple(self._events), legal=True,
                                uncertainty="none")

    def _apply(self, state: dict[str, Any], action: Mapping[str, Any]) -> tuple[bool, str | None, str]:
        player = state["player"]
        hand = state["hand"]
        monsters = state["monsters"]
        command = str(action.get("command", ""))
        if command == "end":
            self._events.append({"type": "turn_end"})
            return True, None, "none"
        if command != "play":
            return False, "unsupported command", "unsupported_effect"
        try:
            card_index = int(action["card"])
        except (KeyError, TypeError, ValueError):
            return False, "missing card index", "invalid_action"
        if not 0 <= card_index < len(hand):
            return False, "card index is out of range", "invalid_action"
        card = hand[card_index]
        card_id = str(card.get("id", card.get("name", "")))
        if not bool(card.get("is_playable", True)):
            return False, "card is not playable", "invalid_action"
        cost = int(card.get("cost", 0) or 0)
        energy = int(player.get("energy", 0) or 0)
        if cost > energy:
            return False, "insufficient energy", "invalid_action"
        if card_id not in KNOWN_CARD_IDS and not any(k in card for k in ("damage", "block", "hits")):
            self._events.append({"type": "unknown_card", "card_id": card_id})
            return True, None, "unknown_effect"
        target = None
        if card.get("damage") or card.get("hits"):
            try:
                target_index = int(action.get("target", 0))
                target = monsters[target_index]
            except (TypeError, ValueError, IndexError):
                return False, "target is invalid", "invalid_action"
            if int(target.get("current_hp", 0) or 0) <= 0:
                return False, "target is defeated", "invalid_action"
        player["energy"] = energy - cost
        damage = int(card.get("damage", 0) or 0)
        hits = max(1, int(card.get("hits", 1) or 1))
        vulnerable = int(target.get("vulnerable", 0) or 0) if target else 0
        if damage and target:
            total = damage * hits * (1.5 if vulnerable > 0 else 1.0)
            total = int(total)
            target["current_hp"] = max(0, int(target.get("current_hp", 0) or 0) - total)
        block = int(card.get("block", 0) or 0)
        player["block"] = int(player.get("block", 0) or 0) + block
        if card.get("applies_vulnerable") and target:
            target["vulnerable"] = int(target.get("vulnerable", 0) or 0) + int(card["applies_vulnerable"])
        hand.pop(card_index)
        self._events.append({"type": "card_played", "card_id": card_id,
                             "card_index": card_index, "target_id": target.get("id") if target else None,
                             "damage": damage * hits if target else 0, "block": block,
                             "energy_after": player["energy"]})
        return True, None, "none"


def load_fixture(path: str | Path) -> dict[str, Any]:
    """Load a JSON simulation fixture and require an explicit synthetic marker."""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if payload.get("fixture_type") not in {"synthetic", "real_replay"}:
        raise ValueError("simulation fixture must declare fixture_type")
    if payload.get("kind") != "state_sequence":
        raise ValueError("simulation fixture kind must be state_sequence")
    return payload


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="run a deterministic Ironclad combat fixture")
    parser.add_argument("fixture", type=Path)
    parser.add_argument("--actions", type=Path, required=True,
                        help="JSON array of play/end actions")
    args = parser.parse_args()
    fixture = load_fixture(args.fixture)
    states = fixture.get("states") or fixture.get("messages")
    if not states:
        raise SystemExit("fixture has no states")
    first = states[0].get("game_state", states[0])
    actions = json.loads(args.actions.read_text(encoding="utf-8"))
    print(json.dumps(IroncladCombatSimulator().run(first, actions).as_dict(),
                     ensure_ascii=False, indent=2))
