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
`bench` and `install` (top level): a bake-off job and the llama.cpp install, `{"running", "pid", "since"}` like a process (the app's own, or one pgrep finds: `python -m sts_cua.bench ...`, `bash .../install_llama_cpp.sh`).
`coop` (top level): the co-op session, `{"running", "pid", "since"}` like a process (the app's own or one pgrep finds with `python -m sts_cua ... --coop`).
`busy`: "wsl_shutdown", "stack_down", "stack_up" (until stack_up.sh logs "stack is up"), "supervisor_stop" / "model_stop" / "coop_stop" (between SIGINT and exit), "model_start" (until the model server answers `/health` (llama.cpp) or `/v1/models` (vLLM)), "bench" / "install" (a bake-off job or llama.cpp install the app started; the page keeps Stack up and Stack down usable during them), "reflect", or null. `budget.cap_usd` is the `daily_budget` of the last `/api/runs/start`, else 2.0; `spent_usd` is `supervisor.spent_today` (run summaries plus reflect calls). A process counts as running when the app started it (pid file) or pgrep finds it.

## Stack control (fixed actions only, never arbitrary commands)

- `POST /api/stack/up` -> runs `tools/stack_up.sh` in the background (log `stack`). `{"started": true}`; 409 while one the app started is still running (it stays running, waiting, once the stack is up) or the stack is being stopped.
- `POST /api/stack/down` -> kills a waiting `stack_up.sh` the app started, sends SIGINT to a bench job's process group and SIGTERM to a llama.cpp install's, then runs `tools/stack_down.sh` (stops runs and bench jobs, a llama.cpp install, the model server, game, Steam, Xvfb; not the app). `{"started": true}`; 409 while one is running. Bench and install starts answer 409 while it runs.
- `POST /api/wsl/shutdown` -> body `{"confirm": true}`; runs `cmd.exe /c wsl --shutdown` after replying (frees all WSL RAM/GPU for gaming; this also kills the app). `{"shutting_down": true}`; 400 unless `confirm` is exactly `true`. It runs about 1.5 s after the reply and does not stop the stack first: call `/api/stack/down` before it for a clean Steam shutdown.
- `GET /api/logs/<name>?lines=200` where name in steam|game|model|download|vllm|supervisor|app|stack|reflect|coop|bench|install -> `{"name": str, "lines": [str]}` (lines clamped to 1..2000; a missing file gives `[]`). steam is `/tmp/steam.log`, game `/tmp/sts2.log`, the rest `~/.sts-cua/logs/<name>.log` (`vllm` is the old model server log).

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
  Also 409 while llama.cpp is being installed or a bench job that starts model servers itself is running (`--serve`, or a bench job started outside the app).
- `POST /api/vllm/start` and `POST /api/vllm/stop`: the old endpoints, kept as aliases of `/api/model/start` (with `mode` defaulting to `"solo"`) and `/api/model/stop`.

### llama.cpp

- `GET /api/llama` ->
  ```
  {"found": bool, "path": str|null,            // serve.find_llama_server: --llama-server, $LLAMA_SERVER, ~/llama.cpp/build/bin, PATH
   "version": str|null,                         // `llama-server --version`'s "version: ..." line, run once per binary (path, mtime)
   "release": str|null,                         // the RELEASE file a --prebuilt install leaves next to the binary
   "install": {"running": bool, "pid", "since", "prebuilt": bool|null, "started_at": iso|null,
               "exit_code": int|null,           // of the last install this app saw end; negative: killed by that signal
               "tail": [str],                   // its last 12 lines (from logs/install.log)
               "nvcc_missing": bool}}           // the tail says "nvcc not found": suggest prebuilt
  ```
- `POST /api/llama/install` -> body `{"prebuilt": false, "tag": null}`; runs `bash tools/install_llama_cpp.sh --build`, or `--prebuilt [tag]` (`tag` only with prebuilt, `^b\d{1,6}$`, e.g. `b11900`), in the background, child `install`, log `logs/install.log`, cwd the repo. `{"started": true, "pid": int, "argv": [...]}`; 409 while an install runs, while a model server runs (its binary can't be replaced), while a bench job that starts servers runs, or while the stack is being stopped.

## Bake-off

The Bake-off tab drives `python -m sts_cua.bench` (README "Bake-off"). Each action is one background child named `bench` (pid file `run/bench.pid`, log `logs/bench.log`, cwd the repo, env `PYTHONUNBUFFERED=1`, `STS_CUA_MODELS` set to the app's models dir and `LLAMA_SERVER` to the llama-server it finds, for `--serve`). One at a time: every start answers 409 while a bench job runs (the app's, or one pgrep finds) or the stack is being stopped. Argv comes from the fields below only; bench models are the registry's plus the references `claude-haiku-5-5` and `claude-sonnet-5-5` (API credits).

- `GET /api/bench?lines=200` ->
  ```
  {"job": {"running", "pid", "since", "action": str|null, "external": bool},   // external: started outside the app
   "last": {"action", "argv", "pid", "started_at", "models", "mode", "serve", "running": bool, "exit_code": int|null,
            "output": [str],                     // the job's lines since it started (lines 1..2000)
            "changed": {"rows": [str], "diff": [str]}|null} | null,   // write_docs only, once it reported
   "targets": {"count", "rejected", "items": [{"id", "instruction", "state_type", "check"}],   // first 2000
               "sheets": [{"name": "sheet-01.png", "url": "/api/bench/review/sheet-01.png?v=<mtime>"}],
               "sheets_stale": bool, "error": str|null},   // stale: targets.jsonl is newer than the sheets
   "models": {"local": [{"name", "server", "thinking", "rank", "need_gib", "downloaded", "fits": {"coop", "solo", "max"}}],
              "reference": ["claude-haiku-5-5", "claude-sonnet-5-5"]},
   "modes": ["coop", "solo", "max"],
   "baselines": {"desktop"|"pre-server": {"time", "mode", "gpu_used_mib", "ram_used_mib"}},
   "server": {"running", "model", "mode"}, "clicks_display": ":98"}
  ```
  `changed.rows` are the measure rows `report --write-docs` says it filled; `changed.diff` is the changed lines of `docs/hosting.md` (`-old`, `+new`) against a copy taken when the job started.
- `GET /api/bench/review/<file>` -> image/png, a contact sheet from `bench/review/`: `file` must match `^sheet-\d{1,4}\.png$` and resolve inside that directory (symlinks out are refused); else 400/404.
- `GET /api/bench/results` -> read from `runs/bench/*/results.json` (files that aren't a JSON object, and entries without a model or summary, are skipped), newest result per model (per model and mode for resources), as `report` picks them:
  ```
  {"grounding": [{"model", "local", "created_at", "game_build", "targets", "hit_rate", "hits", "answered", "errors",
                  "malformed", "median_error_px", "latency_p50_s", "latency_p95_s", "tokens_per_s", "dir"}],
   "clicks": [{"model", "local", "created_at", "hits", "answered", "malformed", "errors", "latency_p50_s", "latency_p95_s",
               "median_error_px"}],
   "resources": [{"model", "mode", "created_at", "vram_used_gib", "vram_total_gib", "model_vram_gib", "budget_gib",
                  "fits": bool|null, "server_rss_gib", "ram_used_gib", "game_rss_gib"}],
   "ranking": [{"model", "hit_rate", "latency_p50_s", "local", "fits": {"coop"|"solo": {"fits", "need_gib", "how": "measured"|"est."|"api"}}}],
   "recommended": {"coop": str|null, "solo": str|null},
   "markdown": str}                              // report.block: what --write-docs puts between the bench markers
  ```
- `POST /api/bench/<action>` -> `{"started": true, "pid": int, "action": str, "argv": [...]}`. Actions and bodies:
  - `extract` `{"max_per_run": int|null}` (1..1000) -> `bench extract [--max-per-run N]`
  - `review` `{}` -> `bench review` (remakes the contact sheets)
  - `delete` `{"ids": [str]}` (1..500 ids of current targets, each `^\w[\w.-]{0,127}$`; unknown ids are a 400) -> `bench review --delete ID ...`
  - `grounding` `{"models": [str], "mode": "coop"|"solo"|"max", "serve": false, "limit": int|null}` (1..8 models, limit 1..100000) -> `bench grounding M ... --mode MODE [--serve] [--limit N]`
  - `baseline` `{"label": "pre-server"|"desktop", "mode": str|null}` -> `bench baseline --label L [--mode M]`
  - `resources` `{"model": <registry name>, "mode": str, "game": false, "serve": false}` -> `bench resources M --mode MODE [--game] [--serve]`, plus `--server-log <state-dir>/logs/model.log` for the vLLM model
  - `clicks` `{"model": str, "n": 20}` (n 1..100) -> `bench clicks M --xvfb --display :98 --n N`
  - `write-docs` `{}` -> `bench report --write-docs --docs docs/hosting.md`

  409s besides one-job-at-a-time, so a job doesn't measure or test the wrong thing: a local model without `serve` needs the model server running and serving that model (one local model at a time); `serve` (grounding with a local model, or resources) waits while the app's model start is still loading, llama.cpp is being installed, or a supervisor or co-op session on a local model runs (it would lose its server); resources with `serve` needs no model server running; both baselines need the model server stopped, and `desktop` needs Spire 2 not running in WSL.
- `POST /api/bench/stop` -> SIGINT, then SIGTERM after 10 s, to the job's process group (and a pgrep match). `{"stopped": true, "pids": [int]}`. A model server a `--serve` job started keeps running; stop it on the Model server card.

`--serve` runs `python -m sts_cua.serve start|stop`, which keeps its pid file in `~/.sts-cua/run/` (serve's default), the same as the app's default `--state-dir`.

## Agent runs

- `POST /api/runs/start` -> body
  ```
  {"model": "claude-haiku-5-5" | "claude-sonnet-5-5" | <registry model> | "scripted",
   "thinking": "off"|"on", "effort": "low"|"medium"|"high",
   "local_thinking": "on"|"off"|null, "local_effort": "low"|"medium"|"xhigh"|null,
   "daily_budget": 2.0, "max_runs": int|null, "memory": bool, "game_build": str|null}
  ```
  Optional for a local (registry) model: `"local_thinking": "on"|"off"|null` and `"local_effort": "low"|"medium"|"xhigh"|null`, passed as `--local-thinking` / `--local-effort`; null keeps the default (the registry's thinking setting; co-op: off). 400 for these on a Claude model or scripted, for thinking on or an effort on a model without a thinking mode (qwen3-vl-8b), and for an effort while thinking is off.
  Starts `python -m sts_cua.supervisor ...` as a child process (log to `~/.sts-cua/logs/supervisor.log`, cwd the repo). Only `model` is required; defaults: thinking off, effort low, daily_budget 2.0, max_runs null, memory false, game_build null. `daily_budget` is clamped to 0..5.0 and `max_runs` to 1..1000 (an integer); `game_build` must match `^[\w.-]+$`; anything else invalid is a 400. `--thinking`/`--effort` are passed for Claude models only. `memory: true` passes `--memory memory/notes.jsonl`. 409 if a supervisor is already running (the app's own or one pgrep finds). `{"started": true, "pid": int}`
- `POST /api/runs/stop` -> SIGINT then SIGTERM after 10 s (in the background; the reply comes right away). `{"stopped": true, "pids": [int]}`
- `POST /api/coop/start` -> body
  ```
  {"model": "claude-haiku-5-5" | "claude-sonnet-5-5" | <registry model> | "scripted",
   "thinking": "off"|"on", "effort": "low"|"medium"|"high",
   "local_thinking": "on"|"off"|null, "local_effort": "low"|"medium"|"xhigh"|null,
   "max_cost": 1.0, "idle_minutes": 30, "rules": true, "game_build": str|null}
  ```
  `local_thinking` / `local_effort` as in runs/start (co-op's default is thinking off, so an effort needs `"local_thinking": "on"`).
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
