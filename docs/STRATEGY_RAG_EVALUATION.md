# Strategy RAG evaluation

The repository now has a deterministic, offline audit for the local strategy
index:

```text
python -m spirebrain.analysis.rag_eval
python -m spirebrain.analysis.rag_eval --input spirebrain/data/strategy_knowledge.jsonl --json logs/rag_audit.json
```

The audit checks four things that can be established without a game or a model:

- expected situation queries retrieve their cited source IDs;
- retrieved IDs are compared to manually reviewed per-query allowlists, so
  unrelated citations lower `retrieval.relevance_precision` even when every
  expected source appears;
- every returned citation preserves source metadata, claim type, game scope,
  license and content hash;
- unsupported characters receive no Ironclad claims;
- negative cases for another character, another game ID and another game
  version receive no citations;
- same-character negative queries for uncovered shop-price details,
  self-damage interactions and exact unindexed relic mechanics abstain instead
  of borrowing a loosely related strategy claim;
- retrieved context contains no action fields that could be mistaken for a
  command or candidate identity;
- content-hash and provenance fields are checked, while unverified license
  terms are surfaced separately as `redistribution_ready: false`.

The fixed query suite uses manually curated expected and allowed citation IDs.
This is a regression oracle for retrieval scope, not an expert answer key. A
100% expected hit rate with lower `relevance_precision` is a failing audit, not
a success. Even 100% expected coverage and relevance precision do not mean the
selected action is correct, that a community claim is true, or that a run will
win.

The current corpus contains thirteen bounded paraphrases from eight public
guide/wiki URLs and is Ironclad-only: eight guide summaries have unknown
redistribution terms, four Wiki.gg mechanics are marked CC BY-SA 4.0, and one
Strength/multi-hit heuristic is explicitly labeled as a deduction from that
mechanic rather than an empirically validated build rule. The audit therefore
reports `quality.status` and
`win_rate` as `unknown` until versioned expert labels or paired real-game
outcomes are collected. No network request, API key, provider call or game
configuration is used by the command.

Unknown-license records make `integrity.license_review.redistribution_ready`
false. Treat them as not approved for redistribution until the source terms are
reviewed; this flag is not a legal determination. The audit records source
metadata but does not fetch or verify external pages at runtime.

## Interpreting the report

`retrieval.expected_coverage_rate` measures expected-source retrieval;
`retrieval.relevance_precision` measures returned IDs against the per-query
allowlists; `retrieval.semantic_abstention` checks that currently uncovered
questions produce no citations. `integrity.citation_provenance_rate` measures
whether returned source metadata still agrees with the indexed chunk. These are separate from
`candidate_legality`, player adoption and real outcomes in the regular replay
metrics. They must not be added together or presented as a win-rate estimate.

For a meaningful strategy comparison, keep the same replay states and compare
RAG enabled and disabled arms, then add blinded expert labels or paired seeds.
Until that evidence exists, RAG is a bounded planning hint and local card data,
candidate generation, hard rules and `check_action()` remain authoritative.
