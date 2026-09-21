# JEV API — verified reference and backend wiring

Everything on this page was checked against primary sources on **2026-09-21**:
`docs.typesafe.ai` (introduction, primitives, confidence, quickstart) and the
Cloudflare AI docs page for `typesafe/jev`. Anything not verified is marked as
such. Do not let this file drift from the code: `spirebrain/jev_brain/client.py`
and `client_real.py` are the implementation of what is written here.

## Endpoint

| | |
|---|---|
| URL | `POST https://api.typesafe.ai/v1/systemone` |
| Auth | `Authorization: Bearer $TYPESAFE_API_KEY` |
| Key from | `console.typesafe.ai` (early access is waitlisted) |
| Model | `jev-latest` |
| Content | `application/json` |

Three ways to reach Jev are documented: TypeSafe direct (above), Cloudflare AI
(`typesafe/jev`, 32K context, `env.AI.run('typesafe/jev', {state, questions})`),
and the Vercel AI Gateway model id `typesafe-ai/jev`. This repo implements the
first two — see `OfficialJevClient` and `CloudflareJevClient`.

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
# offline, no key, no network: deterministic mock
export JEV_BACKEND=mock
python -m spirebrain.sim.run_offline

# real JEV, TypeSafe direct
export JEV_BACKEND=official
export TYPESAFE_API_KEY=...

# real JEV, via Cloudflare
export JEV_BACKEND=cloudflare
export CLOUDFLARE_ACCOUNT_ID=...
export CLOUDFLARE_API_TOKEN=...
```

`JEV_BACKEND` is read by `get_client()`. A missing key raises an explicit
`JevApiError` naming the environment variable rather than failing obscurely at
the first call. HTTP 402 (no credits) and non-429 4xx are **not** retried: they
are not transient. 429/5xx/socket errors are retried with exponential backoff.

## Unverified / open

- The early-access rate of $0.042/Mtok comes from press and third-party coverage
  of the launch, not from a pricing page we could fetch on 2026-09-21. Treat it
  as indicative.
- Rate limits are not documented in the pages we read.
- Actual latency from this machine is unknown until a key exists; the offline
  logs record `latency_ms` so the first real run will answer it.
