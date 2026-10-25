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

- No CommunicationMod equivalent exists, so we write our own state-export mod. Spire 2 mods are C# DLLs plus Godot `.pck` files, patching the game with Harmony hooks ([modding tutorial](https://github.com/fresh-milkshake/Modding-Tutorial), pinned to build v0.103.3, may be out of date).
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

- [CommunicationMod](https://github.com/ForgottenArbiter/CommunicationMod): lets another process read state and control the game over stdin/stdout. It kills the controller if it does not print "ready" in time, and any stray print to stdout breaks the protocol. Config lives at `~/.config/ModTheSpire/CommunicationMod/config.properties`, so each parallel instance likely needs its own config dir (inference, untested).
- [spirecomm](https://github.com/ForgottenArbiter/spirecomm): Python client for CommunicationMod, plus a simple scripted AI. A useful baseline to beat.
- sts_lightspeed (gamerpuppy): RNG-accurate C++ simulator, used by RL projects such as [sts-rl-agent](https://huggingface.co/Jialeiv/sts-rl-agent) (self-reports 14% win rate on 50 seeds vs 6% for the stock heuristics) and [AutoClad](https://github.com/benmuth/AutoClad). Not pixels, but useful later if we ever train a policy.
- The Spire 1 tools above (CommunicationMod, spirecomm, sts_lightspeed) don't work on Spire 2, but their protocol and bots are good references for our own mod.

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

Claude needs `ANTHROPIC_API_KEY` (or an `ant auth login` profile). Each run writes `runs/<timestamp>/` with `meta.json`, `steps.jsonl` (thought, reasoning summary, proposed and executed actions, tokens, cost per step), a screenshot per step, and `summary.json`. A run stops at `--max-steps`, at `--max-cost` USD, after 3 steps in a row with no action, or after 3 errors in a row.

Code map:

- `src/sts_cua/loop.py`: screenshot, decide, act, log.
- `src/sts_cua/models/base.py`: the `Model.decide(Observation) -> Decision` interface every adapter implements.
- `src/sts_cua/models/claude.py`: Claude adapter on the `computer_toolset_20260801` tool. Each step is a fresh request (cached system prompt and tools, a text log of recent steps, the current screenshot), so nothing is replayed and cost per step stays flat.
- `src/sts_cua/screen.py`: `X11Screen` (mss capture, xdotool input) and `FakeScreen` for tests.
- `src/sts_cua/runlog.py`, `src/sts_cua/pricing.py`: run logs and per-step cost.

## Status

Harness and Claude adapter built and tested against a virtual display with a scripted model. Not yet run against the live API or the game.
