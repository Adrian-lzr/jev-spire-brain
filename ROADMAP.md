# Jev Spire Brain Roadmap

Legend: `[x]` done · `[~]` done but unreachable until an external condition is met · `[ ]` open

## Phase 0 — Skeleton
- [x] Repository & architecture docs
- [x] Project scaffolding (package layout, tests)
- [ ] Confirm spirecomm dependency strategy (pinned fork vs upstream) — needs a live game to version-check against

## Phase 1 — Wire up the pipe (game ↔ agent)
- [ ] Install & configure ModTheSpire + BaseMod + CommunicationMod (guide: docs/SETUP.md)
- [ ] spirecomm handshake: receive game state, send first action (all conservative rules)
- [ ] Run one full ascent on A0 with rule-only fallback agent
- [~] `jev_brain/state.py` — game state → JEV state digests (written against the documented spirecomm schema; needs live data to confirm field names)

## Phase 2 — The brain (offline-first)
- [x] `jev_brain/client.py` — three primitives, specs, answers, confidence conventions
- [x] `jev_brain/client_real.py` — real HTTP client (TypeSafe direct + Cloudflare), retry, cost, response parsing
- [x] `MockJevClient` / `ScriptedJevClient` — develop and test without an API key
- [x] All 7 decision points wired, each with a confidence threshold and a rule fallback
- [x] Log every call: state digest / questions / answers / probabilities / confidence / latency / cost
- [x] Response parsing pinned to the published API response in tests (no invented field names)

## Phase 3 — HP budget system (the soul)
- [x] `tactical/hp_budget.py` — per-Act budget, spend/restore accounting (pure functions + unit tests)
- [x] Map routing uses parallel Noul probes: "does this path exceed remaining budget?"
- [x] Combat risk gate consults the same budget for posture selection
- [ ] Post-combat Score review of damage-taken reasonableness (needs live combat data)

## Phase 4 — Combat tactics
- [x] Greedy card-play policy (play cards, end turn) — `tactical/combat_greedy.py`
- [ ] Optional: scumthespire-style search with STSStateSaver rollbacks

## Phase 5 — Real JEV + evaluation
- [x] Real client implemented; switch with `JEV_BACKEND=openrouter` (real JEV via OpenRouter's System One route)
- [x] **A full ascent has been run against real JEV**: 24 calls, $0.000528, p50 1435 ms — see docs/JEV_API.md
- [x] `analysis/calibration.py` — confidence-bucket report + fallback ratio + ECE stub
- [x] `analysis/inspect_log.py` — read back what JEV actually answered, per question
- [ ] **Enrich the decision states** (the actual blocker, see below)
- [ ] Run ≥50 ascents, collect stats (win rate, avg death floor, decisions log)
- [ ] Compare: JEV brain vs. random baseline vs. greedy baseline vs. structured-LLM baseline
- [ ] Resolve whether a missing `confidence` field is being read as 0.0 (flagged in the log as `confidence_present`)
- [ ] Reconcile measured latency/cost with vendor claims (both now have real numbers to check against)

## The finding that resets the plan

The first real run returned **61 of 63 answers below the 0.60 confidence floor**,
with every module falling back to its safe rule. Diagnosed from the recorded
payloads: this is not a model problem, it is a **state problem**. We asked
"is this worth the gold for this run's goal?" while passing `{goal, gold}` — no
deck, no relics, no HP — and JEV answered ≈0.4, which is its documented way of
saying *this cannot be answered from what you gave me*.

`state.py` already builds full run digests (deck contents, relics, potions, HP
ratio, act, floor, goal). The decision modules do not use it yet; each one
hand-builds a thinner state. **Wiring the rich digest into all seven decision
points is now the highest-value work in the project** — everything downstream
(the calibration curve, the win-rate comparison, the whole "worth showing"
list) is blocked behind it.

## Definition of "worth showing"
- Agent completes 10 consecutive runs unattended
- Calibration curve published (independent evaluation angle the JEV community currently lacks)
- Demo GIF in README

## What is honestly blocking what
| Blocker | Blocks | Whose move |
|---|---|---|
| CommunicationMod not installed (ModTheSpire + BaseMod + StSLib are in place via Steam Workshop) | Phase 1 entirely, and Phase 3's post-combat review | user downloads 1 jar |
| Decision states too thin | everything in Phase 5, calibration included | me — next turn |
| OpenRouter key permits only the `typesafe` provider | the structured-LLM baseline arm | user (add a provider) or a second key |

