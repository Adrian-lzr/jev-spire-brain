# JEV API — verified reference and backend wiring

Everything on this page was checked against primary sources on **2026-09-21**:
`docs.typesafe.ai` (introduction, primitives, confidence, quickstart) and the
Cloudflare AI docs page for `typesafe/jev`. Anything not verified is marked as
such. Do not let this file drift from the code: `spirebrain/jev_brain/client.py`
and `client_real.py` are the implementation of what is written here.

## Endpoints — four routes, one shape

| Route | URL | Auth | Model id | Notes |
|---|---|---|---|---|
| **OpenRouter (used here)** | `POST https://openrouter.ai/api/v1/systemone` | `OPENROUTER_API_KEY` | `jev-1.13` | ✅ **verified live 2026-09-21.** Adds `id`, `provider`, and `usage.cost` |
| TypeSafe direct | `POST https://api.typesafe.ai/v1/systemone` | `TYPESAFE_API_KEY` | `jev-latest` | key from `console.typesafe.ai`, waitlisted |
| Cloudflare AI | `POST .../accounts/$ID/ai/run` | `CLOUDFLARE_API_TOKEN` | `typesafe/jev` | 32K ctx; answers wrapped under `result.result` |
| Vercel AI Gateway | via gateway SDK | gateway key | `typesafe-ai/jev` | documented by Vercel, not implemented here |

`OpenRouterJevClient` is what this project uses (`JEV_BACKEND=openrouter`).
`CloudflareJevClient` is implemented but unexercised; the TypeSafe-direct client is
identical in shape and only differs by host and key.

### ⚠️ The OpenRouter gotcha that costs an hour

**JEV is not callable through OpenRouter's chat completions endpoint.** All of
these return `400 "... is not a valid model ID"`:

```
POST /api/v1/chat/completions   {model: "typesafe/jev"}          -> 400
POST /api/v1/chat/completions   {model: "typesafe/jev-latest"}   -> 400
POST /api/v1/chat/completions   {model: "typesafe/jev-1.13"}     -> 400
GET  /api/v1/models/typesafe/jev/endpoints                       -> 404
```

JEV is absent from the public `/api/v1/models` listing (446 models on
2026-09-21, no match for `jev`/`typesafe`/`systemone`) — it is reachable but
unlisted. Only the **System One / decisions route** serves it. OpenRouter also
exposes `POST /api/alpha/decisions` with the alias `~typesafe/jev-latest`; both
were verified working, and this repo uses `/api/v1/systemone` because it is the
documented, SDK-compatible shape.

Model-id quirks: bare `jev-1.13` is mapped by OpenRouter onto `typesafe/…`;
`typesafe/jev-1.13` is accepted as-is; the **family alias** `~typesafe/jev-latest`
exists but a pinned `~typesafe/jev-1.13.0` 404s.

### Another gotcha: the account's provider policy

OpenRouter accounts can restrict which providers they will route to, and the
restriction is enforced with a confusing 404 rather than a 403:

```json
{"error":{"message":"No allowed providers are available for the selected model.
Providers serving qwen/qwen3.7-flash: alibaba, but your account's allowed-providers
setting permits only: typesafe.","code":404}}
```

The account this project was developed against permits **only `typesafe`** — which
is why it can call JEV but nothing else. Practical consequence: the structured-LLM
*baseline* client (`JEV_BACKEND=llm`) is code-complete but unrunnable on that key
until a provider is allowed at <https://openrouter.ai/settings/privacy>.

### What a live call actually returned

Verified 2026-09-21, first real run of the project against JEV:

```
POST https://openrouter.ai/api/v1/systemone
{"model":"jev-1.13", "state":"Help! My payouts have been failing for 3 days.", "questions":{…}}

-> {"model":"typesafe/jev-1.13-20260917",
    "provider":"TypeSafe",
    "answers":{"is_urgent":{"type":"noul","noul":0.95},
               "department":{"type":"choice","choice":"billing",
                             "probabilities":{"technical":0.11,"billing":0.89,"sales":0},
                             "confidence":0.84}},
    "usage":{"input_tokens":362,"output_tokens":57,"cost":0.000015204},
    "id":"gen-dec-1789991218-vznpx4vtJonKuiIRyLlv"}
```

Three things this settles that the docs alone did not:

1. `usage.cost` is **provider-computed in USD**. Use it; our price-table estimate
   (`estimate_cost_usd`) is only a fallback. `client_real.resolve_cost` prefers the
   provider's number and records which source was used in `usage.cost_source`.
2. Usage keys came back **snake_case** on this route (`input_tokens`), though docs
   say camelCase may appear. `normalize_usage` accepts both.
3. Latency was **1135–1620 ms** per call (p50 ≈ 1435 ms) from this machine, against
   TypeSafe's US-West claim of 70–500 ms. This matches an independent third-party
   measurement of 0.5–1.4 s via OpenRouter from Asia. Attribute latency in reporting;
   do not repeat the vendor's 70–500 ms figure as if it were measured here.


## Request body

```json
{
  "state": "…or a JSON object / array…",
  "model": "jev-latest",
  "questions": {
    "question_name": {
      "type": "noul | choice | score",
      "instructions": "…",
      "criteria": "…depends on type…"
    }
  }
}
```

`state` may be a string, an object, or an array. **Prefer an object with named
parts** when the context has distinct components — that is what
`spirebrain/jev_brain/state.py` builds. Jev is text-only (no images/audio/video)
and English-first; the docs warn other languages are accepted with lower
accuracy.

## The three primitives

| type | criteria | returns |
|---|---|---|
| `noul` | optional `{"true": "…", "false": "…"}` | `{"noul": 0.26}` — **no separate confidence; the number is the belief** |
| `choice` | `{label: description}` | `{"choice": "billing", "probabilities": {...}, "confidence": 0.8}` |
| `score` | `["lowest", …, "highest"]` — an **ordered list of word-labelled levels** | `{"score": 1.04, "legend": {...}, "probabilities": {...}, "confidence": 0.94}` |

All three may be mixed in one call. Every question is evaluated **in parallel and
in isolation** against the same state, and "adding questions barely changes the
response time". That is why every decision module in this repo batches its
questions into a single call instead of looping.

Score answers may land **between** levels — the docs' own example is `1.04`,
between "Frustrated but civil" and "Very angry".

### Correction we had to make

An earlier version of this repo modelled Score as a numeric range
(`rubric=(0.0, 100.0)`). **No such field exists.** The correct shape is the
ordered level list above. We now normalise the returned level to 0..1 via
`level / (len(criteria) - 1)` so call sites can compare against one threshold,
and keep the raw level plus legend in `JevAnswer.raw`. The 0..1 mapping is our
own convention, not a JEV field — it is documented in `client.py` precisely so
nobody later mistakes it for one.

For Noul, `JevAnswer.value` and `JevAnswer.confidence` are the *same number*.
That is deliberate: it makes Noul directly calibration-testable — statements
answered 0.95 should be true about 95% of the time.

## Response body

```json
{
  "model": "jev-1.13.0",
  "answers": { "…": { "type": "…", "…": "…" } },
  "usage": { "input_tokens": 426, "output_tokens": 73 }
}
```

Cloudflare wraps this: answers live under `result.result`.

## Cost

Jev bills **input tokens only** at the early-access rate of **$0.042 per million
input tokens**; the output line is priced at zero. `client_real.py` accounts on
that basis and is deliberately unrounded — these amounts are ~1e-5, so rounding
to 8 decimals would discard real precision. Verify against your own console
invoice before trusting the arithmetic; the constant lives at the top of
`client_real.py` with the date it was checked.

To put a whole simulated run in perspective: the offline harness makes 24 calls
per ascent at ~500 input tokens each, which is on the order of **$0.0005 per
run** — the cost regime that makes "run 10,000 ascents and draw a calibration
curve" a normal thing to do rather than a research budget.

## Confidence, and how we treat it

Confidence summarises how peaked the answer distribution is. The docs are
explicit that the threshold above which to automate is **not** derivable from the
model — it has to be chosen against the risk of the specific call and validated
on your own labelled data. Our choices, all in `decisions.py`:

- `CONFIDENCE_FLOOR = 0.60` — for Choice and Score, below this we fall back to a rule.
- `NOUL_UNCERTAIN_BAND = (0.40, 0.60)` — for Noul, inside this band means "cannot tell".
- `SCORE_ACTION_FLOOR = 0.55` — a card or upgrade must clear this to be taken at all.

The docs' own worked example is the reason for the first rule: a Choice answered
`billing` at p=0.84 with confidence 0.596 was the model reporting an
*under-specified question*, not a confident answer.

## Wiring it up

```bash
# real JEV — the route this project uses
export JEV_BACKEND=openrouter
export OPENROUTER_API_KEY=...

# offline, no key, no network: deterministic mock
export JEV_BACKEND=mock

# structured-LLM baseline (NOT JEV) — needs a non-restricted key, see gotcha above
export JEV_BACKEND=llm

# other real-JEV routes (different accounts)
export JEV_BACKEND=official    # + TYPESAFE_API_KEY
export JEV_BACKEND=cloudflare  # + CLOUDFLARE_ACCOUNT_ID / CLOUDFLARE_API_TOKEN
```

`JEV_BACKEND` is read by `get_client()`. A missing key raises an explicit
`JevApiError` naming the environment variable rather than failing obscurely at
the first call. HTTP 402 (no credits) and non-429 4xx are **not** retried: they
are not transient. 429/5xx/socket errors are retried with exponential backoff.

Run the whole pipeline against real JEV:

```bash
python -m spirebrain.sim.run_offline --backend=openrouter
python -m spirebrain.analysis.inspect_log --summary   # what JEV actually said
python -m spirebrain.analysis.calibration logs
```

## First real run: what we learned (2026-09-21)

The first end-to-end ascent against real JEV cost **$0.00052836** for 24 calls
(avg $0.000022). Results that matter more than the cost:

| Observation | Number | What it means |
|---|---|---|
| Calls | 24 (3 acts × 8 decision points) | the full pipeline works against the real model |
| Latency | 1135–1620 ms, p50 1435 ms | ~3× slower than the vendor's claim from our network |
| Answers below the 0.60 confidence floor | **61 of 63** | JEV says our questions are under-specified |
| Rule fallbacks fired | 7/8, 8/8, 8/8 per act | on thin state, the safe path *is* the answer |

Representative answers, taken verbatim from `logs/jev_calls.jsonl`:

```
#22  state={"goal":"ascension_20_win","gold":300}
       buy_Ornamental Fan   noul  p(yes)=0.36
       buy_Meat on the Bone noul  p(yes)=0.40
       buy_Card Removal     noul  p(yes)=0.42

#23  state={"deck":"Bash (2E) - Attack; Defend (1E) - Skill; …"}
       Philosopher's Stone  score level=0.76  confidence=0.24
       Runic Dome           score level=1.04  confidence=0.000
       Coffee Dripper       score level=1.26  confidence=0.000
```

Reading these correctly matters, and the honest reading is: **the bottleneck is
our state, not the model.** The shop question asked "is this worth the gold for
this run's goal?" while passing only `{goal, gold}` — no deck, no HP, no relics.
JEV answered ~0.4, which is the documented way of saying *this question cannot be
answered from what you gave me*. The project's existing `state.py` builds far
richer digests; the decision modules simply do not use them yet. Enriching those
states is the next milestone, and it is the difference between a brain that
falls back 8/8 and one that actually steers.

### Open item: missing vs zero confidence

In call #23 two Score answers reported `confidence=0.000` while a sibling in the
same call reported `0.24`. A bare `.get("confidence", 0.0)` cannot distinguish
"the model said zero" from "the field was absent". `parse_answers` now records
`confidence_present` alongside every raw answer so the next run settles it from
recorded bytes rather than guesswork. **Do not read `0.000` as a calibrated zero
until that is resolved.**


## Unverified / open

- The early-access rate of $0.042/Mtok comes from press and third-party coverage
  of the launch, not from a pricing page we could fetch on 2026-09-21. Treat it
  as indicative.
- Rate limits are not documented in the pages we read.
- Actual latency from this machine is unknown until a key exists; the offline
  logs record `latency_ms` so the first real run will answer it.
