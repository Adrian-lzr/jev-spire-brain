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
| CommunicationMod | **subscribed but not downloaded** (Steam ledger lists the id under `WorkshopItemDetails`, not under `WorkshopItemsInstalled`) — see step 1 |
| CommunicationMod config | **does not exist yet** (`%LOCALAPPDATA%\ModTheSpire\CommunicationMod\`) |
| Game data | readable: `gamedata.py` loads 423 cards / 195 relics / 45 potions from the jar |

## 1. Get CommunicationMod

**This machine: subscribed but NOT downloaded.** Verified 2026-09-21 from Steam's
own ledger (`steamapps\workshop\appworkshop_646570.acf`):

| Evidence | Value |
|---|---|
| `WorkshopItemDetails` contains `2131373661` and `3748153752` | the subscription **is** registered on the account |
| `WorkshopItemsInstalled` contains only the original three | the content is **not** on disk |
| `NeedsDownload` | `1` |
| `steamapps\workshop\content\646570\` | three folders: ModTheSpire, BaseMod, StSLib — no `2131373661` |
| Disk-wide search for `CommunicationMod*` | **no file found** anywhere on C:, D:, E: |

`2131373661` is identified as CommunicationMod from an independent listing that
pairs the name with that id (AudioGames forum, "CommunicationMod —
steamcommunity.com/sharedfiles/…2131373661"). Steam's own page for the item could
not be fetched from here, so the decisive confirmation is the downloaded jar's
filename. `3748153752` is a second subscribed item that public listings do not
identify; it is not needed for this project.

Fix — a subscription is not an install:

1. In the Steam client: Library → Slay the Spire → **Workshop**, open the item and
   use **Download** if it is offered; or
2. Restart the Steam client (the three other items arrived this way when they were
   subscribed), or
3. Launch the game once — a launch triggers workshop downloads.

After it lands, expect `…\workshop\content\646570\2131373661\CommunicationMod.jar`.
**Do not copy it anywhere**: ModTheSpire has loaded mods from the Workshop folder
automatically since **v3.7.0** ("Steam Workshop support"), and since v3.13.0 it
caches those paths so they work even with Steam closed. The workshop jar and a jar
in `mods\` are equivalent; enable it in the ModTheSpire launcher's checkbox list
either way.

Fallback if the Workshop item will not download: get `CommunicationMod.jar` from
`https://github.com/ForgottenArbiter/CommunicationMod/releases/latest` in a browser
(the sandbox here cannot fetch GitHub binaries) and put it in
`…\steamapps\common\SlayTheSpire\mods\` — create that folder, it does not exist yet.

Optional but recommended for unattended runs:

- **SuperFastMode** — speeds up animations; unattended runs are otherwise mostly
  waiting.
- **STSStateSaver** — in-battle save states, needed for search-based combat later.

## 2. Point CommunicationMod at the agent

Config file — **the mod creates it on its first load**, so it may not exist yet:

```
%LOCALAPPDATA%\ModTheSpire\CommunicationMod\config.properties
```

**Do not write it by hand.** Run this instead — it produces the format the mod
actually parses, previews first, backs up any existing file, and verifies the result
by decoding it back the way Java does:

```bash
python -m spirebrain.install_mod_config            # preview, writes nothing
python -m spirebrain.install_mod_config --write     # apply
python -m spirebrain.install_mod_config --show      # print the current file, decoded
```

What it writes on this machine:

```properties
command=C\:\\Users\\Lenovo\\.workbuddy\\binaries\\python\\versions\\3.13.12\\python.exe D\:\\ai\u751F\u6210\u89C6\u9891\\jev\u6A21\u578B\\jev-spire-brain\\run_agent.py --backend openrouter
runAtGameStart=true
verbose=true
maxInitializationTimeout=10
```

**Six details verified in the source on 2026-09-21, four of which the docs and forum
posts get wrong** (`CommunicationMod.java`, `SpireConfig.java`, `ConfigUtils.java`):

1. **Only four keys exist**: `command`, `runAtGameStart`, `verbose`,
   `maxInitializationTimeout`. Anything else in the file is ignored.
2. **It is read as ISO-8859-1.** `SpireConfig.load()` is
   `properties.load(new FileInputStream(file))`, and `java.util.Properties` reads
   bytes as ISO-8859-1 while decoding `\uXXXX` escapes. **So raw UTF-8 in a path is
   read back as mojibake and the launch fails** — exactly the trap this repo's own
   path would have hit (it contains 生成视频 and 模型). Escaping those characters is
   what `Properties.store()` itself emits, so the escape form *is* the native
   format. (An earlier version of this document told you to paste the raw path. It
   was wrong; that is why the installer exists.)
3. **The command is split on whitespace and handed straight to `ProcessBuilder`**:
   `getString("command").trim().split("\\s+")`. There is no shell and no quoting, so
   **no path may contain a space** and quotes would become part of an argument. The
   installer prints the exact argv the mod will build so this is visible before you
   start the game.
4. **`maxInitializationTimeout=10` is the Ready window** — the game blocks for that
   many seconds waiting for our first line, then kills the process. Our launcher
   sends `Ready` immediately, so 10 is generous; raise it only if the interpreter is
   slow to start.
5. **`runAtGameStart=true` makes the mod spawn us when the game boots.** For the
   smoke test that is what you want; set it to `false` to launch by hand.
6. **Our stderr goes to `communication_mod_errors.log`** in the game folder
   (`builder.redirectError(appendTo(...))`), and our stdout is the protocol. So if
   nothing happens, that file is the first place to look.

Do **not** point `command=` at `spirebrain/driver/stdio.py` — see point 1 of the
trap list below.

### The five traps

1. **Point `command=` at `run_agent.py`, never at `spirebrain/driver/stdio.py`.**
   Running a script puts *that script's* directory first on `sys.path`, so the
   module file cannot `import spirebrain`. Tested: the module-file route raises
   `ModuleNotFoundError`; the launcher route works.
2. **No wrapper scripts.** stdout *is* the protocol — a `.bat` that echoes anything
   would inject its own text into the state stream. Diagnostics belong on stderr,
   which the mod already redirects to `communication_mod_errors.log`.
3. **The first line we send must be `Ready`.** The mod blocks waiting for it
   (see `maxInitializationTimeout` above) and kills the process on timeout.
   `stdio.py` sends it before reading anything.
4. **The game spawns us with the game's environment, not your shell's.**
   `run_agent.py` therefore loads `.env` from the repo root; without that the agent
   would start keyless and silently fall back to the mock.
5. **A subscription is not a download.** Verified live 2026-09-21: the Workshop id
   appeared in `WorkshopItemDetails` (subscribed) while `WorkshopItemsInstalled`
   still listed only the other three mods, and `NeedsDownload=1`. Steam processed
   it only when the client next ran. `python -m spirebrain.doctor` reports the two
   states separately for exactly this reason.

### The evening of 2026-09-21, in one paragraph

The first live launch *worked* — ModTheSpire's own log shows
`Received message from external process: Ready` — and then looked exactly like a
dead agent. The game sent one state (`in_game: false`, the main menu), our
transport stayed silent by design (auto-start off), the player quit a minute
later, and the only traces were one 144-byte log line and a banner in
`communication_mod_errors.log` whose em dash had arrived as mojibake (`a1 aa`,
because Python defaulted stderr to cp936). Every silent-looking failure since has
been given a voice: menu idling prints why it is idling, the shutdown line counts
menu states and substitutions, stderr is UTF-8, and the banner avoids non-ASCII.

### The game is not in English on this machine

`preferences/STSGameplaySettings` has `"LANGUAGE": "ZHS"`, so CommunicationMod
reports Chinese display names ("打击") — and the English effect lookup used to
match on `name`, silently finding nothing. Fixed by looking up `id` first
(language-independent, camel-cased: `PommelStrike`) and resolving it against the
English localization keys (`Pommel Strike`) with a whitespace/case-insensitive
index; the digest then labels the card with the English name plus real runtime
numbers (`Deal 9 damage. Draw 1 card.`). `tests/test_live_state.py` pins all of
it with fixtures shaped like the live payload.

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
   The ModTheSpire log is the authoritative record: on the first real launch it
   showed `Received message from external process: Ready` under
   `- Communication Mod`, which is the definitive "the agent was pulled up".
2. The agent's own traces: `communication_mod_errors.log` in the game folder
   (our stderr — the ready banner, the menu notice, the shutdown summary) and
   `logs/pipe.jsonl` in the repo (every message the game sent, every command we
   answered).
3. **What you see at the main menu depends on the mode and auto-start.** In
   **advise** mode (the default) the agent prints one line and waits — you start
   the run by hand, because class/ascension/seed are your decisions, and
   `--auto-start` is ignored on purpose. In `play` mode without `--auto-start` it
   also waits; with `--auto-start` (recommended for unattended sessions:
   `python -m spirebrain.install_mod_config --auto-start --mode play --write`) it
   sends `start IRONCLAD 0` itself when the game offers `start`.
4. Each stable in-game state gets exactly one command. In advise mode that command
   is always a poll (`wait`/`state`), and the recommendation goes to the
   dashboard and `logs/advice.jsonl` instead of to the game.

Success criteria — the goal is **pipe integrity**, not intelligence:

- [ ] `[stdio] ready — backend=…` appears in `communication_mod_errors.log`
      (readable UTF-8, no mojibake)
- [ ] `logs/pipe.jsonl` grows: one record per message, with `"sent"` filled in
      for in-game states (menu states correctly show `"sent": null`)
- [ ] The cursor moves and options get picked, i.e. commands are accepted
- [ ] No protocol errors in the error log; the shutdown line
      (`stdin closed after N messages…`) names zero substitutions, or only
      explainable ones
- [ ] A full Act 1 completes without the pipe dying

Expect conservative play at this stage: many judgements fall back to rules. That
is the designed behaviour while the confidence thresholds are unvalidated — see
`docs/MEASUREMENTS.md`.

Timing, measured, so nobody rediscovers it in a hang: the *initialisation*
timeout is the only hard deadline (`maxInitializationTimeout=10`, seconds, and
our `Ready` precedes any JEV call, so it is not at risk). During play there is
no documented per-command timeout, but each answer blocks the game's UI thread,
and real JEV answers take 0.5–1.6 s per call — so a screen that batches many
questions into one call (the shop asks several) is felt as a short freeze. That
is acceptable for a watched smoke test; if unattended runs need snappier play,
the fix is batching questions per screen (already the design) rather than
threading (never: two commands for one state is a protocol violation).

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

### Which mode the game-spawned agent runs in

| Mode | Behaviour | Flag |
|---|---|---|
| `advise` (default) | Recommends. Sends only `wait`/`state`. Ignores `--auto-start`. | (nothing) or `--mode advise` |
| `play` | Auto-plays; the mode the offline measurements needed. | `--mode play` |

The mode is written into the mod config rather than left implicit, so the file on
disk is the record of what the launched agent will do:

```bash
python -m spirebrain.install_mod_config --mode advise --write   # coach (default)
python -m spirebrain.install_mod_config --mode play --auto-start --write  # bot
```

Two logs, two jobs, both in the repo:

| File | What is in it |
|---|---|
| `logs/pipe.jsonl` | every message the game sent and every command we answered (in advise mode: all polls) |
| `logs/advice.jsonl` | every recommendation, and every verdict on what you did — with the evidence for the inference (`hand_lost`, `energy_spent`, `gold`, `deck_gained`, …) |

If the advice panel never fills in, check in this order: is the dashboard running
(the agent publishes over HTTP and is silent by design when nothing is listening);
does `logs/advice.jsonl` have lines (then the chain worked and the page is the
problem); is the screen one the router models (an unmodeled screen gets no advice
by design — there is nothing to recommend on a screen we cannot read).

## 7. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Subscribed on the Workshop but no jar appears in `content\646570\` | a subscription is not an install; `NeedsDownload` stays `1` because Steam never processed the item (the other three arrived only when the client next ran) | open the item in the Steam client and use Download, restart the Steam client, or launch the game once |
| `Could not start external process`, and the path in the config looks like `D:\aiæç...` | the config was written as raw UTF-8, but it is read as ISO-8859-1 | rewrite it with `python -m spirebrain.install_mod_config --write`; never hand-edit that file with a non-ASCII path |
| Game hangs ~10 s, then the process exits | no `Ready` handshake, or the command line is wrong | check `command=`; the launcher sends `Ready` already. Look in `communication_mod_errors.log` in the game folder for our stderr |
| `ModuleNotFoundError: spirebrain` | `command=` points at the module file | point it at `run_agent.py` |
| Nothing at all in the log | `config.properties` path wrong, or the mod is not enabled in the ModTheSpire list | the folder only exists after the mod has loaded once |
| Agent runs but never acts | every state answered with `state`/`wait` | check `logs/pipe.jsonl` for `substitutions`: an unoffered verb is a bug in our router, and the substitution log names it |
| Agent exits on a screen we have not seen | unhandled screen type → `wait` → game waits | add the screen to `GRID_SCREENS` or a handler in `agent.py` |
| Wrong card upgraded | grid index ordering assumption | **PHASE 1 VERIFY**: `_on_rest` assumes the upgrade grid follows the deck array; check on the live pipe |
| Purchases pick the wrong item | shop shelf ordering assumption | **PHASE 1 VERIFY**: `_on_shop` assumes cards, then relics, then potions |
| Steam overlay interferes | overlay steals input | disable the overlay for this game |

Useful while testing: the mod's in-game settings panel (Mods → Communication Mod)
has a **"(Re)start external process"** button, so a config or code change does not
need a game restart — and a toggle for `runAtGameStart`.
