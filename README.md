# Jev Spire Brain

> A JEV-powered external brain for [Slay the Spire](https://store.steampowered.com/app/646570/Slay_the_Spire/) 鈥?letting a TypeSafe **System One model** make the strategy calls while deterministic code handles the tactics.

璁?JEV锛圱ypeSafe System One 鍐崇瓥妯″瀷锛変綔涓哄缃ぇ鑴戠帺鏉€鎴皷濉旓細璇箟鍒ゆ柇褰?JEV锛岀簿纭绠楀綊浠ｇ爜銆?

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

- **Map routing** 鈥?`Choice` over reachable nodes + parallel `Noul` risk probes against the HP budget (`MapRouter`)
- **Card rewards** 鈥?`Score` 脳 candidates against the deck, with a skip path (`CardRewardJudge`)
- **Events** 鈥?`Choice` over event options (`EventChooser`)
- **Rest sites** 鈥?`Noul` (heal?) + `Choice` (which upgrade) in one call (`RestSiteDecider`)
- **Shops** 鈥?one parallel `Noul` per affordable item, "worth the gold for this goal?" (`ShopDecider`)
- **Boss relics** 鈥?`Score` 脳 3; the pick is mandatory, so an unsure pick is flagged rather than skipped (`BossRelicJudge`)
- **Combat risk** 鈥?`Noul` (will predicted damage exceed the HP budget?) 鈫?tactical posture (`CombatRiskGate`)

Combat card play itself stays in code (search/greedy), per the drone-rule: *JEV cannot be the perception layer and cannot run at control rate.*

## Two conventions that shape every decision module

Both come straight from the official docs ([docs/JEV_API.md](docs/JEV_API.md)):

1. **One call, many questions.** Jev evaluates every question in parallel against the same state and adding questions barely costs latency 鈥?so each module asks everything it needs at once for free.
2. **Noul has no separate confidence** 鈥?the returned probability *is* the belief. A probability near 0.5 means "cannot tell", so the band `(0.40, 0.60)` triggers a rule fallback instead of a coin-flip action.

## Status

馃毀 **Both halves of Phase 1 exist now, and the brain has run against real JEV.**
All 7 decision modules + HP budget + greedy combat + logging + simulation harness
+ agent router + the real JEV client + the **CommunicationMod stdio transport** are
in place, with **165 tests passing**. Live decisions and simulator decisions now
share the same rich `RunContext`, so both paths ask JEV against the full run
digest and the game's own card/relic text.

What is left is hardware, not code: install CommunicationMod (see
[docs/SETUP.md](docs/SETUP.md)), which is a jar download and one config line.

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

**Everything measured so far is in [docs/MEASUREMENTS.md](docs/MEASUREMENTS.md)** 鈥?
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
- **`PLAY` is 1-indexed** while `CHOOSE` is 0-indexed 鈥?the conversion happens in
  exactly one function;
- **there is no `skip`, `purge`, or `smith` verb** 鈥?skipping a reward and leaving
  a shop are both `RETURN`;
- **nothing prints to stdout except `Ready` and commands**, because stdout *is*
  the protocol.

Plus three rules that keep a long run alive: answer only when the game says it is
ready, never send a verb the game did not advertise, and one command per state.

## Prerequisites

- Steam copy of Slay the Spire
- ModTheSpire + BaseMod (+ StSLib) + CommunicationMod 鈥?all free
- Python 3.10+
- JEV access 鈥?**not required for development**: the mock client lets you build,
  run and test everything offline. Real JEV is reachable on OpenRouter's System
  One route (see [docs/JEV_API.md](docs/JEV_API.md)).

## Setup

```bash
git clone https://github.com/Adrian-lzr/jev-spire-brain.git
cd jev-spire-brain
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
non-ASCII characters written as raw UTF-8 is silently misread 鈥?and because the
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
- MIT License 鈥?see [LICENSE](LICENSE).

