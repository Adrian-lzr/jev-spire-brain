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


def test_strategy_rag_fails_closed_when_character_is_missing():
    from spirebrain.knowledge.rag import retrieve_strategy_context

    context = retrieve_strategy_context({"screen_type": "COMBAT", "act": 1})

    assert context["status"] == "unknown_character"
    assert context["items"] == []
    assert context["citation_ids"] == []


def test_strategy_rag_abstains_on_uncovered_mechanics_and_filters_claim_type():
    from spirebrain.knowledge.rag import default_knowledge_base

    knowledge = default_knowledge_base()
    unrelated = knowledge.context(
        "Rupture Bloodletting self damage", character="IRONCLAD",
        tags=("combat",), top_k=8,
    )
    assert unrelated["status"] == "no_match"
    assert unrelated["citation_ids"] == []

    mechanics = knowledge.context(
        "Ironclad Strength attack damage per hit", character="IRONCLAD",
        tags=("mechanic", "strength", "multi_hit"),
        claim_types=("mechanic_fact",), top_k=8,
    )
    assert mechanics["citation_ids"] == ["sts1_ironclad_strength"]
    assert all(item["claim_type"] == "mechanic_fact" for item in mechanics["items"])


def test_strategy_rag_abstains_on_ambiguous_single_keyword_without_claim_type():
    from spirebrain.knowledge.rag import default_knowledge_base

    knowledge = default_knowledge_base()
    ambiguous = knowledge.context("Ironclad Strength", top_k=8)
    assert ambiguous["status"] == "no_match"
    assert ambiguous["citation_ids"] == []

    scoped = knowledge.context(
        "Ironclad Strength", claim_types=("mechanic_fact",), top_k=8,
    )
    assert scoped["citation_ids"] == ["sts1_ironclad_strength"]


def test_strategy_rag_labels_wiki_mechanics_as_version_scoped_soft_evidence():
    from spirebrain.knowledge.rag import default_knowledge_base

    context = default_knowledge_base().context(
        "Ironclad Strength adds attack damage for every multi hit",
        tags=("mechanic", "strength", "multi_hit"),
    )
    item = next(item for item in context["items"]
                if item["chunk_id"] == "sts1_ironclad_strength")
    assert item["game_id"] == "slay-the-spire-1"
    assert item["claim_type"] == "mechanic_fact"
    assert item["prerequisites"]
    assert item["exceptions"]
    assert item["content_hash"]
    assert "soft_evidence_only" in context["usage"]


def test_runtime_rag_query_uses_relics_and_combat_statuses():
    from spirebrain.knowledge.rag import retrieve_strategy_context

    context = retrieve_strategy_context({
        "character": "IRONCLAD", "screen_type": "COMBAT", "act": 1,
        "relics": ["Burning Blood"],
        "combat": {
            "player": {"powers": [{"id": "Strength", "amount": 2}]},
            "monsters": [{"powers": [
                {"id": "Vulnerable", "amount": 2}, {"id": "Weak", "amount": 1},
            ]}],
        },
    }, top_k=8)

    citations = set(context["citation_ids"])
    assert {
        "sts1_ironclad_strength", "sts1_ironclad_vulnerable",
        "sts1_ironclad_weak", "sts1_burning_blood",
    } <= citations


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
