# Setup Guide — wiring Jev Spire Brain to the real game

This guide takes you from a Steam install of Slay the Spire to a game process
that streams its full state to our agent and accepts actions back.

> Verified 2026-09-21 against the CommunicationMod README (protocol, handshake,
> command set) and against this machine's own filesystem (paths below). Anything
> that could not be checked offline is marked **PHASE 1 VERIFY** with what to
> look for.

## 0. What this machine already has

Checked directly, not assumed:

| Item | State |
|---|---|
| Game | **installed** at `E:\LeStoreDownload\steam\steamapps\common\SlayTheSpire` (Steam appid 646570; `desktop-1.0.jar`, bundled `jre\bin\java.exe`, `mts-launcher.jar`) |
| Workshop content | 3 items in `E:\LeStoreDownload\steam\steamapps\workshop\content\646570\`: ModTheSpire `1605060445`, BaseMod `1605833019`, StSLib `1609158507` |
| `mods\` folder | **does not exist yet** — create it |
| CommunicationMod | **not installed**; see step 1 |
| CommunicationMod config | **does not exist yet** (`%LOCALAPPDATA%\ModTheSpire\CommunicationMod\`) |
| Game data | readable: `gamedata.py` loads 423 cards / 195 relics / 45 potions from the jar |

## 1. Get CommunicationMod

Two routes. The Workshop is easier; the GitHub release is the authoritative
source and is what the mod's own docs point to.

| Route | Where |
|---|---|
| Steam Workshop | item **2131373661** *(found by search 2026-09-21; not yet confirmed subscribed on this machine)* |
| GitHub | `https://github.com/ForgottenArbiter/CommunicationMod/releases/latest` → `CommunicationMod.jar` |

Then put the jar where the mod loader looks for it:

```
E:\LeStoreDownload\steam\steamapps\common\SlayTheSpire\mods\CommunicationMod.jar
```

Create `mods\` if it is missing (it is). Three of the four mods are already
downloaded via the Workshop; the Workshop copies live in their own folder, so
putting the jar in `mods\` as well is the simplest thing that works — ModTheSpire
reads both locations.

Optional but recommended for unattended runs:

- **SuperFastMode** — speeds up animations; unattended runs are otherwise mostly
  waiting.
- **STSStateSaver** — in-battle save states, needed for search-based combat later.

## 2. Point CommunicationMod at the agent

Config file (verified path; the folder does not exist until the mod first runs,
so start the game once with the mod enabled, or create the file by hand):

```
%LOCALAPPDATA%\ModTheSpire\CommunicationMod\config.properties
```

Minimal working config:

```properties
command=C\:\\Users\\Lenovo\\.workbuddy\\binaries\\python\\versions\\3.13.12\\python.exe D\:\\ai生成视频\\jev模型\\jev-spire-brain\\run_agent.py --backend openrouter
runAtGameStart=true
verbose=true
```

> If the non-ASCII path causes trouble in `config.properties`, move the repo to an
> ASCII path (e.g. `D:\dev\jev-spire-brain`) — everything below is
> path-independent, since the launcher resolves the repo root from `__file__`.

Four things that will bite you, all verified:

1. **Point `command=` at `run_agent.py`, never at `spirebrain/driver/stdio.py`.**
   Running a script puts *that script's* directory first on `sys.path`, so the
   module file cannot `import spirebrain`. Tested: the module-file route raises
   `ModuleNotFoundError`; the launcher route works.
2. **Windows paths must escape backslashes and colons**: `C\:\\...`.
3. **Do not use a wrapper script.** `command=` may be a single program with
   arguments. A `.bat` that echoes anything would put its own text into the
   protocol stream. (The README's own FAQ explains the log-file alternative.)
4. **The first line we send must be `Ready`.** Without it the mod waits ten
   seconds and then kills the process — that is the mod's documented behaviour,
   and `stdio.py` sends it before reading anything.

`run_agent.py` also loads `.env` from the repo root, because a process spawned by
the game inherits the *game's* environment rather than your shell's. Without that
the agent would start keyless and silently fall back to the mock.

## 3. Point the agent at your game data

`gamedata.py` reads the game's own localization files out of `desktop-1.0.jar`
so card and relic text comes from the game rather than from the model's memory.
It already knows this machine's path. For any other install:

```bash
set STS_GAME_DIR=E:\path\to\SlayTheSpire
```

Without it, cards still resolve by name for non-basic cards; basic cards
(`Strike`, `Defend`, `Bash`) need the character to disambiguate their internal
ids (`Strike_R` vs `Strike_G`), and the agent passes the character through.

## 4. Python environment

```bash
cd jev-spire-brain
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

The brain itself is stdlib-only — the JEV client uses `urllib` and the config is
JSON — so nothing is required to run the offline pipeline. `spirecomm` in
`requirements.txt` is a reference implementation, not a dependency: we speak the
protocol directly in `spirebrain/driver/stdio.py`.

Verify offline first (no game, no key):

```bash
python tests\test_hp_budget.py
python tests\test_stdio.py
python tests\test_agent_router.py
python -m spirebrain.sim.run_offline
```

## 5. First live run (smoke test)

1. Launch the game through ModTheSpire with BaseMod + CommunicationMod ticked.
2. Watch the ModTheSpire log window: our stderr appears there, and
   `communication_mod_errors.log` keeps our stdout.
3. Start a run. The agent answers each stable state with exactly one command.

Success criteria — the goal is **pipe integrity**, not intelligence:

- [ ] `[stdio] ready — backend=…` appears in the log (this is stderr, so it does not corrupt the protocol)
- [ ] `logs/pipe.jsonl` grows: one record per message, with `"sent"` filled in
- [ ] The cursor moves and options get picked, i.e. commands are accepted
- [ ] `%LOCALAPPDATA%\ModTheSpire\CommunicationMod\communication_mod_errors.log` shows no protocol errors
- [ ] A full Act 1 completes without the pipe dying

Expect conservative play at this stage: many judgements fall back to rules. That
is the designed behaviour while the confidence thresholds are unvalidated — see
`docs/MEASUREMENTS.md`.

### Re-deciding a recorded session offline

Better than re-running the game to test a prompt change:

```bash
python run_agent.py --replay logs\recorded_states --backend mock
```

Every `.json` message in the directory goes through the same code path, and the
commands are printed instead of sent.

## 6. Switching the brain on

```bash
# .env in the repo root (gitignored):
OPENROUTER_API_KEY=...        # JEV is reachable on OpenRouter's System One route
JEV_BACKEND=openrouter
```

`JEV_BACKEND=mock` needs no key and no network. Real JEV is reached through
`POST /api/v1/systemone` with model `jev-1.13` — the chat-completions endpoint
rejects it. Details and the measured numbers are in `docs/JEV_API.md`.

## 7. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Game hangs ~10 s, then the process exits | no `Ready` handshake, or the command line is wrong | check `command=`; the launcher sends `Ready` already |
| `ModuleNotFoundError: spirebrain` | `command=` points at the module file | point it at `run_agent.py` |
| Nothing at all in the log | `config.properties` path wrong, or escaping broken (`\` and `:`) | the folder only exists after the mod has run once |
| Agent runs but never acts | every state answered with `state`/`wait` | check `logs/pipe.jsonl` for `substitutions`: an unoffered verb is a bug in our router, and the substitution log names it |
| Agent exits on a screen we have not seen | unhandled screen type → `wait` → game waits | add the screen to `GRID_SCREENS` or a handler in `agent.py` |
| Wrong card upgraded | grid index ordering assumption | **PHASE 1 VERIFY**: `_on_rest` assumes the upgrade grid follows the deck array; check on the live pipe |
| Purchases pick the wrong item | shop shelf ordering assumption | **PHASE 1 VERIFY**: `_on_shop` assumes cards, then relics, then potions |
| Steam overlay interferes | overlay steals input | disable the overlay for this game |
