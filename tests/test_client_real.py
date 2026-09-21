"""Tests for the real JEV client: payload shape, response parsing, retry, cost.

Ground truth: `OFFICIAL_FIXTURE` is the response body published in the Cloudflare
AI / TypeSafe docs (verified 2026-09-21). If our parser disagrees with it, our
parser is wrong. No network is touched — `_post` is monkeypatched.
"""

from __future__ import annotations

import io
import sys
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from spirebrain.jev_brain.client import (
    ChoiceSpec,
    NoulSpec,
    ScoreSpec,
    build_questions_json,
)
from spirebrain.jev_brain.client_real import (
    JevApiError,
    OfficialJevClient,
    estimate_cost_usd,
    parse_answers,
)

# --- published example response (Cloudflare AI docs for typesafe/jev) -------- #
OFFICIAL_FIXTURE = {
    "model": "jev-1.13.0",
    "answers": {
        "is_urgent": {"type": "noul", "noul": 0.95},
        "department": {
            "type": "choice",
            "choice": "billing",
            "confidence": 0.8,
            "probabilities": {"billing": 0.87, "sales": 0.0, "technical": 0.13},
        },
        "frustration": {
            "type": "score",
            "score": 1.04,
            "confidence": 0.94,
            "legend": {"0": "Calm", "1": "Frustrated", "2": "Very angry"},
            "probabilities": {"0": 0.0, "1": 0.96, "2": 0.04},
        },
    },
    "usage": {"input_tokens": 426, "output_tokens": 73},
}

SPECS = {
    "is_urgent": NoulSpec(instructions="Does this convey urgency?"),
    "department": ChoiceSpec(instructions="Which team should handle this?",
                             criteria={"billing": "Payments", "technical": "Bugs",
                                       "sales": "Pricing"}),
    "frustration": ScoreSpec(instructions="How frustrated is the customer?",
                             criteria=["Calm", "Frustrated", "Very angry"]),
}


def test_payload_matches_documented_field_names():
    q = build_questions_json(SPECS)
    assert q["is_urgent"] == {"type": "noul",
                              "instructions": "Does this convey urgency?"}
    assert q["department"]["type"] == "choice"
    assert q["department"]["criteria"]["billing"] == "Payments"
    assert q["frustration"]["type"] == "score"
    # Score criteria is an ORDERED LIST of word-labelled levels, not a range.
    assert q["frustration"]["criteria"] == ["Calm", "Frustrated", "Very angry"]


def test_parse_official_fixture():
    parsed = parse_answers(OFFICIAL_FIXTURE["answers"], SPECS)

    # Noul: the probability IS the belief -> value == confidence, no second field.
    assert parsed["is_urgent"].value == 0.95
    assert parsed["is_urgent"].confidence == 0.95

    # Choice: label + confidence, probabilities kept raw for logs.
    assert parsed["department"].value == "billing"
    assert parsed["department"].confidence == 0.8
    assert parsed["department"].raw["probabilities"]["technical"] == 0.13

    # Score: level 1.04 on a 3-level rubric -> 1.04 / 2 = 0.52 normalised.
    assert abs(parsed["frustration"].value - 0.52) < 1e-9
    assert parsed["frustration"].confidence == 0.94
    assert parsed["frustration"].raw["legend"]["1"] == "Frustrated"


def test_score_normalisation_across_level_counts():
    two = ScoreSpec("x", ["no", "yes"])
    four = ScoreSpec("x", ["a", "b", "c", "d"])
    assert two.normalize(1.0) == 1.0
    assert two.normalize(0.5) == 0.5
    assert four.normalize(0.0) == 0.0
    assert abs(four.normalize(1.5) - 0.5) < 1e-9
    assert four.normalize(99.0) == 1.0  # clamped
    assert four.normalize(-3.0) == 0.0


def test_score_spec_requires_two_levels():
    try:
        ScoreSpec("x", ["only one"])
    except ValueError as e:
        assert "2 ordered levels" in str(e)
    else:
        raise AssertionError("expected ValueError")


def test_cost_only_input_tokens():
    assert estimate_cost_usd({"input_tokens": 0, "output_tokens": 500}) == 0.0
    assert estimate_cost_usd({"input_tokens": 1_000_000}) == 0.042
    assert abs(estimate_cost_usd(OFFICIAL_FIXTURE["usage"]) - 426 * 0.042 / 1e6) < 1e-12


class _FakeClient(OfficialJevClient):
    """OfficialJevClient with the network replaced by a scripted list of outcomes."""

    def __init__(self, outcomes, **kw):
        kw.setdefault("backoff", 0.0)
        super().__init__(api_key="test-key", **kw)
        self.outcomes = list(outcomes)
        self.attempts = 0

    def _post(self, payload):
        self.attempts += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("http://x", code, "boom", None,
                                  io.BytesIO(b'{"error":"nope"}'))


def test_round_trip_with_fake_transport():
    client = _FakeClient([OFFICIAL_FIXTURE])
    resp = client.ask({"state": "help"}, SPECS)
    assert resp.backend == "official"
    assert resp.model == "jev-1.13.0"
    assert resp.usage["input_tokens"] == 426
    assert abs(resp.cost_usd - 426 * 0.042 / 1e6) < 1e-12
    assert client.calls == 1
    assert abs(client.total_cost_usd - resp.cost_usd) < 1e-12


def test_retries_transient_429_then_succeeds():
    client = _FakeClient([_http_error(429), _http_error(503), OFFICIAL_FIXTURE])
    resp = client.ask("s", SPECS)
    assert client.attempts == 3
    assert resp.answers["is_urgent"].value == 0.95
    assert client.calls == 1  # only the successful call counts as a call


def test_retries_exhausted_raises():
    client = _FakeClient([_http_error(500)] * 5, max_retries=2)
    try:
        client.ask("s", {"q": NoulSpec(instructions="?")})
    except JevApiError as e:
        assert "HTTP 500" in str(e)
    else:
        raise AssertionError("expected JevApiError")


def test_402_is_not_retried():
    client = _FakeClient([_http_error(402)])
    try:
        client.ask("s", {"q": NoulSpec(instructions="?")})
    except JevApiError as e:
        assert "402" in str(e)
    else:
        raise AssertionError("expected JevApiError")
    assert client.attempts == 1  # no retry burned


def test_400_fails_fast():
    client = _FakeClient([_http_error(400)])
    try:
        client.ask("s", {"q": NoulSpec(instructions="?")})
    except JevApiError as e:
        assert "HTTP 400" in str(e)
    else:
        raise AssertionError("expected JevApiError")
    assert client.attempts == 1


def test_missing_key_is_an_explicit_error():
    import os
    saved = os.environ.pop("TYPESAFE_API_KEY", None)
    try:
        OfficialJevClient()
    except JevApiError as e:
        assert "TYPESAFE_API_KEY" in str(e)
    else:
        raise AssertionError("expected JevApiError")
    finally:
        if saved:
            os.environ["TYPESAFE_API_KEY"] = saved


def test_cloudflare_envelope_is_unwrapped():
    from spirebrain.jev_brain.client_real import CloudflareJevClient

    client = CloudflareJevClient(account_id="acct", api_key="tok", backoff=0.0)
    client._post = lambda payload: {  # type: ignore[method-assign]
        "result": {"result": dict(OFFICIAL_FIXTURE), "success": True},
        "success": True,
    }
    resp = client.ask("s", SPECS)
    assert resp.backend == "cloudflare"
    assert resp.answers["department"].value == "billing"


def test_oversized_request_is_refused_before_calling():
    """A state too big to send must fail loudly, never be silently truncated."""
    client = _FakeClient([OFFICIAL_FIXTURE])
    huge_state = {"deck": ["Strike"] * 20_000}     # comfortably over MAX_REQUEST_BYTES
    try:
        client.ask(huge_state, SPECS)
    except JevApiError as e:
        assert "over the" in str(e) and "truncated" in str(e)
    else:
        raise AssertionError("expected JevApiError for an oversized request")
    assert client.attempts == 0   # the API was never called


def test_request_under_the_cap_is_sent():
    client = _FakeClient([OFFICIAL_FIXTURE])
    client.ask({"state": "small"}, SPECS)
    assert client.attempts == 1


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all client_real tests passed")
