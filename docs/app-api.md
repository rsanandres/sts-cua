# sts-cua control app: API contract (v1)

Server: `python -m sts_cua.app [--host 127.0.0.1] [--port 8765] [--repo ~/sts-cua] [--token-file ~/.sts-cua/app-token] [--state-dir ~/.sts-cua] [--display :99] [--vllm PATH] [--llama-server PATH] [--models-dir DIR]`. `--models-dir` defaults like `python -m sts_cua.serve`: `$STS_CUA_MODELS`, else `~/models`, else `/mnt/d/models` if only it exists. Pid files of the children it starts go in `<state-dir>/run/`, logs in `<state-dir>/logs/`.
Python stdlib only (http.server.ThreadingHTTPServer), no new dependencies. Serves the page from `src/sts_cua/app/static/` at `/`.
Runs inside WSL on Raph's PC. Binds 127.0.0.1 by default. Phone access is via `tailscale serve` on Windows proxying to localhost (tailnet only), so the server never binds a public interface itself.

Auth, always on: the token file (32+ random bytes, hex, mode 600) is created on first start. Every request under /api/ except /api/health (screenshots included) needs `Authorization: Bearer <token>` or the `sts_cua_token` cookie (HttpOnly, SameSite=Strict). `GET /login?token=<token>` checks the token with a constant-time compare, sets the cookie and redirects to `/`; a wrong token gets 401. No localhost exemption: `tailscale serve` traffic also arrives from 127.0.0.1, so the source address proves nothing. The static page itself (`/`, `/<file>`) needs no token. The Windows launcher reads the token from WSL and opens `/login?token=...`; on the phone, open that URL once.
POST bodies must be `Content-Type: application/json` (blocks simple cross-site form posts), else 415; at most 64 KiB (413); a JSON object or empty (read as `{}`).

All JSON. Errors: `{"error": "message"}` with 4xx/5xx: 400 invalid input, 401 no or wrong token, 404, 405 wrong method, 409 already running or busy, 503 screen unavailable. Times are ISO 8601 UTC. Money is USD floats.

## Status

`GET /api/health` -> `{"ok": true, "version": "0.1.0"}` (no auth)

`GET /api/status` ->
```
{
  "processes": {               // each: {"running": bool, "pid": int|null, "since": iso|null}
    "steam": {...}, "xvfb": {...}, "game": {...}, "model": {...}, "supervisor": {...}
  },
  "gpu": {"used_mib": int, "total_mib": int} | null,       // /usr/lib/wsl/lib/nvidia-smi or nvidia-smi
  "ram": {"used_mib": int, "total_mib": int},               // inside WSL (/proc/meminfo)
  "budget": {"date": "2026-10-25", "spent_usd": 0.42, "cap_usd": 2.0},  // America/Los_Angeles day, from runs/*/summary.json
  "mod": {"reachable": bool, "state_type": str|null, "act": int|null, "floor": int|null, "hp": int|null, "max_hp": int|null},
  "busy": str|null             // an action in progress, see below
}
```
`coop` (top level): the co-op session, `{"running", "pid", "since"}` like a process (the app's own or one pgrep finds with `python -m sts_cua ... --coop`).
`busy`: "wsl_shutdown", "stack_down", "stack_up" (until stack_up.sh logs "stack is up"), "supervisor_stop" / "model_stop" / "coop_stop" (between SIGINT and exit), "model_start" (until the model server answers `/health` (llama.cpp) or `/v1/models` (vLLM)), "reflect", or null. `budget.cap_usd` is the `daily_budget` of the last `/api/runs/start`, else 2.0; `spent_usd` is `supervisor.spent_today` (run summaries plus reflect calls). A process counts as running when the app started it (pid file) or pgrep finds it.

## Stack control (fixed actions only, never arbitrary commands)

- `POST /api/stack/up` -> runs `tools/stack_up.sh` in the background (log `stack`). `{"started": true}`; 409 while one the app started is still running (it stays running, waiting, once the stack is up) or the stack is being stopped.
- `POST /api/stack/down` -> kills a waiting `stack_up.sh` the app started, then runs `tools/stack_down.sh` (stops runs, the model server, game, Steam, Xvfb; not the app). `{"started": true}`; 409 while one is running.
- `POST /api/wsl/shutdown` -> body `{"confirm": true}`; runs `cmd.exe /c wsl --shutdown` after replying (frees all WSL RAM/GPU for gaming; this also kills the app). `{"shutting_down": true}`; 400 unless `confirm` is exactly `true`. It runs about 1.5 s after the reply and does not stop the stack first: call `/api/stack/down` before it for a clean Steam shutdown.
- `GET /api/logs/<name>?lines=200` where name in steam|game|model|download|vllm|supervisor|app|stack|reflect|coop -> `{"name": str, "lines": [str]}` (lines clamped to 1..2000; a missing file gives `[]`). steam is `/tmp/steam.log`, game `/tmp/sts2.log`, the rest `~/.sts-cua/logs/<name>.log` (`vllm` is the old model server log).

## Model server

The same code as `python -m sts_cua.serve` (docs/hosting.md, "Serving the local models"); models come from `src/sts_cua/models/registry.py` only. One server at a time on 127.0.0.1:8000, pid file `<state-dir>/run/model.pid`, log `logs/model.log`. Load times also go to `logs/model_loads.jsonl`.

- `GET /api/models` ->
  ```
  {"models": [{"name", "family", "server": "llama"|"vllm", "repo", "rank", "file_gib", "need_gib",
               "fits": {"coop": bool, "solo": bool}, "downloaded": bool,
               "download": {"bytes": int, "total": int, "running": bool},
               "license", "thinking": bool, "notes", "running": bool}],      // strongest (highest rank) first
   "best": {"coop": str|null, "solo": str|null},   // what "best" would start now: downloaded, fits min(free VRAM, budget)
   "gpu": {"used_mib", "free_mib", "total_mib"} | null,
   "server": {"running": bool, "pid": int|null, "model": str|null, "mode": "coop"|"solo"|null,
              "served": [str]|null, "port": 8000, "ready": bool},   // served: the ids /v1/models lists
   "download": {"running": bool, "model": str|null}}
  ```
  `fits` compares `need_gib` (files plus an allowance for KV cache and buffers) with the mode's budget (co-op 13 GiB, solo 20 GiB). `download.bytes` counts finished files plus unfinished `.part`/`.incomplete` files, for a progress bar.
- `POST /api/model/start` -> body `{"model": "best" | <registry name>, "mode": "coop"|"solo", "force": false}`. `best` is the highest-ranked downloaded model whose need fits min(free VRAM, mode budget). An explicit model must be downloaded (llama.cpp) and fit both, unless `force`. Starts `llama-server` (or `vllm serve` for the vLLM baseline) with the flags in docs/hosting.md and returns right away. `{"started": true, "pid": int, "model": str, "mode": str, "reason": str}`. 409 if a model server is running (the app's own or one pgrep finds), or with the reason when the model isn't downloaded or doesn't fit; 500 if llama-server/vllm isn't installed.
- `POST /api/model/stop` -> SIGINT, then SIGTERM after 10 s, to the app's server or any process matching `vllm[ ]serve|vllm[.]entrypoints|^[^ ]*llama-server( |$)` (same pattern as `tools/stack_down.sh`). `{"stopped": true, "pids": [int]}` (empty if none was running)
- `POST /api/model/download` -> body `{"model": <registry name>}`; runs `python -m sts_cua.serve --models-dir <dir> download <model>` in the background (log `download`). `{"started": true, "pid": int, "model": str}`; 409 while a download runs. Progress is in `GET /api/models`.
- `POST /api/vllm/start` and `POST /api/vllm/stop`: the old endpoints, kept as aliases of `/api/model/start` (with `mode` defaulting to `"solo"`) and `/api/model/stop`.

## Agent runs

- `POST /api/runs/start` -> body
  ```
  {"model": "claude-haiku-5-5" | "claude-sonnet-5-5" | "qwen3-vl-8b" | "scripted",
   "thinking": "off"|"on", "effort": "low"|"medium"|"high",
   "daily_budget": 2.0, "max_runs": int|null, "memory": bool, "game_build": str|null}
  ```
  Starts `python -m sts_cua.supervisor ...` as a child process (log to `~/.sts-cua/logs/supervisor.log`, cwd the repo). Only `model` is required; defaults: thinking off, effort low, daily_budget 2.0, max_runs null, memory false, game_build null. `daily_budget` is clamped to 0..5.0 and `max_runs` to 1..1000 (an integer); `game_build` must match `^[\w.-]+$`; anything else invalid is a 400. `--thinking`/`--effort` are passed for Claude models only. `memory: true` passes `--memory memory/notes.jsonl`. 409 if a supervisor is already running (the app's own or one pgrep finds). `{"started": true, "pid": int}`
- `POST /api/runs/stop` -> SIGINT then SIGTERM after 10 s (in the background; the reply comes right away). `{"stopped": true, "pids": [int]}`
- `POST /api/coop/start` -> body
  ```
  {"model": "claude-haiku-5-5" | "claude-sonnet-5-5" | "qwen3-vl-8b" | "scripted",
   "thinking": "off"|"on", "effort": "low"|"medium"|"high",
   "max_cost": 1.0, "idle_minutes": 30, "rules": true, "game_build": str|null}
  ```
  Starts one co-op agent session, `python -m sts_cua --coop ...` (never the supervisor), log `~/.sts-cua/logs/coop.log`, cwd the repo. Every field is optional; the defaults are the fastest settings: Haiku 5.5, thinking off, effort low, max_cost 1.0 (clamped to 0..5.0), idle_minutes 30 (clamped to 5..120, `--coop-idle-minutes`), rules true (`false` passes `--no-rules`), game_build as in runs/start. 409 if a co-op session or a supervisor is running (both drive the agent's display); `/api/runs/start` likewise answers 409 while a co-op session runs. `{"started": true, "pid": int, "settings": {...}}`
- `POST /api/coop/stop` -> SIGINT then SIGTERM after 10 s. `{"stopped": true, "pids": [int]}`
- `GET /api/live` -> the newest run dir under runs/:
  ```
  {"run_id": str|null, "kind": "game"|"reset"|null, "model": str|null, "running": bool,
   "step": int, "cost_usd": float,
   "last": {"step": int, "thought": str, "executed": [action dicts], "error": str|null,
            "state": {...summarized state...}|null, "notes_shown": [str]|null, "time": iso} | null,
   "screenshot": "/api/runs/<run_id>/screenshots/step_0012.png" | null}
  ```
  `running`: no summary.json yet and a `python -m sts_cua` or `sts_cua.supervisor` process is alive. `step`/`cost_usd` come from summary.json once the run ended; before that from the last line of steps.jsonl (read from the end) and a running total that reads only the bytes added since the last call. `screenshot` is null until the last step's file exists.
- `GET /api/screen.png` -> a fresh capture of the agent's display (:99) via mss, or 503 if Xvfb is down.

## Results

- `GET /api/ledger?limit=100` -> `{"entries": [ledger.jsonl lines, newest first]}` (limit 1..1000)
- `GET /api/runs?limit=50` -> `{"runs": [{"run_id", "kind", "model", "started_at", "end_reason", "steps", "cost_usd", "outcome", "floor", "score", "seed", "outcome_source"}]}` (newest first, limit 1..500; from meta/summary/score.json, `floor` is score.json's `max_floor`; missing files -> nulls). A co-op run's row also has `"mode": "coop"`; solo rows have no `mode`. `/api/live` adds the same field for a co-op run.
  `seed` and `outcome_source` ("compendium" when the outcome came from the game's run history, "states" when inferred from the logged states) are score.json's; runs scored before they existed have nulls.
- `GET /api/runs/<run_id>` -> `{"meta": {...}, "summary": {...}|null, "score": {...}|null}`
- `GET /api/runs/<run_id>/steps?offset=0&limit=50` -> `{"total": int, "steps": [steps.jsonl records without "raw"]}` (limit 1..500)
- `GET /api/runs/<run_id>/screenshots/<file>.png` -> image/png (path-traversal safe: run_id and file must match `^[\w.-]+$`, and the resolved path must stay inside runs/, so symlinks out are refused)

## Memory

- `GET /api/memory/notes?status=all|probation|active|retired` -> `{"path": str, "notes": [note dicts from memory/notes.jsonl]}`
- `POST /api/memory/notes/<id>/retire` -> `{"retired": true}`; 404 for an unknown id, 409 while a reflect is running. (A supervisor with `--memory` also edits the store after each run; a retire landing in those few seconds can be lost.)
- `POST /api/memory/reflect` -> body `{"run_id": str}`; runs `python -m sts_cua.memory reflect runs/<run_id>` in the background (log `reflect`). `{"started": true}`; 404 for an unknown run, 409 while one is running.

## Evaluations

Read-only views of memory A/B experiments (`python -m sts_cua.eval`, files in `runs/evals/<name>/`).

- `GET /api/evals` -> `{"evals": [{"name", "created_at", "mode": "paired"|"unpaired", "arms": [str, str], "planned_runs", "valid_runs", "complete": bool, "stop_reason": str|null, "cost_usd", "p_value": float|null}]}`, newest first. A directory without a readable `plan.json` is skipped.
- `GET /api/evals/<name>` -> `{"plan": {...plan.json}, "report": {...}, "results": [results.jsonl lines]}`. The report is rebuilt from the results on each call (same shape as `report.json`: `per_arm` mean/median score, win rate, mean floor, cost; `test` with `test`, `p_value`, `method`, `effect_size`, `mean_difference`; `win_rate_test`; `notes`; `warnings`). `name` must match `^[\w.-]+$` and stay inside `runs/evals/`; 404 when there is no plan, 500 when it can't be read.

An evaluation run (`python -m sts_cua.eval run`) counts as the supervisor in `/api/status`, `/api/live` and `/api/runs/stop`, and `tools/stack_down.sh` stops it.
