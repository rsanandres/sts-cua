"""The local-model bake-off: grounding targets from real runs, per-model scoring, and measured VRAM and RAM.

    python -m sts_cua.bench extract                 # runs/*/steps.jsonl -> bench/targets.jsonl + bench/images/
    python -m sts_cua.bench review                  # contact sheets with the targets drawn; --delete ID ... drops bad ones
    python -m sts_cua.bench grounding qwen3.8-27b   # hit rate, error, latency, tokens/s per model
    python -m sts_cua.bench clicks qwen3.8-27b --display :99 --n 20   # live on tools/click_target.py
    python -m sts_cua.bench baseline                # VRAM and RAM before the model server starts
    python -m sts_cua.bench resources qwen3.8-27b --mode solo
    python -m sts_cua.bench report --write-docs     # tables, the ranking, and docs/hosting.md

Results go to runs/bench/<timestamp>/results.json and results.md; `report` reads the newest
result per model (and per model and mode for resources).
"""
