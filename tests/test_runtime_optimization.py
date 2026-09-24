from __future__ import annotations

import io
import urllib.error

from spirebrain.driver.decision_state import recommendation_key, state_id
from spirebrain.brain.action_broker import reconcile
from spirebrain.brain.protocol import ActionCandidate, StrategicPlan
from spirebrain.jev_brain.client import NoulSpec
from spirebrain.jev_brain.client_real import OfficialJevClient, JevApiError


def test_semantic_state_ignores_poll_noise_but_tracks_hand_and_energy():
    base = {"screen_type": "COMBAT", "current_hp": 40, "energy": 2,
            "animation_counter": 1, "timestamp": 10, "hand": ["Strike"]}
    noisy = {**base, "animation_counter": 99, "timestamp": 99,
             "uuid": "new", "trace_id": "different"}
    changed = {**base, "energy": 1}
    assert state_id(base) == state_id(noisy)
    assert recommendation_key(base) == recommendation_key(noisy)
    assert state_id(base) != state_id(changed)


def test_plan_preference_requires_candidate_signature_match():
    game = {"screen_type": "COMBAT", "energy": 1}
    old = ActionCandidate("combat:play:0:0", "play", "Strike",
                          command={"command": "play", "card": 0, "target": 0})
    new = ActionCandidate("combat:play:0:0", "play", "Defend",
                          command={"command": "play", "card": 0})
    plan = StrategicPlan("p", "state", "local", current_objective="survive",
                         preferred_candidates=[old.candidate_id])
    plan.bind_candidates([old])
    command, decision = reconcile(game=game, candidates=[new], fallback=new.command,
                                  plan=plan, state_id="new")
    assert command == new.command
    assert decision.primary_candidate is new


def test_jev_auth_failure_is_fast_and_does_not_retry():
    class Fake(OfficialJevClient):
        def __init__(self):
            super().__init__(api_key="test", max_retries=3, backoff=0)
            self.calls_seen = 0

        def _post(self, payload):
            self.calls_seen += 1
            raise urllib.error.HTTPError("http://x", 403, "forbidden", None,
                                         io.BytesIO(b"{}"))

    client = Fake()
    try:
        client.ask("state", {"q": NoulSpec("x")})
    except JevApiError:
        pass
    else:
        raise AssertionError("expected auth error")
    assert client.calls_seen == 1
    assert client.metrics["auth_errors"] == 1
