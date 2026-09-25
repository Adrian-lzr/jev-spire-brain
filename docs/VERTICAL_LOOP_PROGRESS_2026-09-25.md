# Vertical Loop Optimization Progress (2026-09-25)

## Baseline and scope

- Baseline commit: `e0ff85bf86d8fee4e634f0940504397847f824d9`.
- Python: 3.12, invoked with `C:\Users\Lenovo\AppData\Local\Programs\Python\Python312\python.exe`.
- Existing user changes in `tests/test_runtime_optimization.py` and `.playwright-cli/` were preserved and are not part of this change.
- No API key, paid provider, game process, or CommunicationMod configuration was used.

## Implemented loop improvements

- Offline evaluation now executes distinct `rules`, `jev`, `strategic`, and `full` stacks. It reports provider attempts, advise wire safety, candidate legality, fallback rate, and unknown real outcomes separately.
- Replay fixtures require synthetic type, explicit kind, non-empty payloads, and typed state messages. Metrics rejects missing or empty input instead of producing an empty success.
- Strategic planning is explicit. `StrategicOrchestrator.choose()` only reconciles an already acquired plan and cannot issue an implicit second provider request. `DecisionBudget(max_calls=0)` is an explicit no-call budget.
- Decision traces can reference bounded, sanitized SHA-256 content-addressed state, candidate, and response blobs. Blob reads verify integrity and enforce a size limit; provider attempt events remain owned by the provider logging decorator.
- The bounded combat search pilot stops when lethal or full threat coverage is achieved and prefers the shortest sufficient sequence.

## Verification evidence

- `python -m pytest -q`: **402 passed**.
- `python -m compileall -q spirebrain start.py run_agent.py`: passed.
- `python tools/check_contracts.py`: passed; Java build was not attempted because game JAR dependencies are environment-specific.
- `python tools/verify_offline.py`: tests, compile, contracts, metrics, and replay all passed.
- Synthetic provider calls for one state fixture: rules `0/0`, JEV `1/0`, strategic `0/2`, full `2/2` (JEV/strategic).
- Synthetic replay sent only `wait` commands in `advise`; candidate legality was `2/2` for the checked actions.

## Limits

These are offline contract and replay measurements, not model quality or win-rate evidence. Scheduler scenarios are validated as event fixtures but are not yet driven through a fake-clock live scheduler. The combat search pilot is not connected to the live recommendation path. Real game, Java build, paid API, and win-rate validation remain outstanding.
