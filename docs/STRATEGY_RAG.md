# Strategy RAG knowledge base

The strategic provider now receives a small offline retrieval context for
Ironclad planning nodes. It is deliberately not a card database and it does
not produce commands. Local game data, candidate generation, `check_action()`
and hard guide rules remain authoritative.

## Sources collected

The repository stores short paraphrased claims, not full article text, in
`spirebrain/data/strategy_knowledge.jsonl`. Each row keeps its public URL,
collection date, game ID/version, character scope, claim type, evidence label,
license, applicability conditions, exceptions and a content hash. Strategy
heuristics and deterministic game-mechanic facts are separate claim types;
neither creates a command or bypasses local legality.

- [Forgotten Arbiter Ironclad Win Streak Guide](https://gameplay.tips/guides/1855-slay-the-spire.html): defensive framework, compact deck, resource and upgrade context.
- [Panacea A20 Ironclad Guide](https://gameplay.tips/guides/slay-the-spire-ironclad-guide-high-winrate-ascension-20-heart-killing.html): Act 1 HP/resource budget and conditional tier-list guidance.
- [FantasyWarden Ironclad Builds](https://fantasywarden.com/games/slay-the-spire-ironclad-builds): adaptive pivots among Strength, Block, Exhaust and hybrid signals.
- [dood Ironclad Guide](https://www.dood.gg/en/slay-the-spire/guides/ironclad-guide/): starter-deck foundation and timing of Barricade-style plans.
- [Slay the Spire Wiki](https://slaythespire.wiki.gg/wiki/Ironclad): four short, independently paraphrased Ironclad mechanics for Strength, Vulnerable, Weak and Burning Blood. The Strength page also supports one explicitly labeled strategy heuristic: compare a Strength source with multi-hit attacks because Strength modifies each hit. That heuristic is a deduction from a mechanic, not a strategy-guide recommendation or a measured outcome.

The claims are community strategy evidence, not measured win-rate labels. Card
numbers and action legality come from the installed game data and local rules.
Eight guide/article summaries currently have `license_status: unknown`; their
source terms were not independently verified. They are marked as single-source
strategy rather than consensus. The audit reports `redistribution_ready: false`
until rights are reviewed. The four wiki mechanism summaries retain their
CC BY-SA 4.0 attribution. This status is provenance bookkeeping, not legal
advice or proof that an unknown source may be republished.

The retrieval index is deterministic lexical TF-IDF-like scoring, filtered by
exact game ID and version, so offline replay is reproducible and no runtime
network request is needed. Broad queries need at least two content anchors;
single-anchor retrieval is reserved for recognized mechanic facts. Ambiguous
one-term queries abstain unless the caller narrows the claim type or only one
chunk matches. An explicit Slay the Spire 2 or unknown-version state receives
no Slay the Spire 1 claims.
Wiki mechanics are marked `mechanic_fact`, scoped to `slay-the-spire-1`, and
include prerequisites and exceptions. Wiki pages can include later-game
material, so these paraphrases are not a replacement for local game data or a
real-version compatibility check. The derived Strength heuristic carries the
same attribution/license metadata and is never presented as validated player
advice.

The MIT-licensed [`sts-rl-agent`](https://github.com/Jialeiv/sts-rl-agent/tree/039c199933b7d3a2e5d8fde51ae77a9175e1031f)
was reviewed as a methodology reference, not ingested as player-strategy
knowledge. Its [evaluation notes](https://github.com/Jialeiv/sts-rl-agent/blob/039c199933b7d3a2e5d8fde51ae77a9175e1031f/docs/evaluation.md)
describe fixed-seed comparisons and explicitly discuss seed leakage and
validation-set limits. Its policy implementation does not expose a small,
source-grounded Ironclad heuristic suitable for this corpus, so no claim or
performance result from that project is copied into runtime advice.

## Runtime contract

`StrategicPlanner` adds a bounded `knowledge_context` field to strategic model
payloads. It contains only a few claims and citation IDs. The model may use it
as a soft planning hint, but it cannot introduce a command, candidate ID or
unverified numeric card effect. Other characters receive an explicit
`unsupported_character` result rather than Ironclad advice.

The default `config/strategy.json` value is `brain.knowledge_enabled: true`.
Set it to `false` for a same-input control run; this removes retrieved claims
from the strategic payload without changing rules, candidates, JEV or budgets.

Run an offline query with:

```text
python -m spirebrain.knowledge.rag --query "Ironclad Act 1 HP elite block"
```

Use `python -m spirebrain.analysis.evaluate --input
tests/fixtures/replay/advisor_states.json --knowledge both` to run the same
synthetic input with retrieval enabled and disabled. The report exposes
`knowledge_citation_count`; it deliberately does not claim that either arm is
stronger without expert labels or real game outcomes.

Future real-game calibration must compare retrieved versus non-retrieved
strategy stacks on the same seeds and report scene-level quality separately
from win rate. No win-rate improvement is claimed by this knowledge base alone.
