# Strategy RAG knowledge base

The strategic provider now receives a small offline retrieval context for
Ironclad planning nodes. It is deliberately not a card database and it does
not produce commands. Local game data, candidate generation, `check_action()`
and hard guide rules remain authoritative.

## Sources collected

The repository stores short paraphrased claims, not full article text, in
`spirebrain/data/strategy_knowledge.jsonl`. Each row keeps its public URL,
collection date, game version, character scope, trust label and claim ID.

- [Forgotten Arbiter Ironclad Win Streak Guide](https://gameplay.tips/guides/1855-slay-the-spire.html): defensive framework, compact deck, resource and upgrade context.
- [Panacea A20 Ironclad Guide](https://gameplay.tips/guides/slay-the-spire-ironclad-guide-high-winrate-ascension-20-heart-killing.html): Act 1 HP/resource budget and conditional tier-list guidance.
- [FantasyWarden Ironclad Builds](https://fantasywarden.com/games/slay-the-spire-ironclad-builds): adaptive pivots among Strength, Block, Exhaust and hybrid signals.
- [dood Ironclad Guide](https://www.dood.gg/en/slay-the-spire/guides/ironclad-guide/): starter-deck foundation and timing of Barricade-style plans.

The claims are community strategy evidence, not measured win-rate labels. Card
numbers and action legality come from the installed game data and local rules.
The retrieval index is deterministic lexical TF-IDF-like scoring so offline
replay is reproducible and no runtime network request is needed.

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
