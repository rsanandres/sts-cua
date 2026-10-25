import json
import threading
from pathlib import Path
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from PIL import Image

from sts_cua.actions import Click, Drag, Key, MouseMove, Scroll, TypeText, Wait
from sts_cua.models.base import HistoryEntry, Observation
from sts_cua.models.qwen import QwenConfig, QwenModel, adapt_prompt, to_pixels


def call(**arguments):
    return '<tool_call>\n' + json.dumps({"name": "computer_use", "arguments": arguments}) + '\n</tool_call>'


def completion(content, finish_reason="stop", logprobs=None, **extra):
    choice = {"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": finish_reason}
    if logprobs is not None:
        choice["logprobs"] = {"content": logprobs}
    choice.update(extra.pop("choice", {}))
    return {
        "id": "chatcmpl-1",
        "model": "Qwen/Qwen3-VL-8B-Instruct-FP8",
        "choices": [choice],
        "usage": {"prompt_tokens": 2400, "completion_tokens": 60, "total_tokens": 2460},
        **extra,
    }


class FakeTransport:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def __call__(self, url, payload, headers, timeout):
        self.calls.append((url, payload, headers, timeout))
        return self.response


def decide(content, frame, history=(), config=None, **kwargs):
    transport = FakeTransport(completion(content, **kwargs))
    model = QwenModel(config or QwenConfig(), transport=transport)
    return model.decide(Observation(len(history) + 1, frame, list(history))), transport


def test_request_shape(frame):
    history = [
        HistoryEntry(1, "Thought: open the map. Action: press m.", [Key("m")]),
        HistoryEntry(2, "", [Click(640, 360)], unchanged=True),
    ]
    _, transport = decide(call(action="wait", time=1), frame, history, QwenConfig(goal="Reach floor 2"))
    url, req, headers, _ = transport.calls[0]
    assert url == "http://localhost:8000/v1/chat/completions"
    assert headers == {"Content-Type": "application/json"}
    assert req["model"] == "Qwen/Qwen3-VL-8B-Instruct-FP8"
    assert (req["temperature"], req["top_p"], req["top_k"]) == (0.7, 0.8, 20)
    assert req["logprobs"] is True and req["return_token_ids"] is True
    assert "top_logprobs" not in req

    system, user = req["messages"]
    text = system["content"][0]["text"]
    assert "<tools>\n" in text and '"name": "computer_use"' in text
    assert "The screen's resolution is 1000x1000." in text
    assert "<tool_call>\n{\"name\": <function-name>, \"arguments\": <args-json-object>}\n</tool_call>" in text
    assert "# Response format" in text and "1 to 9, then 0" in text

    prompt, image = user["content"]
    assert prompt["text"].startswith("The user query: Reach floor 2")
    assert "Step 3." in prompt["text"]
    assert "Step 1: press m (Thought: open the map. Action: press m.)" in prompt["text"]
    # History is shown on the model's 0-1000 scale, with the unchanged-screen note.
    assert "Step 2: click at (500, 500) -> the screen did not change" in prompt["text"]
    assert "Notes:" not in prompt["text"]
    assert image["type"] == "image_url"
    assert image["image_url"]["url"].startswith("data:image/png;base64,")


def test_first_step_and_default_goal(frame):
    _, transport = decide(call(action="wait", time=1), frame)
    text = transport.calls[0][1]["messages"][1]["content"][0]["text"]
    assert text.startswith("The user query: Play Slay the Spire 2 and win the run.")
    assert "this is the first step" in text


def test_notes_included_when_present(frame):
    class WithNotes(Observation):
        notes = "Nibbit: block on turn 2."

    transport = FakeTransport(completion(call(action="wait", time=1)))
    QwenModel(transport=transport).decide(WithNotes(1, frame, []))
    text = transport.calls[0][1]["messages"][1]["content"][0]["text"]
    assert text.endswith("Notes:\nNibbit: block on turn 2.")


def test_system_prompt_uses_relative_coordinates():
    prompt = QwenModel().system_text()
    assert "pixels in the screenshot" not in prompt
    assert "0 to 1000 scale" in prompt
    assert "about x 500, y 417" in prompt
    assert "reply with the word DONE" not in prompt and "action terminate" in prompt
    # Custom prompts get the coordinate sentence rewritten too.
    assert "0 to 1000" in adapt_prompt((Path(__file__).parent.parent / "prompts" / "click_target.txt").read_text())


def test_parse_every_action(frame):
    content = "Thought: Strike the Nibbit.\nAction: select card 1 and click the enemy.\n" + "\n".join([
        call(action="key", keys=["1"]),
        call(action="left_click", coordinate=[742, 625]),
        call(action="right_click", coordinate=[500, 500]),
        call(action="middle_click", coordinate=[500, 500]),
        call(action="double_click", coordinate=[500, 500]),
        call(action="triple_click", coordinate=[500, 500]),
        call(action="mouse_move", coordinate=[100, 100]),
        call(action="left_click_drag", coordinate=[200, 200]),
        call(action="scroll", pixels=-3),
        call(action="scroll", pixels=500, coordinate=[500, 500]),
        call(action="hscroll", pixels=2),
        call(action="key", keys=["ctrl", "Enter"]),
        call(action="key", keys=["esc"]),
        call(action="key", keys=["E"]),
        call(action="type", text="hi"),
        call(action="wait", time=2),
    ])
    decision, _ = decide(content, frame)
    assert decision.actions == [
        Key("1"),
        Click(950, 450),
        Click(640, 360, button="right"),
        Click(640, 360, button="middle"),
        Click(640, 360, count=2),
        Click(640, 360, count=3),
        MouseMove(128, 72),
        Drag(128, 72, 256, 144),  # starts where the cursor was left
        Scroll("down", 3),
        Scroll("up", 10, 640, 360),  # capped
        Scroll("right", 2),
        Key("ctrl+Return"),
        Key("Escape"),
        Key("e"),
        TypeText("hi"),
        Wait(2.0),
    ]
    assert decision.thought == "Thought: Strike the Nibbit. Action: select card 1 and click the enemy."
    assert not decision.done
    assert decision.stop_reason == "stop"
    assert decision.usage.input_tokens == 2400 and decision.usage.output_tokens == 60
    assert decision.usage.cost_usd == 0


def test_drag_starts_from_last_pointer_in_history(frame):
    history = [HistoryEntry(1, "", [Click(100, 200), Key("1")])]
    decision, _ = decide(call(action="left_click_drag", coordinate=[500, 500]), frame, history)
    assert decision.actions == [Drag(100, 200, 640, 360)]
    decision, _ = decide(call(action="left_click_drag", coordinate=[500, 500]), frame)
    assert decision.actions == [] and "skipped unsupported: left_click_drag" in decision.thought


@pytest.mark.parametrize("coord, expected", [
    ([0, 0], (0, 0)),
    ([1000, 1000], (1279, 719)),
    ([999, 999], (1279, 719)),
    ([500, 500], (640, 360)),
    ([-50, 2000], (0, 719)),
    ([500.4, 250.0], (641, 180)),
    ("(500, 500)", (640, 360)),
    ([1], None),
    (None, None),
    (["a", "b"], None),
])
def test_to_pixels(coord, expected):
    assert to_pixels(coord, (1280, 720)) == expected


def test_coordinates_use_real_screen_size():
    big = Image.new("RGB", (1920, 1080))
    decision, _ = decide(call(action="left_click", coordinate=[500, 1000]), big)
    assert decision.actions == [Click(960, 1079)]


@pytest.mark.parametrize("content", [
    "",
    "I am not sure what to do.",
    "<tool_call>\n{not json}\n</tool_call>",
    '<tool_call>\n{"name": "other_tool", "arguments": {"action": "left_click"}}\n</tool_call>',
    '<tool_call>\n{"name": "computer_use", "arguments": {}}\n</tool_call>',
    '<tool_call>\n["left_click"]\n</tool_call>',
    call(action="left_click"),  # no coordinate
    call(action="fly", coordinate=[1, 2]),
    call(action="wait", time="soon"),
    call(action="scroll", pixels="a lot"),
    call(action="key", keys=[]),
])
def test_malformed_output_gives_no_action(frame, content):
    decision, _ = decide(content, frame)
    assert decision.actions == []
    assert not decision.done


def test_truncated_and_mixed_calls(frame):
    # A good call, then one cut off by max_tokens with no closing tag.
    content = call(action="key", keys=["e"]) + '\n<tool_call>\n{"name": "computer_use", "arguments": {"action": "left_'
    decision, _ = decide(content, frame, finish_reason="length")
    assert decision.actions == [Key("e")]
    assert "[unparsed tool call x1]" in decision.thought
    assert decision.stop_reason == "length"
    assert decision.raw["parse_errors"]


def test_missing_close_tag_still_parses(frame):
    content = 'Thought: end turn.\n<tool_call>\n{"name": "computer_use", "arguments": {"action": "key", "keys": ["e"]}}'
    decision, _ = decide(content, frame)
    assert decision.actions == [Key("e")]


def test_arguments_as_string_and_server_tool_calls(frame):
    tool_calls = [{"id": "c1", "type": "function", "function": {
        "name": "computer_use", "arguments": json.dumps({"action": "left_click", "coordinate": [500, 500]})}}]
    decision, _ = decide(None, frame, choice={"message": {"role": "assistant", "content": None, "tool_calls": tool_calls}})
    assert decision.actions == [Click(640, 360)]


def test_terminate_sets_done(frame):
    content = "Thought: the map is open.\nAction: finish.\n" + call(action="terminate", status="success")
    decision, _ = decide(content, frame)
    assert decision.done and decision.actions == []
    assert decision.stop_reason == "terminate_success"

    decision, _ = decide(call(action="terminate", status="failure"), frame)
    assert decision.done and decision.stop_reason == "terminate_failure"


def test_actions_before_terminate_still_run(frame):
    decision, _ = decide(call(action="key", keys=["e"]) + call(action="terminate", status="success"), frame)
    assert decision.actions == [Key("e")] and decision.done


def test_answer_sets_done(frame):
    decision, _ = decide(call(action="answer", text="Floor 3, 54 HP."), frame)
    assert decision.done and decision.stop_reason == "answer"
    assert decision.thought.endswith("Answer: Floor 3, 54 HP.")


def test_bare_done_text_sets_done(frame):
    decision, _ = decide("DONE: on the Act 1 map.", frame)
    assert decision.done and decision.actions == []


def test_think_block_goes_to_reasoning(frame):
    decision, _ = decide("<think>Enemy attacks for 12.</think>\nThought: block.\n" + call(action="key", keys=["2"]), frame)
    assert decision.reasoning == "Enemy attacks for 12."
    assert decision.thought == "Thought: block."
    decision, _ = decide("Opened think in the prompt.</think>\nThought: block.\n" + call(action="key", keys=["2"]), frame)
    assert decision.reasoning == "Opened think in the prompt."
    assert decision.thought == "Thought: block."


def test_raw_keeps_training_data(frame):
    logprobs = [
        {"token": "<tool_call>", "logprob": -0.01, "bytes": [60], "top_logprobs": []},
        {"token": "\n", "logprob": -0.2, "bytes": [10], "top_logprobs": []},
    ]
    content = "Thought: x.\n" + call(action="key", keys=["e"])
    decision, transport = decide(
        content, frame, logprobs=logprobs, prompt_token_ids=[1, 2, 3], choice={"token_ids": [151657, 198]},
    )
    raw = decision.raw
    assert raw["response_text"] == content
    assert raw["logprobs"] == logprobs
    assert raw["token_ids"] == [151657, 198] and raw["prompt_token_ids"] == [1, 2, 3]
    assert raw["usage"]["prompt_tokens"] == 2400
    assert raw["tool_calls"] == [{"action": "key", "keys": ["e"]}]
    sent = transport.calls[0][1]
    logged = raw["request"]
    # The logged request matches what was sent, except the image is a placeholder.
    assert logged["messages"][0] == sent["messages"][0]
    assert logged["messages"][1]["content"][0] == sent["messages"][1]["content"][0]
    url = logged["messages"][1]["content"][1]["image_url"]["url"]
    assert url.startswith("<screenshot 1280x720 png sha256=")
    assert json.dumps(raw)  # serializable for steps.jsonl


def test_top_logprobs_and_seed(frame):
    _, transport = decide(call(action="wait", time=1), frame, config=QwenConfig(top_logprobs=5, seed=7))
    req = transport.calls[0][1]
    assert req["top_logprobs"] == 5 and req["seed"] == 7


def test_no_choices_raises(frame):
    model = QwenModel(transport=FakeTransport({"object": "error", "message": "bad"}))
    with pytest.raises(RuntimeError):
        model.decide(Observation(1, frame, []))


class _Stub(BaseHTTPRequestHandler):
    requests: list = []
    status = 200
    reply: dict = {}

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).requests.append((self.path, dict(self.headers), body))
        data = json.dumps(self.reply if self.status == 200 else {"error": "nope"}).encode()
        self.send_response(self.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


@pytest.fixture
def server():
    _Stub.requests, _Stub.status = [], 200
    httpd = HTTPServer(("127.0.0.1", 0), _Stub)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_port}/v1"
    httpd.shutdown()


def test_over_http(server, frame):
    _Stub.reply = completion(call(action="left_click", coordinate=[500, 500]))
    model = QwenModel(QwenConfig(base_url=server + "/", api_key="secret", served_model="sts-lora"))
    decision = model.decide(Observation(1, frame, []))
    assert decision.actions == [Click(640, 360)]
    path, headers, body = _Stub.requests[0]
    assert path == "/v1/chat/completions"
    assert headers["Authorization"] == "Bearer secret"
    assert body["model"] == "sts-lora"


def test_http_error_raises(server, frame):
    _Stub.status = 400
    with pytest.raises(RuntimeError, match="HTTP 400"):
        QwenModel(QwenConfig(base_url=server)).decide(Observation(1, frame, []))
