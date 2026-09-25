"""Structured-LLM BASELINE — a chat model posing as JEV, for comparison only.

> Real JEV is available and wired up: see `client_real.OpenRouterJevClient`
> (`JEV_BACKEND=openrouter`). This module is deliberately kept as the *baseline*
> arm of the project's flagship experiment — JEV's calibrated probabilities
> versus an LLM's self-reported confidence on identical questions and states.

JEV itself is reachable with the credentials we have, but through a route that is
easy to miss and has one sharp edge (verified live 2026-09-21):

  * `POST https://openrouter.ai/api/v1/systemone` with model `jev-1.13` serves
    real JEV and returns `provider: "TypeSafe"`.
  * OpenRouter's **chat completions** endpoint rejects it: any of `typesafe/jev`,
    `typesafe/jev-latest`, `typesafe/jev-1.13` answers
    `400 "... is not a valid model ID"`. So a chat-completions client can never
    reach JEV, which is precisely why this baseline exists as a separate thing.

WHAT THIS BASELINE IS NOT (read this before quoting any number it produces)
---------------------------------------------------------------------------
  * **Calibration.** JEV's probabilities come from a training objective (RLCD)
    that rewards being right at the stated probability. Here "confidence" is the
    model's *self-reported* belief, with no such guarantee — self-reported
    confidence from chat models is a well-known weak spot. Measuring exactly how
    far off it is is the point of keeping this client; we log it verbatim and
    never relabel it as JEV's calibrated probability.
  * **Parallelism.** JEV evaluates every question in one pass at nearly constant
    latency. Here all questions share one generated JSON object, so latency and
    cost grow with the number of questions and with answer verbosity.
  * **Failure mode.** JEV cannot emit anything outside the schema. A
    schema-constrained LLM also cannot, but it can still pick the wrong label —
    and pick it *confidently*.

Consequently `backend_name` is `llm:<model>`, never `jev`, and every log line
carries it. If you mix logs from real JEV and from here, filter on this field.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Any

from spirebrain.jev_brain.client import (
    ChoiceSpec,
    JevAnswer,
    JevClient,
    JevResponse,
    NoulSpec,
    QuestionSpec,
    ScoreSpec,
    dump_state,
)
from spirebrain.redaction import redact_text

ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"

# USD per token, read from /api/v1/models on 2026-09-21. Verify before trusting
# any cost figure; unknown models are costed at zero and flagged in the response.
PRICES: dict[str, tuple[float, float]] = {
    # model id: (prompt $/token, completion $/token)
    "qwen/qwen3.7-flash": (0.00000003, 0.00000013),
    "inference-net/schematron-v2-turbo": (0.00000003, 0.00000015),
    "inference-net/schematron-v2-small": (0.00000005, 0.00000023),
    "~deepseek/deepseek-flash-latest": (0.00000012, 0.00000048),
    "qwen/qwen3.8-27b:free": (0.0, 0.0),
}

# Cheap, fast, and confirmed to support structured output. Shortlist if you want
# to A/B: schematron-v2-turbo (schema-specialised), deepseek-flash-latest
# (closest in spirit to the JEV comparison), qwen3.8-27b:free (free tier,
# 1000 req/day).
#
# ⚠️ ACCOUNT PRECONDITION: this baseline needs an OpenRouter account whose
# allowed-providers policy permits the serving provider. The key this project was
# developed against permits **only `typesafe`** (so it can reach JEV but nothing
# else), and any call here answers:
#   404 "No allowed providers are available for the selected model …
#        your account's allowed-providers setting permits only: typesafe"
# Fix: https://openrouter.ai/settings/privacy — add the provider, or use a second
# key for the baseline. Until then this client is code-complete but unrunnable.
DEFAULT_MODEL = "qwen/qwen3.7-flash"

TRANSIENT_STATUS = {408, 429, 500, 502, 503, 504}

SYSTEM_PROMPT = (
    "You evaluate typed questions against a game state and return a judgement for "
    "each question independently. Rules:\n"
    "1. Every question is evaluated in isolation against the same state. Do not let "
    "one answer influence another.\n"
    "2. Answer ONLY with the required JSON. No prose, no explanation.\n"
    "3. 'confidence' must be your honest probability (0 to 1) that your specific "
    "answer is correct. 0.5 means you genuinely cannot tell. Do not inflate it: a "
    "reported 0.9 should be right about 90% of the time.\n"
    "4. For a yes/no question, give the probability that the answer is yes.\n"
    "5. For a score, pick the single level index that best fits, even if the state "
    "sits between two levels - choose the nearer one.\n"
    "6. Judge only from the state provided. If the state lacks what a question "
    "needs, say so with confidence near 0.5 rather than guessing."
)


class OpenRouterError(RuntimeError):
    """Raised when OpenRouter rejects a request we cannot retry out of."""


# --------------------------------------------------------------------------- #
# Schema + parsing (pure functions: unit-testable without network)
# --------------------------------------------------------------------------- #
def build_json_schema(questions: dict[str, QuestionSpec]) -> dict:
    """Our three primitives -> one strict JSON schema.

    This is the whole trick of the stand-in: `Choice` becomes an enum, `Score` an
    integer level index, `Noul` a probability. The model cannot answer off-schema,
    so callers get the same shapes JEV would give them.
    """
    props: dict[str, Any] = {}
    for name, spec in questions.items():
        if isinstance(spec, ChoiceSpec):
            props[name] = {
                "type": "object",
                "properties": {
                    "choice": {"type": "string", "enum": list(spec.criteria)},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                },
                "required": ["choice", "confidence"],
                "additionalProperties": False,
            }
        elif isinstance(spec, ScoreSpec):
            props[name] = {
                "type": "object",
                "properties": {
                    "level": {"type": "integer", "minimum": 0,
                              "maximum": len(spec.criteria) - 1,
                              "description": "0 = " + spec.criteria[0] + " ... "
                                             + str(len(spec.criteria) - 1) + " = "
                                             + spec.criteria[-1]},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                },
                "required": ["level", "confidence"],
                "additionalProperties": False,
            }
        elif isinstance(spec, NoulSpec):
            props[name] = {
                "type": "object",
                "properties": {
                    "probability": {"type": "number", "minimum": 0, "maximum": 1,
                                    "description": "probability that the answer is yes"},
                },
                "required": ["probability"],
                "additionalProperties": False,
            }
        else:
            raise TypeError(f"unknown question spec: {type(spec)!r}")
    return {
        "name": "typed_answers",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": props,
            "required": list(props),
            "additionalProperties": False,
        },
    }


def build_question_block(questions: dict[str, QuestionSpec]) -> str:
    """Human/machine-readable rendering of the questions (goes in the user turn)."""
    lines = ["QUESTIONS:"]
    for name, spec in questions.items():
        if isinstance(spec, ChoiceSpec):
            opts = "; ".join(f'"{k}" = {v}' for k, v in spec.criteria.items())
            lines.append(f'- "{name}" (choice): {spec.instructions}\n  options: {opts}')
        elif isinstance(spec, ScoreSpec):
            levels = " | ".join(f"{i} = {c}" for i, c in enumerate(spec.criteria))
            lines.append(f'- "{name}" (score): {spec.instructions}\n  levels: {levels}')
        elif isinstance(spec, NoulSpec):
            lines.append(f'- "{name}" (yes/no): {spec.instructions}')
    return "\n".join(lines)


def parse_structured_answers(obj: dict, questions: dict[str, QuestionSpec]) -> dict[str, JevAnswer]:
    """Model JSON -> JevAnswer, normalising Score levels exactly as client_real does."""
    parsed: dict[str, JevAnswer] = {}
    for name, spec in questions.items():
        raw = obj.get(name)
        if not isinstance(raw, dict):
            raise OpenRouterError(f"missing answer for question {name!r}")
        if isinstance(spec, ChoiceSpec):
            if raw.get("choice") not in spec.criteria:
                raise OpenRouterError(f"choice {raw.get('choice')!r} not in options for {name!r}")
            parsed[name] = JevAnswer(value=raw["choice"],
                                     confidence=float(raw.get("confidence", 0.0)),
                                     raw={"type": "choice", **raw})
        elif isinstance(spec, ScoreSpec):
            level = int(raw["level"])
            parsed[name] = JevAnswer(value=spec.normalize(level),
                                     confidence=float(raw.get("confidence", 0.0)),
                                     raw={"type": "score", "level": level,
                                          "legend": dict(enumerate(spec.criteria)),
                                          **raw})
        elif isinstance(spec, NoulSpec):
            p = float(raw["probability"])
            # Same convention as JEV: for a yes/no question the number IS the belief.
            parsed[name] = JevAnswer(value=p, confidence=p,
                                     raw={"type": "noul", "noul": p, **raw})
        else:
            raise TypeError(f"unknown question spec: {type(spec)!r}")
    return parsed


def extract_json(content: str) -> dict:
    """Tolerate the usual wrappers: bare JSON, ```json fences, leading prose."""
    text = content.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.lstrip().lower().startswith("json"):
            text = text.lstrip()[4:]
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise OpenRouterError(f"no JSON object in model output: {content[:200]!r}") from None
        return json.loads(text[start:end + 1])


def estimate_cost_usd(model: str, usage: dict) -> tuple[float, bool]:
    """-> (usd, price_known). Unknown models are costed at zero and flagged."""
    if model not in PRICES:
        return 0.0, False
    pin, pout = PRICES[model]
    return (int(usage.get("prompt_tokens", 0)) * pin
            + int(usage.get("completion_tokens", 0)) * pout), True


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #
class LlmStructuredClient(JevClient):
    """Implements the JevClient interface on top of OpenRouter chat completions.

    This is the BASELINE arm, not JEV. For real JEV use
    `client_real.OpenRouterJevClient` (JEV_BACKEND=openrouter).
    """

    backend_name = "llm"

    def __init__(
        self,
        api_key: str | None = None,
        model: str = DEFAULT_MODEL,
        endpoint: str = ENDPOINT,
        timeout: float = 90.0,
        max_retries: int = 3,
        backoff: float = 1.0,
    ) -> None:
        self.api_key = api_key or os.environ.get("OPENROUTER_API_KEY", "")
        if not self.api_key:
            raise OpenRouterError(
                "no API key: set OPENROUTER_API_KEY or pass api_key=. "
                "Use JEV_BACKEND=mock to develop offline."
            )
        self.model = model
        self.endpoint = endpoint
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff = backoff
        self.calls = 0
        self.total_cost_usd = 0.0
        self.price_known = model in PRICES

    # -- transport (overridable in tests) ---------------------------------- #
    def _post(self, payload: dict) -> dict:
        request = urllib.request.Request(
            self.endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                # OpenRouter uses these two for attribution on its dashboard.
                "HTTP-Referer": "https://github.com/Adrian-lzr/jev-spire-brain",
                "X-Title": "jev-spire-brain",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def build_payload(self, state, questions: dict[str, QuestionSpec]) -> dict:
        user = f"STATE:\n{dump_state(state)}\n\n{build_question_block(questions)}"
        return {
            "model": self.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user},
            ],
            "response_format": {"type": "json_schema", "json_schema": build_json_schema(questions)},
            "temperature": 0,
            "max_tokens": 2000,
        }

    # -- public API -------------------------------------------------------- #
    def ask(self, state, questions: dict[str, QuestionSpec]) -> JevResponse:
        payload = self.build_payload(state, questions)
        last_err: Exception | None = None
        for attempt in range(self.max_retries + 1):
            t0 = time.perf_counter()
            try:
                body = self._post(payload)
            except urllib.error.HTTPError as err:
                # Do not persist provider response bodies: they may echo the
                # request or an Authorization header.
                detail = ""
                if err.code in TRANSIENT_STATUS and attempt < self.max_retries:
                    last_err = err
                    time.sleep(self.backoff * (2**attempt))
                    continue
                raise OpenRouterError(redact_text(f"HTTP {err.code}: {detail}")) from err
            except (urllib.error.URLError, TimeoutError, OSError) as err:
                if attempt < self.max_retries:
                    last_err = err
                    time.sleep(self.backoff * (2**attempt))
                    continue
                raise OpenRouterError(f"network failure: {type(err).__name__}") from err

            latency_ms = int((time.perf_counter() - t0) * 1000)

            if isinstance(body.get("error"), dict):  # OpenRouter sometimes 200s an error
                raise OpenRouterError(redact_text(
                    f"api error: {body['error'].get('message', body['error'])}"))
            choices = body.get("choices") or []
            if not choices:
                raise OpenRouterError(redact_text(
                    f"no choices in response: {json.dumps(body)[:300]}"))

            content = choices[0].get("message", {}).get("content") or ""
            answers = parse_structured_answers(extract_json(content), questions)

            usage = body.get("usage", {}) or {}
            cost, known = estimate_cost_usd(self.model, usage)
            self.calls += 1
            self.total_cost_usd += cost
            return JevResponse(
                answers=answers,
                latency_ms=latency_ms,
                backend=f"llm:{self.model}",
                cost_usd=cost,
                model=body.get("model", self.model),
                usage={**usage, "price_known": known},
            )
        raise OpenRouterError(f"exhausted retries: {last_err}")


# --------------------------------------------------------------------------- #
# Smoke test: one real call, tiny cost. Run with OPENROUTER_API_KEY set.
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    client = LlmStructuredClient()
    resp = client.ask(
        {"act": 1, "floor": 7, "hp": {"current": 62, "max": 80},
         "hp_budget": {"remaining_spendable": 15},
         "map": {"reachable": [
             {"symbol": "M", "note": "monster fight"},
             {"symbol": "E", "note": "elite fight, good rewards"},
             {"symbol": "R", "note": "rest site"}],
             "estimated_cost_hp": {"M": 6, "E": 24, "R": 0}}},
        {
            "too_costly": NoulSpec(
                instructions="Would walking into the elite fight cost more HP than this "
                             "run can afford above its act reserve?"),
            "route": ChoiceSpec(
                instructions="Which node best serves finishing Act 1 safely?",
                criteria={"M": "monster fight (moderate HP cost)",
                          "E": "elite fight (high HP cost, relic reward)",
                          "R": "rest site (no HP cost, heal or upgrade)"}),
        },
    )
    print(f"backend : {resp.backend}")
    print(f"model   : {resp.model}")
    print(f"latency : {resp.latency_ms} ms")
    print(f"cost    : ${resp.cost_usd:.8f}  (usage={resp.usage})")
    for k, a in resp.answers.items():
        print(f"  {k}: value={a.value}  confidence={a.confidence}")
    print("\nNOTE: this is a structured-output LLM standing in for JEV, not JEV. "
          "Its 'confidence' is self-reported and uncalibrated.")
