import json
import runpy
import sys
from pathlib import Path

import pytest


def test_apply_settings_updates_and_backs_up(tmp_path, monkeypatch):
    acct = tmp_path / "7656000000000001"
    acct.mkdir()
    original = {"fullscreen": True, "window_size": {"X": 1920, "Y": 1080}, "language": "eng", "mod_settings": None}
    (acct / "settings.save").write_text(json.dumps(original))

    script = Path(__file__).parent.parent / "tools/apply_game_settings.py"
    ns = runpy.run_path(str(script))
    monkeypatch.setitem(ns["main"].__globals__, "SAVE_ROOT", tmp_path)
    monkeypatch.setattr(ns["subprocess"], "run", lambda *a, **k: type("R", (), {"returncode": 1})())
    monkeypatch.setattr(sys, "argv", ["apply_game_settings.py", "--enable-mods"])
    ns["main"]()

    data = json.loads((acct / "settings.save").read_text())
    assert data["fullscreen"] is False and data["window_size"] == {"X": 1280, "Y": 720}
    assert data["language"] == "eng"  # untouched keys survive
    assert data["mod_settings"] == {"mods_enabled": True}
    assert list(acct.glob("settings.save.before-agent-*"))


def test_refuses_while_game_runs(tmp_path, monkeypatch):
    script = Path(__file__).parent.parent / "tools/apply_game_settings.py"
    ns = runpy.run_path(str(script))
    monkeypatch.setattr(ns["subprocess"], "run", lambda *a, **k: type("R", (), {"returncode": 0})())
    monkeypatch.setattr(sys, "argv", ["apply_game_settings.py"])
    with pytest.raises(SystemExit, match="running"):
        ns["main"]()
