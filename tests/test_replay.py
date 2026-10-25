import json
import shutil

import pytest
from PIL import Image

from sts_cua.actions import Click, Drag, Key, Scroll, Wait, action_from_dict, action_to_dict
from sts_cua.record import ScreenRecorder
from sts_cua.replay import BAR_HEIGHT, highlights, highlights_markdown, load_run, main, render_frame, step_diffs


def test_actions_round_trip():
    for a in (Click(5, 6, count=2), Drag(1, 2, 3, 4), Key("e", repeat=2), Scroll("down", 3, 10, 20), Wait(0.5)):
        assert action_from_dict(action_to_dict(a)) == a


def map_state(floor, hp=80):
    return {"state_type": "map", "floor": floor, "player": {"hp": hp, "max_hp": 80, "gold": 99}}


def fight_state(floor, hp, enemies=("Nibbit",)):
    return {"state_type": "monster", "floor": floor, "player": {"hp": hp, "max_hp": 80, "gold": 99},
            "enemies": [{"name": n} for n in enemies]}


def write_run(tmp_path, records, summary=None):
    run_dir = tmp_path / "run"
    (run_dir / "screenshots").mkdir(parents=True)
    (run_dir / "meta.json").write_text(json.dumps({"model": "claude-sonnet-5-5", "goal": "Reach floor 2"}))
    lines = []
    for i, r in enumerate(records, 1):
        shot = f"screenshots/step_{i:04d}.png"
        Image.new("RGB", (320, 180), r.pop("color", (i * 20, 0, 0))).save(run_dir / shot)
        lines.append(json.dumps({"step": i, "time": f"2026-10-25T18:00:{i:02d}+00:00", "screenshot": shot, **r}))
    (run_dir / "steps.jsonl").write_text("\n".join(lines) + "\n")
    if summary:
        (run_dir / "summary.json").write_text(json.dumps(summary))
    return run_dir


CLICK = [action_to_dict(Click(50, 60))]


def sample_run(tmp_path):
    same = (9, 9, 9)
    return write_run(tmp_path, [
        {"state": map_state(0), "executed": CLICK, "thought": "Click the bottom node."},
        {"state": fight_state(1, 80), "executed": CLICK},
        {"state": fight_state(1, 66), "executed": CLICK},  # big hit
        {"state": {**fight_state(1, 66, ()), "combat_ended": True}, "executed": CLICK, "color": same},
        {"state": map_state(1, 66), "executed": CLICK, "color": same, "thought": "Click the row 2 node."},
        {"state": map_state(1, 66), "executed": CLICK, "color": same},
        {"state": map_state(1, 66), "executed": CLICK, "color": same},
        {"state": map_state(2, 66), "executed": [], "done": True, "thought": "DONE on floor 2"},
    ], summary={"end_reason": "done", "steps": 8, "cost_usd": 0.05})


def test_highlights_find_fights_floors_hits_stuck_and_end(tmp_path):
    run_dir = sample_run(tmp_path)
    _, steps, summary = load_run(run_dir)
    items = [(h.step, h.kind) for h in highlights(steps, step_diffs(run_dir, steps), summary)]
    assert items == [
        (2, "floor"), (2, "fight"), (3, "big hit"), (4, "fight won"),
        (5, "stuck"), (8, "floor"), (8, "done"), (8, "end"),
    ]
    text = {h.kind: h.text for h in highlights(steps, step_diffs(run_dir, steps), summary)}
    assert text["fight won"] == "Beat Nibbit, lost 14 HP (HP 66)"
    assert text["stuck"] == "Screen didn't change for 3 steps. AI: Click the row 2 node."  # step 4 has no thought


def test_logged_screen_diff_wins_over_screenshots(tmp_path):
    run_dir = write_run(tmp_path, [{"executed": CLICK}, {"executed": CLICK, "screen_diff": 0.2}])
    _, steps, _ = load_run(run_dir)
    assert step_diffs(run_dir, steps) == [None, 0.2]


def test_markdown_gives_screen_times_when_recorded(tmp_path):
    run_dir = sample_run(tmp_path)
    (run_dir / "screen.json").write_text(json.dumps({"started_at": "2026-10-25T18:00:00+00:00"}))
    meta, steps, summary = load_run(run_dir)
    md = highlights_markdown(run_dir, meta, steps, highlights(steps, step_diffs(run_dir, steps), summary), 2.0)
    assert "| replay.mp4 | screen.mp4 | step | what |" in md
    assert "| 0:06 | 0:04 | 4 | **fight won**: Beat Nibbit" in md


def test_frame_has_bar_and_click_marker():
    shot = Image.new("RGB", (1280, 720), (0, 0, 0))
    record = {"step": 3, "thought": "Play Strike on the Nibbit.", "executed": [action_to_dict(Click(640, 300))],
              "state": fight_state(1, 70)}
    frame = render_frame(shot, record, 0.0123, "claude-sonnet-5-5")
    assert frame.size == (1280, 720 + BAR_HEIGHT)
    assert frame.getpixel((640 + 20, 300)) != (0, 0, 0)  # circle drawn around the click
    assert frame.getpixel((640, 300)) != (0, 0, 0)


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")
def test_cli_writes_video_and_highlights(tmp_path):
    run_dir = sample_run(tmp_path)
    main([str(run_dir), "--seconds-per-step", "0.5"])
    assert (run_dir / "replay.mp4").stat().st_size > 0
    assert (run_dir / "highlights.md").read_text().startswith("# Highlights: run")


def test_recorder_command(tmp_path):
    cmd = ScreenRecorder(":99", (1280, 720), tmp_path).command()
    assert cmd[cmd.index("-f") + 1] == "x11grab"
    assert cmd[cmd.index("-video_size") + 1] == "1280x720"
    assert cmd[cmd.index("-i") + 1] == ":99"
    assert cmd[-1] == str(tmp_path / "screen.mp4")
