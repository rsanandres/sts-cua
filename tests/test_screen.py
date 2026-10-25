import os
import shutil
import subprocess
import time

import pytest

from sts_cua.actions import Click, Drag, Key, MouseMove, Scroll, TypeText, Wait
from sts_cua.screen import xdotool_args


def test_click_args():
    assert xdotool_args(Click(10, 20)) == ["mousemove", "--sync", "10", "20", "sleep", "0.25", "click", "--repeat", "1", "1"]
    assert xdotool_args(Click(1, 2, button="right", count=2)) == [
        "mousemove", "--sync", "1", "2", "sleep", "0.25", "click", "--repeat", "2", "3",
    ]


def test_click_with_modifiers_releases_in_reverse():
    args = xdotool_args(Click(5, 5, modifiers="ctrl+shift"))
    assert args == [
        "mousemove", "--sync", "5", "5", "sleep", "0.25", "keydown", "ctrl", "keydown", "shift",
        "click", "--repeat", "1", "1", "keyup", "shift", "keyup", "ctrl",
    ]


def test_other_args():
    assert xdotool_args(Drag(1, 2, 3, 4)) == [
        "mousemove", "--sync", "1", "2", "sleep", "0.25", "mousedown", "1",
        "mousemove", "--sync", "3", "4", "sleep", "0.25", "mouseup", "1",
    ]
    assert xdotool_args(MouseMove(7, 8)) == ["mousemove", "--sync", "7", "8"]
    assert xdotool_args(Scroll("down", 3)) == ["click", "--repeat", "3", "5"]
    assert xdotool_args(Scroll("up", 1, 9, 9))[:4] == ["mousemove", "--sync", "9", "9"]
    assert xdotool_args(Key("ctrl+s", 2)) == ["key", "--repeat", "2", "ctrl+s"]
    assert xdotool_args(TypeText("-hi")) == ["type", "--delay", "12", "--", "-hi"]
    assert xdotool_args(Wait(1)) is None


@pytest.fixture
def xvfb():
    if not (shutil.which("Xvfb") and shutil.which("xdotool")):
        pytest.skip("needs Xvfb and xdotool")
    display = ":97"
    proc = subprocess.Popen(
        ["Xvfb", display, "-screen", "0", "1280x720x24", "-nolisten", "tcp"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    for _ in range(50):
        if os.path.exists(f"/tmp/.X11-unix/X{display[1:]}"):
            break
        time.sleep(0.1)
    yield display
    proc.terminate()
    proc.wait(timeout=5)


def test_x11_capture_and_click(xvfb):
    from sts_cua.screen import X11Screen

    screen = X11Screen(xvfb)
    assert screen.size == (1280, 720)
    assert screen.capture().size == (1280, 720)
    screen.perform(Click(321, 123))
    assert screen.pointer() == (321, 123)
    screen.perform(Drag(10, 10, 640, 360))
    assert screen.pointer() == (640, 360)


def test_click_target_registers_hits(xvfb, tmp_path):
    import json
    import sys

    pytest.importorskip("pygame")
    from sts_cua.screen import X11Screen

    out = tmp_path / "target.json"
    app = subprocess.Popen(
        [sys.executable, "tools/click_target.py", "--out", str(out)],
        env={**os.environ, "DISPLAY": xvfb},
    )
    try:
        for _ in range(50):
            if out.exists():
                break
            time.sleep(0.1)
        time.sleep(0.5)  # first frame drawn
        screen = X11Screen(xvfb)
        for _ in range(2):
            x, y, w, h = json.loads(out.read_text())["button"]
            screen.perform(Click(x + w // 2, y + h // 2))
            time.sleep(0.3)
        screen.perform(Click(5, 700))
        time.sleep(0.3)
        score = json.loads(out.read_text())
        assert (score["hits"], score["misses"]) == (2, 1)
    finally:
        app.terminate()
        app.wait(timeout=5)
