# Release Audit — 2026-09-28

This is an engineering and replay audit, not a gameplay-quality or win-rate
claim. The audit was run from commit `2df3c82a36af4dc8a78cbca9b601fa722fb50ea0`
with uncommitted working-tree changes present. Runtime: Windows,
CPython 3.12.10 (`C:\Users\Lenovo\AppData\Local\Programs\Python\Python312\python.exe`).

## Offline Evidence

| Check | Result | Meaning |
| --- | ---: | --- |
| `python -m pytest -q` | 530 passed | Python tests, synthetic fixtures only |
| `python -m compileall -q spirebrain start.py run_agent.py tools` | passed | Syntax/bytecode check |
| `python tools/check_contracts.py` | passed | Python/dashboard/Java static contract |
| `python tools/verify_offline.py` | 9 checks passed | Tests, compile, contracts, metrics, evaluation, replay, async replay, RAG and launcher smoke |
| `python -m spirebrain.analysis.rag_eval` | passed | 9/9 expected retrieval cases, citation provenance 1.0, Ironclad isolation |

The four synthetic evaluation stacks made distinct provider calls on the same
fixture. All stacks reported legal actions and `advise` wire safety. Real
outcomes remain `unknown`.

## Smoke Gate

`tools/real_game_smoke.py` now reports three separate signals:

- `pipeline_passed`: the game/agent trace produced at least one decision;
- `provider_health_passed`: both JEV and strategic traces contain only explicit
  successful provider results;
- `release_passed` (also `passed`): the pipeline and both providers are healthy,
  and no provider result was later marked expired.

An opted-in smoke with provider fallback, unavailable providers, unknown health,
or stale results exits non-zero. This prevents a rules-only run from being
accepted as a healthy model integration while preserving the diagnostic
pipeline signal.

The most recent 30-second real-game smoke before this audit produced three
decisions. One strategic response completed after its state was obsolete and was
discarded; JEV requests failed at the network layer. Therefore that run is
pipeline evidence with degraded provider health, not release success. The
smoke runner backs up and restores CommunicationMod configuration and the
Ironclad save targets; no key or game configuration is included here.

## RAG Audit

The local, source-cited Ironclad index contains eight paraphrased chunks from
four public guide URLs. Fixed queries cover nine strategy scenarios. Retrieval
coverage and citation provenance are measured, but lexical retrieval is not an
expert correctness label. No result is converted into a command, candidate ID,
or legality decision. There are no expert labels or paired real-game outcomes,
so strategy quality and win rate remain `unknown`.

## Remaining Release Risks

- Clean Linux runtime evidence is still unavailable on this host (no WSL
  distribution or Docker).
- Java compilation with the game/ModTheSpire/BaseMod jars is environment-bound;
  CI performs static contract checks only.
- The smoke runner now defaults to `advise`; `--mode play` is an explicit
  automation stress mode. The prior live run used play mode and was intentionally
  degraded, so it cannot prove that a stable player-controlled state receives a
  usable strategic response. A fresh advise-mode run is required after provider
  configuration is verified.
- No expert-labeled decisions, paired seeds, or sufficiently large real-game
  sample exists to support a win-rate claim.
- Real player-visible display latency, full game semantics for powers/relic
  chains/random effects, and cross-version CommunicationMod fields still need
  manual replay coverage.

## Follow-up Evidence

The later 30-second real-game smoke completed with `pipeline_passed=true` and
`strategic_health=healthy`. The strategic provider returned two successful
responses; both arrived after their decision states had changed and were
discarded as action proposals. Their strategic context was retained for later
planning. This is evidence that the provider is reachable and stale concrete
actions are not published; it is not evidence that a full strategic proposal
was displayed in time.

JEV made four requests: three succeeded and one failed with a network error.
Accordingly, `jev_health=degraded` and `release_passed=false`; the agent
continued its decision pipeline using its fallback path. This smoke is not a
fully healthy release pass. Request lifecycle diagnostics include request and
response counts, expired results, in-flight request IDs, and whether the run
ended with a request pending. Provider traces now carry generation and trigger
metadata, while replaced pending work records the new pending state, generation,
and run identity. These fields support stale-result diagnosis without logging
credentials or full request bodies.

The current offline verification after these changes reports 561 tests passed,
`compileall` passed, static contracts passed, and all 10 offline verifier checks
passed. Synthetic combat evaluation still reports legality and objective
coverage only; real outcomes and win rate remain `unknown`.

The real-game smoke used the existing guarded backup/restore flow for local
game configuration and save targets. It did not establish player-visible
latency, and it must not be interpreted as a win-rate or strategic-quality
claim. The JEV network failure remains an integration issue to investigate.

## Latest Verification (2026-09-28)

The current working tree was rechecked with CPython 3.12.10 on Windows:

| Check | Result | Scope |
| --- | ---: | --- |
| `python -m pytest -q` | 571 passed | Offline unit, replay, provider, simulator and smoke tests |
| `python -m compileall -q spirebrain start.py run_agent.py tools` | passed | Python syntax/bytecode |
| `python tools/check_contracts.py` | passed | Python/dashboard/Java field and default contract |
| `python tools/verify_offline.py` | 10/10 passed | Tests, compile, contracts, metrics, evaluation, replay, async replay, RAG, combat benchmark and launcher smoke |

The latest opted-in `play` smoke completed the pipeline and reached the
strategic provider, but strategic responses arrived after their state had
changed and were therefore retained only as `gpt_context`; no stale concrete
candidate was published. JEV requests included successful responses as well as
bounded network/timeout failures, so the run remained degraded and did not
pass the release gate. The default `advise` smoke does not start a new game on
behalf of the player; without a manually started run it correctly reports no
decision rather than treating that as provider failure.

A separate live strategic provider probe using the resolved runtime
configuration received a structured plan successfully (`deepseek` backend,
`deepseek-flash` model, approximately 11.5 seconds). The probe confirms
reachability and response parsing only; it is not a player-visible latency,
gameplay-quality or win-rate measurement. No response body, credential or
full request payload is stored in this audit.

The trace audit also now validates pending state, generation and run identity
types on `expired_result` events. This prevents malformed asynchronous
lifecycle data from being accepted by replay. The focused trace/replay/smoke
suite passed 60 tests. Historical test counts above remain for traceability;
the current count is 571 passed and real outcomes remain `unknown`.

The live provider diagnostic was subsequently run with the resolved runtime
configuration. The strategic provider returned a structured plan successfully
(`deepseek`/`deepseek-flash`, about 4.4 seconds). This proves connectivity and
schema parsing for one request, but not player-visible latency or gameplay
quality. Smoke and provider logs are now bounded JSONL files (32 MiB active file
and two backups by default) so repeated real-game polling cannot grow the
working directory without limit.

The offline test helpers now explicitly inject `brain_backend="none"` unless a
test is exercising a strategic mock. This prevents a developer `.env` from
starting a live strategic worker during a temporary-directory test and removes
the intermittent Windows `WinError 145` cleanup race. After this isolation,
the full suite and the independent offline verifier both pass serially.

Rotated trace files remain replayable: the active JSONL and retained backups are
loaded oldest-first, deduplicated by `trace_sequence`, and the next writer
continues from the maximum retained sequence. Strategic provider logs use the
same bounded rotation, so replay evidence remains available without allowing
long-running sessions to grow these files indefinitely.
