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
- [x] **Three ascents run against real JEV** — see the experiment table in docs/JEV_API.md
- [x] `analysis/calibration.py` — confidence-bucket report + fallback ratio + ECE stub
- [x] `analysis/inspect_log.py` — read back what JEV actually answered, per question
- [x] **Rich decision states** (`RunContext`): deck, relics, potions, HP budget, act, floor, gold, goal
- [x] **Distribution-based acceptance** for Choice/Score: fallbacks 8/8 → 5/8; the brain now picks routes and refuses costly events on its own
- [ ] **Make Score decisions discriminate** — card rewards still always skip (deck 10 → 10). See the three candidate experiments below
- [ ] Decide whether `confidence=0.000` on some Score answers means zero or absent (log now records `confidence_present`)
- [ ] Decide whether the remaining 5/8 fallbacks are the floor being right (genuinely hard calls) or still too strict
- [ ] Run ≥50 ascents, collect stats (win rate, avg death floor, decisions log)
- [ ] Compare: JEV brain vs. random baseline vs. greedy baseline vs. structured-LLM baseline
- [ ] Wire `RunContext` into `driver/agent.py` so live play gets rich states too (currently only the simulator passes one)

## What the measurements changed about our beliefs

Two hypotheses were tested against the real model and one of them failed:

1. **"Thin states cause the low confidences."** ❌ **Refuted.** Enriching every
   decision to the full run digest left the score at 61/63 below the floor and
   raised input cost ~48%. Confidence on *preference* questions is a flatness
   statistic, not a correctness signal; a model told everything still has no
   sharp answer to "which of these three cards is better".
2. **"A 0.60 confidence floor is the right gate."** ❌ **Miscalibrated.** It made
   the brain never act — 8/8 fallbacks, i.e. an expensive rule-based bot. The
   type docs warn that this threshold must be chosen per-domain against risk.
   Replacing it for Choice/Score with a distribution test (top option is the most
   likely, clears 0.50, leads by 0.15) cut fallbacks to 5/8 and produced the first
   genuinely model-chosen actions: a route and a refusal of a costly event.

Both were invisible until the real model was called. That is the argument for
running the expensive-looking experiment early.

## Three cheap experiments queued for the Score problem

Card rewards and boss relics still never discriminate, so the deck never grows.
In ascending order of effort:

1. **Coarser rubric** — collapse the 4-level CARD_RUBRIC to 2 levels ("does not
   help" / "clearly helps"). Fewer levels means a more peaked distribution.
2. **Different primitive** — ask three separate Noul questions per card
   ("does this fix a weakness the deck has?", "does it duplicate something the
   deck already does well?") and combine in code. The docs recommend decomposing
   a multi-factor judgment into atomic questions, which is exactly this case.
3. **Accept that this may be a real limit** — if neither helps, the honest
   conclusion is that JEV cannot rank cards from a text digest, and the card
   policy should stay a rule while JEV keeps the decisions it demonstrably owns
   (routing, event consequences, HP-budget risk).

## Definition of "worth showing"
- Agent completes 10 consecutive runs unattended
- Calibration curve published (independent evaluation angle the JEV community currently lacks)
- Demo GIF in README

## What is honestly blocking what
| Blocker | Blocks | Whose move |
|---|---|---|
| CommunicationMod not installed (ModTheSpire + BaseMod + StSLib are in place via Steam Workshop) | Phase 1 entirely, and Phase 3's post-combat review | user downloads 1 jar |
| Score answers carry no discriminative signal | a deck that grows, and therefore any win-rate comparison | me — 3 experiments above |
| OpenRouter key permits only the `typesafe` provider | the structured-LLM baseline arm | user (add a provider) or a second key |


