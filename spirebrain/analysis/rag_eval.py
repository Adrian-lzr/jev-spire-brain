"""Offline audit for the source-cited strategy knowledge index.

The retrieval layer is deliberately a soft-input feature.  A citation hit is
not a label for a good action and it cannot establish a win rate.  This module
therefore measures only corpus integrity, character isolation, provenance and
coverage of a small, versioned query suite.  It never calls a provider or a
network endpoint.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Iterable, Mapping

from spirebrain.knowledge.rag import DEFAULT_PATH, StrategyKnowledgeBase


SCHEMA_VERSION = 1

# These queries are intentionally expressed in terms of observable strategic
# situations rather than card verdicts.  The expected IDs are provenance
# checks, not "correct action" labels.
DEFAULT_QUERY_CASES: tuple[dict, ...] = (
    {
        "name": "act1_route_budget",
        "query": "Ironclad Act 1 route elite HP budget",
        "tags": ["act_1", "route", "elite"],
        "expected": ["panacea_act1_resources"],
    },
    {
        "name": "conditional_card_reward",
        "query": "Ironclad card reward conditional build",
        "tags": ["card_reward", "conditional"],
        "expected": ["panacea_tier_list_context"],
    },
    {
        "name": "adaptive_build_pivot",
        "query": "Ironclad adaptive strength block exhaust pivot",
        "tags": ["pivot", "adaptive"],
        "expected": ["fantasywarden_pivot"],
    },
    {
        "name": "exhaust_engine",
        "query": "Ironclad exhaust Corruption Feel No Pain",
        "tags": ["exhaust"],
        "expected": ["fantasywarden_exhaust"],
    },
    {
        "name": "upgrade_removal",
        "query": "Ironclad upgrade remove Strike Defend",
        "tags": ["upgrade", "remove"],
        "expected": ["forgotten_arbiter_upgrades"],
    },
    {
        "name": "starter_foundation",
        "query": "Ironclad starter deck early damage block",
        "tags": ["starter_deck", "damage", "block"],
        "expected": ["dood_ironclad_foundation"],
    },
    {
        "name": "block_stack_timing",
        "query": "Ironclad Barricade Entrench block timing",
        "tags": ["barricade", "block"],
        "expected": ["dood_barricade_timing"],
    },
    {
        "name": "shop_removal_budget",
        "query": "Ironclad shop gold remove Strike preserve resources",
        "tags": ["shop", "remove", "strike"],
        "expected": ["forgotten_arbiter_upgrades"],
    },
    {
        "name": "combat_survival_baseline",
        "query": "Ironclad combat incoming damage output block survival",
        "tags": ["combat", "damage", "block"],
        "expected": ["dood_ironclad_foundation"],
    },
)


def _safe_case(case: Mapping[str, object], index: int) -> dict:
    """Validate and bound one query case before using it in an audit."""

    if not isinstance(case, Mapping):
        raise ValueError(f"query_cases[{index}] must be an object")
    name = str(case.get("name") or "").strip()
    query = str(case.get("query") or "").strip()
    tags = case.get("tags", [])
    expected = case.get("expected", [])
    if not name or not query:
        raise ValueError(f"query_cases[{index}] requires name and query")
    if not isinstance(tags, (list, tuple)) or not all(str(tag).strip() for tag in tags):
        raise ValueError(f"query_cases[{index}].tags must be a non-empty-string array")
    if not isinstance(expected, (list, tuple)) or not all(str(item).strip() for item in expected):
        raise ValueError(f"query_cases[{index}].expected must be a non-empty-string array")
    return {
        "name": name[:120],
        "query": query[:500],
        "tags": [str(tag).strip().lower()[:80] for tag in tags[:20]],
        "expected": [str(item).strip()[:120] for item in expected[:20]],
    }


def evaluate_knowledge_base(path: str | Path = DEFAULT_PATH, *,
                            query_cases: Iterable[Mapping[str, object]] = DEFAULT_QUERY_CASES,
                            top_k: int = 3) -> dict:
    """Audit a local index without exposing claims or invoking a model.

    The returned report contains IDs and bounded diagnostics only.  It does
    not copy claim text, prompts, request bodies or credentials into metrics.
    """

    target = Path(path)
    knowledge = StrategyKnowledgeBase.from_jsonl(target)
    cases = [_safe_case(case, index) for index, case in enumerate(query_cases)]
    if not cases:
        raise ValueError("query_cases must not be empty")

    known = {chunk.chunk_id: chunk for chunk in knowledge.chunks}
    source_urls = {chunk.source_url for chunk in knowledge.chunks}
    characters = sorted({chunk.character for chunk in knowledge.chunks})
    query_reports: list[dict] = []
    provenance_checks: list[bool] = []
    action_field_checks: list[bool] = []
    for case in cases:
        context = knowledge.context(case["query"], character="IRONCLAD",
                                    tags=case["tags"], top_k=top_k)
        citations = [str(item) for item in context.get("citation_ids", [])]
        expected = set(case["expected"])
        matched_expected = sorted(expected & set(citations))
        item_checks: list[bool] = []
        item_action_checks: list[bool] = []
        for item in context.get("items", []):
            chunk_id = str(item.get("chunk_id") or "")
            chunk = known.get(chunk_id)
            item_checks.append(bool(
                chunk is not None
                and item.get("source_url") == chunk.source_url
                and item.get("source_type") == chunk.source_type
                and item.get("trust") == chunk.trust
            ))
            # Retrieved context must remain advisory data.  These fields are
            # forbidden even if a future corpus row accidentally contains them.
            item_action_checks.append(not any(
                key in item for key in ("command", "candidate_id", "card_index", "target_index")
            ))
        provenance_checks.extend(item_checks)
        action_field_checks.extend(item_action_checks)
        query_reports.append({
            "name": case["name"],
            "status": str(context.get("status") or "unknown"),
            "citation_ids": citations,
            "expected_citation_ids": sorted(expected),
            "matched_expected_ids": matched_expected,
            "expected_hit": bool(matched_expected),
            "item_count": len(context.get("items") or []),
        })

    # Explicitly check unsupported-character isolation.  It is a hard safety
    # property: an Ironclad claim must never silently become another character's
    # strategic evidence.
    unsupported = knowledge.context(
        "Ironclad Act 1 route elite HP budget", character="THE_SILENT",
        tags=("act_1", "route"), top_k=top_k,
    )
    # ``StrategyKnowledgeBase.context`` is intentionally lower-level than
    # ``retrieve_strategy_context`` and reports an empty result as
    # ``no_match``.  The public wrapper adds ``unsupported_character``; both
    # statuses are safe as long as no items or citations cross the boundary.
    character_isolated = (
        unsupported.get("status") in {"unsupported_character", "no_match"}
        and not unsupported.get("items")
        and not unsupported.get("citation_ids")
    )

    expected_hits = sum(1 for item in query_reports if item["expected_hit"])
    query_count = len(query_reports)
    corpus_valid = bool(knowledge.chunks) and all(
        chunk.source_url.startswith("https://")
        and chunk.character in {"", "IRONCLAD", "ANY"}
        and bool(chunk.chunk_id and chunk.text)
        for chunk in knowledge.chunks
    )
    provenance_rate = (
        sum(provenance_checks) / len(provenance_checks) if provenance_checks else 1.0
    )
    action_field_safe = all(action_field_checks) if action_field_checks else True
    passed = bool(
        corpus_valid
        and query_count > 0
        and expected_hits == query_count
        and character_isolated
        and provenance_rate == 1.0
        and action_field_safe
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "source": {
            "fixture_type": "local_strategy_index",
            "path": str(target),
            "chunk_count": len(knowledge.chunks),
            "source_count": len(source_urls),
            "characters": characters,
        },
        "retrieval": {
            "query_count": query_count,
            "expected_hit_count": expected_hits,
            "expected_coverage_rate": expected_hits / query_count,
            "queries": query_reports,
        },
        "integrity": {
            "corpus_valid": corpus_valid,
            "citation_provenance_rate": provenance_rate,
            "character_isolation": character_isolated,
            "advisory_context_has_no_action_fields": action_field_safe,
        },
        "quality": {
            "status": "unknown",
            "labels": 0,
            "win_rate": "unknown",
            "reason": "retrieval coverage and citation provenance have no expert labels or real-game outcome ground truth",
        },
        "passed": passed,
        "limitations": [
            "lexical retrieval is not semantic correctness",
            "community claims are soft evidence and not measured win-rate labels",
            "offline audit does not call GPT/JEV or execute a game",
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="audit the offline strategy RAG index")
    parser.add_argument("--input", default=str(DEFAULT_PATH))
    parser.add_argument("--top-k", type=int, default=3)
    parser.add_argument("--json", dest="output")
    args = parser.parse_args(argv)
    try:
        report = evaluate_knowledge_base(args.input, top_k=args.top_k)
    except (OSError, ValueError) as exc:
        print(json.dumps({"error": "invalid_knowledge_index",
                          "kind": type(exc).__name__}, ensure_ascii=False))
        return 2
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
