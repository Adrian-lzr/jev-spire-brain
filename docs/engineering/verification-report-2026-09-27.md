# Closed-loop plan verification report

Date: 2026-09-27
Branch: `plan/closed-loop-20260927`
Baseline commit: `faea6b019f0d81119d014c1b33f8f6052039a984`
Environment: Windows, CPython 3.12.10

## Offline evidence

| Check | Result | Scope |
|---|---:|---|
| `python -m pytest -q` | 468 passed | Python unit/integration tests, synthetic only |
| `python -m compileall -q spirebrain start.py run_agent.py` | passed | Python syntax/bytecode compilation |
| `python tools/check_contracts.py` | passed | Shared fixture, Python/dashboard projection, Java static consumer surface |
| `python tools/verify_offline.py` | passed | Tests, compile, contracts, metrics, four-stack evaluation, sync/async replay, launcher smoke |
| `python java/build.py` | passed | Local Java 8-target overlay build and jar verification; not installed into the game |
| Representative evaluation | passed | 33 synthetic states across map/combat/reward/event/shop/rest/death/victory |
| Environment doctor | no FAIL | Local game/mod/config discovery; no model request |

The representative four-stack run reported these provider call counts:

| Stack | JEV calls | Strategic calls | Total |
|---|---:|---:|---:|
| rules | 0 | 0 | 0 |
| jev | 19 | 0 | 19 |
| strategic | 0 | 31 | 31 |
| full | 25 | 31 | 56 |

All four stacks preserved `advise` wire safety. Synthetic runs do not contain
ground-truth labels, so real outcomes and win rate are `unknown`.

## Implemented evidence

- Semantic combat search enumerates supported card-target branches, tracks visible
  per-enemy HP/block/threat, and distinguishes immediate facts from projections.
- Run/memory workers consume deep snapshots; owner commit is the only plan commit
  path. Lifecycle regressions cover A-to-B-to-A, same-seed restart, deviation,
  late result, zero-call budget, snapshot isolation and owner-only copying.
- Provider request/response identities and advice revisions are traceable. New
  memory/result events use canonical event kinds while legacy aliases remain
  readable. Trace writes expose contract errors without blocking the game loop.
- Cross-language validation consumes the shared advice fixture and checks types,
  null/default behavior, enums and Java accessors without pretending to build the
  game mod in CI.
- Setup documentation is machine-independent; no API key or local game config is
  committed by offline checks.

## Not verified

- Clean Linux runtime: this host has no installed WSL distribution and Docker is
  unavailable. GitHub Actions is configured for Ubuntu but has not been used as a
  substitute for local evidence.
- Portable Java compilation in CI (the local build above depends on machine-installed
  game/Mod JARs and is intentionally not represented as a CI build pass).
- Paid or live GPT/JEV requests, provider latency, API availability and cost.
- Real CommunicationMod play, overlay rendering and player-visible display time.
- Full game semantics for powers, strength/weakness interactions, random effects,
  relic chains and hidden RNG.
- Expert-labeled decision quality, paired game outcomes and win-rate improvement.

The correct release claim at this point is **offline engineering and replay
improvement**, not improved game win rate. A real `advise` run and then controlled
paired seeds are required before making a gameplay-quality claim.
