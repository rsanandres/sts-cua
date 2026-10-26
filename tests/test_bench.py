import argparse
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
from PIL import Image

from sts_cua.actions import Click, Key, action_to_dict
from sts_cua.bench import grounding, report, resources
from sts_cua.bench import results as R
from sts_cua.bench import targets as T
from sts_cua.bench.__main__ import main
from sts_cua.models.base import Decision, Usage
from sts_cua.models.factory import add_model_args
from sts_cua.screen import FakeScreen

DOCS = Path(__file__).resolve().parents[1] / "docs" / "hosting.md"


# ---- synthetic run logs


def write_run(runs_dir, name, records, model="claude-haiku-5-5 thinking=off effort=low", size=(1280, 720)):
    run_dir = runs_dir / name
    (run_dir / "screenshots").mkdir(parents=True)
    (run_dir / "meta.json").write_text(json.dumps({"model": model, "screen_size": list(size),
                                                   "game_build": "v0.107.1"}))
    lines = []
    for i, r in enumerate(records, 1):
        shot = f"screenshots/step_{i:04d}.png"
        Image.new("RGB", size, r.pop("color", (i * 20, 0, 0))).save(run_dir / shot)
        lines.append(json.dumps({"step": i, "screenshot": shot, "source": "model", **r}))
    (run_dir / "steps.jsonl").write_text("\n".join(lines) + "\n")
    return run_dir


def click(x, y):
    return [action_to_dict(Click(x, y))]


def fight(hand=("Strike", "Defend"), energy=3, enemy_hp=12):
    return {"state_type": "monster", "floor": 1, "round": 1, "player": {"hp": 70, "energy": energy},
            "hand": [{"slot": i + 1, "name": n} for i, n in enumerate(hand)],
            "enemies": [{"name": "Nibbit", "hp": enemy_hp}]}


def sample_runs(tmp_path):
    runs = tmp_path / "runs"
    same = (9, 9, 9)
    write_run(runs, "20261025T100000Z", [
        # 1: map click, screen changes, floor goes up: kept (map_moved).
        {"thought": "I see the map. I'll click the bottom node at (640, 650) to start the act.",
         "executed": click(640, 650), "state": {"state_type": "map", "floor": 0}, "screen_diff": None},
        # 2: end turn, hand changes: kept (end_turn).
        {"thought": "No energy left, so I'll end my turn.", "executed": click(1100, 600), "state": fight(),
         "screen_diff": 30.0},
        # 3: play a card: next screen identical -> dropped (screen unchanged).
        {"thought": "Play card 2 on the Nibbit.", "executed": click(900, 300), "state": fight(()),
         "screen_diff": 25.0, "color": same},
        # 4: end turn, screen changes but the mod's state is the same -> dropped (no effect).
        {"thought": "Ending the turn now.", "executed": click(1100, 600), "state": fight(()),
         "screen_diff": 0.2, "color": same},
        # 5: key then click: not a single click.
        {"thought": "Select card 1 and click the Nibbit.", "executed": [action_to_dict(Key("1"))] + click(900, 300),
         "state": fight(()), "screen_diff": 30.0},
        # 6: a co-op rule acted, not the model.
        {"thought": "[rule end_turn] nothing playable", "executed": click(1100, 600), "state": fight(()),
         "source": "rule", "screen_diff": 30.0},
        # 7: a reward click with no state after it: kept, unchecked.
        {"thought": "Take the gold reward.", "executed": click(640, 300), "state": None, "screen_diff": 30.0},
        # 8: last step, nothing after it.
        {"thought": "Click Proceed.", "executed": click(1150, 650), "state": None, "screen_diff": 30.0},
    ])
    # The map step leads to a fight on floor 1 (step 2's state).
    # A local model's run is never a source of targets.
    write_run(runs, "20261025T110000Z", [
        {"thought": "Click the End Turn button.", "executed": click(1100, 600), "state": fight()},
        {"thought": "Click the End Turn button.", "executed": click(1100, 600), "state": fight(())},
    ], model="Qwen/Qwen3-VL-8B-Instruct-FP8 temperature=0.7")
    return runs


@pytest.mark.parametrize("thought, state, expected", [
    ("I'll click the bottom node at (640, 650) to start the act.", None, "Click the bottom node"),
    ("The Nibbit attacks for 6. I'll end my turn.", {"state_type": "monster"}, "Click the End Turn button"),
    ("Play card 2 on the Nibbit.", None, "Play card 2 on the Nibbit"),
    ("Rewards are shown. Now let me click the Proceed button, since nothing is left.", None, "Click the Proceed button"),
    ("Selecting the Ironclad at x=300, y=400.", None, "Select the Ironclad"),
    ("I see the main menu.", None, None),
    ("Click (640, 300).", None, None),  # nothing left once the coordinates are gone
])
def test_instruction_from_thought(thought, state, expected):
    assert T.instruction_from(thought, state) == expected


def test_extract_keeps_changed_screens_with_an_effect(tmp_path):
    runs = sample_runs(tmp_path)
    bench = tmp_path / "bench"
    out = T.extract(runs, bench)
    got = T.load_targets(bench)
    assert [t.id for t in got] == ["20261025T100000Z-0001", "20261025T100000Z-0002", "20261025T100000Z-0007"]
    assert out["added"] == 3 and out["total"] == 3 and out["runs"] == 1
    assert out["skipped"] == {"screen unchanged": 1, "no effect in mod state": 1, "last step": 1}
    first, end_turn, reward = got
    assert (first.instruction, first.x, first.y, first.radius, first.check) == (
        "Click the bottom node", 640, 650, 40.0, "map_moved")
    assert (end_turn.instruction, end_turn.check, end_turn.state_type) == ("Click the End Turn button", "end_turn", "monster")
    assert (reward.instruction, reward.check) == ("Take the gold reward", None)
    assert all((bench / t.image).exists() and t.image.startswith("images/") for t in got)
    assert first.source["run"] == "20261025T100000Z" and first.source["game_build"] == "v0.107.1"

    # Re-extracting adds nothing; a deleted target stays deleted.
    assert T.extract(runs, bench)["added"] == 0
    assert T.delete(["20261025T100000Z-0007"], bench) == 1
    assert not (bench / "images" / "20261025T100000Z-0007.png").exists()
    assert T.extract(runs, bench)["added"] == 0
    assert [t.id for t in T.load_targets(bench)] == ["20261025T100000Z-0001", "20261025T100000Z-0002"]
    assert T.rejected(bench) == {"20261025T100000Z-0007"}


def test_extract_recomputes_diffs_for_old_runs_and_scales_the_radius(tmp_path):
    runs = tmp_path / "runs"
    write_run(runs, "old", [
        {"thought": "Click the Proceed button.", "executed": click(320, 180)},
        {"thought": "Click the Proceed button.", "executed": click(320, 180), "color": (40, 0, 0)},  # same as next
        {"thought": "", "executed": [], "color": (40, 0, 0)},
    ], size=(640, 360))
    T.extract(runs, tmp_path / "bench")
    got = T.load_targets(tmp_path / "bench")
    assert [t.id for t in got] == ["old-0001"] and got[0].radius == 20.0 and got[0].size == (640, 360)


def test_contact_sheet(tmp_path):
    runs = sample_runs(tmp_path)
    bench = tmp_path / "bench"
    T.extract(runs, bench)
    sheets = T.contact_sheets(bench, columns=2, thumb_width=200, per_sheet=2)
    assert [p.name for p in sheets] == ["sheet-01.png", "sheet-02.png"]
    sheet = Image.open(sheets[0])
    assert sheet.size == (400, 112 + 58)
    # The target circle is drawn in red on the first thumbnail (640, 650 -> 100, 101.6; radius 40 -> 6.25).
    assert sheet.getpixel((100, 102))[0] > 200


# ---- hit/miss math


def target(x=100, y=100, radius=40.0):
    return T.Target("t", "images/t.png", "Click it", x, y, radius, (1280, 720))


def test_score_hit_miss_and_malformed():
    assert grounding.score(target(), Click(124, 132)) == {"hit": True, "error_px": 40.0, "x": 124, "y": 132,
                                                          "malformed": False}
    assert grounding.score(target(), Click(125, 132))["hit"] is False
    assert grounding.score(target(), None) == {"hit": False, "error_px": None, "x": None, "y": None, "malformed": True}


def test_summary_counts_errors_apart():
    items = [
        {"hit": True, "error_px": 3.0, "malformed": False, "latency_s": 1.0, "tokens_per_s": 40.0, "error": None},
        {"hit": False, "error_px": 90.0, "malformed": False, "latency_s": 2.0, "tokens_per_s": 50.0, "error": None},
        {"hit": False, "error_px": None, "malformed": True, "latency_s": 3.0, "error": None},
        {"hit": False, "error_px": None, "malformed": False, "latency_s": 0.1, "error": "ConnectionRefusedError()"},
    ]
    s = grounding.summarize(items)
    assert (s["targets"], s["answered"], s["errors"], s["hits"], s["malformed"]) == (4, 3, 1, 1, 1)
    assert s["hit_rate"] == round(1 / 3, 4) and s["median_error_px"] == 46.5
    assert (s["latency_p50_s"], s["latency_p95_s"], s["tokens_per_s"]) == (2.0, 3.0, 45.0)
    assert R.percentile([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 95) == 10 and R.percentile([], 95) is None


# ---- a grounding run against a stub OpenAI-compatible server


def tool_call(x, y):
    return '<tool_call>\n{"name": "computer_use", "arguments": {"action": "left_click", "coordinate": [%d, %d]}}\n</tool_call>' % (x, y)


class StubServer:
    """Answers /v1/chat/completions with canned replies in order (a dict is a body, an int an HTTP error)."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.requests = []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                stub.requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                reply = stub.replies.pop(0)
                if isinstance(reply, int):
                    self.send_response(reply)
                    self.end_headers()
                    self.wfile.write(b'{"error": "boom"}')
                    return
                body = json.dumps(reply).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server.server_port}/v1"

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()


def completion(content, timings=None):
    body = {"id": "x", "model": "m", "choices": [{"index": 0, "message": {"role": "assistant", "content": content},
                                                   "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1000, "completion_tokens": 20}}
    if timings:
        body["timings"] = timings
    return body


def bench_with_targets(tmp_path, n):
    bench = tmp_path / "bench"
    (bench / "images").mkdir(parents=True)
    ts = []
    for i in range(n):
        Image.new("RGB", (1280, 720), (i, 0, 0)).save(bench / "images" / f"t{i}.png")
        ts.append(T.Target(f"t{i}", f"images/t{i}.png", f"Click thing {i}", 640, 360, 40.0, (1280, 720),
                           source={"run": "r1", "game_build": "v0.107.1"}))
    T.save_targets(ts, bench)
    return bench, ts


def model_ns(*argv):
    parser = argparse.ArgumentParser()
    add_model_args(parser)
    return parser.parse_args(list(argv))


def test_grounding_against_stub_server(tmp_path):
    bench, ts = bench_with_targets(tmp_path, 5)
    replies = [
        completion("Thought: there. " + tool_call(500, 500), {"predicted_per_second": 42.5, "prompt_per_second": 900.0}),
        completion(tool_call(510, 510)),          # (653, 367): 14.8 px from (640, 360), a hit
        completion(tool_call(900, 100)),          # far: miss
        completion("I can't see it."),            # no tool call: malformed
        completion("<tool_call>{not json</tool_call>"),  # broken call: malformed
    ]
    with StubServer(replies) as stub:
        args = model_ns("--base-url", stub.url)
        result = grounding.run(["qwen3-vl-8b"], ts, bench, args)
    m = result["models"][0]
    hits = [i["hit"] for i in m["items"]]
    assert hits == [True, True, False, False, False]
    assert [i["malformed"] for i in m["items"]] == [False, False, False, True, True]
    assert m["items"][0]["error_px"] == 0.0 and m["items"][1]["error_px"] == 14.8
    assert m["items"][0]["tokens_per_s"] == 42.5 and m["items"][1]["tokens_per_s"] is None
    s = m["summary"]
    assert (s["hits"], s["malformed"], s["errors"], s["hit_rate"], s["tokens_per_s"]) == (2, 2, 0, 0.4, 42.5)
    assert result["game_build"] == "v0.107.1" and result["targets"] == 5 and result["runs"] == 1
    # The grounding prompt and instruction reach the model; local models run at temperature 0.
    req = stub.requests[2]
    assert req["temperature"] == 0.0
    assert "Return only the click" in req["messages"][0]["content"][0]["text"]
    assert "Return only the click for: Click thing 2" in req["messages"][1]["content"][0]["text"]
    assert "0 to 1000 scale" in req["messages"][0]["content"][0]["text"]  # the adapter rewrote the pixel sentence
    md = grounding.markdown(result)
    assert "| qwen3-vl-8b | 40% (2/5) | 14.8 |" in md


def test_grounding_stops_a_model_after_repeated_errors(tmp_path):
    bench, ts = bench_with_targets(tmp_path, 5)
    with StubServer([500, 500, 500]) as stub:
        result = grounding.run(["qwen3-vl-8b"], ts, bench, model_ns("--base-url", stub.url))
    s = result["models"][0]["summary"]
    assert (s["targets"], s["errors"], s["answered"], s["hit_rate"]) == (3, 3, 0, None)
    assert "HTTP 500" in result["models"][0]["items"][0]["error"]


def test_grounding_uses_optional_adapter_features(tmp_path):
    bench, ts = bench_with_targets(tmp_path, 1)
    built = []

    class Grounder:
        name = "fake"

        def ground(self, image, instruction):
            return Decision(thought=instruction, actions=[Click(640, 360)], usage=Usage(output_tokens=5),
                            raw={"timings": {"predicted_per_second": 10.0}})

    def build(args):
        built.append(args)
        return Grounder()

    args = model_ns()
    args.grounding = False  # an adapter's grounding mode, when the factory has one
    result = grounding.run(["fake"], ts, bench, args, build=build)
    item = result["models"][0]["items"][0]
    assert item["hit"] and item["tokens_per_s"] == 10.0 and item["output_tokens"] == 5
    assert built[0].grounding is True and built[0].goal == "Return only the click for: Click thing 0"
    assert args.goal is None  # the caller's namespace is left alone


def test_run_clicks_reads_the_target_page():
    frame = Image.new("RGB", (1280, 720))
    page = {"clicks": [], "button": [100, 100, 180, 70]}

    class Model:
        name = "fake"
        replies = [[Click(190, 135)], [], [Click(10, 10)]]

        def decide(self, obs):
            return Decision(thought="", actions=self.replies[obs.step - 1])

    class Screen(FakeScreen):
        def perform(self, action):
            super().perform(action)
            x, y, w, h = page["button"]
            hit = x <= action.x <= x + w and y <= action.y <= y + h
            page["clicks"] = page["clicks"] + [{"pos": [action.x, action.y], "hit": hit}]

    out = grounding.run_clicks(Model(), Screen([frame]), lambda: dict(page), 3, sleep=lambda s: None)
    assert [i["hit"] for i in out["items"]] == [True, False, False]
    assert [i["malformed"] for i in out["items"]] == [False, True, False]
    assert out["items"][0]["error_px"] == 0.0
    assert out["summary"]["hits"] == 1 and out["summary"]["malformed"] == 1


# ---- resources


NVIDIA_GPU = "0, NVIDIA GeForce RTX 4090, 19876, 24564\n"
NVIDIA_APPS = "4242, [N/A]\n"
MEMINFO = """MemTotal:       32768000 kB
MemFree:         1000000 kB
MemAvailable:   20480000 kB
Cached:         11000000 kB
SwapTotal:       8388608 kB
SwapFree:        8388608 kB
"""
VLLM_LOG = """INFO 10-27 gpu_model_runner.py: Model loading took 9.87 GiB and 21.3 seconds
INFO 10-27 gpu_worker.py: Available KV cache memory: 1.13 GiB
"""


def test_parse_nvidia_smi_and_meminfo():
    assert resources.parse_gpu(NVIDIA_GPU) == [{"index": 0, "name": "NVIDIA GeForce RTX 4090", "used_mib": 19876.0,
                                               "total_mib": 24564.0}]
    assert resources.parse_gpu("0, NVIDIA GeForce RTX 4090, [N/A], [N/A]\n") == []
    assert resources.parse_apps(NVIDIA_APPS) == {} and resources.parse_apps("17, 9000\n") == {17: 9000.0}
    assert resources.parse_meminfo(MEMINFO) == {"total_mib": 32000.0, "used_mib": 12000.0, "cached_mib": 10742.2,
                                                "swap_used_mib": 0.0}
    assert resources.parse_vllm_log(VLLM_LOG) == {"weights_gib": 9.87, "kv_cache_gib": 1.13}
    assert resources.parse_vllm_log("nothing here") is None


def fake_proc(tmp_path, procs):
    """procs: pid -> (ppid, rss kB, cmdline)."""
    root = tmp_path / "proc"
    root.mkdir()
    (root / "meminfo").write_text(MEMINFO)
    for pid, (ppid, rss, cmd) in procs.items():
        d = root / str(pid)
        d.mkdir()
        (d / "stat").write_text(f"{pid} (x y) S {ppid} 1 1 0")
        (d / "status").write_text(f"Name:\tx\nVmRSS:\t  {rss} kB\n")
        (d / "cmdline").write_text(cmd.replace(" ", "\0"))
    return root


def test_measure_from_canned_readings(tmp_path):
    root = fake_proc(tmp_path, {
        4242: (1, 2 * 1024 * 1024, "vllm serve Qwen/Qwen3-VL-8B-Instruct-FP8"),
        4243: (4242, 1024 * 1024, "VLLM::EngineCore"),
        500: (1, 3 * 1024 * 1024, "SlayTheSpire2"),
        600: (1, 512 * 1024, "steam"),
        601: (600, 512 * 1024, "steamwebhelper"),
        700: (1, 100 * 1024, "Xvfb :99"),
    })
    pgrep = {"vllm serve|vllm.entrypoints|llama-server": "4242\n", "SlayTheSpire2": "500\n", "steam": "600\n",
             "Xvfb :": "700\n"}

    def runner(argv):
        if argv[0] == "nvidia-smi":
            return NVIDIA_GPU if "--query-gpu" in argv[1] else NVIDIA_APPS
        return pgrep.get(argv[-1], "")

    log = tmp_path / "vllm.log"
    log.write_text(VLLM_LOG)
    baselines = {"desktop": {"gpu": {"used_mib": 3380.0}, "ram": {"used_mib": 4000.0}},
                 "pre-server": {"gpu": {"used_mib": 6400.0}, "ram": {"used_mib": 9000.0}}}
    r = resources.measure("qwen3-vl-8b", "coop", baselines, proc=resources.Proc(root, runner), game=True,
                          server_log=str(log))
    assert (r["vram_used_gib"], r["vram_total_gib"], r["vram_free_gib"]) == (19.41, 23.99, 4.58)
    assert r["model_vram_gib"] == 13.16 and r["games_vram_gib"] == 2.95
    assert r["server"]["pids"] == [4242, 4243] and r["server"]["rss_gib"] == 3.0
    assert r["server"]["cmd"].startswith("vllm serve")
    assert r["ram_used_gib"] == 11.72 and r["ram_delta_gib"] == 2.93
    assert r["game"]["running"] and r["game"]["rss_gib"] == 4.1 and r["game"]["steam"]["pids"] == [600, 601]
    assert r["activations_gib"] == 2.16
    assert "| Model VRAM, used minus pre-server baseline (GiB) | 13.16 |" in resources.markdown(r)


def test_measure_without_gpu_or_baseline(tmp_path):
    r = resources.measure("x", "solo", {}, proc=resources.Proc(fake_proc(tmp_path, {}), lambda argv: ""))
    assert r["gpu"] is None and r["model_vram_gib"] is None and r["server"]["pids"] == [] and r["game"] is None


# ---- report and the docs writer


def grounding_result(created, models):
    return {"kind": "grounding", "created_at": created, "game_build": "v0.107.1", "targets": 50, "runs": 4,
            "models": [{"model": name, "summary": {"targets": 50, "answered": 50, "errors": 0, "hits": round(rate * 50),
                                                   "malformed": 1, "hit_rate": rate, "median_error_px": 12.0,
                                                   "latency_p50_s": lat, "latency_p95_s": lat * 2,
                                                   "tokens_per_s": 30.0}, "items": []}
                       for name, rate, lat in models]}


def resources_result(model, mode, created="2026-10-27T10:00:00+00:00", **extra):
    base = {"kind": "resources", "created_at": created, "model": model, "mode": mode, "vram_used_gib": 20.0,
            "vram_total_gib": 24.0, "model_vram_gib": 12.0, "games_vram_gib": None,
            "server": {"pids": [1], "rss_gib": 3.2, "cmd": "llama-server -m x.gguf"}, "ram_used_gib": 11.0, "game": None}
    return {**base, **extra}


def test_latest_ranking_and_recommendation():
    results = [
        grounding_result("2026-10-26T10:00:00+00:00", [("holo4-27b", 0.5, 2.0)]),
        grounding_result("2026-10-27T10:00:00+00:00", [("holo4-27b", 0.9, 3.0), ("qwen3.8-27b", 0.9, 2.5),
                                                       ("ui-venus-2-9b", 0.8, 1.0), ("claude-sonnet-5-5", 0.95, 4.0)]),
        resources_result("holo4-27b", "coop", model_vram_gib=17.5),
    ]
    data = report.latest(results)
    ranked = report.ranking(data)
    assert [r["model"] for r in ranked] == ["claude-sonnet-5-5", "qwen3.8-27b", "holo4-27b", "ui-venus-2-9b"]
    assert ranked[2]["fits"]["coop"] == (False, 17.5, "measured")
    assert ranked[1]["fits"]["coop"][2] == "est." and ranked[0]["fits"]["solo"] == (None, None, "api")
    # Co-op: the 27B models don't fit 13 GiB (holo4 measured, qwen3.8 estimated), so the 9B wins there.
    assert report.recommended(ranked) == {"coop": "ui-venus-2-9b", "solo": "qwen3.8-27b"}
    md = report.ranking_markdown(ranked)
    assert md.splitlines()[2].startswith("1. **claude-sonnet-5-5**: 95% hits, 4.00 s median; co-op API")
    assert "Recommended: co-op **ui-venus-2-9b**, solo **qwen3.8-27b**." in md


def measured(**kw):
    rs = [grounding_result("2026-10-27T10:00:00+00:00", [("qwen3.8-27b", 0.9, 2.5)])]
    rs += [resources_result("qwen3.8-27b", "solo", **kw)]
    return report.latest(rs)


def test_docs_writer_appends_section_and_touches_only_measured_rows(tmp_path):
    real = DOCS.read_text(encoding="utf-8")
    assert real.rstrip("\n").endswith(report.END) and real.count(report.START) == 1  # the section is last
    original = real.split("\n\n" + report.SECTION + "\n")[0] + "\n"  # the real file without the section
    assert report.START not in original
    docs = tmp_path / "hosting.md"
    docs.write_text(original)
    touched = report.write_docs(docs, measured(), today="2026-10-27")
    text = docs.read_text()
    assert touched == ["ram_server"]
    # Everything before the new section is the original, except the one measured row.
    head, section = text.split("\n\n" + report.SECTION + "\n\n", 1)
    changed = [(a, b) for a, b in zip(original.rstrip("\n").split("\n"), head.split("\n")) if a != b]
    assert changed == [("  - The vLLM process itself: **measure**.",
                        "  - The vLLM process itself: 3.2 GiB RSS (qwen3.8-27b on llama-server, measured 2026-10-27)."
                        " <!-- bench:ram_server -->")]
    assert len(head.split("\n")) == len(original.rstrip("\n").split("\n"))
    # Rows without a measurement keep their text, **measure** included.
    assert "  - The AI's copy, Steam for Linux and Xvfb: **measure**." in text
    assert "| Two Spire 2 copies | 3.7 | Steam's recommended spec, 2 GB VRAM each. **Measure**" in text
    assert section.startswith(report.START + "\n") and section.endswith(report.END + "\n")
    assert "| qwen3.8-27b | 2026-10-27 | 90% (45/50) | 12.0 | 2.50 / 5.00 | 30.0 | 1 |" in section
    assert "| qwen3.8-27b | solo | 2026-10-27 | 20.0 / 24.0 | 12.0 | 20 | 3.2 | 11.0 | - |" in section
    assert "Recommended: co-op none that fits, solo **qwen3.8-27b**." in section

    # Idempotent: the same data writes the same file.
    assert report.write_docs(docs, measured(), today="2026-10-27") == ["ram_server"]
    assert docs.read_text() == text


def test_docs_writer_with_markers_and_every_row(tmp_path):
    docs = tmp_path / "hosting.md"
    docs.write_text(DOCS.read_text(encoding="utf-8") + "\nFooter stays.\n")  # the real file, with its markers
    data = measured(model_vram_gib=13.2, games_vram_gib=3.1, activations_gib=2.4,
                    server={"pids": [1], "rss_gib": 4.0, "cmd": "python -m vllm.entrypoints.openai.api_server"},
                    game={"running": True, "rss_gib": 5.5, "game": {"rss_gib": 3.0}, "steam": {"rss_gib": 2.0},
                          "xvfb": {"rss_gib": 0.5}})
    data["resources"][("qwen3.8-27b", "coop")] = data["resources"].pop(("qwen3.8-27b", "solo"))
    touched = report.write_docs(docs, data, today="2026-10-27")
    text = docs.read_text()
    assert sorted(touched) == ["ram_game", "ram_server", "vram_activations", "vram_games"]
    assert "Nothing measured yet" not in text and text.endswith(f"{report.END}\n\nFooter stays.\n")
    assert text.count(report.START) == 1 and text.count(report.SECTION) == 1
    assert ("| ...activations, CUDA graphs, vision encoder (part of the 13.2) | 2.4 | measured 2026-10-27 "
            "(qwen3.8-27b): model VRAM minus vLLM's") in text
    assert ("| Two Spire 2 copies | 3.1 | measured 2026-10-27: GPU used with both copies running minus the desktop "
            "baseline <!-- bench:vram_games --> |") in text
    assert "  - The vLLM process itself: 4.0 GiB RSS (qwen3.8-27b on vLLM, measured 2026-10-27)." in text
    assert ("  - The AI's copy, Steam for Linux and Xvfb: 5.5 GiB RSS (game 3.0, Steam 2.0, Xvfb 0.5; "
            "measured 2026-10-27). <!-- bench:ram_game -->") in text
    # Untouched rows and the plan sections stay as they were.
    assert "| Windows desktop, idle | 3.3 | `nvidia-smi` on the PC, 2026-10-24 |" in text
    assert "5. Replace the **measure** rows above with the results." in text
    assert "Rows marked **measure** are planning numbers" in text

    # A newer measurement updates the tagged rows on the next write.
    data["resources"][("qwen3.8-27b", "coop")]["games_vram_gib"] = 3.3
    report.write_docs(docs, data, today="2026-10-27")
    assert "| Two Spire 2 copies | 3.3 |" in docs.read_text()
    assert report.write_docs(docs, data, today="2026-10-27") and docs.read_text().count("bench:vram_games") == 1


def test_measure_rows_need_the_marker_and_their_section():
    text = "\n".join([
        "## VRAM budget", "| Two Spire 2 copies | 3.7 | Steam's spec, already measured |",
        "## Other", "| Two Spire 2 copies | 3.7 | **measure** |",
    ])
    data = measured(games_vram_gib=3.1)
    data["resources"][("qwen3.8-27b", "coop")] = data["resources"].pop(("qwen3.8-27b", "solo"))
    out, touched = report.replace_measure_rows(text, data)
    assert out == text and touched == []


# ---- CLI


def test_cli_extract_review_and_report(tmp_path, capsys):
    runs = sample_runs(tmp_path)
    bench = tmp_path / "bench"
    main(["extract", "--runs-dir", str(runs), "--bench-dir", str(bench)])
    main(["review", "--runs-dir", str(runs), "--bench-dir", str(bench), "--delete", "20261025T100000Z-0007"])
    assert (bench / "review" / "sheet-01.png").exists() and len(T.load_targets(bench)) == 2
    out_dir = R.new_dir(runs)
    R.write(out_dir, grounding_result("2026-10-27T10:00:00+00:00", [("ui-venus-2-9b", 0.8, 1.0)]), "")
    docs = tmp_path / "hosting.md"
    docs.write_text("# Hosting\n")
    main(["report", "--runs-dir", str(runs), "--write-docs", "--docs", str(docs)])
    out = capsys.readouterr().out
    assert "1. **ui-venus-2-9b**: 80% hits" in out and "measure rows: none" in out
    assert docs.read_text().startswith(f"# Hosting\n\n{report.SECTION}\n\n{report.START}\n")
