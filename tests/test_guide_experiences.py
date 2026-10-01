from __future__ import annotations

from copy import deepcopy

from spirebrain.guide.rules import GuideAwareClient
from spirebrain.driver.agent import _experience_scene


def _experience(*, status="active", validation=None, experience_id="exp-1"):
    validation = validation or {
        "passed": True,
        "fixture_type": "fixed_provider_responses",
        "fixture_hash": "sha256:" + "a" * 64,
        "sample_count": 2,
        "experience_id": experience_id,
        "experience_applied": True,
        "illegal_action_count": 0,
        "candidate_set_change_count": 0,
        "hard_constraint_violation_count": 0,
    }
    return {
        "experience_id": experience_id,
        "status": status,
        "policy_type": "soft_preference",
        "character": "IRONCLAD",
        "scenes": ["COMBAT"],
        "acts": [1],
        "title": "Retain a potion when safe",
        "claim": "Prefer keeping one potion for a nearby elite when this turn is safe.",
        "prerequisites": ["a potion is available", "an elite is nearby"],
        "exceptions": ["using it is needed to prevent lethal damage"],
        "replay_validation": validation,
        "evidence_event_ids": ["trace:private"],
        "command": {"command": "play", "card_index": 999},
    }


class _Capture:
    def ask(self, state, questions):
        self.state = state
        self.questions = questions
        return state


def test_only_active_fixed_replay_experiences_are_injected_as_soft_context():
    capture = _Capture()
    client = GuideAwareClient(capture)
    client.experiences = [
        _experience(status="pending_review", experience_id="pending"),
        _experience(status="rejected", experience_id="rejected"),
        _experience(status="revoked", experience_id="withdrawn"),
        _experience(status="active", experience_id="failed-replay", validation={
            "passed": False, "fixture_type": "fixed_provider_responses",
            "fixture_hash": "sha256:failed", "sample_count": 2,
            "illegal_action_count": 0, "candidate_set_change_count": 0,
            "hard_constraint_violation_count": 0,
        }),
        _experience(experience_id="active"),
    ]
    state = {
        "legal_candidates": ["play:0", "end_turn"],
        "verified_experiences": {"items": [{"experience_id": "stale"}]},
    }
    original_state = deepcopy(state)
    questions = {"tactical": {"criteria": {"play:0": "legal", "end_turn": "legal"}}}

    client.ask(state, questions)

    context = capture.state["verified_experiences"]
    assert [item["experience_id"] for item in context] == ["active"]
    assert context[0]["policy_type"] == "soft_preference"
    assert "optional" in context[0]["priority"].lower()
    assert "Ignore when current facts" in context[0]["guidance"]
    assert capture.state["legal_candidates"] == ["play:0", "end_turn"]
    assert capture.state["legal_candidates"] == state["legal_candidates"]
    assert state == original_state
    assert capture.questions is questions
    assert "command" not in context[0]
    assert "evidence_event_ids" not in context[0]
    assert "replay_validation" not in context[0]
    assert "trace:private" not in repr(capture.state)


def test_stale_experience_context_is_removed_when_experience_is_withdrawn():
    capture = _Capture()
    client = GuideAwareClient(capture)
    state = {"legal_candidates": ["choose:0"], "verified_experiences": {"items": [
        {"experience_id": "old-run"},
    ]}}

    client.experiences = [_experience()]
    client.ask(state, {})
    assert capture.state["verified_experiences"][0]["experience_id"] == "exp-1"

    client.experiences = [_experience(status="revoked")]
    client.ask(state, {})
    assert "verified_experiences" not in capture.state
    assert capture.state["legal_candidates"] == ["choose:0"]


def test_experience_context_is_bounded_and_redacted():
    capture = _Capture()
    client = GuideAwareClient(capture)
    record = _experience()
    record["title"] = "T" * 300
    record["claim"] = "C" * 800 + " API key: exposed-token"
    record["prerequisites"] = ["P" * 300] * 20
    client.experiences = [record] + [
        _experience(experience_id=f"exp-{index}") for index in range(5)
    ]

    client.ask({"legal_candidates": ["a", "b"]}, {})

    context = capture.state["verified_experiences"]
    assert len(context) == GuideAwareClient.MAX_EXPERIENCES
    serialized = repr(context)
    assert "exposed-token" not in serialized
    assert len(context[0]["title"]) <= 120
    assert len(context[0]["soft_preference"]) <= 500
    assert all(len(value) <= 160 for value in context[0]["prerequisites"])
    assert len(serialized) < 6000


def test_non_ironclad_or_out_of_scope_records_are_not_sent():
    capture = _Capture()
    client = GuideAwareClient(capture)
    wrong_character = _experience()
    wrong_character["character"] = "THE_SILENT"
    client.experiences = [wrong_character]

    client.ask(["opaque game state"], {})

    assert capture.state == ["opaque game state"]


def test_communicationmod_combat_screen_none_resolves_to_combat_experience_scope():
    assert _experience_scene({
        "screen_type": "NONE", "room_phase": "COMBAT",
    }) == "COMBAT"
    assert _experience_scene({"screen_type": "SHOP"}) == "SHOP"
    assert _experience_scene({"screen_type": "NONE", "room_phase": "EVENT"}) == "NONE"
