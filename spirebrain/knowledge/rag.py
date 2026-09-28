"""Small deterministic RAG layer for source-cited Slay the Spire strategy.

The runtime deliberately uses a local lexical index instead of a network call
or a heavyweight embedding dependency.  Knowledge entries are paraphrased,
bounded and source-cited; card numbers and legality remain authoritative from
local game data and the action checker.
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Iterable


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PATH = ROOT / "data" / "strategy_knowledge.jsonl"
TOKEN_RE = re.compile(r"[a-z][a-z0-9_+#-]*|[0-9]+|[\u4e00-\u9fff]", re.IGNORECASE)


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
    trust: str = "community_consensus"
    captured_at: str = ""

    @classmethod
    def from_dict(cls, value: dict) -> "KnowledgeChunk":
        if not isinstance(value, dict):
            raise ValueError("knowledge chunk must be an object")
        required = ("chunk_id", "title", "source_url", "source_type",
                    "game_version", "character", "text")
        missing = [key for key in required if not str(value.get(key) or "").strip()]
        if missing:
            raise ValueError(f"knowledge chunk missing: {', '.join(missing)}")
        tags = value.get("tags", [])
        if isinstance(tags, str):
            tags = [tags]
        if not isinstance(tags, list):
            raise ValueError("knowledge chunk tags must be an array")
        return cls(
            chunk_id=str(value["chunk_id"])[:120],
            title=str(value["title"])[:240],
            source_url=str(value["source_url"])[:500],
            source_type=str(value["source_type"])[:80],
            game_version=str(value["game_version"])[:80],
            character=str(value["character"]).upper()[:40],
            tags=tuple(str(tag).lower()[:80] for tag in tags if str(tag).strip()),
            text=str(value["text"])[:1800],
            trust=str(value.get("trust") or "community_consensus")[:80],
            captured_at=str(value.get("captured_at") or "")[:40],
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
                 tags: Iterable[str] = (), top_k: int = 4) -> list[Retrieval]:
        character = str(character or "").upper()
        if character and character != "IRONCLAD":
            return []
        query_tokens = set(_tokens(query)) | set(_tokens(" ".join(tags)))
        if not query_tokens:
            return []
        wanted_tags = {str(tag).lower() for tag in tags if str(tag).strip()}
        ranked: list[Retrieval] = []
        for chunk, tokens in zip(self.chunks, self._tokens):
            if chunk.character not in {"", "IRONCLAD", "ANY"}:
                continue
            counts = Counter(tokens)
            matched = query_tokens & set(counts)
            if not matched:
                continue
            score = sum(self._idf.get(token, 1.0) * min(3, counts[token])
                        for token in matched)
            score += 0.75 * len(wanted_tags & set(chunk.tags))
            ranked.append(Retrieval(chunk, score, tuple(sorted(matched))))
        ranked.sort(key=lambda item: (-item.score, item.chunk.chunk_id))
        return ranked[:max(0, min(8, int(top_k)))]

    def context(self, query: str, *, character: str = "IRONCLAD",
                tags: Iterable[str] = (), top_k: int = 3,
                max_chars: int = 3600) -> dict:
        results = self.retrieve(query, character=character, tags=tags, top_k=top_k)
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
                "matched_terms": list(result.matched_terms),
            })
            remaining -= len(text)
        return {
            "status": "ready" if items else "no_match",
            "character": str(character or "").upper(),
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
    profile = game.get("strategy_context") or game.get("strategy") or {}
    selected = profile.get("selected") if isinstance(profile, dict) else {}
    profile_id = selected.get("id") if isinstance(selected, dict) else ""
    query = " ".join(filter(None, [screen, f"act {act}", profile_id, *card_ids]))
    tags = [screen.lower(), str(profile_id or "").lower()]
    return query, tuple(tag for tag in tags if tag)


def retrieve_strategy_context(game: dict, *, top_k: int = 3,
                              knowledge_base: StrategyKnowledgeBase | None = None) -> dict:
    character = str((game or {}).get("character") or (game or {}).get("class") or "").upper()
    if character and character != "IRONCLAD":
        return {"status": "unsupported_character", "character": character,
                "items": [], "citation_ids": [],
                "usage": "ironclad-only strategy knowledge"}
    query, tags = _state_query(game)
    try:
        return (knowledge_base or default_knowledge_base()).context(
            query, character="IRONCLAD", tags=tags, top_k=top_k)
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
