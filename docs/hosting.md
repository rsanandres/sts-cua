# Hosting: the model and two game copies on one PC

How Raph's PC runs the local model, the AI's copy of Spire 2 and Raph's own copy at the same time, so Raph and the AI can play co-op together. Researched 2026-10-25. Rows marked **measure** are planning numbers, not measured on the PC yet.

The PC: Windows, RTX 4090 (24 GiB VRAM, about 3.3 GiB used by the desktop at idle), 64 GB RAM, WSL2 Ubuntu 24.04.

## Layout

```
Windows                                    WSL2 (Ubuntu 24.04)
+----------------------------+             +--------------------------------------+
| Steam, Raph's account      |             | Steam for Linux, agent account       |
| Spire 2, Windows build     |   Steam     | Spire 2, Linux build, on Xvfb :99    |
| Raph plays and hosts       |<--co-op---->|   + state-export mod                 |
+----------------------------+   invite    | harness (sts_cua) --> vLLM :8000     |
                                           | vLLM: Qwen3-VL-8B-Instruct-FP8       |
                                           +--------------------------------------+
                      one RTX 4090, shared by both sides
```

## Co-op: why a second copy

- Spire 2 co-op is online only, up to 4 players, joined by Steam friend invite. There's no matchmaking, LAN or direct IP ([co-op guide](https://brokenbuilds.gg/slay-the-spire-2/guides/slay-the-spire-2-co-op-guide)).
- Each player needs their own copy, and there's no friend pass ([Destructoid](https://www.destructoid.com/?p=1188972)). $24.99 on 2026-10-25 ([store API](https://store.steampowered.com/api/appdetails?appids=2868840&cc=us)).
- One Steam account can't be signed in twice at once ([Steam support](https://help.steampowered.com/en/faqs/view/71EA-CDCE-FB5C-82B3)), so the AI needs its own account.
- There's no turn timer, and Steam tags the game "Playable without Timed Input" (store API above). A turn ends when every player presses End Turn, so a slow model only makes Raph wait.
- The map path is voted on. Card rewards, shops and events are separate for each player ([co-op rules](https://allthings.how/slay-the-spire-2-co-op-multiplayer-every-mechanic-and-rule-explained/)).
- The save belongs to the host, and the host must stay connected. Raph hosts.

**Decided 2026-10-25:** buy a second copy on a second Steam account (the agent account). Steam for Linux in WSL signs in as that account. Raph plays the Windows build on his own account.

### Setup

1. Make the agent account, buy Spire 2 on it, and add it as a Steam friend of Raph's account.
2. Sign Steam for Linux in WSL in to the agent account. Redo the agent profile settings from the README there: profile preferences are cloud-synced per account, so Profile 3's settings don't carry over. The machine `settings.save` stays.
3. Keep both copies on the same build and Steam branch. Check the version on both main menus before a session.
4. Start the AI's copy first with `tools/launch_sts2.sh`, so it launches directly on Xvfb with the GPU.
5. Raph hosts on Windows and invites the agent account. Accept the invite in the WSL Steam window.
6. Once the agent's game shows Raph's lobby, start the agent with `python -m sts_cua --coop` (or the app's Co-op section). The README's "Co-op" section has the whole flow.

### To verify

- Whether Steam co-op works between two accounts on one PC, one on Windows and one in WSL's NAT network. Nothing found either way.
- Whether accepting an invite hands the lobby to the already running copy. If Steam launches a new copy instead, it goes through Steam's runtime container, which can't reach the GPU in WSL (see `src/sts_cua/game.py`).

### Fallback without Steam: `--fastmp`

The retail build has undocumented dev flags that connect copies on one machine without Steam ([STS2-Hand-and-Brain](https://github.com/jkclark/STS2-Hand-and-Brain)):

- Host: `--force-steam off --fastmp host_standard`
- Client: `--force-steam off --fastmp join --clientId 1000`

How they behave:
- The copies connect over ENet to a hard-coded `127.0.0.1`, UDP port 33771 ([GItS PR 985](https://github.com/MontySteele/GItS/pull/985)).
- Each copy uses a test profile instead of the Steam save.
- Start the copies a few seconds apart, or give each its own `--log-file`.

Limits:
- From Windows to WSL this needs WSL mirrored networking ([WSL networking](https://learn.microsoft.com/en-us/windows/wsl/networking)).
- It's untested on the Linux build.
- Mega Crit's rule is still one copy per player.

## Mods in co-op

- A mod whose manifest sets `affects_gameplay: false` isn't checked against the host's mods ([v0.99.1 patch notes](https://steamcommunity.com/ogg/2868840/announcements/detail/502852451136701667)). STS2MCP sets it ([manifest](https://github.com/Gennadiyev/STS2MCP/blob/main/mod_manifest.json)).
- So the AI's state-export mod should set it too. Raph's copy can then probably stay unmodded; not tested yet.
- A separate check can still disconnect: clients compare a hash of the game's model IDs. A mod that changed the model database got "ModelDb hash mismatch" and a disconnect on matching game versions ([BaseLib-StS2#172](https://github.com/Alchyr/BaseLib-StS2/issues/172)).
- Beta v0.108.0 leaves non-gameplay models out of that hash. Until that reaches the public branch, the state-export mod must not register models. A mod that only reads state and writes JSON registers none.
- STS2MCP calls its own multiplayer support beta.

## Model

### Stronger options (researched 2026-10-25)

Qwen3-VL-8B below was the first pick, and it's too weak for Spire. Sizes are GiB from the Hugging Face API. Benchmarks are as published, mostly by each model's own authors.

| Mode | Model | Checkpoint (GiB) | Published scores | License | Notes |
|---|---|---|---|---|---|
| Solo (~20 GiB free) | **Qwen3.8-27B** (Aug 2026, 27.78B dense) | unsloth UD-Q4_K_XL 16.35 + mmproj 0.86 ([HF](https://huggingface.co/unsloth/Qwen3.8-27B-GGUF)) | OSWorld-Verified 84.3, AndroidWorld 81.9 ([model card](https://huggingface.co/Qwen/Qwen3.8-27B)) | Apache-2.0 | Thinking is on by default; turn it off per request with `chat_template_kwargs: {"enable_thinking": false}`. The vLLM 4-bit builds are 19.6-20.4 GiB, so use llama.cpp |
| Solo, A/B against the above | **Holo4-27B** (Sep 2026, fine-tune of Qwen3.8-27B) | q4_k_m 15.72 + 0.86 ([HF](https://huggingface.co/Hcompany/Holo4-27B-GGUF)) | OSWorld 85.2, OSWorld 2.0 61.7 (Qwen3.8-27B: 48.0), AndroidWorld 85.1, at about a third of the base model's cost per task ([H Company](https://hcompany.ai/newsroom/holo4)) | CC BY-NC 4.0 (HF metadata) | Same architecture as Qwen3.8-27B, so it serves the same way |
| Co-op (~13 GiB free) | **UI-Venus-2-9B** (Aug 2026, from Qwen3.5-9B) | bartowski Q8_0 8.89 + mmproj ([HF](https://huggingface.co/bartowski/UI-Venus-2-9B-GGUF)) | OSWorld-Verified 70.8, ScreenSpot-Pro 73.0, AndroidWorld 80.2 ([model card](https://huggingface.co/inclusionAI/UI-Venus-2-9B)) | "Pending final confirmation" | Card: reasoning on for agent tasks, off with temperature 0 for grounding. Coordinate format not stated on the card; check the UI-Venus repo's parsers |
| Co-op, tight | Qwen3.8-27B UD-IQ3_XXS | 10.18 + 0.86 | Scores above are for full precision | Apache-2.0 | Fits but leaves almost no headroom. Untested at 3 bits for clicking; a 4-bit AWQ build of Qwen3.5-27B scored 60.9 on ScreenSpot-Pro against 70.3 at full precision, which the authors put down to quantization and prompt format ([2604.15376](https://arxiv.org/abs/2604.15376)) |

Limits on what these numbers mean:
- **The benchmarks mostly measure clicking.** OSWorld and AndroidWorld test operating apps from screenshots. Spire is hard because of strategy, and there no local model has evidence yet. In AgenticSTS (Spire 2, A0, text state), Qwen 3.6-27B won 0 of 5 runs, while Gemini 3.1 Pro won 3 of 10, or 6 of 10 with memory ([2607.02255](https://arxiv.org/abs/2607.02255)).
- **Splitting the work is the design with evidence.** AgenticSTS made a median of 67 strategic calls per run and resolved roughly 500 routine decisions mechanically or with a fast tier. Here that means:
  - The co-op rules (`rules.py`) handle routine moves.
  - A local model or Haiku handles combat.
  - Sonnet handles rewards, map, shop, events and bosses.
  - Claude's decisions become fine-tuning data for the local model.
- **Not worth it now: offloading experts to system RAM.** Big MoE models (Qwen3.5-122B-A10B, GLM-4.6V) run with llama.cpp expert offload, but we estimate 6-22 s per step on this PC from published 4080/4090 figures, for a few benchmark points. Revisit with 96 GB+ RAM.
- **Measure before switching.** Run each candidate on fixed Spire click targets (`tools/click_target.py` style) and a few combats, then compare hit rate, latency and floors reached.

### Current adapter: Qwen3-VL-8B

| Option | Weights | Click coordinates | Image tokens at 1280x720 | Notes |
|---|---|---|---|---|
| **Qwen3-VL-8B-Instruct-FP8 on vLLM** (pick) | 10.59 GB | 0-1000 relative ([cookbook](https://github.com/QwenLM/Qwen3-VL/blob/main/cookbooks/computer_use.ipynb)) | 880 (resized to 1280x704, 32 px patches) | Official checkpoint, Apache-2.0 ([HF](https://huggingface.co/Qwen/Qwen3-VL-8B-Instruct-FP8)) |
| UI-TARS-1.5-7B AWQ on vLLM | 6.92 GB | Absolute pixels on the resized 1288x728 image ([coordinates](https://github.com/bytedance/UI-TARS/blob/main/README_coordinates.md)) | 1,196 (28 px patches) | Community quant ([HF](https://huggingface.co/flin775/UI-TARS-1.5-7B-AWQ)). Its card says it isn't tuned for games |
| Qwen3-VL-8B-Instruct Q4_K_M GGUF + F16 mmproj on llama.cpp | 5.03 + 1.16 GB | 0-1000 relative | 880 | Fallback if VRAM is tight. Runs natively on Windows ([HF](https://huggingface.co/Qwen/Qwen3-VL-8B-Instruct-GGUF)) |

Why the pick:
- The relative coordinates don't depend on how the server resizes the screenshot, which matters for a pixel agent.
- The FP8 build runs natively on the 4090.
- Holo2-8B, GUI-Owl-1.5-8B and MAI-UI-8B use the same Qwen3-VL architecture. They swap in at the same memory for comparisons.

KV cache for Qwen3-VL-8B, from `config.json` (36 layers, 8 KV heads, head dim 128, BF16): 2 × 36 × 8 × 128 × 2 = 147,456 bytes per token. That's 1.13 GiB for an 8,192-token context. Each step is a fresh request (system prompt, a short text log, one screenshot), so 8k is plenty, and one sequence covers one AI seat.

```
vllm serve Qwen/Qwen3-VL-8B-Instruct-FP8 \
  --gpu-memory-utilization 0.55 \
  --max-model-len 8192 \
  --max-num-seqs 1 \
  --limit-mm-per-prompt '{"image":1,"video":0}'
```

vLLM gotchas:
- `--gpu-memory-utilization` is a fraction of *total* VRAM and defaults to 0.92 ([cache.py](https://github.com/vllm-project/vllm/blob/main/vllm/config/cache.py)).
- vLLM refuses to start if that much isn't free, even when `--kv-cache-memory-bytes` is set ([`request_memory`](https://github.com/vllm-project/vllm/blob/main/vllm/v1/worker/utils.py)). On this PC the default fails, so always pass it.
- Start both games before vLLM, so its startup check sees the real free memory.
- Pinned memory is off by default under WSL2. `VLLM_WSL2_ENABLE_PIN_MEMORY=1` turns it on ([cuda.py](https://github.com/vllm-project/vllm/blob/main/vllm/platforms/cuda.py)).
- If activations don't fit in the budget, vLLM fails at startup with no room for the KV cache. Raise the fraction a little or add `--enforce-eager`, which skips CUDA graph memory but runs slower.

## Serving the local models

`python -m sts_cua.serve` (and the app's Control > Model server card, which calls the same code) downloads, starts and stops the models in `src/sts_cua/models/registry.py`. One server at a time, on `127.0.0.1:8000`, where `--base-url` points by default. Its pid file is `~/.sts-cua/run/model.pid` and its log `~/.sts-cua/logs/model.log`; Stack down stops it too.

1. **Install llama.cpp** with `tools/install_llama_cpp.sh` (into `~/llama.cpp`, prints the `llama-server` path). It builds with `cmake -B build -DGGML_CUDA=ON -DCMAKE_CUDA_ARCHITECTURES=89` ([build docs](https://github.com/ggml-org/llama.cpp/blob/master/docs/build.md#cuda)), which needs the CUDA toolkit inside WSL. Per [NVIDIA's WSL guide](https://docs.nvidia.com/cuda/wsl-user-guide/index.html), install only the `cuda-toolkit-12-x` package from the WSL-Ubuntu repo, never a Linux driver or the `cuda`/`cuda-drivers` meta-packages. `--prebuilt` instead fetches the release's `ubuntu-cuda-12.8-x64` binaries plus the CUDA runtime, with no toolkit (layout not checked on the PC yet). `serve` looks for `llama-server` at `--llama-server`, `$LLAMA_SERVER`, `~/llama.cpp/build/bin/llama-server`, then PATH. The app's Control > Model server card runs the same script (Install llama.cpp, with a Prebuilt release option) and shows the path and version it finds.
2. **Where the models live.** `--models-dir`, else `$STS_CUA_MODELS`, else `~/models`, else `/mnt/d/models` when only that exists, laid out as `<dir>/<repo>/<file>`. The app takes the same `--models-dir` and env var. D: has about 1 TB free, so `export STS_CUA_MODELS=/mnt/d/models` is the likely setting. Windows drives reach WSL through drvfs, which is slower than the Linux filesystem ([WSL file systems](https://learn.microsoft.com/en-us/windows/wsl/filesystems)), so for weights under `/mnt/` llama-server gets `--load-mode none` (no mmap; older builds' `--no-mmap`). Every load time goes to `model.log` and `~/.sts-cua/logs/model_loads.jsonl` with the path and load mode, so a copy on `~/models` can be compared. **Measure** which is faster.
3. **List and download.** `python -m sts_cua.serve list` shows the models dir and its free space, then each model's rank, file and needed GiB, which budget it fits and whether it's downloaded. `python -m sts_cua.serve download holo4-27b` fetches exactly the registry's weights and mmproj: it uses `hf download` if huggingface_hub is installed, else HTTPS with resume. It checks free disk first and every byte size after. The vLLM baseline downloads into the Hugging Face cache instead, where `vllm serve` looks (and fetches it itself if missing).
4. **Start.** `python -m sts_cua.serve start <model> --mode coop|solo|max` (max: no budget, only the free VRAM, for when Raph isn't using the PC), or `start best --mode ...` for the highest-ranked downloaded model that fits. It reads free VRAM with `nvidia-smi` (`/usr/lib/wsl/lib/nvidia-smi` first), and refuses a model whose need is over the free VRAM or the mode's budget (co-op 13 GiB, solo 20 GiB) unless `--force`. `best` uses the smaller of the two and prints what it picked and which stronger models it skipped. It waits for `/health` (llama.cpp) or `/v1/models` (vLLM), up to `--timeout` seconds (900 by default). `status` shows the served model, pid and VRAM; `stop` stops it.
5. **Start the games first.** The VRAM check sees only what's in use when it runs, so start the AI's copy (and Raph's, for co-op) before the model.

The llama.cpp command, with flags as in the [server README](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md) on 2026-10-26:

```
llama-server -m <weights> --mmproj <mmproj> -c 8192 -ngl 999 -fa on --jinja -np 1 \
  --host 127.0.0.1 --port 8000 --alias <served name>
```

- `-ngl 999` puts every layer on the GPU; newer builds also accept `all`.
- `--jinja` is now on by default; it's passed for older builds.
- `-np 1` is one slot, for one AI seat.
- `--alias` is the id `/v1/models` reports, so `--served-model` names it.
- The vLLM baseline runs the `vllm serve` command under "Current adapter" with `--host 127.0.0.1 --port 8000` added.

Which model when (ranks are in the registry; the bake-off replaces them with measurements):

| Mode | Pick | Fallback |
|---|---|---|
| Solo | holo4-27b (18.1 GiB with overhead) | qwen3.8-27b, or qwen3.8-27b-q5 (19.7) when measured free VRAM allows |
| Co-op | ui-venus-2-9b (11.3) | qwen3.8-27b-iq3 (12.6, little headroom), qwen3-vl-8b on vLLM |
| Max | `start best --mode max`: the strongest downloaded model that fits the VRAM measured free | For when Raph isn't using the PC: no co-op copy, nothing else on the GPU. qwen3.8-27b-q5 (19.7) fits only then |

## VRAM budget (GiB)

| Item | VRAM | Source |
|---|---|---|
| Total | 24 | |
| Windows desktop, idle | 3.3 | `nvidia-smi` on the PC, 2026-10-24 |
| vLLM at `--gpu-memory-utilization 0.55` | 13.2 | 0.55 × 24 |
| ...FP8 weights (part of the 13.2) | 9.9 | 10.59 GB from HF |
| ...KV cache, 8k tokens (part of the 13.2) | 1.1 | math above |
| ...activations, CUDA graphs, vision encoder (part of the 13.2) | 2.2 left | **measure**: read vLLM's startup log |
| Two Spire 2 copies | 3.7 | Steam's recommended spec, 2 GB VRAM each. **Measure**: Raph's copy may run above 1280x720 |
| Headroom | 3.8 | |

Spillover:
- Since driver 536.40, CUDA allocations spill into system RAM when VRAM runs out, and get slow instead of failing ([NVIDIA](https://nvidia.custhelp.com/app/answers/detail/a_id/5490/~/system-memory-fallback-for-stable-diffusion)).
- Set NVIDIA Control Panel > Manage 3D settings > "CUDA - Sysmem Fallback Policy" to "Prefer No Sysmem Fallback".
- Reports conflict on whether WSL respects that setting ([WSL #11050](https://github.com/microsoft/WSL/issues/11050)). Also watch Task Manager > Performance > GPU > Shared GPU memory during a run.

## RAM budget

- 64 GB total. The WSL VM gets 50% by default (32 GB), plus swap of 25% (16 GB) ([wsl-config](https://learn.microsoft.com/en-us/windows/wsl/wsl-config)).
- Inside WSL:
  - vLLM memory-maps the weights, so expect about 10.6 GB of reclaimable page cache.
  - The vLLM process itself: **measure**.
  - The AI's copy, Steam for Linux and Xvfb: **measure**.
  - Steam recommends 8 GB of RAM for the whole system.
- Raph's copy runs on the Windows side, outside the VM.
- Start with the default 32 GB. Raise `memory` in `.wslconfig` only if `free -h` shows the VM swapping during a co-op run.

## Harness changes co-op needs

- Done: co-op mode (`python -m sts_cua --coop`, `src/sts_cua/coop.py`, `src/sts_cua/rules.py`) with its own idle rule: waits don't count while the mod says it's Raph's move or a vote is open, and a 30-minute no-progress timeout instead.
- Done: the Qwen3-VL adapter (`src/sts_cua/models/qwen.py`).
- Done: the co-op screens in `docs/action-space.md` ("Co-op"), mostly unverified.
- For the `--fastmp` fallback only:
  - `game.launch` refuses a second copy, since it checks with `pgrep`.
  - Every copy logs to the same file.

## Measure on the PC

1. VRAM baseline on Windows: `nvidia-smi --query-gpu=memory.used --format=csv -l 2`. Start the AI's copy, then Raph's copy, and note each jump.
2. Start vLLM. Read "Model loading took" and "Available KV cache memory" in its log.
3. Play one co-op turn. Shared GPU memory on Task Manager > Performance > GPU should stay flat.
4. RAM inside WSL: `free -h`, then `ps -eo pid,rss,comm --sort=-rss | head`. Compare with the VmmemWSL row in Task Manager.
5. Replace the **measure** rows above with the results.

## Measured on the PC

<!-- bench:start -->
Nothing measured yet. `python -m sts_cua.bench report --write-docs` fills this in from the bake-off (README, "Bake-off").
<!-- bench:end -->
