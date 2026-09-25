# Phase 6 validation checklist

This checklist separates offline evidence from real-game evidence. Synthetic
fixtures do not establish win rate or model quality.

## Offline

```powershell
$py = 'C:\Users\Lenovo\AppData\Local\Programs\Python\Python312\python.exe'
& $py -m pytest -q
& $py tools/verify_offline.py
& $py -m spirebrain.analysis.evaluate --input tests/fixtures/replay/advisor_states.json
```

Check that `display_status`, `mode`, `config_id`, and model connection fields
are present in `/state`, SSE advice events, and the Java `FeedClient.Snapshot`.

## Real game, increasing risk

1. Start with a recorded or synthetic replay for map, reward, shop, rest, event,
   combat, card grid, return-to-menu, and a new run. Confirm old advice is
   cleared when `state_id` changes.
2. Run `advise` mode with the player operating every action. Confirm the wire
   commands remain only `wait` or `state`, and compare the in-game overlay with
   the browser `/state` snapshot.
3. Only after the previous pass, test a limited `play`-mode scene with a fixed
   seed and a local mock provider. Stop on any illegal candidate or stale panel.
4. Run multiple short sessions including dashboard shutdown, provider timeout,
   return to menu, death, and a new run. Confirm `run_epoch` changes and old
   advice never reappears.

For each issue, save the smallest state sequence and provider response as a
synthetic fixture before changing code. Record game version, mod versions,
configuration ID, seed, scene, and whether the observation is real or synthetic.
