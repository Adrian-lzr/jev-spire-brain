# Setup Guide — wiring Jev Spire Brain to the real game

This guide takes you from a Steam install of Slay the Spire to a game process
that streams its full state to our agent and accepts actions back.

> Verified against the CommunicationMod / Slay-AI public docs (2026-09).
> Paths use `C:\Program Files (x86)\Steam\steamapps\common\SlayTheSpire` as the
> default install location — adjust to yours.

## 1. Install the three mods (all free)

| Mod | Where | Purpose |
|---|---|---|
| ModTheSpire | https://github.com/kiooeht/ModTheSpire/releases | mod loader, launches the game with mods |
| BaseMod | Steam Workshop (search "BaseMod") or GitHub releases | dependency layer most mods need |
| CommunicationMod | https://github.com/ForgottenArbiter/CommunicationMod/releases | the pipe: game state JSON out, action commands in |

Optional but recommended for faster iteration:

| Mod | Purpose |
|---|---|
| SuperFastMode | speeds up animations (up to +1000%) for unattended runs |
| STSStateSaver | in-battle save states (needed for search-based combat later, Phase 4) |

Setup:

1. Put the downloaded `.jar` files into `<game folder>/mods/` (create it if missing).
2. Launch via ModTheSpire: `<game folder>/jre/bin/java -jar ModTheSpire.jar`,
   tick BaseMod + CommunicationMod (+ optionals), hit Play.
   (Or install everything from the Steam Workshop and launch with mods enabled.)

## 2. Point CommunicationMod at our agent

CommunicationMod spawns an external process and talks to it over stdin/stdout.
Edit (Windows):

```
%LOCALAPPDATA%\ModTheSpire\CommunicationMod\config.properties
```

Minimal working config for the offline-first loop:

```properties
command=C\:\\Python313\\python.exe C\:\\path\\to\\jev-spire-brain\\spirebrain\\driver\\agent.py
runAtGameStart=true
verbose=true
```

Notes (this format is picky — copy carefully):

- Windows paths MUST escape backslashes and colons: `C\:\\...`
- Use your real `python.exe` absolute path (the managed interpreter in this
  workspace is fine: `C:\Users\Lenovo\.workbuddy\binaries\python\versions\3.13.12\python.exe`)
- `runAtGameStart=true` boots the agent with the game

## 3. Python environment

```bash
cd jev-spire-brain
python -m venv .venv
.venv\Scripts\activate        # Windows
pip install -r requirements.txt
```

Verify the offline brain still passes (no game needed):

```bash
python tests\test_hp_budget.py
python tests\test_decisions.py
python -m spirebrain.sim.run_offline
```

## 4. First live run (smoke test)

1. Start the game through ModTheSpire with the mods enabled.
2. Begin any new run. CommunicationMod pushes a JSON state blob each time the
   game reaches a stable point; with `verbose=true` you'll see traffic.
3. Our agent (Phase 1 wiring target) answers with conservative rule actions —
   expect it to pick the first option everywhere. That is correct behavior:
   the goal of the smoke test is **pipe integrity**, not intelligence.

Success criteria:

- [ ] State JSON appears (log file or verbose console)
- [ ] Agent's action commands are accepted (cursor moves / options picked)
- [ ] A full A0 run completes without the pipe dying

## 5. Switching the brain on

Once the pipe is proven:

```bash
# config/strategy.json -> jev section already defaults to mock backend.
# For the real model (Phase 5): set backend official + provide access per
# https://docs.typesafe.ai/ (or Vercel AI Gateway model `typesafe-ai/jev`).
```

## 6. Troubleshooting

| Symptom | Fix |
|---|---|
| Game starts, no state output | config.properties path wrong or escaping broken (`\` and `:`) |
| Agent process not spawning | python path wrong; test the command line manually in cmd |
| Agent crashes mid-run | check that fallbacks logged `used_fallback=true` — a crash means an unhandled screen type, file an issue |
| Pipe dies on language mismatch | set game language to English or Chinese; event text is passed to JEV as-is |
| Steam overlay interferes | disable overlay for the game during automated runs |
