# Jev Spire Brain

> A GPT + JEV external coach for [Slay the Spire](https://store.steampowered.com/app/646570/Slay_the_Spire/) — GPT owns the run strategy, JEV ranks local choices, and deterministic code handles legality and execution.

GPT is the strategic brain, JEV is the tactical layer, and CommunicationMod is only reached through the local legal-action broker. Without an OpenAI key the existing JEV + guide-rule path remains available.

Ironclad is no longer driven by one fixed draft heuristic. The editable
`config/archetypes.json` profiles cover adaptive port-filling, Strength,
Exhaust, Block, self-damage and Status-conversion lines. The selector commits
only after enough deck evidence, returns to adaptive survival play at low HP,
and sends the selected profile to both GPT and JEV. See
[`docs/STRATEGY_PROFILES.md`](docs/STRATEGY_PROFILES.md) for the source-backed
rules.

## What it is

Jev Spire Brain is a three-layer agent architecture for Slay the Spire:

| Layer | Responsibility | Implementation |
|---|---|---|
| Strategic brain | run goals, build direction, resource budget, replanning | `spirebrain/brain/` (`StrategicPlan`, OpenAI/mock providers) |
| Tactical layer | rank a small legal candidate set and translate intent | JEV primitives (`Choice` / `Score` / `Noul`) |
| Legality & execution | enumerate candidates, enforce hard rules, emit one protocol command | `ActionBroker` + deterministic code (spirecomm) |

The agent observes the full game state via [CommunicationMod](https://github.com/ForgottenArbiter/CommunicationMod) (JSON state stream), decides with calibrated probabilistic judgments, and sends actions back through the same pipe.

## Two modes, and the default is the one that helps you play

The project exists to make the player better, not to replace them, so the agent
has two jobs and **`advise` is the default**:

| Mode | What it does | How to get it |
|---|---|---|
| **`advise`** (default) | Recommends. Sends the game nothing but polls (`wait`/`state`) — no play, no choose, no proceed, no auto-start. You keep the mouse and the keyboard. | `python start.py` (or just double-click `日常启动.bat`) |
| `play` | Auto-plays the run itself. This is what the offline measurements needed. | `python start.py --play` |

In advice mode the same pipeline runs — the JEV call, the score gate, the
tactical layer — and only the destination of the answer changes: a
**recommendation for you** instead of a command for the game. Then the other half
runs, and it is the reason this project can say anything honest about its own
advice: every state change is diffed against the recommendation to infer **what
you actually did**, and the result is scored.

| Term | Meaning |
|---|---|
| `match` | You did what was recommended |
| `mismatch` | You did something identifiable, and it was different |
| `unobserved` | The state does not single out an action (a potion, a text event) — reported honestly instead of guessed |

Where to watch it — **the panel is in the game** (since 2026-09-22 evening):

| Surface | What it shows | Needs |
|---|---|---|
| **In-game overlay** (primary) | `战斗 / 出「痛击」→ 咔咔 / 为什么 / 你刚才: 出「打击」 没采纳 / 命中率` + the HP-budget bar. `F8` hides and shows it. | the `SpireBrainOverlay` mod (installed with `python java/build.py --install`) and the agent running |
| Dashboard page (`python start.py` opens it) | the full reasoning: Score rankings, Noul probe tables, route preview, the advice panel, and the verdict history | a browser tab — optional, for studying a run afterwards |

A coach you have to alt-tab to read is not a coach, which is why the in-game
panel is the default answer and the browser is now the *optional* one. Rebuild
and reinstall the panel any time with:

```bash
python java/build.py --install      # compile (needs a JDK), verify, copy into mods\
```

The panel shows `waiting for the agent (python start.py)` when the agent is not
running — silence would be indistinguishable from a broken mod.

`logs/advice.jsonl` keeps the machine-readable record of everything: one line per
recommendation and per verdict, with the *evidence* for every inference — a
verdict nobody can audit is not a measurement.

Two honest limitations, measured rather than assumed: **advice lag** (if you act
before a recommendation lands, that pair scores as `unobserved` rather than as a
false mismatch) and **concealed actions** (potions and text-event choices leave no
trace in the state). Both show up as `unobserved`, never as an invented verdict.

See what the advisor looks like before wiring anything up:

```bash
python run_dashboard.py --advise-demo          # scripted ascent, mock brain, no key
python run_dashboard.py --advise-demo --backend openrouter   # real JEV advice
```

## Decision points supported by JEV

All seven are implemented in `spirebrain/jev_brain/decisions.py` and covered by tests.

- **Map routing** — `Choice` over reachable nodes + parallel `Noul` risk probes against the HP budget (`MapRouter`)
- **Card rewards** — `Score` × candidates against the deck, with a skip path (`CardRewardJudge`)
- **Events** — `Choice` over event options (`EventChooser`)
- **Rest sites** — `Noul` (heal?) + `Choice` (which upgrade) in one call (`RestSiteDecider`)
- **Shops** — one parallel `Noul` per affordable item, "worth the gold for this goal?" (`ShopDecider`)
- **Boss relics** — `Score` × 3; the pick is mandatory, so an unsure pick is flagged rather than skipped (`BossRelicJudge`)
- **Combat risk** — `Noul` (will predicted damage exceed the HP budget?) →tactical posture (`CombatRiskGate`)

GPT is called at strategic boundaries (new run/act, map, rewards, shop, combat start and major resource changes). Ordinary combat steps do not call GPT again: local combat rules enumerate the next legal card, target, potion or end-turn action, and JEV can rank only that bounded set. No model can emit a raw CommunicationMod command.

## Two conventions that shape every decision module

Both come straight from the official docs ([docs/JEV_API.md](docs/JEV_API.md)):

1. **One call, many questions.** Jev evaluates every question in parallel against the same state and adding questions barely costs latency — so each module asks everything it needs at once for free.
2. **Noul has no separate confidence** — the returned probability *is* the belief. A probability near 0.5 means "cannot tell", so the band `(0.40, 0.60)` triggers a rule fallback instead of a coin-flip action.

## Status

**Both halves of Phase 1 exist now, and the brain has run against real JEV.**
All 7 decision modules + HP budget + greedy combat + logging + simulation harness
+ agent router + the real JEV client + the **CommunicationMod stdio transport** are
in place, with the offline suite run by `python -m pytest -q`. Live decisions and simulator
decisions share the same rich `RunContext`, so both paths ask JEV against the full
run digest and the game's own card/relic text.

**Advisor mode is in (2026-09-22 evening), and it is the default.** The agent can
now do the job the project was actually for: recommend to a human who is playing,
then measure whether the recommendation was taken (`driver/witness.py`, 15 tests
in `tests/test_advise.py`). The hard rule is asserted on every screen: in advice
mode the only verbs that ever reach the game are `wait` and `state`.

✅ **Phase 1.5 — the interaction layer — is in.** The brain now has a window:
`spirebrain/overlay/` streams every decision (value, confidence, probabilities,
Score distributions, fallback reasons) to a local web dashboard over SSE, live
while the run happens, plus a `军师建议` panel for advisor mode. Zero game
invasion: the feed is an optional constructor argument, and a dead dashboard can
never break a run (proven by
`tests/test_overlay.py::test_agent_survives_a_hostile_feed`).

**The guards** are three runaway guards ported from
[Ethics03/jevspire](https://github.com/Ethics03/jevspire) (the only other
CommunicationMod+JEV project), plus a fourth of our own, all "stop spending, keep
the game alive": a **stall guard** that latches auto-decisions off when the
identical game state returns after our command (it did not take effect —
retrying is how an agent burns money in a loop); an **action limit** (default 5000
commands, **per run** — refilled on every menu→in-game edge,
`JEVBRAIN_MAX_ACTIONS` to change, <=0 unlimited; unlimited by default in advise
mode, where every command is a poll and the count is a clock, not a budget) that
stops a confused run loudly instead of quietly; **navigation without the model** —
non-decision screens get Proceed and an unchanged shop shelf/gold snapshot is
not re-asked, while a changed shelf or purchase can trigger a new strategic
plan; and the **unmodeled-screen ladder**
(own design, 2026-09-22 evening): a screen whose `available_commands` offer no
advancing verb gets two waits, SPACE, ESCAPE, a centre click — three cycles at
most — then a loud pause with a reason, auto-resuming the moment a known
screen returns. Born from a real death: after Neow's reward the game showed a
screen the mod cannot act on, and the old code answered with 991 `wait 20` in
66 seconds, hit the cap, and exited — the player saw "agent not reachable".
Replayed through the fixed transport: 15 commands, one announcement, alive.
The next death that night was a GRID screen offering `[confirm, cancel, ...]`:
two real protocol verbs our alias table was silently rewriting to `proceed` and
`return`, which the screen had not offered, so the line degraded to a `wait` on a
screen that had already been answered. Both nights' failures are pinned in
`tests/test_guards.py` and `tests/test_stdio.py`.

## Quick start — one command

```bash
python start.py --demo      # no game, no API key: watch a simulated run right now
```

A dashboard opens in your browser and a simulated ascent plays out on it —
every route preview, card score, confidence bar and fallback reason, live.

To have the brain **advise you while you play** (the default mode):

```bash
python start.py             # environment check + dashboard + the mod config line
```

Then launch Slay the Spire through ModTheSpire with the mods enabled and **play
normally**. The panel on the left tells you what the brain would do and why; after
each of your moves it shows what you did and whether it matched. The agent sends
the game nothing but polls, and it will not even start a run for you — picking
class, ascension and seed stays yours.

To have it **play the run itself** instead: `python start.py --play`.

On Windows the two double-clickable launchers cover both:
`日常启动.bat` (advise) and `首次启动（含环境配置）.bat` (first run: environment
check + mod config).

With **real JEV** put `OPENROUTER_API_KEY=...` in a `.env` file, then:

```bash
python start.py --setup     # also writes the game's mod config (backs up the old one)
```

`--setup` is remembered — after the first time, plain `python start.py` is the
whole routine.

The advisor now has an optional strategic model layer above JEV. Put
`OPENAI_API_KEY=...` (or the provider-neutral `BRAIN_API_KEY=...`) in `.env` to
enable it. The client speaks the common OpenAI-compatible Chat Completions
protocol, so mainland providers can be selected without code changes:
`BRAIN_BACKEND=deepseek|qwen|zhipu|moonshot|siliconflow|doubao`, with
`BRAIN_MODEL` and `BRAIN_ENDPOINT` when overriding the built-in presets. The
legacy `OPENAI_MODEL` / `OPENAI_BRAIN_ENDPOINT` names remain supported. GPT (or
the selected model) creates a short run plan and
resource goal; JEV and the local legality layer choose the concrete candidate.
Network-backed strategic requests run in a bounded background worker in both
advisor and play modes, so a slow API cannot stall the CommunicationMod pipe;
the current JEV/rule candidate is used until a state-matched plan arrives.
The game overlay shows the current step, GPT goal and a legal alternative. A
missing key, timeout or malformed response falls back to the existing JEV and
guide rules without blocking the game. Set `BRAIN_BACKEND=mock` for an offline
strategic-brain demo, or `BRAIN_BACKEND=jev`/`disabled` to keep the old path.
Strategic calls are recorded separately in `logs/brain_calls.jsonl`. Compatible
providers may return a compact `steps`/`plan` list; only candidate IDs and short
reasons are adapted into a local plan, and all actual commands still pass local
candidate generation and legality checks.

Model settings are intended to be edited in the local player console, not by hand
in a file. Start `python start.py`; it opens `http://127.0.0.1:8787` automatically.
Use **快速配置** to choose a provider, test the connection, and press **应用并启动军师**.
The dialog configures the
strategic backend/model/endpoint and JEV provider credentials, reports connection
tests without revealing saved keys, and writes settings to the project `.env` file.
The file is an implementation detail; players do not need to open or edit it.
Restart the game agent after saving for changes to take effect. The dashboard binds
to loopback only; credentials never leave the local machine.

Everything start.py does, done manually:

```bash
python -m spirebrain.doctor                              # what is missing, and how to fix it
python run_dashboard.py --demo        # simulated run, mock JEV, no key needed
python run_dashboard.py --advise-demo # the advisor panel: scripted player, real chain
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

离线回放和指标入口（不会调用模型或修改游戏配置）：

```bash
python -m spirebrain.analysis.replay --input tests/fixtures/replay
python -m spirebrain.analysis.metrics --input tests/fixtures/metrics/decision_trace.jsonl
python tools/verify_offline.py           # tests + compile + contracts + metrics + replay
```

指标输入缺失时命令会以非零状态退出；合成 fixture 只用于验证管道和字段契约，
不代表真实游戏胜率。

`tools/verify_offline.py` 会逐项执行离线检查并在某项失败后继续输出其余结果，
适合作为干净检出和 CI 的单一入口。当前执行起点、配置摘要、已知限制和未验证的
真实游戏行为记录在 [`docs/BASELINE_2026-09-24.md`](docs/BASELINE_2026-09-24.md)。

`doctor` exists because "is the mod installed?" turned out to need six manual
checks, and one of them is genuinely non-obvious: a Steam Workshop **subscription**
and a Workshop **download** are different states in `appworkshop_<appid>.acf`. The
id under `WorkshopItemDetails` means Steam knows you subscribed; only
`WorkshopItemsInstalled` means the files are on disk.

`install_mod_config` exists because the config file is read as **ISO-8859-1**
(`SpireConfig.load()` →`Properties.load(FileInputStream)`), so a path with
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

# strategic GPT brain (independent of JEV_BACKEND)
export BRAIN_BACKEND=openai         # openai | mock | disabled
export OPENAI_API_KEY=...
export OPENAI_MODEL=gpt-4.1-mini
```

The verified request/response shapes, the confidence conventions, and what is
still unverified are all written down in [docs/JEV_API.md](docs/JEV_API.md).

## Credits & license

- Architecture informed by [spirecomm](https://github.com/dweih/spirecomm) (MIT), [CommunicationMod](https://github.com/ForgottenArbiter/CommunicationMod), and the JEV ecosystem's `jev-drone` / `typesafe-mario` layered designs.
- JEV is a model by [TypeSafe AI](https://typesafe.ai/). This project is independent and not affiliated.
- MIT License — see [LICENSE](LICENSE).

