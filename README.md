# Jev Spire Brain

> A JEV-powered external brain for [Slay the Spire](https://store.steampowered.com/app/646570/Slay_the_Spire/) — letting a TypeSafe **System One model** make the strategy calls while deterministic code handles the tactics.

JEV (the TypeSafe System One decision model) plays as the external brain: semantic judgement goes to JEV, exact arithmetic to the code.

## What it is

Jev Spire Brain is a three-layer agent architecture for Slay the Spire:

| Layer | Responsibility | Implementation |
|---|---|---|
| Strategy | goals, risk appetite, HP budget policy | `config/strategy.json` (human-defined) |
| Semantic judgment | "which option serves the strategy" | JEV primitives (`Choice` / `Score` / `Noul`) |
| Tactics & execution | combat search, HP arithmetic, I/O | deterministic code (spirecomm) |

The agent observes the full game state via [CommunicationMod](https://github.com/ForgottenArbiter/CommunicationMod) (JSON state stream), decides with calibrated probabilistic judgments, and sends actions back through the same pipe.

## Decision points owned by JEV

All seven are implemented in `spirebrain/jev_brain/decisions.py` and covered by tests.

- **Map routing** — `Choice` over reachable nodes + parallel `Noul` risk probes against the HP budget (`MapRouter`)
- **Card rewards** — `Score` × candidates against the deck, with a skip path (`CardRewardJudge`)
- **Events** — `Choice` over event options (`EventChooser`)
- **Rest sites** — `Noul` (heal?) + `Choice` (which upgrade) in one call (`RestSiteDecider`)
- **Shops** — one parallel `Noul` per affordable item, "worth the gold for this goal?" (`ShopDecider`)
- **Boss relics** — `Score` × 3; the pick is mandatory, so an unsure pick is flagged rather than skipped (`BossRelicJudge`)
- **Combat risk** — `Noul` (will predicted damage exceed the HP budget?) 鈫?tactical posture (`CombatRiskGate`)

Combat card play itself stays in code (search/greedy), per the drone-rule: *JEV cannot be the perception layer and cannot run at control rate.*

## Two conventions that shape every decision module

Both come straight from the official docs ([docs/JEV_API.md](docs/JEV_API.md)):

1. **One call, many questions.** Jev evaluates every question in parallel against the same state and adding questions barely costs latency — so each module asks everything it needs at once for free.
2. **Noul has no separate confidence** — the returned probability *is* the belief. A probability near 0.5 means "cannot tell", so the band `(0.40, 0.60)` triggers a rule fallback instead of a coin-flip action.

## Status

🚧 **Both halves of Phase 1 exist now, and the brain has run against real JEV.**
All 7 decision modules + HP budget + greedy combat + logging + simulation harness
+ agent router + the real JEV client + the **CommunicationMod stdio transport** are
in place, with **182 tests passing**. Live decisions and simulator decisions now
share the same rich `RunContext`, so both paths ask JEV against the full run
digest and the game's own card/relic text.

✅ **Phase 1.5 — the interaction layer — is in.** The brain now has a window:
`spirebrain/overlay/` streams every decision (value, confidence, probabilities,
Score distributions, fallback reasons) to a local web dashboard over SSE, live
while the run happens. Zero game invasion: the feed is an optional constructor
argument, and a dead dashboard can never break a run (proven by
`tests/test_overlay.py::test_agent_survives_a_hostile_feed`).

GUARDS **Three runaway guards, ported from [Ethics03/jevspire](https://github.com/Ethics03/jevspire)**
(the only other CommunicationMod+JEV project), plus a fourth of our own, all
"stop spending, keep the game alive": a **stall guard** that latches
auto-decisions off when the identical game state returns after our command (it
did not take effect — retrying is how an agent burns money in a loop); an
**action limit** (default 5000 commands, **per run** — refilled on every
menu→in-game edge, `JEVBRAIN_MAX_ACTIONS` to change, <=0 unlimited) that stops
a confused run loudly instead of quietly; **navigation without the model** —
non-decision screens get Proceed and a shop is asked once per floor, so no JEV
call is ever spent re-deriving "leave"; and the **unmodeled-screen ladder**
(own design, 2026-09-22 evening): a screen whose `available_commands` offer no
advancing verb gets two waits, SPACE, ESCAPE, a centre click — three cycles at
most — then a loud pause with a reason, auto-resuming the moment a known
screen returns. Born from a real death: after Neow's reward the game showed a
screen the mod cannot act on, and the old code answered with 991 `wait 20` in
66 seconds, hit the cap, and exited — the player saw "agent not reachable".
Replayed through the fixed transport: 15 commands, one announcement, alive.
Covered in `tests/test_guards.py`.

## Quick start — one command

```bash
python start.py --demo      # no game, no API key: watch a simulated run right now
```

A dashboard opens in your browser and a simulated ascent plays out on it —
every route preview, card score, confidence bar and fallback reason, live.

To play with **real JEV** put `OPENROUTER_API_KEY=...` in a `.env` file, then:

```bash
python start.py --setup     # checks the environment, starts the dashboard,
                            # and writes the game's mod config (backs up the old one)
```

Start Slay the Spire through ModTheSpire with the mods enabled. The brain
plays; the dashboard shows it thinking. `--setup` is remembered — after the
first time, plain `python start.py` is the whole routine.

Everything start.py does, done manually:

```bash
python -m spirebrain.doctor                              # what is missing, and how to fix it
python run_dashboard.py --demo        # simulated run, mock JEV, no key needed
python run_dashboard.py --demo --optimistic          # JEV steers the run
python run_dashboard.py --demo --backend openrouter  # REAL JEV answers live
python run_dashboard.py               # server only; watch a live agent
```

The game-spawned agent joins with one flag — decisions stream to the
dashboard over HTTP, and stay silent when no dashboard is running:

```bash
python run_agent.py --backend openrouter --dashboard-url http://127.0.0.1:8787/publish
```

CommunicationMod installation itself (a one-time jar download) is documented
in [docs/SETUP.md](docs/SETUP.md).

```bash
# run everything offline right now (no game, no API key):
python -m spirebrain.doctor                              # what is missing, and how to fix it
python -m spirebrain.sim.run_offline                     # pessimistic mock: every module falls back
python -m spirebrain.sim.run_offline --optimistic         # confident mock: answers steer the run
python -m spirebrain.sim.batch --seeds calibration --backend mock   # 10 ascents, aggregated

# run it against real JEV (needs OPENROUTER_API_KEY; the score gate is switchable):
python -m spirebrain.sim.batch --seeds calibration --backend openrouter --acceptance argmax

# be the thing the game launches:
python run_agent.py --backend openrouter
python run_agent.py --replay logs/recorded_states        # re-decide recorded states offline

# read back what JEV actually answered:
python -m spirebrain.analysis.inspect_log --summary
python -m spirebrain.analysis.calibration logs
python -m spirebrain.analysis.repeatability --backend openrouter --repeats 5
python -m spirebrain.analysis.confidence           # what the confidence number means
```

**Everything measured so far is in [docs/MEASUREMENTS.md](docs/MEASUREMENTS.md)** — 
each run with its sample size, what it established, what it refuted, and the
protocol the next one has to follow. Four claims have already been overturned by
it, including two of our own. Read that file before quoting any number from this
project.

**One-line summary of the first real run:** 24 calls, $0.000528, and 61 of 63
answers below the 0.60 confidence floor. The floor turned out to be the wrong
instrument for preference questions; see
[docs/JEV_API.md](docs/JEV_API.md#first-real-run-what-we-learned-2026-09-21).

Python package name stays `spirebrain` (import name); repo name is `jev-spire-brain`.

## The protocol layer, which is where unattended runs die

`spirebrain/driver/stdio.py` implements CommunicationMod's side of the pipe.
Four details are easy to get wrong and are all handled, verified against the
mod's README (2026-09-21):

- **`Ready\n` first**, or the game waits ten seconds and kills the process;
- **`PLAY` is 1-indexed** while `CHOOSE` is 0-indexed — the conversion happens in
  exactly one function;
- **there is no `skip`, `purge`, or `smith` verb** — skipping a reward and leaving
  a shop are both `RETURN`;
- **nothing prints to stdout except `Ready` and commands**, because stdout *is*
  the protocol.

Plus three rules that keep a long run alive: answer only when the game says it is
ready, never send a verb the game did not advertise, and one command per state.

## Prerequisites

- Steam copy of Slay the Spire
- ModTheSpire + BaseMod (+ StSLib) + CommunicationMod — all free
- Python 3.10+
- JEV access — **not required for development**: the mock client lets you build,
  run and test everything offline. Real JEV is reachable on OpenRouter's System
  One route (see [docs/JEV_API.md](docs/JEV_API.md)).

## Setup

```bash
git clone https://github.com/Adrian-lzr/jev-spire-brain.git
cd jev-spire-brain
python start.py --demo                    # the one-command path (see Quick start)
```

Or step by step:

```bash
pip install -r requirements.txt          # optional: the brain is stdlib-only
python -m spirebrain.doctor               # names every missing piece and its fix
python -m spirebrain.install_mod_config    # preview the mod config it will write
python -m spirebrain.install_mod_config --write   # apply (backs up any existing file)
python tests\test_stdio.py               # prove the pipe logic offline
```

`doctor` exists because "is the mod installed?" turned out to need six manual
checks, and one of them is genuinely non-obvious: a Steam Workshop **subscription**
and a Workshop **download** are different states in `appworkshop_<appid>.acf`. The
id under `WorkshopItemDetails` means Steam knows you subscribed; only
`WorkshopItemsInstalled` means the files are on disk.

`install_mod_config` exists because the config file is read as **ISO-8859-1**
(`SpireConfig.load()` 鈫?`Properties.load(FileInputStream)`), so a path with
non-ASCII characters written as raw UTF-8 is silently misread — and because the
command is split on whitespace and passed to `ProcessBuilder`, so no path may
contain a space. Both are hand-edit traps; the installer escapes, validates, and
verifies by decoding the result back the way Java does.

## Switching the brain on

```bash
export JEV_BACKEND=mock             # default: deterministic, offline
export JEV_BACKEND=openrouter       # real JEV, System One route ...
export OPENROUTER_API_KEY=...       #    ... needs this
export JEV_BACKEND=official         # TypeSafe direct (waitlisted early access)
export TYPESAFE_API_KEY=...
export JEV_BACKEND=cloudflare       # Cloudflare AI
export CLOUDFLARE_ACCOUNT_ID=... CLOUDFLARE_API_TOKEN=...
export JEV_BACKEND=llm              # labelled stand-in, NOT JEV: a schema-constrained
                                    # chat model through the same three primitives
```

The verified request/response shapes, the confidence conventions, and what is
still unverified are all written down in [docs/JEV_API.md](docs/JEV_API.md).

## Credits & license

- Architecture informed by [spirecomm](https://github.com/dweih/spirecomm) (MIT), [CommunicationMod](https://github.com/ForgottenArbiter/CommunicationMod), and the JEV ecosystem's `jev-drone` / `typesafe-mario` layered designs.
- JEV is a model by [TypeSafe AI](https://typesafe.ai/). This project is independent and not affiliated.
- MIT License — see [LICENSE](LICENSE).

