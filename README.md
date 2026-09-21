# Jev Spire Brain

> A JEV-powered external brain for [Slay the Spire](https://store.steampowered.com/app/646570/Slay_the_Spire/) — letting a TypeSafe **System One model** make the strategy calls while deterministic code handles the tactics.

让 JEV（TypeSafe System One 决策模型）作为外置大脑玩杀戮尖塔：语义判断归 JEV，精确计算归代码。

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
- **Combat risk** — `Noul` (will predicted damage exceed the HP budget?) → tactical posture (`CombatRiskGate`)

Combat card play itself stays in code (search/greedy), per the drone-rule: *JEV cannot be the perception layer and cannot run at control rate.*

## Two conventions that shape every decision module

Both come straight from the official docs ([docs/JEV_API.md](docs/JEV_API.md)):

1. **One call, many questions.** Jev evaluates every question in parallel against the same state and adding questions barely costs latency — so each module asks everything it needs at once for free.
2. **Noul has no separate confidence** — the returned probability *is* the belief. A probability near 0.5 means "cannot tell", so the band `(0.40, 0.60)` triggers a rule fallback instead of a coin-flip action.

## Status

🚧 **Phases 0/2/3/4 complete offline; Phase 5 client written.** All 7 decision modules + HP budget + greedy combat + logging + simulation harness + the real JEV HTTP client are in place, with **56 tests passing**. Phase 1 (the live game pipe) is the one piece that needs the mods installed — see [docs/SETUP.md](docs/SETUP.md).

```bash
# run everything offline right now (no game, no API key):
python -m spirebrain.sim.run_offline                # pessimistic mock: every module falls back (24 calls)
python -m spirebrain.sim.run_offline --optimistic   # confident mock: JEV actually steers the run
python -m spirebrain.analysis.calibration logs      # confidence bucket report
python tests\test_client_real.py                    # payload/parse against the published API response
```

Python package name stays `spirebrain` (import name); repo name is `jev-spire-brain`.

## Prerequisites

- Steam copy of Slay the Spire
- ModTheSpire + BaseMod + CommunicationMod (free)
- Python 3.10+
- JEV access (TypeSafe early access, Cloudflare `typesafe/jev`, or Vercel AI Gateway `typesafe-ai/jev`) — **not required for development**: the mock client lets you build, run and test everything offline.

## Setup (once playable)

```bash
git clone https://github.com/Adrian-lzr/jev-spire-brain.git
cd jev-spire-brain
pip install -r requirements.txt
# configure CommunicationMod to launch driver/agent.py (see docs/SETUP.md)
```

## Switching the brain on

```bash
export JEV_BACKEND=mock        # default: deterministic, offline
export JEV_BACKEND=official    # TypeSafe direct ...
export TYPESAFE_API_KEY=...    #    ... needs this
export JEV_BACKEND=cloudflare  # Cloudflare AI ...
export CLOUDFLARE_ACCOUNT_ID=... CLOUDFLARE_API_TOKEN=...   #    ... needs these
```

The verified request/response shapes, the confidence conventions, and what is
still unverified are all written down in [docs/JEV_API.md](docs/JEV_API.md).

## Credits & license

- Architecture informed by [spirecomm](https://github.com/dweih/spirecomm) (MIT), [CommunicationMod](https://github.com/ForgottenArbiter/CommunicationMod), and the JEV ecosystem's `jev-drone` / `typesafe-mario` layered designs.
- JEV is a model by [TypeSafe AI](https://typesafe.ai/). This project is independent and not affiliated.
- MIT License — see [LICENSE](LICENSE).
