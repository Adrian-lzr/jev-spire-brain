# Measurements

Every number this project has produced, what it did and did not establish, and the
protocol that governs the next one. Keep this file honest: if a claim is not
traceable to a row here, it is not a claim, it is a guess.

Everything below was produced by `python -m spirebrain.sim.run_offline --backend=openrouter`
on real JEV (`typesafe/jev-1.13-20260917` via OpenRouter's System One route) unless
stated otherwise. Raw call logs land in `logs/` (gitignored) and are readable with
`python -m spirebrain.analysis.inspect_log`.

## Run log

| # | Date | Change under test | Answers < 0.60 | Rule fallbacks (per act) | Cost / run | p50 latency |
|---|---|---|---|---|---|---|
| 1 | 2026-09-21 | thin states, 0.60 confidence floor | 61 / 63 | 8, 8, 8 | $0.000528 | 1435 ms |
| 2 | 2026-09-21 | + full run digest (`RunContext`) | 61 / 63 | 7, 8, 8 | $0.000783 | 1334 ms |
| 3 | 2026-09-21 | + distribution-based acceptance for Choice/Score | 61 / 63 | **5, 5, 5** | $0.000783 | 1328 ms |
| 4 | 2026-09-21 | + the game's own card/relic text (`gamedata.py`) | **59 / 63** | 6, 4, 5 | $0.000859 | 1320 ms |
| 5 | 2026-09-21 | repeatability probe: 4 scenarios × 5 identical repeats | — | **0 % flip rate** | $0.0007 (20 calls) | — |
| 6 | 2026-09-21 | **pre-registered arm A:** 10 ascents, `margin` gate | — | 0.667 rate (160/240) | $0.000858 | — |
| 7 | 2026-09-21 | **pre-registered arm B:** 10 ascents, `argmax` gate | — | 0.508 rate (122/240) | $0.000881 | — |
| 8 | 2026-09-21 | repeatability probe under `argmax` | — | **0 % flip rate** | $0.0006 (20 calls) | — |
| 9 | 2026-09-21 | **re-run of both arms on the fixed harness** (seed-driven maps) | — | margin 0.537 / argmax **0.411** | $0.001004 / $0.001027 | — |
| 10 | 2026-09-21 | **corpus analysis of every logged answer** (no API calls) | — | 1410 distributions, r = +0.89 | $0 | — |

Runs 6–8 are the pre-registered experiment from the previous version of this file.
Runs 6 and 7 also destroyed the harness they were run on (below), so run 9 repeats
both arms on the repaired instrument.

## Run 9 — the comparison that decided the Score gate

`python -m spirebrain.sim.batch --seeds calibration --backend openrouter --acceptance {margin,argmax}`

10 ascents per arm, same seeds, same harness, same code except the gate.

| | `margin` | `argmax` |
|---|---|---|
| Cards taken (10 ascents) | 1 | **30 — 3 in every ascent** |
| Deck at Act 3 start | 10.1 | **13.0** |
| Mean HP at each act end | 48.3 / 43.9 / 43.0 | **49.5 / 44.7** / 43.0 |
| Rule fallback rate | 0.537 | **0.411** |
| JEV calls per ascent | 27 | 27 |
| Cost per ascent | $0.001004 | $0.001027 (+2.3 %) |
| Decision flip rate (5 repeats, run 8) | 0 % | 0 % |

**Every rejection margin made was a shape rejection.** Not one of the 29 rejections
across the ten ascents was "the value is too low" — the values ran 0.657–0.757,
comfortably above the 0.55 action floor. Thirteen said *"landed on level 2.1x,
distribution favours 3"* and sixteen said *"flat distribution"*. So the gate was
never protecting the deck from bad cards; it was refusing to choose between cards
it had already ranked, and it did so on a criterion (`landed == argmax level`) that
was invented from a single run and never justified.

**Pre-registered acceptance, as written before the run:**

- (a) takes cards in every ascent — **met**, 3 per ascent, 10/10;
- (b) does not lower mean HP at act-3 entry by more than 5 % of max HP — **met on
  the proxy available**: per-act end HP is 49.5/44.7/43.0 versus 48.3/43.9/43.0;
- (c) 0 % flip rate under `--repeats 5` — **met** (run 8).

**Honest caveats on (b).** The simulator resets HP to 80 at the start of each act,
so "HP entering Act 3" as a cumulative quantity does not exist here — what is
comparable is the per-act ending HP, which is what the table reports. Carrying HP
across acts is a harness improvement with its own pre-registration, queued in the
ROADMAP. Also note act 3 ended at 43.0 in both arms: under this simulator the card
gate has no effect there.

**Decision.** `argmax` is adopted as the default Score gate, recorded in
`config/strategy.json` (`jev.score_acceptance`) so it stays a strategy-layer choice
rather than a hard-coded one, with `margin` still selectable for comparison.

**What this does NOT establish.** That the deck improved. The simulator has no
ground truth for card quality, so "3 cards instead of 1" is a change in *what the
agent does*, not evidence that it plays better. That claim needs recorded live runs
or a labelled dataset, and it is now the top item in the ROADMAP.


## What each measurement actually established

| # | Established | Did NOT establish |
|---|---|---|
| 1 | Real JEV answers our question sets at all; the pipeline runs end to end | That thin states were the problem (refuted by run 2) |
| 2 | **Refutation:** enriching the state to the full run digest changed nothing (61/63) while raising input cost ~48 %. Confidence on *preference* questions is a distribution-flatness statistic, not a correctness signal | That our questions are well-posed |
| 3 | A 0.60 confidence floor made the brain never act (8/8 fallbacks = an expensive rule-based bot). Distribution-based acceptance cut fallbacks to 5/8 and produced the first genuinely model-chosen actions: a route, and a refusal of an event the old fallback would have paid 25 % max HP for | That the new margins (0.50 / 0.15) are the *right* values — they were fitted to this single run |
| 4 | Real card/relic text moved a boss-relic confidence from 0.040 → 0.570 and made the ranking legible; floor-breaches fell 61 → 59 | That card rewards are fixed (they still always skip); that JEV's relic ranking matches expert consensus (see below) |
| 5 | **No threshold in the project is a coin at 5 repeats.** All four scenarios returned the same verdict on every repeat | Stability at higher repeat counts, or for question sets not probed |
| 6 | The `margin` gate blocks **100 %** of card rewards: 30 rejections over 10 ascents, none of them because the value was too low — every one was a distribution-shape rejection | Anything about card quality, because the harness defect below made these 10 ascents one ascent |
| 7 | **`argmax` takes 3 cards per ascent, every ascent** (deck 10 → 13) and cuts rule fallbacks from 0.667 to 0.508 — while costing the same per call ($0.000858 → $0.000881, +2.7 %, from the longer deck digest) | That the wider deck *helps*, or that act-3 entry HP is unharmed: see the next row |
| 8 | The `argmax` gate is stable: 0 % flip rate across all four scenarios at 5 repeats, so criterion (c) of the pre-registration is met | Those four scenarios only; other question sets are unprobed |
| 9 | On a harness that can fail, `argmax` takes 3 cards per ascent in 10/10 runs where `margin` took 1 in 10, raises fallback-free play from 46 % to 59 %, and costs 2.3 % more per ascent. All 29 of margin's rejections were distribution-shape rejections, none a value rejection | That the deck got *better*: no ground truth exists for card quality here |
| 10 | **What confidence is.** Over 1410 answers carrying a distribution, confidence correlates **+0.885 (Choice) / +0.899 (Score)** with our own peakedness measure, so it is a flatness statistic we can compute rather than something we must ask for. All 157 `confidence=0.000` answers are genuine zeros (field absent: 0) with peakedness ≤ 0.127, i.e. uniform. And **value anti-correlates with peakedness at r = −0.51**, which is why `margin` fired on 2 of 810 Score answers | What *causes* the anti-correlation; whether 0.55 is the right action floor; whether JEV's own confidence is available for Noul (it is not — its probability is the belief) |

## Run 10 in detail — what the confidence number is

`python -m spirebrain.analysis.confidence` (read-only; no API calls)

Answering a question the project had carried since run 1 and never resolved:
**what does JEV's confidence mean, and is `0.000` a value or a missing field?**

| Question type | n | confidence range | peakedness range | r |
|---|---|---|---|---|
| Choice | 600 | 0.000 – 0.880 | 0.000 – 0.719 | **+0.885** |
| Score | 810 | 0.000 – 0.920 | 0.030 – 0.797 | **+0.899** |

where peakedness = 1 − (normalised entropy of the answer distribution).

**Three findings:**

1. **`0.000` is a genuine zero, 157 times, and never a missing field** (absent: 0
   of 3075 answers). Every zero has peakedness ≤ 0.127 — a near-uniform
   distribution with a top option between 0.29 and 0.50. So a zero means *no
   preference among the options offered*, not *certain this is bad*. The docs' old
   warning ("do not read 0.000 as a calibrated zero") is lifted and replaced by a
   sharper one.
2. **Confidence is peakedness, and we can compute it ourselves.** r ≈ 0.9 against
   our own entropy measure means the model's number adds little we cannot derive
   from the distribution it already returns. This is the justification for reading
   distributions directly in the gates.
3. **Value and peakedness anti-correlate (r = −0.51)** over the 810 Score answers,
   which turns run 9's finding into a structural explanation. The joint
   distribution is nearly a diagonal:

   | value band | near-uniform | weak | clear | strong |
   |---|---|---|---|---|
   | below Filler (<0.33) | 24 | 155 | 101 | 65 |
   | Filler..floor (0.33–0.55) | 198 | 61 | 2 | 1 |
   | Solid..Excellent (0.55–0.80) | **134** | **69** | 0 | 0 |
   | Excellent+ (≥0.80) | 0 | 0 | 0 | 0 |

   `margin` accepted 2 of 810; `argmax` accepts 203. Also visible: **nothing ever
   scored above 0.80**, so the 0.55 floor sits near the top of the range JEV
   actually uses for these judgments.

**What this does not establish.** Anything about whether the accepted options are
*good* — it counts preferences, not outcomes. And a new caveat it does create: every
option `argmax` accepts comes with a flat distribution, so the value floor is
carrying the whole decision. Whether 0.55 is the right place for it is now the first
pre-registered question below.


## The instrument was lying (found by run 6)

Run 6's ten ascents returned **ten identical HP trajectories** — 51, 47, 43 in
every single seed — and the identical route. Firing the same seed-dependence check
on the mock confirmed it: `random.Random(seed)` was constructed correctly, but the
outcomes did not depend on it.

Two causes, both in the harness rather than the agent:

1. **The map was a fixed constant** (two rows, same three nodes every run), so the
   only variation a seed could introduce was *which reward and which event* were
   drawn — and those do not change what a rule fallback does.
2. **The fallback router always walks the least damaging node, and a rest node
   always cost nothing.** So "safest" was always free: routing could not cost HP
   at all, which removed the one variable the HP-budget system exists to reason
   about.

Consequence for reading the table above: **runs 6 and 7 have an effective sample
size of 1**, not 10. Their result — `argmax` takes cards, `margin` never does — is
a property of the gate, which is exactly what was being tested, so it survives.
Their HP column proves nothing, which is why criterion (b) is reported as
**uninformative rather than passed**.

Fixed in the same commit that records the finding:

- maps are generated per seed (3 rows × 3 nodes, act-specific symbol pools, and a
  row is *not* guaranteed to contain a free option);
- node damage is a seeded jitter of the symbol's worst case;
- the walk now spends at most the probe's estimate, recorded per step as
  `probe`/`spent` — previously probes said "24 HP for an elite" while the spend
  was an unrelated `randint(14, 26)`;
- `tests/test_sim_harness.py` asserts seeds change outcomes, routes differ, and
  `spent <= probe`, so this class of defect cannot come back silently.

The locked `TEST_SEEDS` were **not** spent on the broken harness. Spending a test
set on an instrument that cannot fail the test is worse than not measuring.

## Run 5 in detail — the repeatability probe

`python -m spirebrain.analysis.repeatability --repeats 5`

Each scenario sends the **identical** request five times. If a gate flips, that
threshold is a coin rather than a knob, and tuning it further would only fit noise.

| Scenario | Verdict stability | Confidence spread | Notes |
|---|---|---|---|
| map routing | `n3` ×5, 0/5 fallback | 0.460 – 0.580 (0.120) | all three HP probes stayed at 0.04–0.19, i.e. clearly "not over budget" |
| card reward | `skip` ×5, 5/5 fallback | 0.080 – 0.360 (**0.280**) | values were stable and **correctly ordered** — see below |
| shop purchase | `leave` ×5, 5/5 fallback | 0.430 – 0.450 (0.020) | card removal ranked top (0.43–0.45), matching guide consensus |
| boss relic | `Coffee Dripper` ×5, 5/5 flagged | 0.010 – 0.060 (0.050) | Runic Dome carried the only real confidence (0.58–0.65) |

**Three findings worth more than the flip rate:**

1. **The card model can rank, it just cannot be peaked.** Normalised values were
   stable to ±0.02 across repeats and consistently ordered
   **Pommel Strike 0.68–0.70 > Twin Strike 0.55–0.57 > Anger 0.32–0.35** — which
   agrees with the guides ("card draw is energy"; Pommel Strike is damage *plus*
   draw). Pommel Strike's 0.69 also clears the 0.55 action floor. Yet every reward
   was skipped, because our `accept_score` additionally demands a peaked
   distribution (top ≥ 0.50, lead ≥ 0.15) and this distribution is not peaked.
   **So the blocker is not the model, and not the text — it is our margin test,
   which was invented from one run.** Hypothesis for the next experiment: for
   Score questions where the value floor is cleared and the ranking is stable
   across repeats, take the argmax instead of demanding a margin.
2. **Shop ranking aligns with expert consensus.** JEV put card removal top and
   Meat on the Bone second — the guides' own priority ("$75 to delete a Strike
   beats buying an unrelated rare"). Weak evidence of domain sense, but not
   nothing.
3. **Boss relics: the confidence asymmetry is informative.** Runic Dome (drawback:
   you can no longer see enemy intents) is the one the model is *confident* about
   (0.58–0.65) and it ranks it worst, every time. Coffee Dripper (drawback: you
   can no longer rest) is ranked best with essentially zero confidence
   (0.01–0.06). That reads as *"I can evaluate this drawback, and it is bad"*
   rather than *"I am repeating the drawback text"* — which is the interpretation
   the ROADMAP previously asserted without evidence. It still does not settle
   whether JEV understands the +1 energy; it does make the confident ranking
   harder to dismiss.

## Protocol for every future measurement

Adopted 2026-09-21 after external research showed this project had been drawing
conclusions from single 24-call runs. An independent 108-claim study documents its
own conclusion reversing twice ("at 22 claims it said Jev won calibration
decisively, and at 72 it said the field was tied").

1. **Split the seeds.**
   - `CALIBRATION_SEEDS` — used to pick thresholds. Tune here freely.
   - `TEST_SEEDS` — **locked.** Run once, at the end, and never re-run after
     changing anything. If you look at it twice it stops being a test set.
2. **State the acceptance criteria before the run**, in this file, in the
   "Next run" block below. No post-hoc interpretation.
3. **Repeat before you tune.** Any threshold that flips under
   `analysis/repeatability.py` is not tunable — it is noise.
4. **Report the sample size next to every number.** 63 answers cannot support a
   claim about a 0.05 difference.
5. **Version the prompt.** Question wording changes the raw confidence numbers
   (documented externally), so a threshold is only meaningful together with the
   question set that produced it. `prompt_version` is logged per call.
6. **Prefer a standard over an invention.** `jevcal` already does what our
   thresholds need (pick the threshold that meets an accuracy target on your own
   data, report how much traffic still needs an LLM, fail CI when an update breaks
   calibration). Our calibration tooling should converge on that shape.

## Next run: pre-registered criteria

Written before the run, as required by point 2 above.

Three open questions, in the order they must be answered: the harness one
invalidates the others, and the last one still has no ground truth.

**Question 1 — the harness (must come first).** HP resets to 80 at each act start,
so cumulative HP damage — the thing the HP-budget system reasons about — cannot be
observed. Does carrying HP across acts (with the act-reserve policy recomputed on
entry) change the routing decisions?

**Design.** 10 ascents on `CALIBRATION_SEEDS`, `argmax`, HP carried across acts.
Compare the chosen route's probe cost per row against the run-9 batch.

**Acceptance for keeping it.** Carrying HP is adopted only if the routing decision
differs from run 9 in at least 3 of 30 row decisions — i.e. only if the budget
actually constrains routing once damage is cumulative. If nothing changes, the
extra realism buys nothing and the simpler harness stays.

**Question 2 — where should `SCORE_ACTION_FLOOR` sit?** Run 10 showed that every
option `argmax` accepts arrives with a flat distribution, so this single number
carries the whole card decision — and 0.55 was chosen from one run.

**Design.** Replay the logged Score answers offline at floors 0.33 / 0.45 / 0.55 /
0.67 and report cards taken per ascent and the resulting deck composition. Offline,
so the sweep is free; then one confirming ascent batch at whichever floor the sweep
suggests.

**Acceptance.** A new floor is adopted only if it (a) changes cards taken per
ascent, (b) keeps the run-9 HP table within 5 %, and (c) survives `--repeats 5` at
0 % flip rate. **This can select a threshold but cannot show it is correct** — "takes
more cards" is not "takes better cards", and the gap between those needs labels.

**Question 3 — card quality (blocked on ground truth).** Does taking three cards
per ascent beat taking one, measured by something other than deck size?

**Design.** Cannot be answered in the simulator: it has no notion of a card being
*good*, only of its text. Two routes, both requiring the live pipe: (a) record real
ascent states via `--replay` and score the gate against outcomes (floor reached,
HP at death); (b) label a small set of card-reward decisions by hand and compute
agreement. Until one exists, deck size is a description, not a result.

**Explicitly not a criterion.** Any claim about win rate. `TEST_SEEDS` remain
untouched and must not be spent on a harness whose HP semantics are still wrong.

## Known threats to validity

- **n = 1 run per configuration.** Every row in the run log above is a single
  ascent. Treat differences under ~10 % as noise.
- **The simulator's damage is synthetic**, drawn from `NODE_DAMAGE_HINT` guesses I
  wrote. Nothing here validates the HP-budget system against real game damage.
- **`usage.cost` is the provider's number**, which is trustworthy; latency is
  measured from one machine in one network, and JEV's vendor figure (70–500 ms) is
  measured from US West, so neither is comparable to the other.
- **`MockJevClient` cannot validate any threshold.** It returns fixed values by
  construction, so it proves the plumbing and nothing else.
- **No ground-truth labels.** Nothing in this repo can yet compute accuracy or a
  Brier score for our own decisions, because the simulator does not record whether
  a judgement turned out to be right. Until it does, "calibration" here means
  consistency, not correctness.
- **The margin gate's consistency check is stricter than it looks.** In run 6,
  roughly a third of the rejections were not "flat distribution" but *"landed on
  level 2.14, distribution favours 3"* — the model's own fractional score and its
  own level distribution disagreed. Requiring them to agree is an extra
  requirement that was never justified, and it is part of what `argmax` removes.
- **Run 10's correlation is a description, not a mechanism.** confidence tracks our
  peakedness measure at r ≈ 0.9 *on this question set*, with these rubrics, at this
  prompt version. It says confidence behaves like a flatness statistic; it does not
  say JEV computes entropy, and a different question set could break the relation.
- **The `argmax` gate leans entirely on one unvalidated number.** Every option it
  accepted in run 10's corpus came with a flat distribution, so `SCORE_ACTION_FLOOR`
  is doing all the work. Until Question 2 below is run, "the deck grew" and "the
  gate got looser" are the same statement.
- **`confidence` for Noul is not a separate field at all** — its probability *is*
  its belief — so none of run 10's correlation applies to Noul questions, which are
  two thirds of all the answers logged (1665 of 3075).

