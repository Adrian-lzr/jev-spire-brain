"""Editable Ironclad strategy profiles and conservative run-time selection.

The guide sources agree on two ideas that a single fixed policy loses:
"play to the run" and commit only after the deck supplies a real signal.  This
module turns those ideas into a small, auditable profile selector.  It never
forces a build from one lucky card; until a profile reaches its configured
signal threshold the result remains ``adaptive``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from spirebrain.cards import meta
from spirebrain.cards.knowledge import IRONCLAD_ARCHETYPES, archetype_signal

ROOT = Path(__file__).resolve().parents[1]
PROFILE_PATH = ROOT / "config" / "archetypes.json"


@dataclass(frozen=True)
class StrategyProfile:
    id: str
    label: str
    signal_cards: tuple[str, ...] = ()
    payoff_cards: tuple[str, ...] = ()
    min_signal: int = 0
    focus: tuple[str, ...] = ()
    avoid: tuple[str, ...] = ()
    resource_policy: str = ""
    source_note: str = ""
    aliases: tuple[str, ...] = ()

    def model_dict(self) -> dict:
        return {
            "id": self.id,
            "label": self.label,
            "signal": list(self.signal_cards),
            "payoff": list(self.payoff_cards),
            "focus": list(self.focus),
            "avoid": list(self.avoid),
            "resource_policy": self.resource_policy,
            "source": self.source_note,
        }


@dataclass(frozen=True)
class StrategySelection:
    profile: StrategyProfile
    scores: dict[str, float] = field(default_factory=dict)
    confidence: float = 0.0
    reason: str = ""
    alternatives: tuple[str, ...] = ()

    def model_dict(self) -> dict:
        return {
            "selected": self.profile.model_dict(),
            "scores": dict(self.scores),
            "confidence": round(float(self.confidence), 3),
            "reason": self.reason,
            "alternatives": list(self.alternatives),
        }


def _fallback_profiles() -> list[StrategyProfile]:
    return [StrategyProfile("adaptive", "自适应端口", focus=("damage", "block", "draw", "energy", "aoe", "scaling"))]


def load_profiles(path: str | Path | None = None) -> tuple[StrategyProfile, ...]:
    """Load editable profiles, falling back safely if a local edit is invalid."""
    source = Path(path) if path else PROFILE_PATH
    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
        rows = raw.get("profiles", []) if isinstance(raw, dict) else []
        out: list[StrategyProfile] = []
        for row in rows:
            if not isinstance(row, dict) or not str(row.get("id", "")).strip():
                continue
            def strings(key: str) -> tuple[str, ...]:
                value = row.get(key, [])
                if isinstance(value, str):
                    return (value,)
                return tuple(str(item) for item in value if item is not None)
            out.append(StrategyProfile(
                id=str(row["id"]), label=str(row.get("label", row["id"])),
                signal_cards=strings("signal_cards"), payoff_cards=strings("payoff_cards"),
                min_signal=max(0, int(row.get("min_signal", 0) or 0)),
                focus=strings("focus"), avoid=strings("avoid"),
                resource_policy=str(row.get("resource_policy", "")),
                source_note=str(row.get("source_note", "")), aliases=strings("aliases"),
            ))
        if out:
            return tuple(out)
    except (OSError, ValueError, TypeError, KeyError):
        pass
    return tuple(_fallback_profiles())


def _card_names(deck: list[Any]) -> set[str]:
    names: set[str] = set()
    for card in deck or []:
        if isinstance(card, str):
            raw = card
        elif isinstance(card, dict):
            raw = ""
            for key in ("id", "card_id", "name"):
                if card.get(key):
                    raw = str(card[key])
                    break
        else:
            raw = ""
        if not raw:
            continue
        # CommunicationMod uses class ids (HeavyBlade, Strike_R), while the
        # editable guide uses display/game ids (Heavy Blade, Strike_Red). Use
        # the installed card metadata as the source of truth for both forms.
        info = meta.card(raw)
        names.add(str(info.get("game_id") or raw))
    return names


def select_strategy(deck: list[Any] | None, *, act: int = 1,
                    hp: int | None = None, max_hp: int | None = None,
                    gold: int | None = None, relics: list[Any] | None = None,
                    profiles: tuple[StrategyProfile, ...] | None = None) -> StrategySelection:
    """Select a strategy only after the deck provides enough evidence.

    Core cards count twice, payoff cards once.  A tie stays adaptive unless one
    profile has a clear lead, which prevents the coach from oscillating between
    incompatible builds after every reward screen.
    """
    profiles = profiles or load_profiles()
    by_id = {profile.id: profile for profile in profiles}
    adaptive = by_id.get("adaptive") or _fallback_profiles()[0]
    names = _card_names(deck or [])
    scores: dict[str, float] = {}
    signal_counts: dict[str, int] = {}
    for profile in profiles:
        if profile.id == "adaptive":
            continue
        core = len(names & set(profile.signal_cards))
        payoff = len(names & set(profile.payoff_cards))
        signal_counts[profile.id] = core + payoff
        score = core * 2.0 + payoff * 0.8
        # A low-health run should not commit to a slow profile merely because a
        # single payoff card appeared; the plan can still use its cards, but the
        # top-level strategy remains survival/adaptive until HP recovers.
        if max_hp and hp is not None and hp / max_hp < 0.35:
            score *= 0.75
        scores[profile.id] = round(score, 3)
    ranked = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    if max_hp and hp is not None and hp / max_hp < 0.35:
        return StrategySelection(
            adaptive, scores, 0.0,
            "生命低于安全线，暂不强行承诺慢速流派，先解决当前战斗和防御端。",
            tuple(item[0] for item in ranked[:3]),
        )
    if not ranked or signal_counts.get(ranked[0][0], 0) < max(
            1, int((by_id.get(ranked[0][0]) or adaptive).min_signal)):
        return StrategySelection(adaptive, scores, 0.0,
                                 "尚未形成明确流派，优先补当前卡组最弱端口。",
                                 tuple(item[0] for item in ranked[:2]))
    winner_id, winner_score = ranked[0]
    runner_score = ranked[1][1] if len(ranked) > 1 else 0.0
    if winner_score - runner_score < 1.25:
        return StrategySelection(adaptive, scores, 0.0,
                                 "多个方向信号接近，保持自适应，不强行凑流派。",
                                 tuple(item[0] for item in ranked[:3]))
    winner = by_id.get(winner_id, adaptive)
    confidence = min(1.0, 0.45 + (winner_score - runner_score) / 8.0)
    reason = f"卡组已出现「{winner.label}」信号：核心 {len(names & set(winner.signal_cards))} 张，配合 {len(names & set(winner.payoff_cards))} 张。"
    return StrategySelection(winner, scores, confidence, reason,
                             tuple(item[0] for item in ranked[1:3]))


def strategy_context(game: dict, *, profiles: tuple[StrategyProfile, ...] | None = None) -> dict:
    """Build a bounded, JSON-safe context for GPT/JEV and browser logs."""
    game = game if isinstance(game, dict) else {}
    def number(value: Any) -> int | None:
        if isinstance(value, dict):
            value = value.get("current", value.get("value"))
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None
    deck = game.get("deck") or []
    # Some CommunicationMod versions put combat state under `combat.player`.
    combat_player = ((game.get("combat") or game.get("combat_state") or {}).get("player")
                     if isinstance(game.get("combat") or game.get("combat_state"), dict)
                     else {}) or {}
    current_hp = game.get("current_hp", game.get("hp"))
    max_hp = game.get("max_hp")
    if current_hp is None:
        current_hp = combat_player.get("current_hp", combat_player.get("hp"))
    if max_hp is None:
        max_hp = combat_player.get("max_hp")
    selection = select_strategy(
        deck, act=number(game.get("act", 1)) or 1,
        hp=number(current_hp),
        max_hp=number(max_hp),
        gold=game.get("gold"), relics=game.get("relics"), profiles=profiles,
    )
    return selection.model_dict()


def profile_for_run(deck: list[Any], *, act: int = 1, hp: int | None = None,
                    max_hp: int | None = None) -> StrategySelection:
    """Small helper for card-grading callers that already have a deck list."""
    return select_strategy(deck, act=act, hp=hp, max_hp=max_hp)
