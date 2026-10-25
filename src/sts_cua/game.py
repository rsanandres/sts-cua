"""Start, ready and quit Slay the Spire 2 on a virtual X display.

Facts found on Raph's machine (WSL2, Spire 2 build 23811903, v0.107.1):
- Launch the game directly, outside Steam's runtime container. Inside it the game
  can't reach WSL's GPU libraries and fails to create an OpenGL context. Steam must
  still be running so Steamworks can check ownership.
- GALLIUM_DRIVER=d3d12 + --rendering-driver opengl3 renders on the GPU.
- The first frame lays out wrong until the window is resized by one pixel and back.
- SIGTERM is ignored; quit through the in-game menu. The process can still hang after
  Quit -> Yes once saves are written, so quit_game force-kills it after a timeout.
- The first click after the window gains focus only focuses it; focus() clicks a
  harmless spot first.
"""

from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from .actions import Click
from .screen import X11Screen

DEFAULT_GAME_DIR = Path.home() / ".steam/debian-installation/steamapps/common/Slay the Spire 2"
APP_ID = "2868840"
MENU_READY_LINE = "Time to main menu:"

# Main menu of a fresh profile at 1280x720 (Singleplayer, Multiplayer, Settings, Quit).
QUIT_BUTTON = (517, 605)
QUIT_CONFIRM = (750, 482)


@dataclass
class GameConfig:
    display: str = ":99"
    size: tuple[int, int] = (1280, 720)
    game_dir: Path = DEFAULT_GAME_DIR
    log_path: Path = Path("/tmp/sts2.log")
    extra_env: dict[str, str] = field(default_factory=dict)


def launch_command(config: GameConfig) -> tuple[list[str], dict[str, str]]:
    env = {
        **os.environ,
        "SteamAppId": APP_ID,
        "SteamGameId": APP_ID,
        "GALLIUM_DRIVER": "d3d12",
        "DISPLAY": config.display,
        **config.extra_env,
    }
    return ["./SlayTheSpire2", "--rendering-driver", "opengl3"], env


def is_running() -> bool:
    return subprocess.run(["pgrep", "-x", "SlayTheSpire2"], capture_output=True).returncode == 0


def ensure_display(config: GameConfig) -> None:
    """Start Xvfb on the display unless an X server already owns it."""
    probe = subprocess.run(
        ["xdotool", "getdisplaygeometry"], env={**os.environ, "DISPLAY": config.display}, capture_output=True
    )
    if probe.returncode == 0:
        return
    w, h = config.size
    subprocess.Popen(
        ["Xvfb", config.display, "-screen", "0", f"{w}x{h}x24", "-nolisten", "tcp"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    time.sleep(1)


def launch(config: GameConfig) -> None:
    if is_running():
        raise RuntimeError("SlayTheSpire2 is already running; refusing to start a second copy")
    cmd, env = launch_command(config)
    log = open(config.log_path, "w")
    subprocess.Popen(
        cmd, cwd=config.game_dir, env=env,
        stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
    )


def wait_for_menu(log_path: Path, timeout: float = 90, poll: float = 1.0) -> float:
    """Block until the game logs that the main menu loaded. Returns seconds waited."""
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        if log_path.exists() and MENU_READY_LINE in log_path.read_text(errors="replace"):
            return time.monotonic() - start
        time.sleep(poll)
    raise TimeoutError(f"no {MENU_READY_LINE!r} in {log_path} after {timeout}s")


def fix_layout(config: GameConfig) -> None:
    """Resize by one pixel and back; the game lays out correctly only after a resize."""
    env = {**os.environ, "DISPLAY": config.display}
    window = subprocess.run(
        ["xdotool", "search", "--name", "Slay the Spire 2"],
        env=env, capture_output=True, text=True, check=True,
    ).stdout.split()[0]
    w, h = config.size
    subprocess.run(["xdotool", "windowsize", window, str(w - 1), str(h - 1)], env=env, check=True)
    time.sleep(1)
    subprocess.run(["xdotool", "windowsize", window, str(w), str(h)], env=env, check=True)
    time.sleep(2)


def start(config: GameConfig | None = None, settle: float = 6.0) -> GameConfig:
    """Display up, game launched, main menu loaded and laid out."""
    config = config or GameConfig()
    ensure_display(config)
    launch(config)
    wait_for_menu(config.log_path)
    time.sleep(settle)
    fix_layout(config)
    time.sleep(4)  # menu fade-in
    focus(config)
    return config


def focus(config: GameConfig) -> None:
    """Click the empty top-left corner so the next click reaches the game, not the window manager."""
    X11Screen(config.display).perform(Click(5, 360))
    time.sleep(0.3)


def quit_game(config: GameConfig, timeout: float = 15) -> None:
    """Quit from the main menu: Quit, then Yes. Force-kill if the process hangs on exit."""
    screen = X11Screen(config.display)
    screen.perform(Click(*QUIT_BUTTON))
    time.sleep(1)
    screen.perform(Click(*QUIT_CONFIRM))
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        if not is_running():
            return
        time.sleep(0.5)
    subprocess.run(["pkill", "-KILL", "-x", "SlayTheSpire2"], check=False)
