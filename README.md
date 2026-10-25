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
- Whether Spire 2 writes a run-history file we can score from without a mod.
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

Code map:

- `src/sts_cua/loop.py`: screenshot, decide, act, log.
- `src/sts_cua/models/base.py`: the `Model.decide(Observation) -> Decision` interface every adapter implements.
- `src/sts_cua/models/claude.py`: Claude adapter on the `computer_toolset_20260801` tool. Each step is a fresh request (cached system prompt and tools, a text log of recent steps, the current screenshot), so nothing is replayed and cost per step stays flat.
- `src/sts_cua/screen.py`: `X11Screen` (mss capture, xdotool input) and `FakeScreen` for tests.
- `src/sts_cua/runlog.py`, `src/sts_cua/pricing.py`: run logs and per-step cost.

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

The harness only reads `GET /api/v1/singleplayer` (`src/sts_cua/state.py`) and logs a summary per step as ground truth (screen type, floor, HP, energy, hand, enemies and intents). The agent never sees it; it stays pixel-only. Never call the mod's POST endpoints from the harness.

## Play runs back to back

Needs the game on the main menu (`tools/launch_sts2.sh`) and the STS2MCP mod, which the supervisor reads to tell when a run is over.

```
python -m sts_cua.supervisor --display :99 --model scripted --max-runs 1 --max-steps 5   # dry run, no API calls
python -m sts_cua.supervisor --display :99 --model claude-haiku-5-5 --thinking off --daily-budget 2.00 --game-build v0.107.1
```

Each game run starts a new singleplayer run from the main menu and plays it to the end. A run stops when the mod shows the game-over screen or a menu after the run started, or at the usual caps (`--max-steps` 3000, `--max-cost` per run, default whatever is left of today's budget, 10 idle steps in a row). Then the supervisor writes `score.json` into the run dir: outcome (won, died, abandoned, unknown), max floor, bosses killed, and a per-combat breakdown. It also appends one line to `runs/ledger.jsonl`. Score is 100 for a win, otherwise floor + 52/3 per boss killed (AgenticSTS's score).

If the game isn't on the main menu with no saved run before a run, a short reset run (meta `kind: "reset"`, no score) asks the model to get it there. The daily budget is the sum of `cost_usd` in `runs/*/summary.json` that ended today in America/Los_Angeles, reset runs and earlier sessions included. The supervisor stops when less than `--reserve` ($0.10) is left, after `--max-runs`, when the mod can't be reached, when the reset fails, or after 3 runs in a row that never got into a run.

- `src/sts_cua/supervisor.py`: the session loop and budget accounting.
- `src/sts_cua/score.py`: outcome, score and per-combat breakdown from a run's logged states (`load_states(run_dir)`), and the stop check (`RunOverDetector`).

## Status

Harness runs Spire 2 end to end on Raph's PC. Haiku 5.5 won the first fight of Act 1 (14 steps, $0.0056, run 20261025T164617Z). The supervisor for back-to-back full runs (`python -m sts_cua.supervisor`) is written and tested on fakes only; it has not run against the game yet.
