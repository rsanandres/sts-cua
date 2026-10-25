#!/bin/bash
# Bring the agent's stack up inside WSL: virtual screen :99, Steam, and Spire 2 on the
# main menu. Run by tools/windows/sts-cua-on.cmd, or directly from a WSL terminal.
# Keeps running in the foreground; Ctrl+C (or sts-cua-off.cmd) takes the stack down.
set -uo pipefail
cd "$(dirname "$0")/.."
export DISPLAY=:0  # Steam's own window goes to the Windows desktop (WSLg)

log() { echo "[$(date +%H:%M:%S)] $*"; }

CONN_LOG=~/.steam/debian-installation/logs/connection_log.txt
# The log keeps earlier sessions' "Logged On" lines, so when we start Steam ourselves,
# only count lines written after this point (from the top again if Steam rotates the log).
SINCE=0
if ! pgrep -x steam >/dev/null; then
  SINCE=$(stat -c %s "$CONN_LOG" 2>/dev/null || echo 0)
  log "starting Steam (log in here the first time; tick 'remember me')"
  nohup setsid steam -cef-disable-gpu >/tmp/steam.log 2>&1 </dev/null &
fi

log "waiting for Steam to log in"
logged_on() {
  pgrep -x steam >/dev/null || return 1
  local size; size=$(stat -c %s "$CONN_LOG" 2>/dev/null || echo 0)
  (( size < SINCE )) && SINCE=0
  tail -c +$((SINCE + 1)) "$CONN_LOG" 2>/dev/null | tail -n 40 | grep -q "Logged On"
}
for _ in $(seq 1 180); do
  logged_on && break
  sleep 2
done
logged_on || log "warning: Steam login not confirmed yet; the game may fail its Steam check"

if pgrep -x SlayTheSpire2 >/dev/null; then
  log "Spire 2 already running"
else
  log "starting virtual screen :99 and Spire 2"
  .venv/bin/python -c "
from pathlib import Path
from sts_cua import game
game.start(game.GameConfig(log_path=Path('/tmp/sts2.log')))
" || { log "game failed to start; see /tmp/sts2.log"; exit 1; }
fi

log "stack is up: Steam + Spire 2 on :99. Start runs with tools/run_agent.sh (or python -m sts_cua)."
log "leave this window open; close it or run sts-cua-off to stop everything."
trap 'log "stopping"; tools/stack_down.sh; exit 0' INT TERM HUP
while true; do sleep 60; done
