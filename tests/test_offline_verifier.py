import sys


def test_offline_verifier_decodes_utf8_child_output_on_windows(tmp_path):
    from tools.verify_offline import run_check

    result = run_check(
        "utf8_fixture",
        [sys.executable, "-c",
         "import sys; sys.stdout.reconfigure(encoding='utf-8'); print('策略大脑')"],
        tmp_path,
    )

    assert result["passed"] is True
    assert "策略大脑" in result["output"]


def test_offline_verifier_validates_strategy_rag_output_contract():
    from tools.verify_offline import _validate_strategy_rag_output

    result = _validate_strategy_rag_output({
        "passed": True,
        "output": ('{"status":"ready","character":"IRONCLAD",'
                   '"citation_ids":["ironclad-act1"],"items":[{'
                   '"chunk_id":"ironclad-act1",'
                   '"source_url":"https://example.invalid/guide",'
                   '"claim":"Preserve HP before optional elites."}]}'),
    })

    assert result["passed"] is True
    assert result["validation"] == {
        "status": "ready", "character": "IRONCLAD", "citation_count": 1}


def test_offline_verifier_rejects_rag_success_without_citations_or_with_commands():
    from tools.verify_offline import _validate_strategy_rag_output

    result = _validate_strategy_rag_output({
        "passed": True,
        "output": ('{"status":"ready","character":"IRONCLAD",'
                   '"citation_ids":[],"items":[{'
                   '"chunk_id":"uncited","source_url":"https://example.invalid/guide",'
                   '"command":{"command":"proceed"}}]}'),
    })

    assert result["passed"] is False
    assert "citation_ids_do_not_match_items" in result["validation_error"]
    assert "item_0_contains_executable_field" in result["validation_error"]
