import json
from pathlib import Path

import pytest


ROOT = Path(__file__).parent
KNOWLEDGE = ROOT.parent / "spirebrain" / "data" / "strategy_knowledge.jsonl"


def test_rag_quality_audit_reports_coverage_without_claiming_quality():
    from spirebrain.analysis.rag_eval import evaluate_knowledge_base

    report = evaluate_knowledge_base(KNOWLEDGE)

    assert report["passed"] is True
    assert report["source"]["chunk_count"] == 8
    assert report["source"]["source_count"] == 4
    assert report["retrieval"]["expected_coverage_rate"] == 1.0
    assert report["integrity"]["citation_provenance_rate"] == 1.0
    assert report["integrity"]["character_isolation"] is True
    assert report["quality"]["status"] == "unknown"
    assert report["quality"]["win_rate"] == "unknown"


def test_rag_quality_audit_does_not_emit_claim_text_or_credentials():
    from spirebrain.analysis.rag_eval import evaluate_knowledge_base

    rendered = json.dumps(evaluate_knowledge_base(KNOWLEDGE), ensure_ascii=False)
    assert "Authorization" not in rendered
    assert "api_key" not in rendered.lower()
    # The report is an audit of IDs and counts, not a second copy of the corpus.
    assert "这是一套社区玩家提出" not in rendered


def test_rag_quality_audit_rejects_empty_query_suite():
    from spirebrain.analysis.rag_eval import evaluate_knowledge_base

    with pytest.raises(ValueError, match="query_cases"):
        evaluate_knowledge_base(KNOWLEDGE, query_cases=[])


def test_rag_quality_cli_rejects_missing_index(tmp_path, capsys):
    from spirebrain.analysis.rag_eval import main

    assert main(["--input", str(tmp_path / "missing.jsonl")]) == 2
    assert '"error": "invalid_knowledge_index"' in capsys.readouterr().out
