"""Official JEV HTTP client — real JEV, three routes.

Verified 2026-09-21:

  TypeSafe direct : POST https://api.typesafe.ai/v1/systemone
                    Authorization: Bearer $TYPESAFE_API_KEY
                    body: {state, model, questions}   model: jev-latest

  OpenRouter      : POST https://openrouter.ai/api/v1/systemone
                    Authorization: Bearer $OPENROUTER_API_KEY
                    body: {state, model, questions}   model: jev-1.13
                    -> adds `id`, `provider`, and `usage.cost`.
                    **Chat completions does NOT serve JEV** — only this route does.

  Cloudflare AI   : POST https://api.cloudflare.com/client/v4/accounts/$ACCOUNT_ID/ai/run
                    Authorization: Bearer $CLOUDFLARE_API_TOKEN
                    body: {"model": "typesafe/jev", "input": {state, questions}}
                    response is wrapped: answers live under result.result

`OpenRouterJevClient` is the one this project uses. Implemented on the standard
library (urllib) on purpose: the brain must be able to run on a bare Python
install, so no httpx/requests requirement.

Design notes
------------
* One call carries MANY questions, evaluated in parallel by the model. Adding
  questions barely changes latency — that is the whole point of System One, and
  this client is built to exploit it (see `ask_many` in the decision modules).
* Retry only on transient failures (429 / 5xx / socket errors), with backoff.
  4xx other than 429 is a bug in our payload — fail loudly, do not retry.
* HTTP 402 means "no credits loaded"; that is not transient either.
* Cost: JEV bills input tokens only (output is priced at zero) as of 2026-09-21.
  Verify against your console invoice before trusting the accounting.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Any

from spirebrain.jev_brain.client import (
    DEFAULT_MODEL,
    ENDPOINT,
    ChoiceSpec,
    JevAnswer,
    JevClient,
    JevResponse,
    NoulSpec,
    QuestionSpec,
    ScoreSpec,
    build_questions_json,
)

# USD per 1M input tokens (JEV early access, 2026-09-21). Output priced at 0.
INPUT_USD_PER_MTOK = 0.042

CLOUDFLARE_ENDPOINT = (
    "https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/run"
)

# OpenRouter's System One (decisions) route. Verified 2026-09-21: this is the ONLY
# OpenRouter path that serves JEV — its chat completions endpoint rejects it.
OPENROUTER_SYSTEMONE = "https://openrouter.ai/api/v1/systemone"

TRANSIENT_STATUS = {429, 500, 502, 503, 504}


class JevApiError(RuntimeError):
    """Raised when the API rejects a request we cannot retry out of."""


# --------------------------------------------------------------------------- #
# Response parsing (pure, unit-testable without network)
# --------------------------------------------------------------------------- #
def parse_answers(answers_obj: dict, questions: dict[str, QuestionSpec]) -> dict[str, JevAnswer]:
    """Normalise the API's `answers` object into our JevAnswer values.

    Keeps the raw API object so logs can carry probabilities + legend.

    One subtlety we make explicit: `confidence` is recorded as `confidence_present`
    in the raw payload. Our first live run (2026-09-21) came back with some Score
    answers reporting confidence 0.000 next to a sibling reporting 0.24 in the same
    call, and a bare `.get("confidence", 0.0)` cannot tell "the model said zero"
    from "the field was absent". Rather than guess, we flag it and let the log
    settle the question on the next run.
    """
    parsed: dict[str, JevAnswer] = {}
    for name, raw in answers_obj.items():
        qtype = raw.get("type")
        spec = questions.get(name)
        raw = {**raw, "confidence_present": "confidence" in raw}
        if qtype == "choice":
            parsed[name] = JevAnswer(
                value=raw.get("choice"),
                confidence=float(raw.get("confidence", 0.0)),
                raw=raw,
            )
        elif qtype == "score":
            level = float(raw.get("score", 0.0))
            normalized = spec.normalize(level) if isinstance(spec, ScoreSpec) else level
            parsed[name] = JevAnswer(value=normalized, confidence=float(raw.get("confidence", 0.0)), raw=raw)
        elif qtype == "noul":
            p = float(raw.get("noul", 0.5))
            # No separate confidence field: the probability IS the belief.
            parsed[name] = JevAnswer(value=p, confidence=p, raw=raw)
        else:
            raise JevApiError(f"unknown answer type {qtype!r} for question {name!r}")
    return parsed


def estimate_cost_usd(usage: dict) -> float:
    """Input tokens are billed; output is priced at zero (see module docstring).

    Deliberately unrounded: these amounts are ~1e-5, so rounding to 8 decimals
    would throw away real precision.

    This is the *fallback* estimate. When the provider reports its own cost
    (OpenRouter returns `usage.cost`), that number is authoritative — see
    `resolve_cost`.
    """
    return int(usage.get("input_tokens", 0)) * INPUT_USD_PER_MTOK / 1_000_000


def normalize_usage(usage: dict) -> dict:
    """Accept both usage conventions and always expose snake_case.

    TypeSafe direct answers with `input_tokens`; OpenRouter may answer with
    camelCase `inputTokens`. Both are documented as accepted, so normalise once
    here instead of sprinkling `.get()` fallbacks through the code.
    """
    out = dict(usage or {})
    for camel, snake in (("inputTokens", "input_tokens"),
                         ("outputTokens", "output_tokens"),
                         ("totalTokens", "total_tokens")):
        if camel in out:
            out.setdefault(snake, out[camel])
    out.setdefault("input_tokens", 0)
    out.setdefault("output_tokens", 0)
    return out


def resolve_cost(usage: dict, model: str = "", price: float = INPUT_USD_PER_MTOK) -> tuple[float, str]:
    """-> (usd, source). Prefer the provider's own number over our price table.

    `source` is "provider" when `usage.cost` was present, else "estimate", and is
    recorded in the response so a later audit can tell the two apart.
    """
    if usage.get("cost") is not None:
        return float(usage["cost"]), "provider"
    return int(usage.get("input_tokens", 0)) * price / 1_000_000, "estimate"


# --------------------------------------------------------------------------- #
# Clients
# --------------------------------------------------------------------------- #
class OfficialJevClient(JevClient):
    """TypeSafe direct endpoint. Reads TYPESAFE_API_KEY unless a key is passed."""

    backend_name = "official"
    key_env = "TYPESAFE_API_KEY"

    def __init__(
        self,
        api_key: str | None = None,
        model: str = DEFAULT_MODEL,
        endpoint: str = ENDPOINT,
        timeout: float = 10.0,
        max_retries: int = 3,
        backoff: float = 0.5,
    ) -> None:
        self.api_key = api_key or os.environ.get(self.key_env, "")
        if not self.api_key:
            raise JevApiError(
                f"no API key: set {self.key_env} or pass api_key=. "
                "Use JEV_BACKEND=mock to develop offline."
            )
        self.model = model
        self.endpoint = endpoint
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff = backoff
        self.total_cost_usd = 0.0
        self.calls = 0

    # -- transport (overridable in tests) ---------------------------------- #
    def _post(self, payload: dict) -> dict:
        request = urllib.request.Request(
            self.endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "User-Agent": "jev-spire-brain/0.1",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _build_payload(self, state: str | dict | list, questions: dict[str, QuestionSpec]) -> dict:
        return {"state": state, "model": self.model, "questions": build_questions_json(questions)}

    def _extract(self, body: dict) -> tuple[dict, dict, str]:
        """-> (answers_obj, usage, model). Overridden by the Cloudflare subclass."""
        return body.get("answers", {}), body.get("usage", {}), body.get("model", self.model)

    # -- public API -------------------------------------------------------- #
    def ask(self, state: str | dict | list, questions: dict[str, QuestionSpec]) -> JevResponse:
        payload = self._build_payload(state, questions)
        last_err: Exception | None = None
        for attempt in range(self.max_retries + 1):
            t0 = time.perf_counter()
            try:
                body = self._post(payload)
            except urllib.error.HTTPError as err:  # noqa: PERF203
                detail = err.read().decode("utf-8", "replace")[:300] if err.fp else ""
                if err.code == 402:
                    raise JevApiError(f"402 no credits loaded: {detail}") from err
                if err.code in TRANSIENT_STATUS and attempt < self.max_retries:
                    last_err = err
                    time.sleep(self.backoff * (2**attempt))
                    continue
                raise JevApiError(f"HTTP {err.code}: {detail}") from err
            except (urllib.error.URLError, TimeoutError, OSError) as err:
                if attempt < self.max_retries:
                    last_err = err
                    time.sleep(self.backoff * (2**attempt))
                    continue
                raise JevApiError(f"network failure: {err}") from err

            latency_ms = int((time.perf_counter() - t0) * 1000)
            answers_obj, usage, model = self._extract(body)
            usage = normalize_usage(usage)
            self.calls += 1
            cost, cost_source = resolve_cost(usage, model)
            self.total_cost_usd += cost
            return JevResponse(
                answers=parse_answers(answers_obj, questions),
                latency_ms=latency_ms,
                backend=self.backend_name,
                cost_usd=cost,
                model=model,
                usage={**usage, "cost_source": cost_source},
            )
        raise JevApiError(f"exhausted retries: {last_err}")


class CloudflareJevClient(OfficialJevClient):
    """Cloudflare AI gateway. Model id `typesafe/jev`, 32K context.

    Same question payload, wrapped in `input`; the response is enveloped, so
    answers live under `result.result`.
    """

    backend_name = "cloudflare"

    def __init__(self, api_key: str | None = None, account_id: str | None = None, **kw: Any) -> None:
        account_id = account_id or os.environ.get("CLOUDFLARE_ACCOUNT_ID", "")
        api_key = api_key or os.environ.get("CLOUDFLARE_API_TOKEN", "")
        if not account_id:
            raise JevApiError("set CLOUDFLARE_ACCOUNT_ID (or pass account_id=)")
        kw.pop("model", None)
        super().__init__(api_key=api_key, model="typesafe/jev",
                         endpoint=CLOUDFLARE_ENDPOINT.format(account_id=account_id), **kw)

    def _build_payload(self, state: str | dict | list, questions: dict[str, QuestionSpec]) -> dict:
        return {
            "model": self.model,
            "input": {"state": state, "questions": build_questions_json(questions)},
        }

    def _extract(self, body: dict) -> tuple[dict, dict, str]:
        inner = body.get("result", body)
        if "result" in inner and isinstance(inner["result"], dict):
            inner = inner["result"]
        if not inner.get("success", True) and "answers" not in inner:
            raise JevApiError(f"cloudflare error: {json.dumps(body)[:300]}")
        return inner.get("answers", {}), inner.get("usage", {}), inner.get("model", self.model)


class OpenRouterJevClient(OfficialJevClient):
    """Real JEV, routed through OpenRouter. ← the path this project actually uses.

    Verified live 2026-09-21 with an OpenRouter key whose account policy allows
    only the `typesafe` provider:

        POST https://openrouter.ai/api/v1/systemone
        {"model": "jev-1.13", "state": ..., "questions": {...}}
        -> {"model": "typesafe/jev-1.13-20260917", "provider": "TypeSafe",
            "answers": {...}, "usage": {"input_tokens":…, "output_tokens":…,
                                        "cost": 0.000015204},
            "id": "gen-dec-…"}

    Same request/response shape as TypeSafe direct, plus `id`, `provider` and a
    provider-computed `usage.cost` that we prefer over our own price table.

    GOTCHA, and it costs an hour if you miss it: JEV is NOT callable through
    OpenRouter's chat completions endpoint. `POST /api/v1/chat/completions` with
    any of `typesafe/jev`, `typesafe/jev-latest`, `typesafe/jev-1.13` answers
    `400 "... is not a valid model ID"`. Only the System One / decisions route
    works. OpenRouter also exposes `POST /api/alpha/decisions` with the alias
    `~typesafe/jev-latest`; both were verified working, and this client uses the
    SDK-compatible `/api/v1/systemone` form because it is the documented shape.

    Model ids: bare `jev-1.13` is mapped by OpenRouter onto `typesafe/…`;
    `typesafe/jev-1.13` is accepted as-is. A pinned `~typesafe/jev-1.13.0` 404s —
    only the family alias `~typesafe/jev-latest` exists.
    """

    backend_name = "openrouter-jev"
    key_env = "OPENROUTER_API_KEY"

    def __init__(
        self,
        api_key: str | None = None,
        model: str = "jev-1.13",
        endpoint: str = OPENROUTER_SYSTEMONE,
        timeout: float = 60.0,
        **kw: Any,
    ) -> None:
        super().__init__(api_key=api_key, model=model, endpoint=endpoint, timeout=timeout, **kw)
