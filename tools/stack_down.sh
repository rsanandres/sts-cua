#!/bin/bash
# Take the agent's stack down inside WSL: agent runs, local model server, Spire 2,
# Steam and the virtual screen. tools/windows/sts-cua-off.cmd runs this and then
# `wsl --shutdown`, which hands all of WSL's RAM and GPU memory back to Windows.
set -uo pipefail
log() { echo "[$(date +%H:%M:%S)] $*"; }

stop() {  # stop <description> <pkill pattern args...>
  local what=$1; shift
  if pgrep "$@" >/dev/null; then
    log "stopping $what"
    pkill -INT "$@"
    for _ in 1 2 3 4 5; do pgrep "$@" >/dev/null || return 0; sleep 1; done
    pkill -KILL "$@"
  fi
}

# Agent runs (python -m sts_cua, sts_cua.supervisor, sts_cua.memory, sts_cua.eval, sts_cua.bench) but
# not the control app (python -m sts_cua.app). Same pattern as sts_cua.app.procs.AGENT_PATTERN.
stop "agent runs" -f 'python[0-9.]* -m sts_cua(\.supervisor|\.memory|\.eval|\.bench)?( |$)'
# tools/install_llama_cpp.sh (the app's Install llama.cpp button, or by hand). When the script leads its
# own process group (the app starts it in a new session; a terminal job too), the whole group goes, so a
# running cmake or curl stops with it. Same pattern as sts_cua.app.procs.INSTALL_PATTERN.
for pid in $(pgrep -f '^([^ ]*/)?(ba)?sh [^ ]*install_llama_cpp[.]sh( |$)'); do
  log "stopping the llama.cpp install (pid $pid)"
  if [ "$(ps -o pgid= -p "$pid" | tr -d ' ')" = "$pid" ]; then kill -TERM -- "-$pid" 2>/dev/null; else kill -TERM "$pid" 2>/dev/null; fi
done
# vLLM or llama-server (python -m sts_cua.serve, the app's Model server card, or by hand).
# Same pattern as sts_cua.app.procs.MODEL_PATTERN.
stop "local model server" -f 'vllm[ ]serve|vllm[.]entrypoints|^[^ ]*llama-server( |$)'
# Spire 2 saves at every room; a kill loses at most the current room's progress.
stop "Spire 2" -x SlayTheSpire2
if pgrep -x steam >/dev/null; then
  log "shutting down Steam"
  steam -shutdown >/dev/null 2>&1 || true
  for _ in $(seq 1 20); do pgrep -x steam >/dev/null || break; sleep 1; done
  pkill -KILL -x steam 2>/dev/null || true
fi
stop "virtual screen" -f "Xvfb :99"
log "stack is down"
