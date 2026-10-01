import json
from pathlib import Path

import pytest


ROOT = Path(__file__).parent
KNOWLEDGE = ROOT.parent / "spirebrain" / "data" / "strategy_knowledge.jsonl"


def test_rag_quality_audit_reports_coverage_without_claiming_quality():
    from spirebrain.analysis.rag_eval import evaluate_knowledge_base

    report = evaluate_knowledge_base(KNOWLEDGE)

    assert report["passed"] is True
    assert report["schema_version"] == 2
    assert report["source"]["chunk_count"] == 13
    assert report["source"]["source_count"] == 8
    assert report["retrieval"]["expected_coverage_rate"] == 1.0
    assert report["retrieval"]["relevance_precision"] == 1.0
    assert report["retrieval"]["positive_relevance"] is True
    assert report["retrieval"]["negative_case_count"] == 3
    assert report["retrieval"]["negative_isolation"] is True
    assert report["retrieval"]["negative_retrieval_case_count"] == 3
    assert report["retrieval"]["semantic_abstention"] is True
    assert report["integrity"]["citation_provenance_rate"] == 1.0
    assert report["integrity"]["character_isolation"] is True
    assert report["integrity"]["game_version_isolation"] is True
    assert report["integrity"]["advisory_context_has_no_action_fields"] is True
    assert report["quality"]["status"] == "unknown"
    assert report["quality"]["win_rate"] == "unknown"


def test_rag_quality_audit_does_not_emit_claim_text_or_credentials():
    from spirebrain.analysis.rag_eval import evaluate_knowledge_base

    rendered = json.dumps(evaluate_knowledge_base(KNOWLEDGE), ensure_ascii=False)
    assert "Authorization" not in rendered
    assert "api_key" not in rendered.lower()
    # The report is an audit of IDs and counts, not a second copy of the corpus.
    assert "这是一套社区玩家提出" not in rendered


def test_wiki_mechanics_have_game_scope_license_conditions_and_content_hash():
    from spirebrain.knowledge.rag import default_knowledge_base

    wiki_chunks = [chunk for chunk in default_knowledge_base().chunks
                   if chunk.source_type == "community_wiki"
                   and chunk.claim_type == "mechanic_fact"]
    assert len(wiki_chunks) == 4
    assert all(chunk.claim_type == "mechanic_fact" for chunk in wiki_chunks)
    assert all(chunk.game_id == "slay-the-spire-1" for chunk in wiki_chunks)
    assert all(chunk.license.startswith("CC-BY-SA-4.0") for chunk in wiki_chunks)
    assert all(chunk.license_status == "verified" for chunk in wiki_chunks)
    assert all(chunk.content_hash and chunk.prerequisites and chunk.exceptions
               for chunk in wiki_chunks)


def test_derived_strength_synergy_is_labeled_as_heuristic_not_outcome_evidence():
    from spirebrain.knowledge.rag import default_knowledge_base

    chunk = next(chunk for chunk in default_knowledge_base().chunks
                 if chunk.chunk_id == "sts1_strength_multihit_synergy")
    assert chunk.claim_type == "strategy_heuristic"
    assert chunk.trust == "mechanic_derived_heuristic"
    assert chunk.source_url.endswith("/Strength")
    assert chunk.prerequisites
    assert any("胜率标签" in item for item in chunk.exceptions)


def test_rag_rejects_mismatched_recorded_content_hash():
    from spirebrain.knowledge.rag import KnowledgeChunk

    row = {
        "chunk_id": "tampered", "title": "t", "source_url": "https://example.test",
        "source_type": "strategy_guide", "game_version": "Slay the Spire 1",
        "game_id": "slay-the-spire-1", "character": "IRONCLAD",
        "trust": "single_source_unverified",
        "claim_type": "strategy_heuristic", "license": "unknown",
        "license_status": "unknown", "text": "source claim",
        "content_hash": "0" * 64,
    }
    with pytest.raises(ValueError, match="content hash mismatch"):
        KnowledgeChunk.from_dict(row)


def test_rag_requires_explicit_game_identity_and_provenance():
    from spirebrain.knowledge.rag import KnowledgeChunk

    row = {
        "chunk_id": "unscoped", "title": "t", "source_url": "https://example.test",
        "source_type": "strategy_guide", "game_version": "Slay the Spire 1",
        "character": "IRONCLAD", "trust": "single_source_unverified",
        "claim_type": "strategy_heuristic",
        "license": "unknown", "license_status": "unknown", "text": "claim",
    }
    with pytest.raises(ValueError, match="game_id"):
        KnowledgeChunk.from_dict(row)

    row["game_id"] = "slay-the-spire-1"
    del row["license_status"]
    with pytest.raises(ValueError, match="license_status"):
        KnowledgeChunk.from_dict(row)


def test_rag_fails_closed_on_other_game_version_and_reports_unknown_licenses():
    from spirebrain.knowledge.rag import default_knowledge_base

    knowledge = default_knowledge_base()
    query = "Ironclad Act 1 route elite HP budget"
    assert knowledge.context(query, game_id="slay-the-spire-2")["citation_ids"] == []
    assert knowledge.context(query, game_version="Slay the Spire 2")["citation_ids"] == []

    from spirebrain.analysis.rag_eval import evaluate_knowledge_base
    report = evaluate_knowledge_base(KNOWLEDGE)
    license_audit = report["integrity"]["license_review"]
    assert license_audit["unknown_count"] > 0
    assert license_audit["nonverified_count"] == license_audit["unknown_count"]
    assert license_audit["permission_required_count"] == 0
    assert license_audit["redistribution_ready"] is False


def test_rag_retrieval_abstains_on_uncovered_shop_prices_and_self_damage():
    from spirebrain.knowledge.rag import default_knowledge_base

    knowledge = default_knowledge_base()
    for query in (
        "Ironclad exact shop card removal price relic potion discount",
        "Ironclad Rupture Bloodletting self damage HP interaction",
    ):
        context = knowledge.context(query, character="IRONCLAD", top_k=8)
        assert context["status"] == "no_match"
        assert context["citation_ids"] == []


def test_rag_audit_penalizes_unexpected_matches_even_when_expected_source_hits():
    from spirebrain.analysis.rag_eval import evaluate_knowledge_base

    report = evaluate_knowledge_base(KNOWLEDGE, query_cases=[{
        "name": "strategy_query_with_mechanic_extra",
        "query": "Ironclad adaptive strength block exhaust pivot",
        "tags": ["pivot", "adaptive"],
        "expected": ["fantasywarden_pivot"],
        "allowed": ["fantasywarden_pivot"],
    }])

    assert report["retrieval"]["expected_coverage_rate"] == 1.0
    assert report["retrieval"]["relevance_precision"] < 1.0
    assert report["retrieval"]["positive_relevance"] is False
    assert report["passed"] is False


def test_rag_quality_audit_rejects_empty_query_suite():
    from spirebrain.analysis.rag_eval import evaluate_knowledge_base

    with pytest.raises(ValueError, match="query_cases"):
        evaluate_knowledge_base(KNOWLEDGE, query_cases=[])


def test_rag_quality_audit_rejects_unknown_allowlist_ids():
    from spirebrain.analysis.rag_eval import evaluate_knowledge_base

    with pytest.raises(ValueError, match="unknown chunk IDs"):
        evaluate_knowledge_base(KNOWLEDGE, query_cases=[{
            "name": "typo_allowlist",
            "query": "Ironclad starter deck early foundation",
            "tags": [],
            "expected": ["dood_ironclad_foundation"],
            "allowed": ["dood_ironclad_foundation", "typoed_chunk_id"],
        }])


def test_rag_quality_cli_rejects_missing_index(tmp_path, capsys):
    from spirebrain.analysis.rag_eval import main

    assert main(["--input", str(tmp_path / "missing.jsonl")]) == 2
    assert '"error": "invalid_knowledge_index"' in capsys.readouterr().out
