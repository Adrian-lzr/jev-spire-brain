# Jev Spire Brain Roadmap

Legend: `[x]` done · `[~]` done but unverifiable until an external condition is met · `[ ]` open

> **Read [docs/MEASUREMENTS.md](docs/MEASUREMENTS.md) before quoting any number in
> this file.** Every measured claim carries its sample size there, and four of the
> beliefs this roadmap once asserted have since been refuted by our own runs.

## Phase 0 — Skeleton
- [x] Repository & architecture docs
- [x] Project scaffolding (package layout, tests)
- [~] Confirm spirecomm dependency strategy — we now speak the protocol directly
  (`driver/stdio.py`) instead of depending on `spirecomm`, so this is only about
  whether to keep the package in `requirements.txt` as a reference

## Phase 1 — Wire up the pipe (game ↔ agent)
- [x] **`driver/stdio.py`** — CommunicationMod stdio transport: `Ready`
  handshake, one command per state, `available_commands` whitelist, error
  recovery, pipe log. Verified against the mod's README (2026-09-21)
- [x] **`run_agent.py`** — root launcher: makes `import spirebrain` work without
  PYTHONPATH and loads `.env`, because the game spawns us with its own environment
- [x] Every command the router can emit uses a real protocol verb
  (`test_every_emitted_verb_is_a_real_protocol_verb`)
- [x] `--replay <dir>` — re-decide recorded states offline through the same path
- [x] `docs/SETUP.md` rewritten from verified facts: this machine's paths, the
  Workshop ids, the config location, the four traps
- [ ] **Install CommunicationMod** (Workshop 2131373661 or GitHub release) → first
  live smoke test. *This is the only thing standing between us and a playing agent.*
- [ ] Confirm on the live pipe: grid index order, shop shelf order, and that the
  `screen_state` field name holds (all flagged `PHASE 1 VERIFY` in code)

## Phase 2 — The brain (offline-first)
- [x] `jev_brain/client.py` — three primitives, specs, answers, confidence conventions
- [x] `jev_brain/client_real.py` — real HTTP client (TypeSafe direct + Cloudflare
  + OpenRouter System One), retry policy, cost accounting, response parsing
- [x] `MockJevClient` / `ScriptedJevClient` — develop and test without an API key
- [x] All 7 decision points wired, each with a gate and a rule fallback
- [x] Log every call: state / questions / answers / probabilities / confidence /
  latency / cost / `prompt_version` / `request_bytes`
- [x] Response parsing pinned to the published API response in tests
- [x] 24,000-byte request cap that fails loudly rather than truncating silently

## Phase 3 — HP budget system (the soul)
- [x] `tactical/hp_budget.py` — per-Act budget, spend/restore accounting
- [x] Map routing uses parallel Noul probes: "does this path exceed the budget?"
- [x] Combat risk gate consults the same budget for posture selection
- [ ] Post-combat Score review of damage-taken reasonableness (needs live combat)

## Phase 4 — Combat tactics
- [x] Greedy card-play policy — `tactical/combat_greedy.py`
- [ ] Optional: scumthespire-style search with STSStateSaver rollbacks

## Phase 5 — Real JEV + evaluation
- [x] Real client; `JEV_BACKEND=openrouter` reaches actual JEV
- [x] `analysis/calibration.py` (buckets + fallback ratio + ECE stub),
  `analysis/inspect_log.py`, `analysis/repeatability.py`
- [x] Rich decision states (`RunContext`) — in the simulator **and** in
  `driver/agent.py`, so live play gets the same digest and the game's own text
- [x] **Score gate is switchable** (`margin` / `argmax`) so the comparison is an
  experiment rather than a guess — see `decisions.evaluate_score`
- [x] `sim/batch.py` — run a seed group, aggregate cards/deck/HP/cost per ascent
- [x] Seed split adopted (`config/seeds.json`): calibration vs a **locked** test set
- [x] Repeatability measured, twice: 0 % flip rate under both gates
- [x] **Score gate settled by experiment** — `argmax` adopted (10/10 ascents took
  cards vs 1/10 under `margin`; fallbacks 0.537 → 0.411; 0 % flip rate; +2.3 %
  cost). Recorded in `config/strategy.json`, with `margin` still selectable
- [ ] Carry HP across acts in the simulator — it resets to 80 per act, so
  cumulative HP damage, the thing the HP-budget system reasons about, is currently
  unobservable. Pre-registered in docs/MEASUREMENTS.md, and it must land before
  `TEST_SEEDS` are spent
- [ ] Decide whether `confidence=0.000` on some Score answers means zero or absent
  (the log records `confidence_present`; nobody has read it back yet)
- [ ] Card *quality*, not just card *quantity* — the simulator has no ground truth,
  so this needs recorded live runs (`--replay`) or a labelled dataset
- [ ] Run ≥50 ascents; compare against random / greedy / structured-LLM baselines
- [ ] Converge `analysis/calibration.py` on the `jevcal` shape (pick the threshold
  that meets an accuracy target; fail CI when a model update breaks calibration)

## The instrument was lying (2026-09-21)

Ten ascents per arm produced **one identical HP trajectory** — 51, 47, 43 for every
seed — because the map was a fixed constant and the fallback always walks the
least damaging node, which was always a free rest node. A ten-run experiment had
the information content of a single run.

Fixed: seed-generated maps, seeded damage, `spent <= probe` recorded per step, and
`tests/test_sim_harness.py` to keep it fixed. The locked test set was **not** spent
on the broken harness.

The general lesson is worth more than the bug: *a measurement can be invalidated by
the instrument rather than by the hypothesis, and the only defence is a check that
the instrument can fail.*

## What the measurements changed about our beliefs

Six runs against the real model, four of them disconfirming something we had
written down as true:

1. **"Thin states cause the low confidences."** ❌ Refuted (run 2): the full run
   digest left the count at 61/63 and raised input cost ~48 %.
2. **"A 0.60 confidence floor is the right gate."** ❌ Miscalibrated (run 3): the
   brain never acted — 8/8 fallbacks, an expensive rule-based bot.
3. **"Missing card text is why cards can't be judged."** ⚠️ Partly (run 4): it
   fixed boss relics (0.040 → 0.570 confidence, defensible ranking) but not card
   rewards.
4. **"Enriched states would lift confidence."** ❌ Already refuted by 1, but the
   *mechanism* is now understood: confidence measures distribution flatness, and a
   preference question honestly has a flat distribution.
5. **"SCORE_ACTION_FLOOR is the card blocker."** ❌ Refuted (runs 6–8): the value
   floor was cleared all along (Pommel Strike 0.69 > 0.55). The blocker was the
   *second* requirement, the margin test, which was invented from a single run.
6. **"Ten seeds is ten samples."** ❌ Refuted (run 6): see the instrument section.
7. **"The margin test is what protects the deck from bad cards."** ❌ Refuted
   (runs 6–9): not one of its 29 rejections was a value rejection. It was refusing
   to choose between cards it had already ranked — and 13 of those refusals came
   from a consistency rule (`landed == argmax level`) nobody ever justified.

## One conclusion that needs re-opening

We announced that JEV's boss-relic ranking "became defensible" because it scored
Runic Dome (cannot see enemy intents) worst and Coffee Dripper best. But strategy
consensus holds that **energy relics are almost always correct**, and all three
offered relics were energy relics. So either JEV ranked by "how much this drawback
hurts a beginner" (right for a beginner) or it restated the drawback text without
valuing the +1 energy (shallow). **We asserted the first; the evidence supports
either.** Two probe runs have since shown the model is *confident* only about Runic
Dome (0.58–0.65) while ranking the other two at essentially zero confidence, which
leans toward the first reading — but a lean is not a resolution.

## Definition of "worth showing"
- Agent completes 10 consecutive runs unattended
- A calibration curve on data nobody else has (the independent-evaluation angle the
  JEV community currently lacks)
- Demo GIF in README

## What is honestly blocking what

| Blocker | Blocks | Whose move |
|---|---|---|
| CommunicationMod not installed (Workshop **2131373661**, or the GitHub release) | Phase 1's live smoke test, and everything that needs real screens | user: subscribe in Steam |
| No ground truth for card quality | any claim that the deck *improved*, and a Brier score for our own calls | needs recorded live runs; partly me |
| OpenRouter key permits only the `typesafe` provider | the structured-LLM baseline arm | user (add a provider) or a second key |
| No ground-truth labels anywhere in the simulator | "calibration" meaning correctness rather than consistency | me: record outcomes, not just decisions |
