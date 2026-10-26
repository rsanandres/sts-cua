# sts-cua

A computer-use agent (CUA) that plays Slay the Spire from screen pixels, running many games in parallel inside containers we host, so it plays continuously and every step's reasoning is logged.

**Why Spire:** turn-based (a slow local model is fine), single-player and offline (no anti-cheat or account risk), and every decision is a readable chain of reasoning. Score is simple: floor reached and win rate.

## Architecture (draft)

```
                 +----------------------------------+
                 |  Model server (local NVIDIA GPU) |
                 |  one open CUA model, e.g. vLLM   |
                 +----------------+-----------------+
                                  ^  screenshot + task + history
                                  |  -> "Thought: ... Action: click(x,y)"
        +-------------------------+-------------------------+
        |                         |                         |
+-------+--------+       +--------+-------+        +--------+-------+
| Game box 1     |       | Game box 2     |  ...   | Game box N     |
| Xvfb display   |       | Xvfb display   |        | Xvfb display   |
| StS + mods     |       | StS + mods     |        | StS + mods     |
| harness:       |       | harness:       |        | harness:       |
|  screenshot -> |       |  screenshot -> |        |  screenshot -> |
|  xdotool click |       |  xdotool click |        |  xdotool click |
|  state log  <- |       |  state log  <- |        |  state log  <- |
+-------+--------+       +--------+-------+        +--------+-------+
        |                         |                         |
        +------------> run logs: every Thought/Action, -----+
                       screenshot, true game state, result
```

- **Game box:** one container per game. A virtual display (Xvfb) so no monitor is needed, the game with our state-export mod, and a small harness that takes screenshots and sends clicks (xdotool).
- **Two channels, kept separate:**
  - The **agent** only sees pixels and only acts by clicking. That is what makes it a CUA.
  - A **state-export mod** writes the true game state as JSON. Used for scoring and for checking what the agent should have seen, never fed to the agent.
- **Model server:** one model loaded once on the GPU, serving every game box, so adding games costs CPU and RAM, not more GPUs.
- **Run logs:** the point of the project. Each step stores screenshot, Thought, Action, and the true state, so you can read where the reasoning went wrong.

## Game: Slay the Spire 2

Decided 2026-10-25: build on Spire 2 (Raph owns both). Spire 2 is in Early Access (since 2026-03-05), native Linux, built in Godot.

- No state-export mod exists for Spire 2, so we write our own. Spire 2 mods are C# DLLs plus Godot `.pck` files, patching the game with Harmony hooks ([modding tutorial](https://github.com/fresh-milkshake/Modding-Tutorial), pinned to build v0.103.3, may be out of date).
- Mega Crit made its built-in mod loader official with Steam Workshop support in Major Update 2 (v0.107.1, 2026-06-19) and calls it barebones ([guide](https://bossdown.com/guides/slay-the-spire-2-steam-workshop-mods/)).
- A community MCP server maps 144 hooks and 175 overridable methods across combat, cards, damage, powers, turns and rewards. Useful for finding where to read state ([Nexus](https://www.nexusmods.com/slaythespire2/mods/345)). Third-party, unverified.
- Early Access patches can break the mod and shift the UI, so pin a game build per experiment and record it in each run log.

## Hardware

Raph's machine runs Windows with an NVIDIA RTX 4090, 24 GB VRAM (confirmed with `nvidia-smi` on 2026-10-24; about 3.3 GB already in use by the desktop). A 7B vision model fits at full precision; models around 32B need heavy quantization. Measure real memory use when a model is loaded.

The Linux game containers run under WSL2 or Docker Desktop on that machine.

## Models

Raph's credits are Claude API credits (about $100/month), so the agents are light Claude models plus open models run locally. Prices from https://platform.claude.com/docs/en/about-claude/pricing (checked 2026-10-25).

| Agent | Thinking | Cost per step (est.) | Role |
| --- | --- | --- | --- |
| Claude Haiku 5.5 (`claude-haiku-5-5`) | off, click only | ~$0.0002 | Snap judgment, high volume |
| Claude Haiku 5.5 | on, Thought + click | measure | Same model, with reasoning |
| Claude Sonnet 5.5 (`claude-sonnet-5-5`) | on, Thought + click | ~$0.006 | Strongest baseline, sampled runs |
| Open model on the local 4090 (OpenCUA-7B, UI-TARS-7B, or Qwen-VL) | full reasoning visible | $0 per step | Study the full reasoning tokens |

Per-step estimates assume a 1280x720 screenshot (46x26 = 1,196 visual tokens), the computer-use toolset definitions cached (~4,520 tokens), ~2,000 cached tokens of text history, and ~50 (click only) or ~300 (Thought + click) output tokens. Steps per run are unknown until the scripted-bot prototype measures them.

Claude returns its internal thinking only as a summary, so the visible `Thought:` text is what gets logged for Claude. The local model logs every token.

## Research question

How much does reasoning buy you in Spire, and on which decisions? Run the agents above on the same seeds with the same harness and scoring, then read the runs where the reasoning agent wins and the click-only agent loses.

(Considered and dropped: TypeSafe's Jev classifier. Cheap, but it isn't covered by the Claude credits, it's API-only, and its image input is still experimental.)

## Prior work

- [Spire 2 modding tutorial](https://github.com/fresh-milkshake/Modding-Tutorial): mod loading, Harmony hooks, Godot `.pck` assets. Pinned to build v0.103.3, may be out of date.
- [STS2 Modding Assistant MCP](https://www.nexusmods.com/slaythespire2/mods/345): community tool that maps 144 hooks and 175 overridable methods; a guide to where game state lives. Third-party, unverified.
- No published computer-use or RL agent for Spire 2 found yet.

## To verify

- Spire 2 runs on Linux under Xvfb in a container, with software or GPU rendering.
- Licensing and Steam behavior when running several copies at once from one purchase.
- Whether the game's `saves/history/*.run` files can be read directly without the mod (the mod reads them for `/api/v1/compendium`; their format is the game's and isn't documented).
- Which open CUA model fits the GPU's VRAM.

## Build order

1. Harness with a model-agnostic interface: `model.step(screenshot, history) -> (thought, action)`. Each model adapter converts its own coordinate convention to screen pixels.
2. Claude Haiku 5.5 adapter, then Sonnet 5.5. API first so harness bugs are not confused with a weak model.
3. Spire 2 in a container on Raph's machine, plus the state-export mod.
4. Local open model on the 4090 behind the same interface.
5. Thinking-on vs thinking-off comparison on fixed seeds.

## Run the harness

Needs Python 3.11+, an X display (Xvfb in the game container), and `xdotool`.

```
uv venv && uv pip install -e ".[dev]"
Xvfb :99 -screen 0 1280x720x24 &
python -m sts_cua --display :99 --model scripted --max-steps 3      # dry run, no API calls
python -m sts_cua --display :99 --model claude-haiku-5-5 --thinking off --max-steps 20 --max-cost 0.25
python -m sts_cua --display :99 --model claude-sonnet-5-5 --thinking on --effort medium --max-steps 20
pytest
```

First live test, before Spire: a green button that jumps after every hit, scored in `target.json`.

```
DISPLAY=:99 .venv/bin/python tools/click_target.py --out target.json &
.venv/bin/python -m sts_cua --display :99 --model claude-haiku-5-5 --thinking off --prompt-file prompts/click_target.txt --max-steps 5 --max-cost 0.05
cat target.json
```

Claude needs `ANTHROPIC_API_KEY` (or an `ant auth login` profile). Each run writes `runs/<timestamp>/` with `meta.json`, `steps.jsonl` (thought, reasoning summary, proposed and executed actions, tokens, cost per step), a screenshot per step, and `summary.json`. A run stops at `--max-steps`, at `--max-cost` USD, after 3 steps in a row with no action, or after 3 errors in a row.

### With the local model

Start the model server in WSL with `python -m sts_cua.serve start <model> --mode coop|solo` (or `start best`; `list`, `download`, `status` and `stop` too; see [docs/hosting.md](docs/hosting.md#serving-the-local-models)), or the vLLM command in [docs/hosting.md](docs/hosting.md#model). Then point the harness at it:

```
.venv/bin/python -m sts_cua --display :99 --model qwen3-vl-8b --max-steps 20
.venv/bin/python -m sts_cua --display :99 --model qwen3-vl-8b --base-url http://localhost:8000/v1 --served-model my-lora --temperature 0
```

`--served-model` defaults to `Qwen/Qwen3-VL-8B-Instruct-FP8`, the name `vllm serve` gives it; pass a LoRA adapter's name to run that instead. Set `VLLM_API_KEY` if vLLM runs with `--api-key`. The adapter uses Qwen3-VL's own `computer_use` tool and `<tool_call>` format, with coordinates on a 0-1000 scale converted to screen pixels. Its steps cost $0, and each step's `raw` holds the request (screenshot replaced by a placeholder; it's saved next to the step), the reply text, token usage, per-token logprobs and token IDs, for building SFT and RL data later.

Code map:

- `src/sts_cua/loop.py`: screenshot, decide, act, log.
- `src/sts_cua/models/base.py`: the `Model.decide(Observation) -> Decision` interface every adapter implements.
- `src/sts_cua/models/factory.py`: `add_model_args(parser)` and `build_model(args)`, shared by every CLI that picks a model.
- `src/sts_cua/models/claude.py`: Claude adapter on the `computer_toolset_20260801` tool. Each step is a fresh request (cached system prompt and tools, a text log of recent steps, the current screenshot), so nothing is replayed and cost per step stays flat.
- `src/sts_cua/models/qwen.py`: Qwen3-VL adapter for vLLM's OpenAI-compatible server, the same fresh request each step.
- `src/sts_cua/screen.py`: `X11Screen` (mss capture, xdotool input) and `FakeScreen` for tests.
- `src/sts_cua/runlog.py`, `src/sts_cua/pricing.py`: run logs and per-step cost.
- `src/sts_cua/memory/`: lessons carried between runs (see "Memory across runs").
- `src/sts_cua/coop.py`, `src/sts_cua/rules.py`: co-op mode and its general rules (see "Co-op with Raph").

## Run Slay the Spire 2

Requires Steam for Linux running and logged in inside WSL, Spire 2 installed, and the agent's save profile selected (Profile 3 on Raph's machine).

```
tools/launch_sts2.sh /tmp/sts2.log      # Xvfb :99 + game on the GPU + layout fix, waits for the main menu
```

`src/sts_cua/game.py` holds what we learned getting it running on WSL2:

- Launch the binary directly (`SteamAppId`/`SteamGameId` set, Steam running). Through Steam's runtime container the game can't see WSL's GPU libraries and fails to create an OpenGL context.
- `GALLIUM_DRIVER=d3d12 --rendering-driver opengl3` renders on the RTX 4090 (game log: `Using Device: Microsoft - D3D12 (NVIDIA GeForce RTX 4090)`).
- The first frame lays out wrong until the window is resized by one pixel and back.
- SIGTERM is ignored; `game.quit_game` clicks Quit then Yes.

Game settings for agent runs (machine `settings.save` is not cloud-synced; profile prefs are, so change those in-game): 1280x720 windowed at 0,0, vsync off, 30 fps cap, MSAA off, all volumes 0, skip intro. Agent profile: fast mode, no screen shake, no text effects, hand card numbers shown, gameplay data upload off. Keyboard: `1`-`0` select cards, `E` confirm, `Enter` select, `M` map, `Escape` cancel.

## Game state mod (STS2MCP)

[STS2MCP](https://github.com/Gennadiyev/STS2MCP) (MIT) runs a local API on `localhost:15526`. Release 0.4.0 breaks on Spire 2 v0.107, so we build `main` @ 55e0648 (which has the v0.107 fix) with the .NET 9 SDK against the game's own DLLs:

```
DOTNET_ROOT=~/.dotnet ~/.dotnet/dotnet build STS2_MCP.csproj -c Release \
  -p:STS2GameDir="$HOME/.steam/debian-installation/steamapps/common/Slay the Spire 2" -o out/STS2_MCP
```

Copy `STS2_MCP.dll` + `STS2_MCP.json` into `<game>/mods/` and accept the "Load Mods?" dialog. With mods on, the game uses separate `modded/profileN` saves, so the agent's modded Profile 3 can't touch normal saves.

The harness only reads `GET /api/v1/singleplayer` (`src/sts_cua/state.py`) and, between runs, `GET /api/v1/compendium` for the run history (`src/sts_cua/history.py`). It logs a summary per step as ground truth (screen type, floor, HP, energy, hand, enemies and intents). The agent never sees it; it stays pixel-only. Never call the mod's POST endpoints from the harness.

## Play runs back to back

Needs the game on the main menu (`tools/launch_sts2.sh`) and the STS2MCP mod, which the supervisor reads to tell when a run is over.

```
python -m sts_cua.supervisor --display :99 --model scripted --max-runs 1 --max-steps 5   # dry run, no API calls
python -m sts_cua.supervisor --display :99 --model claude-haiku-5-5 --thinking off --daily-budget 2.00 --game-build v0.107.1
python -m sts_cua.supervisor ... --memory memory/notes.jsonl                # learn across runs (see below)
```

Each game run starts a new singleplayer run from the main menu and plays it to the end. A run stops when the mod shows the game-over screen or a menu after the run started, or at the usual caps (`--max-steps` 3000, `--max-cost` per run, default whatever is left of today's budget, 10 idle steps in a row). Then the supervisor writes `score.json` into the run dir: outcome (won, died, abandoned, unknown), max floor, bosses killed, seed, and a per-combat breakdown. It also appends one line to `runs/ledger.jsonl`. Score is 100 for a win, otherwise floor + 52/3 per boss killed (AgenticSTS's score).

The outcome, floor and seed come from the game's own run history when the run is in it. The game writes a `.run` file per finished run (`win`, `was_abandoned`, `seed`, `start_time`, map points), and the mod summarizes the 20 newest at `GET /api/v1/compendium` (`--compendium-url`). Before each game run the supervisor snapshots the entry ids; after it, the new entry whose `start_time` falls in the run's time window is the run (character and floor break ties, then the newest). It polls up to 3 times, 2 s apart, after a run that reached its end. Then `score.json` and the ledger say `outcome_source: "compendium"`, and keep the states' guess in `outcome_states` (and `max_floor_states` in score.json) when it disagrees; `score.json["history"]` holds the entry. With no new entry (the mod is down, or the run was cut off by a cap), the outcome is inferred from the logged states as before (`outcome_source: "states"`, `seed` null). That inference assumes Act 3 is the last act and that Give Up shows the game-over screen with HP left, so it is only the fallback. Schema and the assumptions still to check on the PC are in `src/sts_cua/history.py`.

If the game isn't on the main menu with no saved run before a run, a short reset run (meta `kind: "reset"`, no score) asks the model to get it there. The daily budget is the sum of `cost_usd` in `runs/*/summary.json` that ended today in America/Los_Angeles, reset runs and earlier sessions included, plus the `cost_usd` of each `runs/*/memory_reflect.json` by its `time` (the run's summary never includes the reflect call, so nothing is counted twice). The supervisor stops when less than `--reserve` ($0.10) is left, after `--max-runs`, when the mod can't be reached, when the reset fails, or after 3 runs in a row that never got into a run.

With `--memory PATH`, each game run gets a fresh `Retriever` over the store (a new probation seed, recorded in `meta.json["memory"]` as with `python -m sts_cua --memory`), and reset runs get no notes. After each game run the supervisor scores it against the store (`memory_score.json`), then makes one reflect call (Sonnet 5.5, `memory_reflect.json`) unless `--no-reflect` is given. Each game run's cost cap keeps `--reflect-reserve` ($0.05, the expected reflect cost) back, and reflect is skipped when less than that is left today. A failed score or reflect is written to `memory_error.json` in the run dir and the session carries on. Ledger lines carry `memory` (the store path, or null), `notes_added` and `notes_changed` (from the reflect results; null when there was no reflect) and `reflect_cost_usd`.

- `src/sts_cua/supervisor.py`: the session loop and budget accounting.
- `src/sts_cua/score.py`: outcome, score and per-combat breakdown from a run's logged states (`load_states(run_dir)`), and the stop check (`RunOverDetector`).
- `src/sts_cua/history.py`: read-only client for the mod's run history, and `find_run`, which picks a finished run's entry.
- `src/sts_cua/eval.py`, `src/sts_cua/stats.py`: the memory A/B evaluation and its tests (see "Memory A/B evaluation").

## Memory across runs

The agent improves across runs without weight updates: after each run it writes a few short lessons, and later runs see the ones that match the current screen.

```
python -m sts_cua --model claude-haiku-5-5 --memory memory/notes.jsonl ...   # show matching notes each step
python -m sts_cua.memory reflect runs/<run>      # score the run, then one Claude call (Sonnet 5.5) edits the store
python -m sts_cua.memory score runs/<run>        # only update note utilities (reflect already does this)
python -m sts_cua.memory list | show <id> | retire <id>
python -m sts_cua.memory reflect runs/<run> --dry-run   # print the request, no API call
```

- **Store:** `memory/notes.jsonl`, one note per line: kind (ui, enemy, card, event, strategy, pitfall), keys (screen, enemy, card, event, act, character, game_version), text up to 300 characters, status (probation, active, retired), utility stats, source runs. The store is generated on Raph's PC from his runs, so `memory/` is in `.gitignore`.
- **Retrieval** (`memory/retrieve.py`, plain code, no model call): a note applies when all its keys match what is on screen (screen type, enemy names, card names in hand, event title, act). Keys may only name things visible on screen; the mod's state is used only because it already names them. Active notes are shown best utility first, capped at ~600 tokens, plus an always-on playbook of general strategy notes capped at ~1,500 tokens. Probation notes are shown on a seeded coin flip per floor so whole combats are A/B units; the seed is in `meta.json` and each step logs `notes_shown`.
- **Delivery:** the notes go into the per-step user message, after the cached tools and system prompt, labelled as lessons from earlier runs.
- **Reflect** (`memory/reflect.py`): one call with a digest of the run (per-combat HP table, outcome, wasted or erroring steps, the agent's thoughts at key moments, the notes it was shown with their stats). It returns delta operations only: ADD (at most 3, deduped against notes with the same keys), UPDATE, DOWNVOTE, RETIRE. Cost goes to `memory_reflect.json` in the run directory.
- **Validation gate** (`memory/score.py`): per note, loss when shown vs hidden (HP lost per combat as a share of max HP for enemy, card and strategy notes; share of wasted steps per screen visit for ui, pitfall and event notes). Utility = hidden minus shown, with a simple Welch interval. Probation becomes active when the lower bound is above 0 after 5 shown and 3 hidden units; any note is retired when the upper bound is below 0. Results go to `memory_score.json`. Fights come from `sts_cua.score.combats`, and reset runs (meta `kind: "reset"`) are neither scored nor reflected on. With `--memory`, the supervisor scores and reflects on every game run itself.

## Memory A/B evaluation

Does memory help? No memory vs a frozen snapshot of the note store, same character, A0, scored by the run score (docs/learning.md).

```
python -m sts_cua.eval snapshot                     # memory/notes.jsonl -> memory/frozen/notes-<date>.jsonl, read-only
python -m sts_cua.eval plan ab1 --arms none frozen:memory/frozen/notes-20261025.jsonl \
    --seeds seeds/eval.txt --model claude-haiku-5-5 --thinking off --daily-budget 2 --max-cost 0.5 --game-build v0.107.1
python -m sts_cua.eval plan ab2 --arms none frozen:... --runs-per-arm 20 ...   # no seeds: unpaired
python -m sts_cua.eval run ab1 --display :99        # plays what is left; run it again tomorrow to resume
python -m sts_cua.eval report ab1                   # per-arm stats and the test, also in the app's Results tab
```

- **Paired, with seeds** (one per line in the seed file): each seed is played once per arm. The goal tells the agent to start a Custom run (Singleplayer > Custom), pick the character, set Ascension 0, leave modifiers off and type the seed into the seed field. The supervisor reads each run's seed from the run history, a run on the wrong seed (or ascension or character) is invalid and retried (`--max-attempts`, default 2), and a valid run whose seed couldn't be read gets a `seed_unverified` warning. Test: Wilcoxon signed-rank on the per-seed score difference (second arm minus first).
- **Unpaired, without seeds:** standard runs with the arms interleaved in time. Test: Mann-Whitney U. The report says it is unpaired; seed variance stays in, so it needs more runs for the same power.
- Arm order alternates AB, BA, AB, ... so neither arm always goes first. Win rate is secondary: exact McNemar on discordant pairs (paired) or Fisher's exact test (unpaired). Effect size is the rank-biserial correlation (+1: every run of the second arm beats the first). The tests are pure Python (`src/sts_cua/stats.py`): exact p-values up to 50 pairs or 60 runs, ties included (exact conditional on the ranks), else a normal approximation with tie and continuity corrections.
- Every run goes through `supervisor.supervise` into `runs/`, so resets, scoring, the ledger and the daily budget work as in any session (`meta.json["eval"]` names the experiment, slot and arm). A run starts only when `--max-cost` is still left today, so the budget never cuts one short. When the budget is spent the eval stops; `run` again skips finished slots. It also stops after 3 runs in a row that never got into a run (for example when Custom mode is locked; plan an unpaired eval then).
- Frozen means the notes are shown but the run is never scored against the store and never reflected on (`SupervisorConfig.memory_frozen`). The plan stores the snapshot's sha256, and the eval stops if the file changes. The no-memory arm writes nothing.
- `plan` refuses evaluation seeds that already appear as the seed of a ledger run that used memory (training runs), plus any in `--training-seeds FILE`.
- Files: `runs/evals/<name>/plan.json` (arms, seeds, model flags, budget, the run order), `results.jsonl` (one line per finished game run, valid or not, with score, floor, outcome, cost and the seed played), `report.json` (rewritten after every run).

Seeds in Spire 2 (read 2026-10-25):
- The Spire 2 wiki's Custom Mode page says custom runs "can be started using a random seed, or by inputting a custom seed", set the Ascension level and take any number of modifiers, and that Custom Mode unlocks after beating Act 3 three times ([wiki](https://slaythespire.wiki.gg/wiki/Slay_the_Spire_2:Custom_Mode), marked under construction).
- SearchTheSpire: copy a seed, "go to singleplayer and start a custom run", which has "a seed field". Seeds assume a fully unlocked profile, because "locked cards, relics and characters shrink the pools the game draws from", and the main and beta branches generate different runs from the same seed ([searchthespire.app](https://searchthespire.app/)). Players share 10-character seeds such as `JDZCSFVCT5` for custom runs ([Steam discussion](https://steamcommunity.com/app/2868840/discussions/0/798967628136784876/)).
- STS2MCP (main @ 55e0648) can embark with a seed (`menu_select` with `seed`, `charSelect.Lobby.SetSeed`) only where the character select has a lobby; standard singleplayer returns "Seeded embark is not supported for standard singleplayer from this API" (`McpMod.Actions.cs`, `docs/raw-full.md`). It is a POST endpoint, which the harness never calls, so the agent types the seed itself. The compendium's run history and `current_run` report each run's `seed` (read-only).
- **Unverified, check on the PC:** that the agent's modded profile has Custom Mode unlocked (modded profiles are separate saves); where the seed field is and what it accepts; that a seed gives the same map, fights and rewards in both arms (unlocks earned during the eval change the pools, so keep the profile's unlocks fixed, ideally fully unlocked); whether custom runs keep Neow's bonus without modifiers; and that `seed` in the run history matches the typed seed.

## Co-op with Raph

The agent plays a run with Raph as a teammate. Raph plays the Windows build on his own Steam account and hosts; the agent plays on its own account (budthud) in WSL, with the game on Xvfb :99 and STS2MCP loaded (it sets `affects_gameplay: false`, so the host doesn't need it). Co-op facts and setup: [docs/hosting.md](docs/hosting.md#co-op-why-a-second-copy); screens and mod fields: [docs/action-space.md](docs/action-space.md#co-op).

Joining (the agent never accepts invites itself; that happens in Steam):

1. Start the agent's game first (`tools/launch_sts2.sh` or the app's Stack up), so it runs on Xvfb with the GPU.
2. Raph hosts on Windows (Multiplayer > Host) and invites budthud from Windows Steam or the lobby's invite button.
3. Raph (or the PC session) accepts the invite in the WSL Steam window. Check the agent's screen (app: fresh capture) shows Raph's lobby.
4. Start the agent once its game shows the lobby:

```
python -m sts_cua --display :99 --coop                      # Haiku 5.5, thinking off, rules on
python -m sts_cua --display :99 --coop --model claude-sonnet-5-5 --thinking on --max-cost 2
```

or the app's Run tab, "Co-op" (`POST /api/coop/start`), with a Stop button. Later options, not built: the in-game Multiplayer > Join list of friends with open lobbies, which the agent could click itself; a Steam `steam://` join URL (unverified for Spire 2). The mod's `menu_select join_<id>` would also work, but the harness never calls the mod's POST endpoints.

How a co-op run differs from solo:

- **Speed.** Defaults are the fastest settings: Haiku 5.5 with thinking off, a 4-step history, 0.3 s settle, 5,000 steps.
- **General rules first** (`src/sts_cua/rules.py`). Before the model, a table of rules reads the mod's state and decides the common cases: wait while it's not the agent's move (enemy turn, after End Turn, ready in the lobby, after voting), with no model call and polling the mod every 0.5 s; end the turn with `e` when no card in hand is playable; vote last on the map and on shared events, matching the majority of the other players (2 to 4 players), ties on the map by a type preference (rest first at low HP); bid on a treasure relic nobody else picked; take gold and potions; proceed when nothing is left; ready up in the lobby. Rules that need a click whose position isn't confirmed yet give the model a one-line instruction instead of acting. Each step logs `source: "rule"` (no model call) or `source: "model"`, plus `rule` when one fired. `--no-rules` asks the model every step. Where a rule fires, the mod's state decides or informs the step, so co-op isn't pixel-only.
- **Prompt.** Claude and Qwen get a co-op addendum after the system prompt (`coop.PROMPT`): teammate, end your turn, vote, leave the teammate's choices alone, never leave the lobby, wait when it's not your move. With `--coop` off the prompt, and so the cached prefix, is unchanged.
- **Waiting is normal.** While `coop.waiting(state)` names a reason, wait-only steps don't count toward the idle stop and an unchanged screen doesn't count as stuck (the step logs `waiting`). The run still stops after `--coop-idle-minutes` (30) with no action of the agent's that changed the screen, at `--max-cost`, or when the run is over (game over, or back at a menu).
- **Not solo data.** `meta.json` has `mode: "coop"`. `score.run_mode(meta)` reads it; memory scoring and reflection skip co-op runs; the supervisor stays solo-only and stops (`coop_session`) if it finds a co-op lobby or run. The app marks co-op runs in Results.

To check on the PC: the waiting and vote screens, whether a teammate's map vote is visible on the agent's screen, the lobby confirm position, whether pressing End Turn again takes the end turn back, and that game over in co-op reads as the `NGameOverScreen` overlay.

## Control app

One page to run everything from the PC or the phone: start and stop the stack and the local model, start and stop supervisor sessions, watch the live run and screen, read results, spend and logs, and retire notes or reflect on a run.

```
.venv/bin/python -m sts_cua.app          # http://127.0.0.1:8765, prints the token on first start
```

On Windows, `tools/windows/sts-cua-app.cmd` starts the server in WSL if it isn't answering and opens it in an Edge app window; [tools/windows/README.md](tools/windows/README.md) covers phone access through `tailscale serve`. The API is [docs/app-api.md](docs/app-api.md).

- Stdlib only (`ThreadingHTTPServer`), bound to 127.0.0.1. Every `/api/` call except `/api/health` needs the token in `~/.sts-cua/app-token` (mode 600), as a Bearer header or the HttpOnly, SameSite=Strict cookie that `/login?token=...` sets. No localhost exemption, since `tailscale serve` traffic also arrives from localhost. POSTs must be JSON.
- Fixed actions only: argv lists built from validated fields (model allowlist, clamped numbers, names matching `^[\w.-]+$`, paths kept inside `runs/`), never a command string from a request.
- Children it starts (supervisor, the model server, model downloads, `stack_up.sh`, reflect) get pid files in `~/.sts-cua/run/`, so a restarted app still knows them, and log to `~/.sts-cua/logs/`. Processes started elsewhere are found with pgrep.
- `tools/stack_down.sh` stops agent runs with a pattern that leaves `python -m sts_cua.app` running.
- `src/sts_cua/app/`: `server.py` (HTTP, auth, static page), `routes.py` (endpoints), `procs.py` (processes), `files.py` (tailing logs, run dirs), `static/` (the page).

## Video footage

The project history for the video lives in [docs/devlog.md](docs/devlog.md).

```
.venv/bin/python -m sts_cua --record ...                          # also writes <run_dir>/screen.mp4 (ffmpeg x11grab, 15 fps)
.venv/bin/python -m sts_cua.replay runs/<run>                     # replay.mp4 + highlights.md in the run dir
.venv/bin/python -m sts_cua.replay runs/<run> --highlights-only   # highlights.md only, no ffmpeg needed
```

- `replay.mp4`: one frame per step, 1.5 s each (`--seconds-per-step`), with a bar showing step, floor, HP, gold, spend and the AI's thought, and red circles on its clicks.
- `highlights.md`: fights started and won, floors, hits of 10+ HP, stuck streaks (3+ steps with no screen change), the end of the run, each with its time in `replay.mp4` and, for recorded runs, in `screen.mp4`.
- Both need `ffmpeg` (`sudo apt install ffmpeg`). Every step now logs `screen_diff` (change since the previous screenshot, 0-255); older runs are recomputed from their screenshots.

## Status

Harness runs Spire 2 end to end on Raph's PC. Haiku 5.5 won the first fight of Act 1 (14 steps, $0.0056, run 20261025T164617Z), then went from the floor 0 map to floor 6, winning 5 fights (96 steps, $0.041, run 20261025T181542Z). The supervisor for back-to-back full runs (`python -m sts_cua.supervisor`) is written and tested on fakes only; it has not run against the game yet.
