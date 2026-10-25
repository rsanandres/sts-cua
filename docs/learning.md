# Learning: memory first, then RL on the 4090

How the agent keeps improving across runs. Researched and decided 2026-10-25. The first goal is solo runs, scored by floor reached and win rate; co-op teammate skill comes later.

Decided:
- Memory-based learning first. It needs no training, and it works for Claude and the local model alike.
- RL weight updates (LoRA on the local model) come later.
- Training compute is the 4090 only, at $0 a month, on top of the $2/day API budget. Revisit once steps per hour are measured.
- The base model stays Qwen3-VL-8B-Instruct ([docs/hosting.md](hosting.md)).
- One AI game copy for now.

## Where Spire 2 agents stand

- Epoch AI (2026-10-14) ran a screenshot-only computer-use harness on Spire 2 at Ascension 10, with 15 single-shot runs per character ([Epoch](https://epoch.ai/publications/ai-systems-are-improving-rapidly-at-slay-the-spire-and-its-sequel)). The harness was built on Inspect ReAct, could look things up on the wiki, and kept notes across context compaction.
  - Win rates: GPT-6 Astra 35%, GPT-6.1 Sol 29%, GPT-6 Sol 13%, Claude Opus 5.5 7%, Claude Fable 5.1 4%, Claude Sonnet 5.5 3%, GPT-6 Luna 0%.
  - The report gives no cost, token or step figures.
- AgenticSTS ([arXiv 2607.02255](https://arxiv.org/abs/2607.02255)) is a text-state Spire 2 agent with typed memory. At A0 it won 3/10 runs without memory and 6/10 with its skill layer. The authors call that directional (Fisher p≈0.37), not proven.
- Reading from pixels doesn't cap the agent: the strongest published Spire 2 result plays from screenshots. Better scaffolding and memory are the levers to pull first.

## Stage 0: groundwork

1. **Supervisor.** Full runs back to back under the daily budget, with end-of-run detection from the mod state.
2. **Score.** 100 for a win, otherwise floor + 52/3 per boss killed (AgenticSTS's score). It moves long before win rate does.
3. **Per-combat breakdown.** Enemy names, HP at start and end, HP lost, won or lost, steps. Both memory and RL reuse it.
4. **Fixed evaluation seeds,** kept separate from the seeds memory is written on. **Unverified:** whether Spire 2 seeds give repeatable runs, and whether the mod can restore a state mid-run.
5. **Measure:** steps per full run, steps per hour, and cost per run.

## Stage 1: memory loop

Based on AgenticSTS, ExpeL ([2308.10144](https://arxiv.org/abs/2308.10144)) and ACE ([2510.04618](https://arxiv.org/abs/2510.04618)).

- **Notes.** Short typed notes (`ui`, `enemy`, `card`, `event`, `strategy`, `pitfall`), each at most 300 characters. Each note is keyed by screen type, enemy, card, event and act, and tracks status (probation, active, retired), times shown and utility. No seed-specific notes.
- **Keys use only what the screen shows:** enemy names, card names in hand, screen type, act and floor from the top bar. The true state picks notes, but it must not leak information the agent couldn't see.
- **Retrieval each step.** Plain code, no LLM call. Notes are capped at about 600 tokens per step, plus a playbook of at most about 1,500 tokens. They go into the per-step message after the cached system prompt, so the cache prefix never changes. The caps are our starting values.
- **Edits after each run.** One Sonnet call returns small edits only: ADD (at most 3 per run), UPDATE, DOWNVOTE or RETIRE.
  - Never rewrite the whole store. On AppWorld, full rewrites collapsed a cheatsheet from 18,282 tokens to 122 and dropped accuracy from 66.7 to 57.1 (ACE).
- **Validation gate.**
  - New notes start on probation and appear in a random half of the places they apply.
  - Utility is scored on dense outcomes: HP lost in combats where the note was shown vs not shown, and wasted steps per screen for UI notes.
  - A note is promoted when the lower confidence bound is above 0 and retired when the upper bound is below 0.
  - Saving everything scored worse than a fixed memory (55.48 vs 67.53), and strict filtering on entry scored 70.95 ([2505.16067](https://arxiv.org/abs/2505.16067)).
- **Guarding against bad notes.** Cap writes per run, require evidence from more than one run, tag notes with the game build, and re-check them after patches. Poisoning attacks need under 0.1% of memory to succeed over 80% of the time ([AgentPoison](https://arxiv.org/abs/2407.12784)), so one bad run must not be able to rewrite the playbook.
- **Claude's memory tool.** `memory_20250818` runs client-side ([docs](https://platform.claude.com/docs/en/agents-and-tools/tool-use/memory-tool)). The API tells the model to view its memory before anything else, which adds round-trips to every per-step request. If we use it at all, it goes in the post-run call only.
- **Evaluation.** Paired fixed seeds: no memory vs a frozen memory snapshot, same character, A0.
  - The primary metric is the score above, compared per seed with a paired test. Win rate is secondary.
  - Telling a 30% win rate from a 60% one takes many runs per arm (about 42 by our own power estimate), so floor-based scores are what fit the budget.

## Stage 2: warm start the local model

- Fine-tune Qwen3-VL-8B (LoRA) on the best filtered steps from Claude-with-memory runs, with the mod state as the filter. Then iterate on the model's own filtered runs (rejection-sampling fine-tuning).
- Evidence:
  - Every GUI RL pipeline we found starts from supervised fine-tuning.
  - On a card game, "without SFT, all end-to-end RL runs fail" ([2501.17161](https://arxiv.org/abs/2501.17161)).
  - Small sets work: PAE started from Claude 3 Sonnet traces ([2412.13194](https://arxiv.org/abs/2412.13194)), and OpenWebRL from 0.4K trajectories ([2606.02031](https://arxiv.org/abs/2606.02031)).
- Caveats:
  - Filtered imitation trails advantage-based RL: DigiRL 57.8 vs 67.2 ([2406.11896](https://arxiv.org/abs/2406.11896)).
  - Claude Sonnet 5.5 is a weak teacher at A10 (3% wins). Train at A0, where its runs go further.
- The local adapter logs request messages, raw outputs and per-token logprobs in every step, so `runs/` doubles as the dataset.

## Stage 3: RL on the 4090

**Episodes.** Each combat is a short episode, rewarded by HP lost and whether it was won. Card picks and map choices get credit from what happens downstream.
- Most Spire 2 decisions are routine (AgenticSTS).
- Splitting into subgoals is how the PokeAgent speedrun winner worked ([2603.15563](https://arxiv.org/abs/2603.15563)).
- Online RL on whole runs with only an end-of-run reward ranked last at this budget. UI-TARS-2 still hit plateaus with far more compute ([2509.02544](https://arxiv.org/abs/2509.02544)).

**Credit per step.**
- GRPO compares several samples from the same state, and the game can't be reset mid-run.
- GiGPO groups steps by repeated states instead, at no extra memory ([2505.10978](https://arxiv.org/abs/2505.10978)). A hash of the mod state can identify repeats.
- Alternatively, a critic that sees the true state (SWEET-RL, [2503.15478](https://arxiv.org/abs/2503.15478)).

**Memory on 24 GiB** (weights from the HF safetensors metadata):

| Setup | VRAM | Fits? |
|---|---|---|
| 8B 4-bit base (7.17 GiB) + LoRA r16 with Adam (0.65 GiB) + checkpointed activations at 3k tokens (about 1.1 GiB) | about 10-12 GiB | Yes, with vLLM asleep |
| 8B training while vLLM also serves the 8B | over the budget | No |
| 4B BF16 LoRA | about 11-12 GiB | Yes. The only size that could train while serving |

**Nightly cycle.**
1. **During the day:** play. Every step's screenshot, prompt, action, logprobs and outcome is logged.
2. **At night:** put vLLM to sleep (`--enable-sleep-mode`, `/sleep`; [docs](https://docs.vllm.ai/en/latest/features/sleep_mode.html)), then train with Unsloth `FastVisionModel` 4-bit QLoRA.
   - Unsloth has a Qwen3-VL-8B vision GRPO notebook ([notebook](https://github.com/unslothai/notebooks/blob/main/nb/Qwen3_VL_(8B)-Vision-GRPO.ipynb)).
   - Train the language layers only, at rank 32. Set `max_prompt_length` to at least about 2,800, because the notebook default of 1,024 cuts off the 880 image tokens.
3. **In the morning:** wake vLLM and load the new adapter without a restart: `--enable-lora --max-lora-rank 32`, then `/v1/load_lora_adapter` with `VLLM_ALLOW_RUNTIME_LORA_UPDATING=True` ([docs](https://docs.vllm.ai/en/latest/features/lora.html)). vLLM's `--max-lora-rank` default is 16.
4. **Before promoting an adapter:** it must beat the current one on the fixed evaluation seeds, otherwise roll back.

To verify on the PC:
- **LoRA on an FP8 base in vLLM.** vLLM's own tests cover GPTQ and AWQ bases. If FP8 doesn't work, serve the 4-bit base the adapter was trained on.
- **WSL2 memory for sleep level 1.** That level moves the weights into system RAM, so raise the VM limit before relying on it ([WSL](https://learn.microsoft.com/en-us/windows/wsl/wsl-config)).

## Cloud, if the $0 plan stalls

- Tinker retired its Qwen3-VL models on 2026-06-12. Its small vision models are Qwen3.5-4B ($0.737/M training tokens) and Qwen3.5-9B ($1.463/M), and both export downloadable PEFT adapters ([pricing](https://tinker-docs.thinkingmachines.ai/tinker/models/models_and_pricing/)). Using Tinker means switching the base model.
- Rented GPUs (read 2026-10-25): Vast.ai H100 on-demand from $1.93/h, RunPod community RTX 4090 $0.34/h.

## Risks

1. **Too few samples.** With one game copy, experience is the bottleneck, not the GPU. AgenticSTS's median run took about 80 minutes for a text agent. Sparse-reward multi-turn RL can collapse ([RAGEN](https://arxiv.org/abs/2504.20073)).
2. **Wrong lessons.** Agents copy what they retrieve. The validation gate and write caps are the defense.
3. **Reward hacking and forgetting.** Score from the mod's true state, not a judge model. RL forgets less than SFT ([2509.04259](https://arxiv.org/abs/2509.04259)).
