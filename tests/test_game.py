import threading
import time

import pytest

from sts_cua import game


def test_launch_command_uses_gpu_and_opengl(tmp_path):
    cmd, env = game.launch_command(game.GameConfig(display=":42"))
    assert cmd == ["./SlayTheSpire2", "--rendering-driver", "opengl3"]
    assert env["GALLIUM_DRIVER"] == "d3d12"
    assert env["DISPLAY"] == ":42"
    assert env["SteamAppId"] == env["SteamGameId"] == "2868840"


def test_wait_for_menu_sees_ready_line(tmp_path):
    log = tmp_path / "sts2.log"
    log.write_text("booting\n")

    def finish():
        time.sleep(0.3)
        log.write_text("booting\n[INFO] Time to main menu: 13,772ms\n")

    threading.Thread(target=finish).start()
    assert game.wait_for_menu(log, timeout=5, poll=0.05) < 5


def test_wait_for_menu_times_out(tmp_path):
    with pytest.raises(TimeoutError):
        game.wait_for_menu(tmp_path / "missing.log", timeout=0.2, poll=0.05)
