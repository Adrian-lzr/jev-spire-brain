"""Small deterministic RAG layer for source-cited Slay the Spire strategy.

The runtime deliberately uses a local lexical index instead of a network call
or a heavyweight embedding dependency.  Knowledge entries are paraphrased,
bounded and source-cited; card numbers and legality remain authoritative from
local game data and the action checker.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Iterable


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PATH = ROOT / "data" / "strategy_knowledge.jsonl"
SUPPORTED_GAME_ID = "slay-the-spire-1"
SUPPORTED_GAME_VERSION = "slay the spire 1"
LICENSE_STATUSES = {"verified", "unknown", "permission_required"}
TOKEN_RE = re.compile(r"[a-z][a-z0-9_+#-]*|[0-9]+|[\u4e00-\u9fff]", re.IGNORECASE)
_STOP_WORDS = frozenset({
    "a", "an", "and", "as", "at", "by", "for", "from", "in", "into",
    "is", "of", "on", "or", "per", "the", "to", "with", "after", "before",
})
_GENERIC_RETRIEVAL_TERMS = frozenset({
    "ironclad", "act", "combat", "map", "shop", "event", "card", "cards",
    "damage", "block", "hp", "health", "attack", "attacks", "route", "build",
    "resource", "resources", "strategy", "strategic", "game", "player", "enemy",
    "enemies", "mechanic", "mechanics", "fact", "facts", "heuristic",
    "act_1", "act_2", "act_3",
})
_MECHANIC_SINGLE_ANCHORS = frozenset({
    "strength", "vulnerable", "weak", "burning_blood",
})


def _tokens(value: str) -> tuple[str, ...]:
    return tuple(TOKEN_RE.findall(str(value or "").lower()))


@dataclass(frozen=True)
class KnowledgeChunk:
    """One bounded, human-reviewed strategy claim with provenance."""

    chunk_id: str
    title: str
    source_url: str
    source_type: str
    game_version: str
    character: str
    tags: tuple[str, ...]
    text: str
    trust: str = "single_source_unverified"
    captured_at: str = ""
    game_id: str = ""
    claim_type: str = "strategy_heuristic"
    license: str = ""
    license_status: str = "unknown"
    prerequisites: tuple[str, ...] = ()
    exceptions: tuple[str, ...] = ()

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()

    @classmethod
    def from_dict(cls, value: dict) -> "KnowledgeChunk":
        if not isinstance(value, dict):
            raise ValueError("knowledge chunk must be an object")
        required = ("chunk_id", "title", "source_url", "source_type",
                    "game_version", "game_id", "character", "trust",
                    "claim_type", "license", "license_status", "text",
                    "content_hash")
        missing = [key for key in required if not str(value.get(key) or "").strip()]
        if missing:
            raise ValueError(f"knowledge chunk missing: {', '.join(missing)}")
        tags = value.get("tags", [])
        if isinstance(tags, str):
            tags = [tags]
        if not isinstance(tags, list):
            raise ValueError("knowledge chunk tags must be an array")
        prerequisites = value.get("prerequisites", [])
        exceptions = value.get("exceptions", [])
        if not isinstance(prerequisites, list) or not isinstance(exceptions, list):
            raise ValueError("knowledge chunk conditions must be arrays")
        license_status = str(value["license_status"]).strip().lower()
        if license_status not in LICENSE_STATUSES:
            raise ValueError("knowledge chunk license_status is invalid")
        text = str(value["text"])[:1800]
        content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        recorded_hash = str(value.get("content_hash") or "").strip().lower()
        if recorded_hash and recorded_hash != content_hash:
            raise ValueError("knowledge chunk content hash mismatch")
        return cls(
            chunk_id=str(value["chunk_id"])[:120],
            title=str(value["title"])[:240],
            source_url=str(value["source_url"])[:500],
            source_type=str(value["source_type"])[:80],
            game_version=str(value["game_version"])[:80],
            character=str(value["character"]).upper()[:40],
            tags=tuple(str(tag).lower()[:80] for tag in tags if str(tag).strip()),
            text=text,
            trust=str(value["trust"])[:80],
            captured_at=str(value.get("captured_at") or "")[:40],
            game_id=str(value["game_id"]).strip().lower()[:80],
            claim_type=str(value["claim_type"]).strip().lower()[:80],
            license=str(value["license"]).strip()[:120],
            license_status=license_status,
            prerequisites=tuple(str(item)[:300] for item in prerequisites[:12]
                                 if str(item).strip()),
            exceptions=tuple(str(item)[:300] for item in exceptions[:12]
                             if str(item).strip()),
        )

    def to_dict(self) -> dict:
        return {
            "chunk_id": self.chunk_id,
            "title": self.title,
            "source_url": self.source_url,
            "source_type": self.source_type,
            "game_version": self.game_version,
            "character": self.character,
            "tags": list(self.tags),
            "text": self.text,
            "trust": self.trust,
            "captured_at": self.captured_at,
            "game_id": self.game_id,
            "claim_type": self.claim_type,
            "license": self.license,
            "license_status": self.license_status,
            "prerequisites": list(self.prerequisites),
            "exceptions": list(self.exceptions),
            "content_hash": self.content_hash,
        }


@dataclass(frozen=True)
class Retrieval:
    chunk: KnowledgeChunk
    score: float
    matched_terms: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        value = self.chunk.to_dict()
        value.update({"score": round(self.score, 4),
                      "matched_terms": list(self.matched_terms)})
        return value


class StrategyKnowledgeBase:
    """Deterministic TF-IDF-like retrieval over reviewed strategy chunks."""

    def __init__(self, chunks: Iterable[KnowledgeChunk]) -> None:
        unique: dict[str, KnowledgeChunk] = {}
        for chunk in chunks:
            if isinstance(chunk, KnowledgeChunk):
                unique[chunk.chunk_id] = chunk
        self.chunks = tuple(unique.values())
        document_tokens = [_tokens(" ".join((chunk.title, chunk.text, *chunk.tags)))
                           for chunk in self.chunks]
        self._tokens = tuple(document_tokens)
        document_frequency = Counter(token for tokens in document_tokens for token in set(tokens))
        self._idf = {
            token: 1.0 + (len(self.chunks) / (1 + count)) ** 0.5
            for token, count in document_frequency.items()
        }

    @classmethod
    def from_jsonl(cls, path: str | Path = DEFAULT_PATH) -> "StrategyKnowledgeBase":
        chunks: list[KnowledgeChunk] = []
        target = Path(path)
        for line_number, line in enumerate(
                target.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            try:
                chunks.append(KnowledgeChunk.from_dict(json.loads(line)))
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise ValueError(f"{target}:{line_number}: invalid knowledge chunk") from exc
        if not chunks:
            raise ValueError(f"{target}: knowledge base is empty")
        return cls(chunks)

    def retrieve(self, query: str, *, character: str = "IRONCLAD",
                 tags: Iterable[str] = (), top_k: int = 4,
                 game_id: str = SUPPORTED_GAME_ID,
                 game_version: str = "Slay the Spire 1",
                 claim_types: Iterable[str] | None = None,
                 min_score: float = 2.0) -> list[Retrieval]:
        character = str(character or "").upper()
        if character and character != "IRONCLAD":
            return []
        if (str(game_id or "").strip().lower() != SUPPORTED_GAME_ID
                or str(game_version or "").strip().lower() != SUPPORTED_GAME_VERSION):
            return []
        raw_tags = tuple(str(tag).strip().lower() for tag in tags if str(tag).strip())
        query_text = re.sub(r"\bact\s*[123]\b", " ", str(query or ""), flags=re.IGNORECASE)
        query_terms = (set(_tokens(query_text)) | set(_tokens(" ".join(raw_tags)))) - _STOP_WORDS
        query_terms -= _GENERIC_RETRIEVAL_TERMS
        if not query_terms:
            return []
        wanted_tags = set(raw_tags) - _STOP_WORDS - _GENERIC_RETRIEVAL_TERMS
        wanted_types = ({str(value).strip().lower() for value in claim_types
                         if str(value).strip()} if claim_types is not None else None)
        ranked: list[Retrieval] = []
        for chunk, tokens in zip(self.chunks, self._tokens):
            if chunk.game_id != SUPPORTED_GAME_ID:
                continue
            if chunk.game_version.strip().lower() != SUPPORTED_GAME_VERSION:
                continue
            if chunk.character not in {"", "IRONCLAD", "ANY"}:
                continue
            if wanted_types is not None and chunk.claim_type not in wanted_types:
                continue
            counts = Counter(tokens)
            matched_terms = query_terms & set(counts)
            matched_tags = (query_terms | wanted_tags) & set(chunk.tags)
            anchors = matched_terms | matched_tags
            if not anchors:
                continue
            # A single shared word such as "remove" or "strength" is too weak
            # to justify adding a claim to a strategic prompt. Keep one-term
            # queries usable, but require two independent anchors for broader
            # queries so lexical overlap does not masquerade as relevance.
            exact_mechanic_anchor = (
                chunk.claim_type == "mechanic_fact"
                and bool(matched_terms & _MECHANIC_SINGLE_ANCHORS)
            )
            if len(query_terms) > 1 and len(matched_terms) < 2 and not exact_mechanic_anchor:
                continue
            score = sum(self._idf.get(token, 1.0) * min(3, counts[token])
                        for token in matched_terms)
            score += 0.75 * len(matched_tags)
            if score < max(0.0, float(min_score)):
                continue
            ranked.append(Retrieval(chunk, score, tuple(sorted(anchors))))
        ranked.sort(key=lambda item: (-item.score, item.chunk.chunk_id))
        if len(query_terms) == 1 and wanted_types is None and len(ranked) > 1:
            # A bare ambiguous keyword cannot distinguish a mechanic fact from
            # a strategy claim that happens to use the same word.
            return []
        return ranked[:max(0, min(8, int(top_k)))]

    def context(self, query: str, *, character: str = "IRONCLAD",
                tags: Iterable[str] = (), top_k: int = 3,
                max_chars: int = 3600,
                game_id: str = SUPPORTED_GAME_ID,
                game_version: str = "Slay the Spire 1",
                claim_types: Iterable[str] | None = None,
                min_score: float = 2.0) -> dict:
        if (str(game_id or "").strip().lower() != SUPPORTED_GAME_ID
                or str(game_version or "").strip().lower() != SUPPORTED_GAME_VERSION):
            return {
                "status": "unsupported_game",
                "character": str(character or "").upper(),
                "game_id": str(game_id or ""),
                "game_version": str(game_version or ""),
                "query": str(query)[:500],
                "items": [], "citation_ids": [],
                "usage": "no strategy claims matched the supported game and version",
            }
        results = self.retrieve(query, character=character, tags=tags, top_k=top_k,
                                game_id=game_id, game_version=game_version,
                                claim_types=claim_types, min_score=min_score)
        remaining = max(0, int(max_chars))
        items = []
        for result in results:
            text = result.chunk.text[:remaining]
            if not text:
                break
            items.append({
                "chunk_id": result.chunk.chunk_id,
                "claim": text,
                "title": result.chunk.title,
                "source_url": result.chunk.source_url,
                "source_type": result.chunk.source_type,
                "trust": result.chunk.trust,
                "game_id": result.chunk.game_id,
                "game_version": result.chunk.game_version,
                "claim_type": result.chunk.claim_type,
                "license": result.chunk.license,
                "license_status": result.chunk.license_status,
                "prerequisites": list(result.chunk.prerequisites),
                "exceptions": list(result.chunk.exceptions),
                "content_hash": result.chunk.content_hash,
                "matched_terms": list(result.matched_terms),
            })
            remaining -= len(text)
        return {
            "status": "ready" if items else "no_match",
            "character": str(character or "").upper(),
            "game_id": SUPPORTED_GAME_ID,
            "game_version": "Slay the Spire 1",
            "query": str(query)[:500],
            "items": items,
            "citation_ids": [item["chunk_id"] for item in items],
            "usage": "soft_evidence_only; local game data and legality override this context",
        }


@lru_cache(maxsize=4)
def default_knowledge_base(path: str = str(DEFAULT_PATH)) -> StrategyKnowledgeBase:
    return StrategyKnowledgeBase.from_jsonl(path)


def _state_query(game: dict) -> tuple[str, tuple[str, ...]]:
    game = game if isinstance(game, dict) else {}
    screen = str(game.get("screen_type") or game.get("screen") or "").upper()
    act = str(game.get("act") or "")
    deck = game.get("deck") or []
    card_ids = []
    for card in deck[:30] if isinstance(deck, list) else []:
        if isinstance(card, dict):
            card_ids.append(str(card.get("id") or card.get("name") or ""))
        else:
            card_ids.append(str(card))
    relics = game.get("relics") or []
    relic_ids = []
    for relic in relics[:30] if isinstance(relics, list) else []:
        if isinstance(relic, dict):
            relic_ids.append(str(relic.get("id") or relic.get("name") or ""))
        else:
            relic_ids.append(str(relic))
    combat_terms: list[str] = []
    combat = game.get("combat") or game.get("combat_state") or {}
    if isinstance(combat, dict):
        actors = [combat.get("player") or {}]
        monsters = combat.get("monsters") or []
        if isinstance(monsters, list):
            actors.extend(monsters[:8])
        for actor in actors:
            if not isinstance(actor, dict):
                continue
            for key in ("powers", "statuses", "buffs", "debuffs"):
                values = actor.get(key) or []
                if isinstance(values, dict):
                    combat_terms.extend(str(name) for name, amount in values.items()
                                        if amount not in (None, 0, False, ""))
                elif isinstance(values, list):
                    for value in values[:16]:
                        if isinstance(value, dict):
                            name = value.get("id") or value.get("name") or value.get("key")
                        else:
                            name = value
                        if name:
                            combat_terms.append(str(name))
            for key in ("strength", "weak", "vulnerable"):
                if actor.get(key) not in (None, 0, False, ""):
                    combat_terms.append(key)
    combat_terms = combat_terms[:32]
    profile = game.get("strategy_context") or game.get("strategy") or {}
    selected = profile.get("selected") if isinstance(profile, dict) else {}
    profile_id = selected.get("id") if isinstance(selected, dict) else ""
    query = " ".join(filter(None, [screen, f"act {act}", profile_id,
                                   *card_ids, *relic_ids, *combat_terms]))
    tags = [screen.lower(), str(profile_id or "").lower(),
            *(str(term).lower().replace(" ", "_") for term in combat_terms)]
    return query, tuple(tag for tag in tags if tag)


def retrieve_strategy_context(game: dict, *, top_k: int = 3,
                              knowledge_base: StrategyKnowledgeBase | None = None) -> dict:
    character = str((game or {}).get("character") or (game or {}).get("class") or "").upper()
    game_id = str((game or {}).get("game_id") or SUPPORTED_GAME_ID).strip().lower()
    game_version = str((game or {}).get("game_version")
                       or (game or {}).get("game_version_name")
                       or "Slay the Spire 1").strip()
    if (game_id != SUPPORTED_GAME_ID
            or game_version.lower() != SUPPORTED_GAME_VERSION):
        return {"status": "unsupported_game", "character": character,
                "game_id": game_id, "game_version": game_version,
                "items": [], "citation_ids": [],
                "usage": "strategy knowledge is scoped to Slay the Spire 1"}
    if not character:
        return {"status": "unknown_character", "character": "",
                "game_id": game_id, "game_version": game_version,
                "items": [], "citation_ids": [],
                "usage": "character identity is required before retrieving strategy"}
    if character and character != "IRONCLAD":
        return {"status": "unsupported_character", "character": character,
                "items": [], "citation_ids": [],
                "usage": "ironclad-only strategy knowledge"}
    query, tags = _state_query(game)
    try:
        return (knowledge_base or default_knowledge_base()).context(
            query, character="IRONCLAD", tags=tags, top_k=top_k,
            game_id=game_id, game_version=game_version)
    except (OSError, ValueError):
        return {"status": "unavailable", "character": "IRONCLAD", "items": [],
                "citation_ids": [], "usage": "local knowledge base unavailable"}


def main(argv: list[str] | None = None) -> int:
    try:
        import sys
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass
    parser = argparse.ArgumentParser(description="offline Slay the Spire strategy retrieval")
    parser.add_argument("--query", required=True)
    parser.add_argument("--character", default="IRONCLAD")
    parser.add_argument("--top-k", type=int, default=3)
    parser.add_argument("--input", default=str(DEFAULT_PATH))
    args = parser.parse_args(argv)
    try:
        result = StrategyKnowledgeBase.from_jsonl(args.input).context(
            args.query, character=args.character, top_k=args.top_k)
    except (OSError, ValueError) as exc:
        print(json.dumps({"error": "knowledge_base_invalid", "detail": type(exc).__name__},
                         ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
