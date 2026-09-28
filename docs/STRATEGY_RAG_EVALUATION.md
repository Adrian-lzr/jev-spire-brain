# Strategy RAG evaluation

The repository now has a deterministic, offline audit for the local strategy
index:

```text
python -m spirebrain.analysis.rag_eval
python -m spirebrain.analysis.rag_eval --input spirebrain/data/strategy_knowledge.jsonl --json logs/rag_audit.json
```

The audit checks four things that can be established without a game or a model:

- expected situation queries retrieve their cited source IDs;
- every returned citation preserves the source metadata from the index;
- unsupported characters receive no Ironclad claims;
- retrieved context contains no action fields that could be mistaken for a
  command or candidate identity.

The fixed query suite is a coverage smoke test, not an expert answer key. A
100% expected hit rate means only that each authored situation has at least one
intended source in the lexical index. It does not mean the selected action is
correct, that the community claim is true, or that a run will win.

The current corpus contains eight bounded paraphrases from four public guide
URLs and is Ironclad-only. The audit therefore reports `quality.status` and
`win_rate` as `unknown` until versioned expert labels or paired real-game
outcomes are collected. No network request, API key, provider call or game
configuration is used by the command.

## Interpreting the report

`retrieval.expected_coverage_rate` measures retrieval wiring and corpus
coverage. `integrity.citation_provenance_rate` measures whether returned source
metadata still agrees with the indexed chunk. These are separate from
`candidate_legality`, player adoption and real outcomes in the regular replay
metrics. They must not be added together or presented as a win-rate estimate.

For a meaningful strategy comparison, keep the same replay states and compare
RAG enabled and disabled arms, then add blinded expert labels or paired seeds.
Until that evidence exists, RAG is a bounded planning hint and local card data,
candidate generation, hard rules and `check_action()` remain authoritative.
