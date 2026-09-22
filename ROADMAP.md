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

## Phase 1.6 — Advisor mode: the agent recommends, the player plays (2026-09-22 evening)

**The repositioning that reordered the roadmap.** The project's purpose is to help
a human play better, not to play for them, so `advise` is now the transport's
default mode and the auto-player is one flag away (`--mode play`). The agent runs
the *same* pipeline — JEV call, score gate, tactical layer — and only the
destination of the answer changed: a recommendation for the player instead of a
command for the game.

- [x] `driver/stdio.py` — `mode="advise"`: the ONLY verbs that ever reach the game
  are `wait` and `state` (asserted on every screen type in
  `test_advise_never_sends_an_advancing_verb_on_any_screen`). The
  unmodeled-screen ladder is disabled (pressing keys is acting) and the stall
  guard cannot arm. `--auto-start` is ignored: class/ascension/seed are the
  player's decisions
- [x] Cost discipline for a polling loop — the brain is asked **once per distinct
  game state**, not once per poll. Polling re-transmits the same state every ~1/3s;
  re-asking JEV each time would turn a $0.001 decision into a per-second bill while
  the player is simply thinking. The action limit is unlimited by default in
  advise mode, where "commands sent" is a clock rather than a budget
- [x] `driver/witness.py` — **player-choice inference**, the half auto-play never
  needed. Two consecutive states are diffed into the action that happened between
  them: the card that left the hand and the monster that lost HP (combat), the
  turn boundary, the card that joined the deck, the gold that went down and the
  shelf slot that emptied, the relic that appeared, HP or an upgrade at the rest
  site, a shrinking option list at an event. Matching is one idea used twice: both
  sides reduce to an **action key** and `None` inside a key means *unknown*, so
  unknown evidence can never manufacture a mismatch
- [x] Honest verdicts — `match` / `mismatch` / `unobserved`, with the **evidence
  for every verdict** carried into the event. `unobserved` is a first-class
  outcome: potions and text-event choices leave no trace in the state, and advice
  lag (the player acted before the recommendation landed) is reported as
  unobserved rather than scored as a disagreement. `agreement` is `None` — not
  `0.0` — until something is judgeable
- [x] `overlay/feed.py` + `server.py` + `dashboard.html` — new `advice` and
  `outcome` events; the `军师建议` panel (recommendation at 19px, the reason, the
  verdict on your last move, and the running agreement rate); `GET /state` now
  carries `last_advice` / `last_outcome` for the in-game overlay, so advice does
  not require a second window. Evidence: `docs/dashboard_advise_panel.png`
- [x] `run_dashboard.py --advise-demo` — the advisor panel with no game and no API
  key: a scripted ascent through the real transport, including a scripted player
  who sometimes follows the advice and sometimes does not (a demo where every
  verdict read "match" would demo a rigged measurement)
- [x] 15 tests in `tests/test_advise.py` + 3 in `tests/test_overlay.py` +
  mode/auto-start coverage in `tests/test_start.py` and
  `tests/test_install_mod_config.py`; 17/17 files green

Two bugs this work found, both pinned by tests afterwards:

- The auto-play guard tests were silently testing adviser behaviour once the
  default flipped. They now ask for `mode="play"` explicitly — a test that changed
  meaning with a default would be worse than one that failed.
- `GET /state` reported `last_outcome: null` while the browser panel showed a
  verdict. Cause: the agent's own `observe()` publishes a `run_state` *between* the
  verdict and the recommendation, and the snapshot's early-exit condition
  (`decision` + `advice` + `run_state`) was satisfied one event too early. Found by
  reading `/state` of a live demo, not by a unit test — which is the argument for
  the visual check.

## Phase 1.5 — The interaction layer (2026-09-22)
- [x] `overlay/feed.py` — `DecisionFeed`: thread-safe event stream
  (`run_state` / `decision` / `run_end`), backlog replay for late subscribers,
  dead-subscriber reaping, optional JSONL journal. A throwing feed can never
  break a run (`test_agent_survives_a_hostile_feed`)
- [x] `driver/agent.py` — optional `feed=` argument: `observe()` publishes the
  run snapshot, `_record()` publishes every decision. `None` (default) changes
  nothing about the existing behaviour
- [x] `sim/run_offline.py` — the harness accepts `feed=` too, so a simulated
  ascent streams exactly like a live one; map decisions carry the full probe
  table (per-node over-budget confidence) for the route preview
- [x] `overlay/server.py` — stdlib SSE server (`GET /` page, `GET /events`
  stream, `POST /publish` for cross-process agents, `GET /health`); bound to
  127.0.0.1 only. Found and fixed here: `feed or DecisionFeed()` silently
  swapped in a second feed because an *empty* feed is falsy (`__len__`)
- [x] `overlay/dashboard.html` — the dashboard: HP + budget bars, decision
  cards with confidence bars, Score rankings, Noul probe chips, posture and
  fallback reasons, SSE auto-reconnect
- [x] `run_dashboard.py` + `overlay/demo.py` — one command to watch a full
  simulated run (`--demo`, `--optimistic`, or `--backend openrouter` for real
  JEV answers)
- [x] 13 new tests in `tests/test_overlay.py` (all 14 test files green)
- [x] **Live-pipe wiring (2026-09-22)** — `BridgeFeed` + `stdio --dashboard-url`:
  the game-spawned agent forwards every decision to a running dashboard over
  HTTP, silently and by design when no dashboard is up (no port binding inside
  the game's process tree; spectator absence is never an error). Proven end to
  end: agent -> bridge -> server -> SSE subscriber
  (`test_end_to_end_bridge_agent_to_server_to_subscriber`)
- [x] **Route preview on the map screen (2026-09-22)** — map decisions now
  carry `detail.choices` (labelled nodes) and `detail.probes[].damage`; the
  dashboard renders each reachable path with its predicted damage and
  over-budget verdict, the chosen route highlighted, and the actual spend
  beneath the probe upper bound. Into the Breach's telegraphing, applied to
  routing

- [x] **One-command launcher (2026-09-22)** — `start.py`: doctor → dashboard →
  browser, in three labelled steps, every failure naming its fix. `--demo`
  plays a simulated run with no game and no key; `--setup` writes the mod
  config with the dashboard pre-wired and is remembered
  (`config/.setup-done`), so the whole routine afterwards is
  `python start.py` + start the game. Backend auto-detects (openrouter with a
  key, mock without) — the demo never depends on a key. Found by testing:
  `install_mod_config.check_argv` flagged URLs as missing files; fixed there
- [x] 18 overlay tests + 5 launcher tests (`tests/test_start.py`); 15/15 test
  files green

- [x] **Runaway guards, ported from Ethics03/jevspire (2026-09-22)** — the only
  other CommunicationMod+JEV project, and the only three of its mechanisms we
  lacked, all "stop spending, keep the game alive":
  1. **Stall guard** (`stdio.py`): the same game state coming back playable
     after we sent a command for it means the command did not take effect;
     after 2 repeats auto-decisions latch off (stderr + a `run_end` feed event)
     until a genuinely new state arrives. `state`/`wait` never arm the guard.
  2. **Action limit** (`stdio.py`): process-level cap, default 200, env
     `JEVBRAIN_MAX_ACTIONS` (<=0 unlimited), with a loud warning and a feed
     event when it trips — a cap that trips silently looks like a crash.
  3. **Navigation without the model** (`agent.py`): non-decision screens get
     Proceed (not a JEV call, not a pipe-stalling `wait`), and a shop is asked
     once per floor — the post-purchase state is the room wanting an exit.
- [x] 11 guard tests (`tests/test_guards.py`); 16/16 test files green

- [x] **Unmodeled-screen ladder + per-run action budget (2026-09-22 evening,
  guard #4, own design)** — the third real-run death, dissected from
  `logs/pipe.jsonl`: after Neow's reward the game showed a screen whose
  `available_commands` were only `[key, click, wait, state]` — no advancing
  verb — and the agent answered 991 `wait 20` in 66 s, hit the 1000 cap, and
  exited ("agent not reachable"). Two root causes, both fixed:
  1. **The ladder** (`stdio.py`): a screen with no advancing verb is
     *unmodeled* — waiting there is the loop, not progress. The transport now
     climbs a ladder (2 waits → SPACE → ESCAPE → centre click, 3 cycles max),
     then pauses loudly with the reason (stderr + `run_end` feed event,
     `reason: unmodeled_screen`) and auto-resumes when a modeled screen
     returns. Replayed the 991-message crime scene through the fix: 15
     commands, one announcement, process alive. Note the subtle trap this
     exposed: "stay silent" must return **None**, not `""` — an empty string
     is falsy in `run()` but breaks `is None` callers and reads as a bug.
  2. **Per-run budget**: the action cap was process-wide, so a player
     restarting a run in-game spent one shared counter — the third run of the
     18:08 session died ~50 commands in, which read as "the cap is too
     small". The cap now resets on every menu→in_game edge (and the default
     is 5000, backstop duty now that the only known loop is gone).
  6 new tests (`tests/test_guards.py`, 17 total there); 16/16 files green.

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
- [x] **Confidence semantics settled** (run 10, no API calls needed): `confidence`
  tracks a peakedness measure we can compute ourselves at r = +0.89/+0.90 over 1410
  answers; all 157 `confidence=0.000` answers are genuine zeros with a near-uniform
  distribution, so a zero means "no preference", not "certain it is bad". See
  `python -m spirebrain.analysis.confidence` and docs/JEV_API.md
- [ ] Where should `SCORE_ACTION_FLOOR` sit? Run 10 showed every option `argmax`
  accepts arrives with a flat distribution, so this one number carries the whole
  card decision — and it was chosen from a single run. Pre-registered as Question 2
  in docs/MEASUREMENTS.md (an offline replay sweep, then one confirming batch)
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
8. **"A `confidence=0.000` answer might be a missing field."** ❌ Resolved as a real
   zero (run 10, 157 of them, field absent 0 times) — and the meaning is the
   opposite of the intuition: a zero means the model had *no preference*, and every
   zero comes with a near-uniform distribution.
9. **"Value and confidence are independent signals."** ❌ Refuted (run 10): they
   anti-correlate at r = −0.51 over 810 Score answers. High-rated options come with
   flat distributions; the model's clear preferences are about options it rates
   *low*. So `margin` was never asking for two conditions — it was asking for two
   that almost never coincide.

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
- Agent completes 10 consecutive runs unattended (`play` mode)
- **Advisor mode: an agreement rate measured on real play.** The two numbers that
  matter are `judged` (how many recommendations the state could even adjudicate)
  and `agreement` over those. Both are already produced live
  (`logs/advice.jsonl`); what is missing is volume and a recording of the
  player's *outcome* — advice that gets followed is not automatically advice that
  was right, and the difference is the whole question
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
| Advisor mode has no live data yet | any statement about its agreement rate — the code path is tested (15 cases) but has never seen a real player | user: one real run in advise mode; the panel and `logs/advice.jsonl` collect everything |
