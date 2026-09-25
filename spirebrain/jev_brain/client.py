"""JEV client interface, specs, and offline mock.

Verified against the official API reference (docs.typesafe.ai + Cloudflare AI
docs, checked 2026-09-21):

    POST https://api.typesafe.ai/v1/systemone
    Authorization: Bearer $TYPESAFE_API_KEY
    Content-Type: application/json
    body:     {"state": <str|object|array>, "model": "jev-latest", "questions": {...}}
    response: {"model": ..., "answers": {name: {...}}, "usage": {input_tokens, output_tokens}}

Question shapes (field names exactly as the API expects):

    noul   {"type": "noul",   "instructions": ..., "criteria"?: {"true": ..., "false": ...}}
           -> {"noul": 0.26}                         # the number IS the belief
    choice {"type": "choice", "instructions": ..., "criteria": {label: description}}
           -> {"choice": "auto_allow", "probabilities": {...}, "confidence": 0.33}
    score  {"type": "score",  "instructions": ..., "criteria": [level, level, level]}
           -> {"score": 1.04, "legend": {...}, "probabilities": {...}, "confidence": 0.94}

CORRECTION (2026-09-21) — Score is *not* a numeric range.
    ScoreSpec used to carry `rubric=(0.0, 100.0)`, which does not exist in the
    API. JEV scores a state against an **ordered list of word-labelled levels**
    and may land *between* levels (the docs' own example: 1.035, between
    "Frustrated but civil" and "Very angry"). We therefore:

      * send `criteria` as a list of level descriptions (lowest -> highest),
      * normalise the returned level to 0..1 via `level / (len(criteria) - 1)`
        so call sites can compare against one threshold,
      * keep the raw level + legend in `JevAnswer.raw` for logs and reports.

    The 0..1 normalisation is OUR convention, not the API's. It is documented
    here precisely so nobody later mistakes it for a JEV field.

    For Noul there is no separate `confidence`: the returned probability is the
    belief, so `JevAnswer.value` and `JevAnswer.confidence` are the same number.
    That is also what makes Noul directly calibration-testable: statements
    answered 0.8 should be true ~80% of the time.

The real HTTP client lives in `client_real.py` (Phase 5). `MockJevClient` below
is a shape-only stand-in so the whole project runs offline without an API key.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

DEFAULT_MODEL = "jev-latest"
ENDPOINT = "https://api.typesafe.ai/v1/systemone"

# Noul answers inside this band mean "cannot tell" -> callers should fall back
# to a rule instead of acting on a coin flip. Mirrors strategy.json.
NOUL_UNCERTAIN_BAND = (0.40, 0.60)

# Bump when any question set, rubric or instruction changes. Question wording
# changes the raw confidence numbers (documented externally), so a threshold is
# only meaningful together with the prompt that produced it. Every log line
# carries this so runs can be compared honestly.
PROMPT_VERSION = 2

# Hard ceiling on one serialised request. The reference implementation for this
# class of project uses 24,000 bytes and *stops before calling the API* when a
# request would exceed it, rather than silently truncating state — silent
# truncation would make the model answer a question about a state it never saw.
MAX_REQUEST_BYTES = 24_000


# --------------------------------------------------------------------------- #
# Question specs
# --------------------------------------------------------------------------- #
@dataclass
class ChoiceSpec:
    """Pick one label out of a set you define.

    `criteria` maps every option label to its description. Per the API docs,
    options are cheap (a few tokens each), so pass the full list, and consider
    including an explicit catch-all so the model can decline to pick.
    """

    instructions: str
    criteria: dict[str, str]

    def to_api(self) -> dict:
        return {
            "type": "choice",
            "instructions": self.instructions,
            "criteria": dict(self.criteria),
        }


@dataclass
class ScoreSpec:
    """Position on an ordered, word-labelled rubric.

    `criteria` is ordered lowest -> highest. The API returns a level index that
    may be fractional; `normalize()` maps it onto 0..1.
    """

    instructions: str
    criteria: list[str]

    def __post_init__(self) -> None:
        if len(self.criteria) < 2:
            raise ValueError("ScoreSpec needs at least 2 ordered levels")

    def to_api(self) -> dict:
        return {
            "type": "score",
            "instructions": self.instructions,
            "criteria": list(self.criteria),
        }

    def normalize(self, level: float) -> float:
        """Map a raw level index onto 0..1 (our convention, see module docstring)."""
        top = len(self.criteria) - 1
        return max(0.0, min(1.0, float(level) / top))


@dataclass
class NoulSpec:
    """A yes/no question, answered as P(yes)."""

    instructions: str
    criteria: dict[str, str] | None = None  # optional {"true": ..., "false": ...}

    def to_api(self) -> dict:
        payload: dict[str, Any] = {"type": "noul", "instructions": self.instructions}
        if self.criteria:
            payload["criteria"] = dict(self.criteria)
        return payload


QuestionSpec = ChoiceSpec | ScoreSpec | NoulSpec


# --------------------------------------------------------------------------- #
# Answers
# --------------------------------------------------------------------------- #
@dataclass
class JevAnswer:
    """One typed answer.

    value depends on the question type:
        Choice -> the chosen label (str)
        Score  -> normalised 0..1 (raw level + legend in `raw`)
        Noul   -> probability in 0..1 (`confidence` equals it)
    """

    value: Any
    confidence: float
    raw: dict = field(default_factory=dict)  # untouched API answer object


@dataclass
class JevResponse:
    answers: dict[str, JevAnswer] = field(default_factory=dict)
    latency_ms: int = 0
    backend: str = "mock"
    cost_usd: float = 0.0
    model: str = ""
    usage: dict = field(default_factory=dict)


class JevClient:
    """Base class: retry, rate limiting, cost accounting live in subclasses."""

    backend_name = "base"

    def ask(self, state: str | dict | list, questions: dict[str, QuestionSpec]) -> JevResponse:
        raise NotImplementedError


# --------------------------------------------------------------------------- #
# Offline mocks
# --------------------------------------------------------------------------- #
def _mock_answer(spec: QuestionSpec, confidence: float) -> JevAnswer:
    if isinstance(spec, ChoiceSpec):
        first = next(iter(spec.criteria))
        return JevAnswer(value=first, confidence=confidence, raw={"type": "choice", "choice": first})
    if isinstance(spec, ScoreSpec):
        # middle level, normalised -> 0.5 for any odd level count
        mid_level = (len(spec.criteria) - 1) / 2
        return JevAnswer(
            value=spec.normalize(mid_level),
            confidence=confidence,
            raw={"type": "score", "score": mid_level, "legend": dict(enumerate(spec.criteria))},
        )
    if isinstance(spec, NoulSpec):
        # The mock answers "yes" with exactly the confidence it was handed. That
        # makes it useful at both ends: confidence=0.55 lands inside the
        # (0.40, 0.60) uncertain band, so every Noul-driven module falls back;
        # confidence=0.90 branches sharply. Real JEV, of course, reads the state.
        return JevAnswer(value=confidence, confidence=confidence,
                         raw={"type": "noul", "noul": confidence})
    raise TypeError(f"unknown question spec: {type(spec)!r}")


class MockJevClient(JevClient):
    """Deterministic offline stand-in — a source of *shape*, not intelligence.

    Default confidence (0.55) sits *below* the 0.60 floor on purpose: it forces
    every decision module down its fallback path, which is exactly what the
    offline simulation is meant to prove. Pass `confidence=0.9` to exercise the
    happy path instead.
    """

    backend_name = "mock"

    def __init__(self, confidence: float = 0.55) -> None:
        self.confidence = confidence

    def ask(self, state: str | dict | list, questions: dict[str, QuestionSpec]) -> JevResponse:
        answers = {k: _mock_answer(spec, self.confidence) for k, spec in questions.items()}
        return JevResponse(answers=answers, latency_ms=1, backend=self.backend_name, model="mock")


class ScriptedJevClient(JevClient):
    """Test helper: reply from a fixed table, falling back to `default`.

    `answers` maps question name -> JevAnswer (or a (value, confidence) tuple).
    Order matters; the first matching key wins.
    """

    backend_name = "scripted"

    def __init__(self, answers: dict[str, Any], default: JevAnswer | None = None) -> None:
        self.answers = answers
        self.default = default or JevAnswer(value=0.5, confidence=0.5, raw={})

    def ask(self, state: str | dict | list, questions: dict[str, QuestionSpec]) -> JevResponse:
        out: dict[str, JevAnswer] = {}
        for key, spec in questions.items():
            if key in self.answers:
                v = self.answers[key]
                out[key] = v if isinstance(v, JevAnswer) else JevAnswer(value=v[0], confidence=v[1])
            else:
                out[key] = _mock_answer(spec, self.default.confidence)
        return JevResponse(answers=out, latency_ms=2, backend=self.backend_name, model="scripted")


# --------------------------------------------------------------------------- #
# Factory
# --------------------------------------------------------------------------- #
def get_client(backend: str | None = None, **kwargs) -> JevClient:
    """`backend`: "mock" (default) | "scripted" | "openrouter" | "llm" | "official" | "cloudflare".

    Also honours the JEV_BACKEND environment variable when `backend` is None.

    * "openrouter" — REAL JEV via OpenRouter's System One route (model jev-1.13).
      This is the route this project uses; it needs OPENROUTER_API_KEY.
    * "llm"        — a schema-constrained chat model posing as JEV. NOT JEV: a
      labelled baseline for comparing self-reported confidence against JEV's
      calibrated probabilities. Backend name is `llm:<model>`.
    * "official"/"cloudflare" — TypeSafe direct / Cloudflare AI (both need their
      own accounts; neither is what we have).
    """
    import os

    backend = (backend or os.environ.get("JEV_BACKEND") or "mock").lower()
    if backend == "mock":
        return MockJevClient(**{k: v for k, v in kwargs.items() if k in {"confidence"}})
    if backend == "scripted":
        return ScriptedJevClient({})
    if backend == "llm":
        from spirebrain.jev_brain.openrouter_client import LlmStructuredClient

        return LlmStructuredClient(**kwargs)
    if backend in ("openrouter", "official", "cloudflare"):
        from spirebrain.jev_brain.client_real import (
            CloudflareJevClient,
            OfficialJevClient,
            OpenRouterJevClient,
        )

        return {
            "openrouter": OpenRouterJevClient,
            "official": OfficialJevClient,
            "cloudflare": CloudflareJevClient,
        }[backend](**kwargs)
    raise ValueError(f"unknown backend: {backend}")


def build_questions_json(questions: dict[str, QuestionSpec]) -> dict:
    """Public helper: spec dict -> the exact `questions` object the API expects."""
    return {k: spec.to_api() for k, spec in questions.items()}


def dump_state(state: str | dict | list) -> str:
    """Serialise a state for logging/estimating token count."""
    if isinstance(state, str):
        return state
    return json.dumps(state, ensure_ascii=False, sort_keys=True)
