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

## What each measurement actually established

| # | Established | Did NOT establish |
|---|---|---|
| 1 | Real JEV answers our question sets at all; the pipeline runs end to end | That thin states were the problem (refuted by run 2) |
| 2 | **Refutation:** enriching the state to the full run digest changed nothing (61/63) while raising input cost ~48 %. Confidence on *preference* questions is a distribution-flatness statistic, not a correctness signal | That our questions are well-posed |
| 3 | A 0.60 confidence floor made the brain never act (8/8 fallbacks = an expensive rule-based bot). Distribution-based acceptance cut fallbacks to 5/8 and produced the first genuinely model-chosen actions: a route, and a refusal of an event the old fallback would have paid 25 % max HP for | That the new margins (0.50 / 0.15) are the *right* values — they were fitted to this single run |
| 4 | Real card/relic text moved a boss-relic confidence from 0.040 → 0.570 and made the ranking legible; floor-breaches fell 61 → 59 | That card rewards are fixed (they still always skip); that JEV's relic ranking matches expert consensus (see below) |
| 5 | **No threshold in the project is a coin at 5 repeats.** All four scenarios returned the same verdict on every repeat | Stability at higher repeat counts, or for question sets not probed |

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

**Question.** Does replacing the Score margin test with a value-floor argmax take
card rewards without hurting decision quality?

**Design.** 10 ascents on `CALIBRATION_SEEDS` with margin, 10 with argmax; compare
cards taken, deck size, HP at act boundaries. Then 10 ascents on `TEST_SEEDS` with
whichever wins, and report that number as the result.

**Acceptance.** The change is adopted only if it (a) raises cards taken above 0,
(b) does not lower mean HP at act-3 entry, and (c) survives one
`--repeats 5` probe with 0 % flip rate. Any other outcome is reported as a null
result, not spun as a partial success.

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
