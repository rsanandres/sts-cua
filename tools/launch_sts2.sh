#!/bin/bash
# Start Xvfb :99 if needed, launch Slay the Spire 2 on it with the GPU, wait for the
# main menu, and apply the one-pixel resize fix. Steam must already be running.
# usage: tools/launch_sts2.sh [logfile]
set -euo pipefail
cd "$(dirname "$0")/.."
exec .venv/bin/python -c "
import sys
from pathlib import Path
from sts_cua import game
cfg = game.start(game.GameConfig(log_path=Path(sys.argv[1])))
print('menu ready on', cfg.display)
" "${1:-/tmp/sts2.log}"
