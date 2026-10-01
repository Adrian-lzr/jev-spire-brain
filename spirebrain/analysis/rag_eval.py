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

from spirebrain.knowledge.rag import (
    DEFAULT_PATH, LICENSE_STATUSES, SUPPORTED_GAME_ID, SUPPORTED_GAME_VERSION,
    StrategyKnowledgeBase,
)


SCHEMA_VERSION = 2

NEGATIVE_QUERY_CASES: tuple[dict, ...] = (
    {"name": "other_character", "character": "THE_SILENT",
     "game_id": "slay-the-spire-1", "game_version": "Slay the Spire 1"},
    {"name": "sts2_game_id", "character": "IRONCLAD",
     "game_id": "slay-the-spire-2", "game_version": "Slay the Spire 2"},
    {"name": "sts2_version", "character": "IRONCLAD",
     "game_id": "slay-the-spire-1", "game_version": "Slay the Spire 2"},
)

# In-domain abstention checks: these ask for detail the current, deliberately
# narrow corpus does not cover. They detect topical near-matches that character
# and game-version isolation tests cannot catch.
NEGATIVE_RETRIEVAL_CASES: tuple[dict, ...] = (
    {
        "name": "shop_price_details_not_covered",
        "query": "Ironclad exact shop card removal price relic potion discount",
        "tags": ["shop"],
    },
    {
        "name": "self_damage_interaction_not_covered",
        "query": "Ironclad Rupture Bloodletting self damage HP interaction",
        "tags": ["combat"],
    },
    {
        "name": "dead_branch_exact_mechanics_not_covered",
        "query": "Ironclad Dead Branch exact random card generation mechanics",
        "tags": ["mechanic"],
        "claim_types": ["mechanic_fact"],
    },
)

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
        "claim_types": ["strategy_heuristic"],
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
        "query": "Ironclad remove Strike or upgrade Defend priority",
        "tags": ["remove", "upgrade"],
        "expected": ["forgotten_arbiter_upgrades"],
        "allowed": ["forgotten_arbiter_upgrades", "dood_ironclad_foundation"],
    },
    {
        "name": "starter_combat_readiness",
        "query": "Ironclad starter deck inefficient filler cards early output block normal fights elites",
        "tags": ["starter_deck", "early_damage", "block"],
        "expected": ["dood_ironclad_foundation"],
    },
    {
        "name": "strength_multi_hit_mechanic",
        "query": "Ironclad Strength attack damage per hit",
        "tags": ["mechanic", "strength", "multi_hit"],
        "expected": ["sts1_ironclad_strength"],
        "allowed": ["sts1_ironclad_strength"],
        "claim_types": ["mechanic_fact"],
    },
    {
        "name": "strength_multi_hit_synergy",
        "query": "Ironclad Strength source multi-hit attacks synergy value",
        "tags": ["strategy", "strength", "multi_hit", "synergy"],
        "expected": ["sts1_strength_multihit_synergy"],
        "allowed": ["sts1_strength_multihit_synergy", "sts1_ironclad_strength"],
        "claim_types": ["strategy_heuristic", "mechanic_fact"],
    },
    {
        "name": "vulnerable_attack_modifier",
        "query": "Ironclad Vulnerable increases attack damage by 50 percent",
        "tags": ["mechanic", "vulnerable", "attack"],
        "expected": ["sts1_ironclad_vulnerable"],
    },
    {
        "name": "weak_attack_modifier",
        "query": "Ironclad Weak reduces attack damage by 25 percent",
        "tags": ["mechanic", "weak", "attack"],
        "expected": ["sts1_ironclad_weak"],
    },
    {
        "name": "burning_blood_post_combat_heal",
        "query": "Ironclad Burning Blood heals 6 HP after combat",
        "tags": ["mechanic", "burning_blood", "heal"],
        "expected": ["sts1_burning_blood"],
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
    allowed = case.get("allowed", expected)
    claim_types = case.get("claim_types", [])
    if not name or not query:
        raise ValueError(f"query_cases[{index}] requires name and query")
    if not isinstance(tags, (list, tuple)) or not all(str(tag).strip() for tag in tags):
        raise ValueError(f"query_cases[{index}].tags must be a non-empty-string array")
    if not isinstance(expected, (list, tuple)) or not all(str(item).strip() for item in expected):
        raise ValueError(f"query_cases[{index}].expected must be a non-empty-string array")
    if not isinstance(allowed, (list, tuple)) or not all(str(item).strip() for item in allowed):
        raise ValueError(f"query_cases[{index}].allowed must be a non-empty-string array")
    if not isinstance(claim_types, (list, tuple)) or not all(
            str(item).strip() for item in claim_types):
        raise ValueError(f"query_cases[{index}].claim_types must be an array of strings")
    normalized_expected = {str(item).strip()[:120] for item in expected[:20]}
    normalized_allowed = {str(item).strip()[:120] for item in allowed[:30]}
    if not normalized_expected <= normalized_allowed:
        raise ValueError(f"query_cases[{index}].expected must be included in allowed")
    return {
        "name": name[:120],
        "query": query[:500],
        "tags": [str(tag).strip().lower()[:80] for tag in tags[:20]],
        "expected": sorted(normalized_expected),
        "allowed": sorted(normalized_allowed),
        "claim_types": [str(item).strip().lower()[:80] for item in claim_types[:12]],
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
    for case in cases:
        referenced_ids = set(case["expected"]) | set(case["allowed"])
        unknown_ids = referenced_ids - known.keys()
        if unknown_ids:
            raise ValueError(
                f"query case {case['name']} references unknown chunk IDs"
            )
    source_urls = {chunk.source_url for chunk in knowledge.chunks}
    characters = sorted({chunk.character for chunk in knowledge.chunks})
    query_reports: list[dict] = []
    negative_reports: list[dict] = []
    semantic_negative_reports: list[dict] = []
    provenance_checks: list[bool] = []
    action_field_checks: list[bool] = []
    for case in cases:
        context = knowledge.context(case["query"], character="IRONCLAD",
                                    tags=case["tags"], top_k=top_k,
                                    claim_types=case["claim_types"] or None)
        citations = [str(item) for item in context.get("citation_ids", [])]
        expected = set(case["expected"])
        allowed = set(case["allowed"])
        matched_expected = sorted(expected & set(citations))
        matched_allowed = sorted(allowed & set(citations))
        unexpected = sorted(set(citations) - allowed)
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
                and item.get("game_id") == chunk.game_id
                and item.get("game_version") == chunk.game_version
                and item.get("claim_type") == chunk.claim_type
                and item.get("license") == chunk.license
                and item.get("license_status") == chunk.license_status
                and item.get("content_hash") == chunk.to_dict()["content_hash"]
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
            "allowed_citation_ids": sorted(allowed),
            "matched_expected_ids": matched_expected,
            "unexpected_citation_ids": unexpected,
            "expected_hit": expected <= set(citations),
            "allowed_precision": (len(matched_allowed) / len(citations)
                                  if citations else 1.0),
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

    for case in NEGATIVE_QUERY_CASES:
        context = knowledge.context(
            "Ironclad Act 1 route elite HP budget",
            character=case["character"], game_id=case["game_id"],
            game_version=case["game_version"], top_k=top_k,
        )
        citations = [str(item) for item in context.get("citation_ids", [])]
        negative_reports.append({
            "name": case["name"],
            "status": str(context.get("status") or "unknown"),
            "citation_ids": citations,
            "passed": not citations,
        })

    for case in NEGATIVE_RETRIEVAL_CASES:
        context = knowledge.context(
            case["query"], character="IRONCLAD", tags=case.get("tags", ()),
            claim_types=case.get("claim_types"), top_k=top_k,
        )
        citations = [str(item) for item in context.get("citation_ids", [])]
        semantic_negative_reports.append({
            "name": case["name"],
            "status": str(context.get("status") or "unknown"),
            "citation_ids": citations,
            "passed": not citations,
        })

    expected_hits = sum(1 for item in query_reports if item["expected_hit"])
    query_count = len(query_reports)
    corpus_valid = bool(knowledge.chunks) and all(
        chunk.source_url.startswith("https://")
        and chunk.game_id == SUPPORTED_GAME_ID
        and chunk.game_version.strip().lower() == SUPPORTED_GAME_VERSION
        and chunk.character in {"", "IRONCLAD", "ANY"}
        and bool(chunk.chunk_id and chunk.text and chunk.claim_type
                 and chunk.trust and chunk.license
                 and chunk.license_status in LICENSE_STATUSES
                 and chunk.content_hash)
        for chunk in knowledge.chunks
    )
    provenance_rate = (
        sum(provenance_checks) / len(provenance_checks) if provenance_checks else 1.0
    )
    action_field_safe = all(action_field_checks) if action_field_checks else True
    negative_isolation = all(case["passed"] for case in negative_reports)
    version_cases = [case for case in negative_reports
                     if case["name"] in {"sts2_game_id", "sts2_version"}]
    game_version_isolation = bool(version_cases) and all(
        case["passed"] for case in version_cases
    )
    semantic_abstention = all(case["passed"] for case in semantic_negative_reports)
    returned_count = sum(item["item_count"] for item in query_reports)
    relevant_return_count = sum(
        item["item_count"] - len(item["unexpected_citation_ids"])
        for item in query_reports
    )
    relevance_precision = (relevant_return_count / returned_count
                           if returned_count else 1.0)
    positive_relevance = all(not item["unexpected_citation_ids"]
                             for item in query_reports)
    license_status_counts = {
        status: sum(chunk.license_status == status for chunk in knowledge.chunks)
        for status in sorted(LICENSE_STATUSES)
    }
    unknown_licenses = [chunk.chunk_id for chunk in knowledge.chunks
                       if chunk.license_status != "verified"]
    license_review = {
        "chunk_count": len(knowledge.chunks),
        "verified_count": len(knowledge.chunks) - len(unknown_licenses),
        "unknown_count": license_status_counts.get("unknown", 0),
        "permission_required_count": license_status_counts.get("permission_required", 0),
        "nonverified_count": len(unknown_licenses),
        "status_counts": license_status_counts,
        "unverified_chunk_ids": unknown_licenses,
        "redistribution_ready": not unknown_licenses,
    }
    passed = bool(
        corpus_valid
        and query_count > 0
        and expected_hits == query_count
        and positive_relevance
        and relevance_precision == 1.0
        and character_isolated
        and negative_isolation
        and semantic_abstention
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
            "relevance_precision": relevance_precision,
            "positive_relevance": positive_relevance,
            "queries": query_reports,
            "negative_case_count": len(negative_reports),
            "negative_isolation": negative_isolation,
            "negative_cases": negative_reports,
            "negative_retrieval_case_count": len(semantic_negative_reports),
            "semantic_abstention": semantic_abstention,
            "negative_retrieval_cases": semantic_negative_reports,
        },
        "integrity": {
            "corpus_valid": corpus_valid,
            "citation_provenance_rate": provenance_rate,
            "character_isolation": character_isolated,
            "game_version_isolation": game_version_isolation,
            "advisory_context_has_no_action_fields": action_field_safe,
            "license_review": license_review,
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
