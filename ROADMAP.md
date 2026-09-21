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
- [x] Real client implemented; switch with `JEV_BACKEND=official|cloudflare`
- [~] Confidence handling validated against a publisher-provided response; **real** calibration needs a key
- [x] `analysis/calibration.py` — confidence-bucket report + fallback ratio + ECE stub
- [ ] Run ≥50 ascents, collect stats (win rate, avg death floor, decisions log)
- [ ] Compare: JEV brain vs. random baseline vs. greedy baseline
- [ ] Reconcile measured latency/cost with the assumed $0.042/Mtok

## Definition of "worth showing"
- Agent completes 10 consecutive runs unattended
- Calibration curve published (independent evaluation angle the JEV community currently lacks)
- Demo GIF in README

## What is honestly blocking what
| Blocker | Blocks | Whose move |
|---|---|---|
| Mods not installed | Phase 1 entirely, and Phase 3's post-combat review | user |
| No JEV API key | real calibration, cost reconciliation, Phase 5 stats | user |
