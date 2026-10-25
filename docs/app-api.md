# sts-cua control app: API contract (v1)

Server: `python -m sts_cua.app [--host 127.0.0.1] [--port 8765] [--repo ~/sts-cua] [--token-file ~/.sts-cua/app-token] [--state-dir ~/.sts-cua] [--display :99] [--vllm PATH]`. Pid files of the children it starts go in `<state-dir>/run/`, logs in `<state-dir>/logs/`.
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
    "steam": {...}, "xvfb": {...}, "game": {...}, "vllm": {...}, "supervisor": {...}
  },
  "gpu": {"used_mib": int, "total_mib": int} | null,       // /usr/lib/wsl/lib/nvidia-smi or nvidia-smi
  "ram": {"used_mib": int, "total_mib": int},               // inside WSL (/proc/meminfo)
  "budget": {"date": "2026-10-25", "spent_usd": 0.42, "cap_usd": 2.0},  // America/Los_Angeles day, from runs/*/summary.json
  "mod": {"reachable": bool, "state_type": str|null, "act": int|null, "floor": int|null, "hp": int|null, "max_hp": int|null},
  "busy": str|null             // an action in progress, see below
}
```
`coop` (top level): the co-op session, `{"running", "pid", "since"}` like a process (the app's own or one pgrep finds with `python -m sts_cua ... --coop`).
`busy`: "wsl_shutdown", "stack_down", "stack_up" (until stack_up.sh logs "stack is up"), "supervisor_stop" / "vllm_stop" / "coop_stop" (between SIGINT and exit), "vllm_start" (until vLLM logs "Application startup complete"), "reflect", or null. `budget.cap_usd` is the `daily_budget` of the last `/api/runs/start`, else 2.0; `spent_usd` is `supervisor.spent_today` (run summaries plus reflect calls). A process counts as running when the app started it (pid file) or pgrep finds it.

## Stack control (fixed actions only, never arbitrary commands)

- `POST /api/stack/up` -> runs `tools/stack_up.sh` in the background (log `stack`). `{"started": true}`; 409 while one the app started is still running (it stays running, waiting, once the stack is up) or the stack is being stopped.
- `POST /api/stack/down` -> kills a waiting `stack_up.sh` the app started, then runs `tools/stack_down.sh` (stops runs, vLLM, game, Steam, Xvfb; not the app). `{"started": true}`; 409 while one is running.
- `POST /api/wsl/shutdown` -> body `{"confirm": true}`; runs `cmd.exe /c wsl --shutdown` after replying (frees all WSL RAM/GPU for gaming; this also kills the app). `{"shutting_down": true}`; 400 unless `confirm` is exactly `true`. It runs about 1.5 s after the reply and does not stop the stack first: call `/api/stack/down` before it for a clean Steam shutdown.
- `POST /api/vllm/start` -> body `{"model": "qwen3-vl-8b"}`; starts vLLM with the docs/hosting.md command (`--gpu-memory-utilization 0.55 --max-model-len 8192 --max-num-seqs 1 --limit-mm-per-prompt '{"image":1,"video":0}'`), log to `~/.sts-cua/logs/vllm.log`. Model from `factory.LOCAL_MODELS` only. `{"started": true, "pid": int}`; 409 if a model server is running.
- `POST /api/vllm/stop` -> SIGINT, then SIGTERM after 10 s, to the app's vLLM or any matching `vllm serve|vllm.entrypoints|llama-server`. `{"stopped": true, "pids": [int]}` (empty if none was running)
- `GET /api/logs/<name>?lines=200` where name in steam|game|vllm|supervisor|app|stack|reflect|coop -> `{"name": str, "lines": [str]}` (lines clamped to 1..2000; a missing file gives `[]`). steam is `/tmp/steam.log`, game `/tmp/sts2.log`, the rest `~/.sts-cua/logs/<name>.log`.

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
