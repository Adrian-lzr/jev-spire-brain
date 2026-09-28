import json


def test_strategy_rag_returns_source_cited_ironclad_context():
    from spirebrain.knowledge.rag import default_knowledge_base

    context = default_knowledge_base().context(
        "Ironclad Act 1 route HP elite damage block", top_k=3)

    assert context["status"] == "ready"
    assert context["items"]
    assert context["citation_ids"] == [item["chunk_id"] for item in context["items"]]
    assert all(item["source_url"].startswith("https://") for item in context["items"])
    assert all("command" not in item and "card_index" not in item
               for item in context["items"])


def test_strategy_rag_does_not_apply_ironclad_claims_to_other_characters():
    from spirebrain.knowledge.rag import retrieve_strategy_context

    context = retrieve_strategy_context({"character": "THE_SILENT",
                                         "screen_type": "COMBAT"})

    assert context["status"] == "unsupported_character"
    assert context["items"] == []
    assert context["citation_ids"] == []


def test_strategic_planner_payload_contains_bounded_retrieval_context():
    from spirebrain.brain.gpt_client import MockStrategicClient
    from spirebrain.brain.planner import StrategicPlanner

    provider = MockStrategicClient()
    planner = StrategicPlanner(provider)
    planner.plan({"character": "IRONCLAD", "screen_type": "EVENT",
                  "act": 1, "deck": ["Strike_Red"],
                  "screen_state": {"options": ["fight", "leave"]}}, [])

    knowledge = provider.payloads[0]["knowledge_context"]
    assert knowledge["status"] in {"ready", "no_match"}
    assert len(json.dumps(knowledge, ensure_ascii=False)) <= 5000
    assert "soft_evidence_only" in knowledge["usage"]


def test_strategy_rag_can_be_disabled_for_same_input_control_runs():
    from spirebrain.brain.gpt_client import MockStrategicClient
    from spirebrain.brain.planner import StrategicPlanner

    provider = MockStrategicClient()
    StrategicPlanner(provider, knowledge_enabled=False).plan(
        {"character": "IRONCLAD", "screen_type": "EVENT", "act": 1}, [])

    knowledge = provider.payloads[0]["knowledge_context"]
    assert knowledge["status"] == "disabled"
    assert knowledge["citation_ids"] == []
