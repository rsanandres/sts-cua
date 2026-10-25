"""Write the agent's low settings into Spire 2's machine settings file for one Steam account.

settings.save is per Steam account and not synced by Steam Cloud, so it is safe to edit,
but only while the game is closed (the game rewrites it at launch and exit).

    .venv/bin/python tools/apply_game_settings.py            # newest account folder
    .venv/bin/python tools/apply_game_settings.py --steam-id 7656...

Profile preferences (fast mode, screen shake, card numbers, upload) live in Steam Cloud
synced files; set those in-game (Settings -> General), not here.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

SAVE_ROOT = Path.home() / ".local/share/SlayTheSpire2/steam"

AGENT_SETTINGS = {
    "fullscreen": False,
    "window_size": {"X": 1280, "Y": 720},
    "window_position": {"X": 0, "Y": 0},
    "resize_windows": False,
    "vsync": "off",
    "fps_limit": 30,
    "msaa": 0,
    "volume_master": 0,
    "volume_bgm": 0,
    "volume_sfx": 0,
    "volume_ambience": 0,
    "skip_intro_logo": True,
    "seen_ea_disclaimer": True,
}


def find_settings(steam_id: str | None) -> Path:
    if steam_id:
        path = SAVE_ROOT / steam_id / "settings.save"
        if not path.exists():
            sys.exit(f"{path} not found; launch the game once on this account first")
        return path
    candidates = sorted(SAVE_ROOT.glob("*/settings.save"), key=lambda p: p.stat().st_mtime)
    if not candidates:
        sys.exit(f"no settings.save under {SAVE_ROOT}; launch the game once first")
    return candidates[-1]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--steam-id", help="64-bit Steam ID folder name; default: most recently used account")
    parser.add_argument("--enable-mods", action="store_true", help="also set mod_settings.mods_enabled = true")
    args = parser.parse_args()

    if subprocess.run(["pgrep", "-x", "SlayTheSpire2"], capture_output=True).returncode == 0:
        sys.exit("Spire 2 is running; quit it first (it rewrites settings.save on exit)")

    path = find_settings(args.steam_id)
    data = json.loads(path.read_text())
    backup = path.with_name(f"settings.save.before-agent-{time.strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(path, backup)

    data.update(AGENT_SETTINGS)
    if args.enable_mods:
        mods = data.get("mod_settings") or {}
        mods["mods_enabled"] = True
        data["mod_settings"] = mods
    path.write_text(json.dumps(data, indent=2))
    print(f"updated {path}\nbackup  {backup}")
    for key in AGENT_SETTINGS:
        print(f"  {key} = {json.dumps(data[key])}")
    if args.enable_mods:
        print(f"  mod_settings = {json.dumps(data['mod_settings'])}")


if __name__ == "__main__":
    main()
