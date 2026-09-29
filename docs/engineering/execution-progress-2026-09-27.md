# Architecture repair execution progress

Date: 2026-09-27
Plan: `D:/jev-spire-brain-ai-execution-plan.md`
Repository HEAD at start: `faea6b019f0d81119d014c1b33f8f6052039a984`
Working branch: `plan/closed-loop-20260927`

## S0 — completed

Environment: Windows, CPython 3.12.10.

Baseline commands and results:

| Command | Exit | Result |
|---|---:|---|
| `python -m pytest -q` | 0 | 425 passed in 7.34s |
| `python tools/verify_offline.py` | 0 | tests, compile, contracts, metrics, replay and launcher smoke passed |
| `python -m compileall -q spirebrain start.py run_agent.py` | 0 | passed |

The baseline is already newer than the plan's historical `7c66ca1` snapshot. The
working tree contains only untracked `.playwright-cli/`, `sendToDevs/`, and
`steam_appid.txt`; these are intentionally excluded from implementation commits.

## Problem mapping at current HEAD

| ID | Current status | Evidence / next action |
|---|---|---|
| F01 | partially addressed | Owner run identity and immutable factual `RunMemorySnapshot` now reach the advice worker; a committed deviation invalidates its cached plan and appears in the next provider payload; typed plan proposal/owner commit remains open |
| F02 | partially addressed | Added C01 defensive choice, C03 remaining-attacker threat/reason, adjusted-damage C08, fake-clock budget exhaustion, input immutability, and legal timeout fallback tests; per-action target enumeration and complete C01–C08 semantics remain open |
| F03 | targeted regression present | `test_sequence_forecast_does_not_overwrite_first_action_facts` separates immediate and projected facts; remaining combat semantics still require audit |
| F04 | targeted regression present | `test_rebinding_facts_after_action_arbitration_uses_final_card` checks final candidate facts; broader play/JEV path coverage remains outstanding |
| F05 | under review | request IDs exist in provider logging; verify task-created decision identity reaches every event |
| F06 | under review | advice revision tracking exists; verify first publish is not replacement |
| F07 | partially addressed | `EnvironmentAdapter` and synthetic game-data fixtures exist; CI should expose checks independently |
| F08 | partially addressed | synchronous and timing replay fixtures exist; timing replay is not yet a substitute for full asynchronous lifecycle replay and controlled real-thread coverage |
| F09 | partially addressed | `DecisionBudget`, latest-wins queues, and a deterministic expired-before-start/no-provider-call regression exist; full A01–A12 lifecycle coverage remains outstanding |
| F10 | partially addressed | static cross-language contract exists; add type/null/default fixture validation |

## Scope and evidence rules

No real provider, API key, game configuration, or automated game input is used by
offline verification. Synthetic replay data remains labelled synthetic. Real-game
and Java-with-game-JAR checks are not claimed unless actually executed.

## Continuation update — 2026-09-27

The implementation is **not complete**. Work remains uncommitted on
`plan/closed-loop-20260927`; no remote push, merge, real API call, or real-game
test has been performed.

This continuation found a concrete S2/F02 defect: `incoming_damage()` counted
stale attacks from enemies marked `is_gone` or `half_dead`, while the per-enemy
threat calculation ignored them. Added
`test_incoming_damage_ignores_gone_and_half_dead_enemies_but_counts_multihit`,
observed it fail first with 112 damage instead of the expected 12, then fixed
both paths to consistently ignore explicitly inactive or known-zero-HP enemies.
Unknown HP remains conservative and keeps an attack intent in the threat sum.

Post-fix validation on Windows / Python 3.12.10:

| Command | Result |
|---|---|
| `python -m pytest -q` | 432 passed in 6.69s |
| `python -m pytest -q tests/test_combat_s2.py tests/test_combat_search_phase4.py tests/test_combat_engine.py` | 13 passed |
| `python -m compileall -q spirebrain start.py run_agent.py` | passed |
| `python tools/check_contracts.py` | passed; static contract only, Java build not attempted |
| `python tools/verify_offline.py` | tests, compile, contracts, metrics, replay, launcher smoke all passed |

The full checklist still has incomplete S1–S7 acceptance work, including clean
Linux CI evidence, the complete C01–C08 and T01–T08/A01–A12 test matrices,
full asynchronous replay, shared schema fixture validation by actual consumers,
the required distinct four-mode evaluation, and architecture/setup/measurement
documentation updates. The pass count is offline correctness evidence only;
it does not establish model quality, Java/game integration, latency improvement,
or win-rate improvement.

## Continuation update — same-scope asynchronous replan

S4 review found a same-scope lifecycle gap. While a strategic request was in
flight, `plan_for()` treated any request from the same run/screen/act/floor as
equivalent. A changed event choice set therefore did not replace the old
request. The result queue also returned positional tuples, and result draining
could use broad scope equivalence instead of proving that the result belonged
to the latest pending request.

Added `test_changed_event_options_supersede_same_scope_inflight_plan`. It first
failed because the changed event choices left `_queued_plan` empty. The fix
binds worker results to their immutable `DecisionRequest`, commits only when
that exact request is still pending, and invalidates pending strategic work
when same-scope strategic inputs change. Ordinary irrelevant poll changes and
combat hand churn remain covered by the existing non-supersession tests;
combat plan requests are superseded for inventory/deck/gold/enemy-composition
changes or a known HP danger-threshold crossing. An invalidated network call is
allowed to finish, but its result is counted and discarded as
`superseded_request`. Scheduler counters and invalidation/discard reasons are
included in strategic diagnostic detail.

Validation on Windows / Python 3.12.10:

| Command | Result |
|---|---|
| `python -m pytest -q tests/test_brain.py tests/test_run_memory_events.py tests/test_phase_b_budget.py` | 43 passed |
| `python -m pytest -q` | 433 passed in 6.48s |
| `python -m compileall -q spirebrain start.py run_agent.py` | passed |
| `python tools/check_contracts.py` | passed; Java build not attempted |
| `python tools/verify_offline.py` | tests, compile, contracts, metrics, replay, launcher smoke passed |

The plan remains incomplete. In particular, S1 clean Linux evidence, full S2/S3
acceptance matrices, S4's remaining A01–A12 lifecycle cases and bounded shutdown
coverage, S5's asynchronous replay and distinct four-mode evaluation, S6's
consumer-level shared schema checks, and S7's documentation/final audit remain
open. No real provider or game session was used.

## Continuation update — bounded shutdown and completion queue

Further S4 inspection found that the advisor completion queue was unbounded and
neither the async strategic worker nor the advisor worker exposed a bounded
shutdown path. Added max-size-one latest-wins advisor completion storage,
generation invalidation during close, bounded worker joins, and `StdioTransport`
cleanup in a `finally` path so EOF or stream failure closes its owned sessions.
`SpireBrainAgent.close()` delegates to the strategic planner; a provider call
that cannot be cancelled may outlive the bounded wait, but its result cannot be
committed after close.

Regression coverage added:

- `test_orchestrator_close_wait_is_bounded_and_invalidates_late_result`
- `test_advisor_completion_queue_is_bounded_and_latest_wins`
- `test_advisor_close_is_bounded_and_invalidates_late_completion`
- `test_stdio_eof_closes_advisor_and_agent_workers`

The first stdio integration-test run exposed a missing test-local import; after
fixing that harness issue, all four targeted lifecycle tests passed. Final
Windows / Python 3.12.10 verification for this continuation:

| Command | Result |
|---|---|
| `python -m pytest -q` | 437 passed in 6.55s |
| `python -m compileall -q spirebrain start.py run_agent.py` | passed |
| `python tools/check_contracts.py` | passed; static check, Java build not attempted |
| `python tools/verify_offline.py` | tests, compile, contracts, metrics, replay, launcher smoke passed |

S4 is still partial: shutdown and same-scope supersession now have focused
evidence, while the remaining lifecycle matrix and bounded-thread integration
cases need completion. S1, S2, S3, S5, S6 and S7 acceptance items remain open as
listed above.

S1 clean Linux execution remains not run. This Windows host has `wsl.exe` but
reports no installed Linux distribution; Docker is not installed. The passing
workflow/static checks are not counted as Linux runtime evidence.

## Continuation update — owner memory snapshots and trace sequence

The advisor worker previously received only the owner run identity. Player
feedback was committed on the main agent, but the persistent private advice
clone could plan with a stale memory snapshot and keep using a plan that the
owner had invalidated. The worker job now carries an immutable
`RunMemorySnapshot`; adoption replaces factual run state and event history,
preserves only the same-run worker plan state, and invalidates that plan when a
new committed deviation is imported. The worker still cannot mutate owner
memory. A regression drives the next strategic provider request and verifies
that its memory payload contains the deviation while worker observations do not
change owner memory.

`DecisionTrace` instances writing the same JSONL path previously each started
their own sequence at 1. A shared path-keyed sequence now continues from the
last valid record and allocates/writes under one lock. Failure-first tests
reproduced both defects before the fixes: worker adoption raised a missing
argument `TypeError`, and two writers produced `[1, 1]` instead of `[1, 2]`.

Validation on Windows / Python 3.12.10:

| Command | Result |
|---|---|
| `python -m pytest -q tests/test_realtime_advisor.py tests/test_run_memory_events.py tests/test_phase5_trace_replay.py tests/test_stdio.py tests/test_phase_b_budget.py` | 73 passed |
| `python -m pytest -q` | 440 passed in 6.58s |
| `python -m compileall -q spirebrain start.py run_agent.py` | passed |
| `python tools/check_contracts.py` | passed; static contract only, Java build not attempted |
| `python tools/verify_offline.py` | tests, compile, contracts, metrics, replay, launcher smoke passed |

The plan remains incomplete. The advice clone still owns a private strategic
plan rather than returning a typed plan proposal for the owner to commit; this
is a remaining S4 ownership item. S1 Linux runtime evidence, the full S2/S3
acceptance matrices, S4 A01–A12 lifecycle coverage, S5 scheduler-driven fault
replay and representative four-stack evaluation, S6 shared-schema consumer
tests, and S7 final documentation/release audit remain open. No real provider,
game session, Java build, or win-rate experiment was used.

## Continuation update — combat threat facts and bounded-search fallback

The C01/C03 replay cases showed that the search result's `lethal_confirmed`
describes a killed target, not necessarily removal of all incoming threat. The
combat presenter now derives its current-step reason from that step's remaining
incoming damage and block, while sequence-level remaining damage is kept under
an explicitly projected field. This prevents the UI from claiming that the
turn's threat is gone when another attacker still deals damage.

Search-budget exhaustion is now distinct from `unknown_effect` and
`no_legal_action`. The tactical engine treats incomplete search as a fallback
condition and retains the legal greedy candidate when available. Added tests
use a monkeypatched clock rather than sleeping and verify both the distinct
fallback reason and unchanged input state. Adjusted enemy `move_adjusted_damage`
is asserted as the authoritative visible value; the base `damage` value remains
covered when the adjusted field is absent.

Failure-first evidence: before the fix, the multiple-attacker test failed
because the reason described the threat as eliminated despite 20 damage still
incoming. A fake-clock test verifies that budget exhaustion is not reported as
an unavailable action; after the fix both cases pass.

Validation on Windows / Python 3.12.10:

| Command | Result |
|---|---|
| `python -m pytest -q tests/test_combat_s2.py tests/test_combat_search_phase4.py tests/test_combat_engine.py` | 19 passed |
| `python -m pytest -q` | 446 passed in 6.70s |
| `python -m compileall -q spirebrain start.py run_agent.py` | passed |
| `python tools/check_contracts.py` | passed; static contract only, Java build not attempted |
| `python tools/verify_offline.py` | tests, compile, contracts, metrics, replay, launcher smoke passed |

At that checkpoint S2 was partial: the simulator still preselected a target
rather than enumerating card/target actions across per-enemy state transitions.
The subsequent continuation below closes the target-branching gap, while
strength, weakness, hit-count and broader status semantics remain unmodeled.
No real game fixture was recorded.

## Continuation update — S2 target branching, S4 lifecycle and S6 contracts

The combat search now enumerates every supported targeted card against each
living enemy instead of selecting one target before search. Branch state keeps
per-enemy effective HP/block, visible threat, energy and vulnerability; later
cards may retarget after an enemy dies. The result exposes `target_path` and
keeps immediate facts separate from projected sequence facts. Unknown/random
effects, complex powers, relic chains and hidden RNG remain explicit fallback
boundaries. Focused combat and real-time tests: 44 passed.

S4 lifecycle coverage now includes A-to-B-to-A generations, same-seed terminal
run isolation, deviation propagation into the next worker payload, late results
after reset, zero-call budget enforcement for scripted providers, no implicit
second planning request, deep request snapshot isolation and owner-only copied
plan commits. `StrategicPlanner` rejects an exhausted budget before invoking any
provider. Focused lifecycle tests: 43 passed.

S6's cross-language check now consumes the shared advice fixture and validates
Python/dashboard keys, Java field declarations and `opt*` accessors, null/default
semantics, source/display/status/mode enums and the ModTheSpire manifest. It is
still a static Java contract check because the game/BaseMod/ModTheSpire JARs are
not CI dependencies. Contract/display/replay tests: 44 passed.

The production trace writer now emits canonical `fact_memory`,
`deviation_record`, `unobserved_record` and `result_record` event types while
`decision_chain()` continues reading the legacy aliases. Latest Windows/Python
3.12.10 offline evidence after these changes:

| Command | Result |
|---|---|
| `python -m pytest -q` | 465 passed in 7.03s |
| `python -m pytest -q tests/test_phase5_trace_replay.py tests/test_s4_lifecycle.py tests/test_display_contract_phase6.py tests/test_combat_s2.py` | 37 passed |
| `python -m compileall -q spirebrain start.py run_agent.py` | passed |
| `python tools/check_contracts.py` | passed; fixture type/null/default/enum check, Java build not attempted |
| `python tools/verify_offline.py` | tests, compile, contracts, metrics, replay, async replay and launcher smoke passed |

The plan is still not complete. S1 has no clean Linux runtime evidence because
this host has no installed WSL distribution or Docker; S3's complete T01-T08
matrix and strict validation of every production event need further coverage;
S5 still has no fixed-model quality labels, although the committed
`representative_states.json` now contains 33 distinct synthetic states across
map, combat, rewards, events, shops, rest, death and victory. Its four-stack
evaluation reports distinct provider call counts and `real_outcomes=unknown`.
S7 final release audit remains open. No real provider, API key, Java game build,
real game session or win-rate experiment was used.

Latest post-fixture verification:

| Command | Result |
|---|---|
| `python -m pytest -q` | 468 passed in 7.66s |
| `python -m spirebrain.analysis.evaluate --input tests/fixtures/replay/representative_states.json` | 33 states; rules 0 provider calls, JEV 19, strategic 31, full 56; all passed |
| `python tools/verify_offline.py` | tests, compile, contracts, metrics, evaluation, replay, async replay and launcher smoke passed |

The local Java build was also run without installation or game-config writes:
`python java/build.py` compiled two sources with `--release 8`, packaged the
overlay and verified a 34-entry jar with 30 shaded `org.json` classes. CI still
uses the static contract check because those game/Mod JARs are not portable CI
dependencies.

Production trace events are validated at write time with strict event semantics;
invalid diagnostics carry bounded `contract_errors` instead of interrupting the
game loop. This preserves the logging failure isolation rule while making
contract drift observable.

## Continuation update — 2026-09-28: provider-attempt and expiry evidence

S3 trace auditing continued without a real provider or game session. Strategic
provider wrappers now retain a bounded `decision_id -> request_id` index, and
final decisions merge the actual JEV and strategic request identities instead
of inferring a request from a reused legacy `brain_request_id`. Advisor expiry
events therefore reference only causal provider requests; when no causal ID is
available they record `request_id=null` rather than attributing an older plan.

Offline timing replay now rejects duplicate responses for one request. Metrics
deduplicate provider-attempt log lines by run, decision, request and attempt
identity and expose `provider_duplicate_event_count`, preventing duplicate log
delivery from inflating call counts. Strategic expiry events use the same
`provider_request_ids` field as JEV and advisor expiry events.

Validation on Windows / Python 3.12.10:

| Command | Result |
|---|---|
| `python -m pytest -q` | 487 passed in 7.79s |
| `python -m compileall -q spirebrain start.py run_agent.py` | passed |
| `python tools/check_contracts.py` | passed; Java build not attempted |
| `python tools/verify_offline.py` | tests, compile, contracts, metrics, evaluation, replay, async replay and launcher smoke passed |

The remaining release limitations are unchanged: no clean Linux runtime,
real provider, real game session, portable Java game-JAR build, fixed-model
quality labels or win-rate experiment has been performed. Synthetic replay and
offline metrics are evidence of protocol/lifecycle behavior only.

The trace consumer now also emits a `request_lifecycle` summary for each
decision. It distinguishes open requests from expired requests and reports
orphan responses, duplicate request/response identities and mismatches in
decision/state/run/attempt fields. This makes an incomplete chain auditable
without treating a missing response as a successful or failed model call.
The latest offline validation after this addition is `488 passed`; compile,
contract and offline verification remain green.

## Continuation update — 2026-09-28: typed advice-worker proposal

The advice worker now returns an immutable `AdviceWorkerResult` containing a
detached `DecisionRequest`, command, history entry and optional strategic-plan
proposal. The owner session still accepts the legacy tuple envelope for
compatibility, but commits only after checking run ID, run epoch, generation,
state fingerprint and worker entry identity. A deliberately mismatched typed
context is recorded as `worker_identity_mismatch` and cannot publish advice or
modify owner memory. This closes the remaining S4 owner-only plan-commit gap
without introducing a queue or framework dependency.

Validation after this change: `489 passed`, compile and cross-language contract
checks passed, and `tools/verify_offline.py` passed all eight offline checks.

The launcher audit also found and removed the last live `.setup-done` branch:
ordinary startup now relies only on read-only CommunicationMod config validity,
while `--setup` remains the sole write path. The marker is ignored and retained
only as a deprecated compatibility function that always returns false. The
setup/startup regression slice now passes 24 tests.

## Continuation update — 2026-09-28: source-cited strategy retrieval

The strategic planning payload now includes a bounded offline
`knowledge_context` for Ironclad only. It is backed by paraphrased,
source-cited claims collected from public Ironclad guides (Forgotten Arbiter,
Panacea, FantasyWarden and dood), with source URL, game version, trust label
and capture date. A local deterministic lexical index retrieves a few relevant
claims; runtime never fetches the Internet. Claims are soft evidence only:
local card data, hard guide rules, candidate generation and `check_action()`
remain authoritative.

The RAG CLI returns UTF-8 JSON and rejects unsupported characters without
applying Ironclad claims. `tests/test_strategy_rag.py` covers citations,
character isolation and planner payload bounds. Full offline validation after
the integration: `497 passed`, compile/contracts passed, and
`tools/verify_offline.py` passed all checks. This is not win-rate evidence; it
still needs fixed-seed comparative evaluation after the plan is complete.

The `brain.knowledge_enabled` switch is explicit in `config/strategy.json` so
the same replay can form a no-RAG control arm without changing rules, JEV,
candidate legality or model budgets.

The offline evaluator now exposes `--knowledge both`; on the committed
synthetic advisor fixture the enabled arm records source citations and the
disabled arm records zero citations with the same wire-safety checks. This is
an input-path control, not a strategy-quality or win-rate result.

`release_report()` now includes trace request completion rate and the bounded
set of RAG citation IDs used by recorded provider events. Missing real labels
remain `unknown`; no metric is promoted to a win-rate claim.

The local Java overlay was also built with the current executable command
`python java/build.py` (the script does not expose the historical `--verify`
flag). It compiled two sources with `javac --release 8`, produced a verified
34-entry `SpireBrainOverlay.jar`, and did not install it into the game.

## Continuation update — trace audit and provider failure evidence

The S3 trace audit found and fixed four metrics/replay issues. Metrics now
combines advice and decision events in source order, keeps the final event per
decision, counts canonical provider response latency once, accepts
`result_record`, and excludes provider failure rows from the final advice
fallback denominator. Structured outcome evidence is never treated as a
win/loss label. Timing replay rejects duplicate `expired_result` terminal
records for one request.

JEV provider errors now carry a typed `error_kind` from the HTTP client through
the logging wrapper. Live smoke evidence that previously appeared as a generic
provider error is now classifiable as network, timeout, auth, rate limit, or
parse/validation failure. Decision handlers also stop reporting a misleading
`KeyError('choice')` when a provider returns a typed fallback.

JEV request inputs (state plus question schema) and structured responses are
stored as bounded, sanitized content-addressed blobs in the decision trace.
`replay_decision_chain(trace, decision_id)` verifies their SHA-256 references
offline and fails on missing or tampered provider evidence. The real-game smoke
runner now restores the exact pre-run file presence, including removing
configuration files created only by a first smoke run.

Validation on Windows / Python 3.12.10:

| Command | Result |
|---|---|
| `python -m pytest -q` | 511 passed in 8.07s |
| `python -m compileall -q spirebrain start.py run_agent.py` | passed |
| `python tools/check_contracts.py` | passed; static contract only |
| `python tools/verify_offline.py` | tests, compile, contracts, metrics, evaluation, replay, async replay, strategy RAG and launcher smoke passed |

The real-game smoke result remains degraded evidence rather than a quality or
win-rate claim: one strategic response completed after its state was obsolete,
while JEV requests failed at the network layer. No new live request was made
for this continuation.

## Continuation update — release gate, live-copy fixture and search stability

The real-game smoke report now separates `pipeline_passed` from
`provider_health_passed` and `release_passed`. An opted-in smoke exits non-zero
when JEV or the strategic provider is unavailable, degraded, unknown, or when
the only strategic result is later expired. This prevents a rules-only run from
being mistaken for a healthy model integration while retaining diagnostic
evidence.

The offline verifier now executes the local strategy-RAG CLI as an independent
check and validates UTF-8 JSON, Ironclad character isolation, HTTPS provenance,
matching citation IDs and the absence of command-like fields. The fixed RAG
audit reports 9/9 expected source hits and citation provenance 1.0; strategy
quality and win rate remain `unknown`.

`spirebrain.sim.ironclad` now provides a deterministic, bounded Ironclad
simulator and an `extract_minimal_game_snapshot()` helper. The latter reads
only selected localization entries from a read-only copy of the installed
`desktop-1.0.jar`, records its SHA-256 and explicitly excludes saves,
preferences, mods, logs and secrets. Synthetic and live-copy fixtures remain
distinct; the complete game JAR is never placed in the repository.

The combat search default budget was raised from 8ms to 20ms after reproducing
Windows scheduler jitter that occasionally stopped before a complete first
layer of candidates. Explicit zero/short budgets still produce the documented
greedy fallback. The target-threat regression is now stable across repeated
runs.

Latest Windows / CPython 3.12.10 evidence:

| Command | Result |
|---|---|
| `python -m pytest -q` | **530 passed** |
| `python -m compileall -q spirebrain start.py run_agent.py tools` | passed |
| `python tools/check_contracts.py` | passed; Java build not attempted |
| `python tools/verify_offline.py` | 9/9 independent checks passed |
| `python -m spirebrain.analysis.rag_eval` | 9/9 expected hits; quality/win rate unknown |
| real smoke, 30s | pipeline passed; provider health degraded; release failed honestly |

The real smoke observed one strategic response discarded as stale and one JEV
network failure. It is integration evidence only, not a gameplay-quality or
win-rate result.
