"""The registry families' adapters: Qwen3.8, Holo4 and UI-Venus-2.

Sample replies follow each family's documented output (sources in each adapter's docstring):
- Qwen3.8: the XML tool call from the Qwen3.8-27B chat template and the OSWorld Qwen
  scaffold (mm_agents/qwen), which also accepts Qwen3-VL's JSON form.
- Holo4: OpenAI-style message.tool_calls (H's function-calling docs), with tool names and
  arguments copied from H's published Holo4-27B OSWorld trajectories.
- UI-Venus-2: "<think>...</think>\\n<action>...</action>" from computer_example.py's output
  format and the repo's examples/example_output.json raw_response.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
from PIL import Image

from sts_cua.actions import Click, Drag, Key, MouseMove, Scroll, TypeText, Wait
from sts_cua.models.base import HistoryEntry, Observation
from sts_cua.models.claude import SYSTEM_PROMPT as CLAUDE_PROMPT
from sts_cua.models.holo4 import TOOLS, Holo4Config, Holo4Model
from sts_cua.models.holo4 import adapt_prompt as holo_prompt
from sts_cua.models.qwen38 import Qwen38Config, Qwen38Model
from sts_cua.models.qwen38 import adapt_prompt as qwen38_prompt
from sts_cua.models.ui_venus import VenusConfig, VenusModel, parse_action
from sts_cua.models.ui_venus import adapt_prompt as venus_prompt

TIMINGS = {"cache_n": 0, "prompt_n": 2210, "prompt_ms": 812.4, "prompt_per_token_ms": 0.37,
           "prompt_per_second": 2720.3, "predicted_n": 61, "predicted_ms": 1650.2,
           "predicted_per_token_ms": 27.05, "predicted_per_second": 36.96}


def completion(content, finish_reason="stop", message=None, timings=TIMINGS, **extra):
    msg = {"role": "assistant", "content": content, **(message or {})}
    response = {
        "id": "chatcmpl-1",
        "model": "served",
        "choices": [{"index": 0, "message": msg, "finish_reason": finish_reason}],
        "usage": {"prompt_tokens": 2210, "completion_tokens": 61, "total_tokens": 2271},
        **extra,
    }
    if timings is not None:
        response["timings"] = timings  # llama-server
    return response


class FakeTransport:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def __call__(self, url, payload, headers, timeout):
        self.calls.append((url, payload, headers, timeout))
        return self.response


def decide(cls, config, content, frame, history=(), notes="", **kwargs):
    transport = FakeTransport(completion(content, **kwargs))
    model = cls(config, transport=transport)
    decision = model.decide(Observation(len(history) + 1, frame, list(history), notes=notes))
    return decision, transport.calls[0][1]


HISTORY = [
    HistoryEntry(1, "Thought: open the map.", [Key("m")]),
    HistoryEntry(2, "", [Click(640, 360)], unchanged=True),
]


@pytest.mark.parametrize("adapt", [qwen38_prompt, holo_prompt, venus_prompt])
def test_every_prompt_edit_applies(adapt):
    prompt = adapt(CLAUDE_PROMPT, strict=True)  # raises if claude.py's wording drifted
    assert "pixels in the screenshot" not in prompt and "reply with the word DONE" not in prompt
    custom = adapt((Path(__file__).parent.parent / "prompts" / "click_target.txt").read_text())
    assert "pixels in the screenshot" not in custom


# ---- Qwen3.8


def xml_call(**params):
    body = "".join(f"<parameter={k}>\n{v if isinstance(v, str) else json.dumps(v)}\n</parameter>\n"
                   for k, v in params.items())
    return f"<tool_call>\n<function=computer_use>\n{body}</function>\n</tool_call>"


def q38(content, frame, thinking=False, history=(), **kwargs):
    config = Qwen38Config(thinking=thinking, goal=kwargs.pop("goal", None),
                          reasoning_effort=kwargs.pop("effort", None))
    return decide(Qwen38Model, config, content, frame, history, **kwargs)


def test_qwen38_request_shape(frame):
    _, req = q38(xml_call(action="wait", time=1), frame, history=HISTORY, goal="Reach floor 2",
                 notes="Rule for this step: vote for the rest site.")
    assert req["model"] == "qwen3.8-27b"
    assert (req["temperature"], req["top_p"], req["max_tokens"]) == (0.0, 0.9, 1024)
    assert "top_k" not in req and "tools" not in req and "return_token_ids" not in req
    assert req["chat_template_kwargs"] == {"enable_thinking": False}
    assert req["logprobs"] is True

    system, user = req["messages"]
    text = system["content"][0]["text"]
    assert text.startswith("You are playing Slay the Spire 2")
    assert "0 to 1000 scale" in text and "The screen's resolution is 1000x1000." in text
    assert '"name": "computer_use"' in text and "Optional `text` parameter can specify modifier keys" in text
    assert "<tool_call>\n<function=example_function_name>\n<parameter=example_parameter_1>" in text
    assert "# Response format" in text and "action terminate" in text

    image, prompt = user["content"]  # screenshot first, as in the OSWorld scaffold
    assert image["image_url"]["url"].startswith("data:image/png;base64,")
    p = prompt["text"]
    assert p.startswith("Please generate the next move according to the UI screenshot")
    assert "Instruction: Reach floor 2" in p and "Step 3." in p
    assert "Step 1: press m (Thought: open the map.)" in p
    assert "Step 2: click at (500, 500) -> the screen did not change" in p
    assert p.endswith("Notes:\nRule for this step: vote for the rest site.")


def test_qwen38_first_step(frame):
    _, req = q38(xml_call(action="wait", time=1), frame)
    text = req["messages"][1]["content"][1]["text"]
    assert "Instruction: Play Slay the Spire 2 and win the run." in text and "Previous actions" in text
    assert text.rstrip().endswith("None")


def test_qwen38_thinking_on(frame):
    _, req = q38(xml_call(action="wait", time=1), frame, thinking=True)
    assert req["chat_template_kwargs"] == {"enable_thinking": True}
    assert (req["temperature"], req["top_p"], req["top_k"], req["max_tokens"]) == (1.0, 0.95, 20, 8192)
    _, req = q38(xml_call(action="wait", time=1), frame, thinking=True, effort="low")
    assert req["chat_template_kwargs"] == {"enable_thinking": True, "reasoning_effort": "low"}
    _, req = q38(xml_call(action="wait", time=1), frame, thinking=False, effort="low")
    assert req["chat_template_kwargs"] == {"enable_thinking": False}


def test_qwen38_parses_every_action(frame):
    content = "Thought: Strike the Nibbit.\nAction: select card 1 and click the enemy.\n" + "\n".join([
        xml_call(action="key", keys=["1"]),
        xml_call(action="left_click", coordinate=[742, 625]),
        xml_call(action="left_click", coordinate=[500, 500], text="shift"),
        xml_call(action="right_click", coordinate=[500, 500]),
        xml_call(action="double_click", coordinate=[500, 500]),
        xml_call(action="triple_click", coordinate=[500, 500]),
        xml_call(action="mouse_move", coordinate=[100, 100]),
        xml_call(action="left_click_drag", coordinate=[200, 200]),
        xml_call(action="scroll", pixels=-3),
        xml_call(action="hscroll", pixels=2, coordinate=[500, 500]),
        xml_call(action="key", keys=["ctrl", "Enter"]),
        xml_call(action="type", text="hi"),
        xml_call(action="wait", time=2),
    ])
    decision, _ = q38(content, frame)
    # int(x * width / 999), as the scaffold's adjust_coordinates
    assert decision.actions == [
        Key("1"),
        Click(950, 450),
        Click(640, 360, modifiers="shift"),
        Click(640, 360, button="right"),
        Click(640, 360, count=2),
        Click(640, 360, count=3),
        MouseMove(128, 72),
        Drag(128, 72, 256, 144),
        Scroll("down", 3),
        Scroll("right", 2, 640, 360),
        Key("ctrl+Return"),
        TypeText("hi"),
        Wait(2.0),
    ]
    assert decision.thought == "Thought: Strike the Nibbit. Action: select card 1 and click the enemy."
    assert decision.raw["tool_calls"][1] == {"action": "left_click", "coordinate": [742, 625]}
    assert not decision.done and decision.stop_reason == "stop"


@pytest.mark.parametrize("coord, expected", [
    ([0, 0], (0, 0)), ([999, 999], (1279, 719)), ([1000, 1000], (1279, 719)),
    ([-5, 2000], (0, 719)), ("[500, 500]", (640, 360)),
])
def test_qwen38_coordinates_clamp(frame, coord, expected):
    decision, _ = q38(xml_call(action="left_click", coordinate=coord), frame)
    assert decision.actions == [Click(*expected)]


def test_qwen38_json_form_server_calls_and_bare_function(frame):
    json_call = '<tool_call>\n{"name": "computer_use", "arguments": {"action": "key", "keys": ["e"]}}\n</tool_call>'
    decision, _ = q38(json_call, frame)
    assert decision.actions == [Key("e")]
    tool_calls = [{"id": "c1", "type": "function", "function": {
        "name": "computer_use", "arguments": json.dumps({"action": "left_click", "coordinate": [500, 500]})}}]
    decision, _ = q38(None, frame, message={"tool_calls": tool_calls})
    assert decision.actions == [Click(640, 360)]
    bare = "Action: end turn.\n<function=computer_use>\n<parameter=action>\nkey\n</parameter>\n<parameter=keys>\n[\"e\"]\n</parameter>\n</function>"
    decision, _ = q38(bare, frame)
    assert decision.actions == [Key("e")] and decision.thought == "Action: end turn."


def test_qwen38_terminate(frame):
    decision, _ = q38("Thought: on the map.\n" + xml_call(action="terminate", status="success"), frame)
    assert decision.done and decision.stop_reason == "terminate_success" and decision.actions == []


@pytest.mark.parametrize("content", [
    "",
    "I'm not sure.",
    "<tool_call>\n<function=computer_use>\n</function>\n</tool_call>",
    "<tool_call>\n<function=other_tool>\n<parameter=action>\nleft_click\n</parameter>\n</function>\n</tool_call>",
    xml_call(action="left_click"),
    xml_call(action="left_click", coordinate="near the enemy"),
    xml_call(action="fly", coordinate=[1, 2]),
    xml_call(action="wait", time="soon"),
])
def test_qwen38_malformed_gives_no_action(frame, content):
    decision, _ = q38(content, frame)
    assert decision.actions == [] and not decision.done


def test_qwen38_truncated_call(frame):
    content = xml_call(action="key", keys=["e"]) + "\n<tool_call>\n<function=computer_use>\n<parameter=action>\nleft_cl"
    decision, _ = q38(content, frame, finish_reason="length")
    assert decision.actions == [Key("e")] and decision.stop_reason == "length"
    assert "[skipped unsupported: left_cl]" in decision.thought


def test_qwen38_thinking_text(frame):
    # llama-server's default reasoning format: thinking in reasoning_content.
    content = "Thought: block.\nAction: play Defend.\n" + xml_call(action="key", keys=["2"])
    decision, _ = q38(content, frame, thinking=True, message={"reasoning_content": "Enemy attacks for 12."})
    assert decision.reasoning == "Enemy attacks for 12."
    assert "12" not in decision.thought and decision.raw["reasoning_content"] == "Enemy attacks for 12."
    # --reasoning-format none: the template opened <think>, so only its close arrives.
    decision, _ = q38("Enemy attacks for 12.\n</think>\n\n" + content, frame, thinking=True)
    assert decision.reasoning == "Enemy attacks for 12."
    assert decision.thought == "Thought: block. Action: play Defend."
    # The next step's history carries the thought, not the reasoning.
    entry = HistoryEntry(1, decision.thought, decision.actions)
    _, req = q38(xml_call(action="wait", time=1), frame, history=[entry])
    assert "Enemy attacks" not in json.dumps(req["messages"])


def test_qwen38_raw_keeps_timings_and_logprobs(frame):
    logprobs = [{"token": "<tool_call>", "logprob": -0.01, "bytes": [60], "top_logprobs": []}]
    transport = FakeTransport(completion(xml_call(action="key", keys=["e"])))
    transport.response["choices"][0]["logprobs"] = {"content": logprobs}
    decision = Qwen38Model(Qwen38Config(), transport=transport).decide(Observation(1, frame, []))
    raw = decision.raw
    assert raw["timings"] == TIMINGS and raw["logprobs"] == logprobs
    assert raw["usage"]["prompt_tokens"] == 2210 and decision.usage.output_tokens == 61
    assert raw["request"]["messages"][1]["content"][0]["image_url"]["url"].startswith("<screenshot 1280x720 png")
    assert json.dumps(raw)


# ---- Holo4


def tool_call(name, **arguments):
    return {"id": f"call_{name}", "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}


def holo(frame, *calls, content="", thinking=True, history=(), reasoning=None, **kwargs):
    message = {"tool_calls": list(calls)} if calls else {}
    if reasoning:
        message["reasoning_content"] = reasoning
    config = Holo4Config(thinking=thinking, goal=kwargs.pop("goal", None))
    return decide(Holo4Model, config, content, frame, history, message=message, **kwargs)


def test_holo4_request_shape(frame):
    _, req = holo(frame, tool_call("wait_desktop", seconds=3), history=HISTORY, goal="Reach floor 2",
                  notes="Nibbit: block on turn 2.")
    assert req["model"] == "holo4-27b"
    assert req["tools"] == TOOLS and req["tool_choice"] == "required"
    names = [t["function"]["name"] for t in req["tools"]]
    assert {"click_desktop", "hotkey_desktop", "scroll_desktop", "wait_desktop", "answer"} <= set(names)
    click = req["tools"][0]["function"]["parameters"]
    assert click["required"] == ["element", "x", "y"]
    assert req["chat_template_kwargs"] == {"enable_thinking": True, "reasoning_effort": "medium"}
    assert (req["temperature"], req["max_tokens"]) == (0.8, 8192)

    system, user = req["messages"]
    text = system["content"][0]["text"]
    assert text.startswith("You are Holo, a computer-use agent designed by H Company.")
    assert "0-1000 grid over the screenshot" in text and "You are playing Slay the Spire 2" in text
    assert "<tool_call>" not in text  # H: no tool-format examples in the system prompt
    task, open_tag, image, close_tag = user["content"]
    assert task["text"].startswith("Task: Reach floor 2")
    assert "Step 2: click at (500, 500) -> the screen did not change" in task["text"]
    assert "Notes:\nNibbit: block on turn 2." in task["text"]
    assert (open_tag["text"], close_tag["text"]) == ("<observation>\n", "\n</observation>")
    assert image["image_url"]["url"].startswith("data:image/png;base64,")


def test_holo4_thinking_off(frame):
    _, req = holo(frame, tool_call("wait_desktop", seconds=3), thinking=False)
    assert req["chat_template_kwargs"] == {"enable_thinking": False}
    assert (req["temperature"], req["max_tokens"]) == (0.0, 1024)


def test_holo4_parses_every_tool(frame):
    calls = [
        tool_call("click_desktop", element="Close button for Chrome update popup", x=982, y=128, button="left"),
        tool_call("click_desktop", element="Strike card", x=500, y=500, button="right"),
        tool_call("double_click_desktop", element="file icon on desktop", x=500, y=500),
        tool_call("move_to_desktop", element='"Mode" menu item to trigger submenu', x=250, y=250),
        tool_call("scroll_desktop", element="Settings left sidebar", x=500, y=500, direction="down", scroll_size=3),
        tool_call("scroll_desktop", element="list", x=500, y=500, direction="up", scroll_size=25),
        tool_call("hotkey_desktop", keys=["ctrl", "a"]),
        tool_call("hotkey_desktop", keys=["right"], repeat_count=11),
        tool_call("write_desktop", content="44", press_enter=True, overwrite=True),
        tool_call("write_at_desktop", content="Tamiflu", element="Search box", x=500, y=500, press_enter=True),
        tool_call("wait_desktop", seconds=3),
        tool_call("mouse_down_desktop", button="left"),
        tool_call("move_to_desktop", element="discard pile", x=100, y=100),
        tool_call("mouse_up_desktop", button="left"),
    ]
    decision, _ = holo(frame, *calls, content="The update popup covers the page; close it first.",
                       reasoning="The popup is in the way.")
    assert decision.actions == [
        Click(1256, 92),  # int(982 / 1000 * 1280), int(128 / 1000 * 720)
        Click(640, 360, button="right"),
        Click(640, 360, count=2),
        MouseMove(320, 180),
        Scroll("down", 3, 640, 360),
        Scroll("up", 10, 640, 360),  # capped
        Key("ctrl+a"),
        Key("Right", repeat=11),
        Key("ctrl+a"), TypeText("44"), Key("Return"),
        Click(640, 360), TypeText("Tamiflu"), Key("Return"),
        Wait(3.0),
        MouseMove(128, 72),
        Drag(640, 360, 128, 72),  # mouse_down where the write_at click left the pointer, up after the move
    ]
    assert decision.thought == "The update popup covers the page; close it first."
    assert decision.reasoning == "The popup is in the way."
    assert decision.raw["tool_calls"][0]["name"] == "click_desktop"
    assert decision.raw["timings"]["predicted_per_second"] == 36.96
    assert not decision.done


@pytest.mark.parametrize("x, y, expected", [
    (0, 0, (0, 0)), (1000, 1000, (1279, 719)), (999, 999, (1278, 719)), (-3, 1200, (0, 719)), ("500", "500", (640, 360)),
])
def test_holo4_coordinates_clamp(frame, x, y, expected):
    decision, _ = holo(frame, tool_call("click_desktop", element="it", x=x, y=y))
    assert decision.actions == [Click(*expected)]


def test_holo4_answer_sets_done(frame):
    decision, _ = holo(frame, tool_call("answer", content="On the Act 1 map, floor 1."))
    assert decision.done and decision.stop_reason == "answer" and decision.actions == []
    assert decision.thought.endswith("Answer: On the Act 1 map, floor 1.")


def test_holo4_reads_calls_left_in_content(frame):
    content = ("End the turn.\n<tool_call>\n<function=hotkey_desktop>\n<parameter=keys>\n[\"e\"]\n</parameter>\n"
               "</function>\n</tool_call>\n<tool_call>\n{\"name\": \"click_desktop\", \"arguments\": "
               "{\"element\": \"enemy\", \"x\": 500, \"y\": 500}}\n</tool_call>")
    decision, _ = holo(frame, content=content)
    assert decision.actions == [Key("e"), Click(640, 360)] and decision.thought == "End the turn."


@pytest.mark.parametrize("call", [
    tool_call("zoom_desktop", x=500, y=11, zoom_factor=2),
    tool_call("shell", command="ls"),
    tool_call("click_desktop", element="no coordinates"),
    tool_call("click_desktop", element="bad", x="left", y=3),
    tool_call("scroll_desktop", element="x", x=1, y=1, direction="sideways"),
    tool_call("hotkey_desktop", keys=[]),
    tool_call("mouse_up_desktop", button="left"),
    {"id": "c", "type": "function", "function": {"name": "click_desktop", "arguments": "{not json"}},
    {"id": "c", "type": "function", "function": {"name": None, "arguments": "{}"}},
])
def test_holo4_malformed_gives_no_action(frame, call):
    decision, _ = holo(frame, call)
    assert decision.actions == [] and not decision.done


def test_holo4_no_tool_call(frame):
    decision, _ = holo(frame, content="I need to look more closely.")
    assert decision.actions == [] and not decision.done and decision.thought == "I need to look more closely."


# ---- UI-Venus-2


def venus(content, frame, thinking=False, history=(), **kwargs):
    config = VenusConfig(thinking=thinking, goal=kwargs.pop("goal", None))
    return decide(VenusModel, config, content, frame, history, **kwargs)


def act(action, think="The map is open."):
    return f"<think>{think}</think>\n<action>{action}</action>"  # examples/example_output.json raw_response


def test_venus_request_shape(frame):
    _, req = venus(act("Wait()"), frame, history=HISTORY, goal="Reach floor 2", notes="Nibbit: block on turn 2.")
    assert req["model"] == "ui-venus-2-9b"
    assert (req["temperature"], req["top_p"], req["max_tokens"]) == (0.0, 0.7, 1024)
    assert "top_k" not in req and "tools" not in req
    assert req["chat_template_kwargs"] == {"enable_thinking": False}

    system, user = req["messages"]
    text = system["content"][0]["text"]
    assert text.startswith("**You are a GUI Agent.**") and "password" not in text
    assert "### User Task\nReach floor 2\n\n### Game Guide" in text
    assert "(999, 999)" in text and "Finished(content=" in text and "You are playing Slay the Spire 2" in text
    assert "### Output Format\n<think> your thinking process </think>\n<action> the next action </action>" in text
    progress, label, image = user["content"]
    assert "Step 2: click at (500, 500) -> the screen did not change" in progress["text"]
    assert "Notes:\nNibbit: block on turn 2." in progress["text"]
    assert label["text"] == "Current Screenshot:\n"
    assert image["image_url"]["url"].startswith("data:image/png;base64,")


def test_venus_thinking_on(frame):
    _, req = venus(act("Wait()"), frame, thinking=True)
    assert req["chat_template_kwargs"] == {"enable_thinking": True}
    assert (req["temperature"], req["max_tokens"]) == (1.0, 4096)


def test_venus_parses_every_action(frame):
    history = [HistoryEntry(1, "", [Click(100, 200)])]
    actions = [
        "Click(box=(500, 300))",
        "Click()",  # at the cursor: where the last action left it
        "DoubleClick(box=(500, 500))",
        "TripleClick(box=(500, 500))",
        "RightClick(box=(500, 500))",
        "MiddleClick(box=(500, 500))",
        "Hover(box=(100, 100))",
        "Drag(end=(200, 200))",
        "Drag(end=(200, 200), start=(500, 500))",
        "Swipe(amount=-5, axis='vertical')",
        "Swipe(amount=3, axis='horizontal')",
        "Type(content='seed\\n')",
        "Hotkey(keys=['ctrl', 'c'])",
        "Hotkey(keys=['down'], repeat=5)",
        "Wait()",
        "MouseDown(box=(500, 500))",
        "MouseUp(box=(100, 100))",
        "Click(point=(500, 500))",  # the mobile prompt's spelling
    ]
    decision, _ = venus("<think>Strike the Nibbit.</think>\n" + "".join(f"<action>{a}</action>" for a in actions),
                        frame, history=history)
    assert decision.actions == [
        Click(640, 216),  # int(500 * 1280 / 999), int(300 * 720 / 999)
        Click(640, 216),
        Click(640, 360, count=2),
        Click(640, 360, count=3),
        Click(640, 360, button="right"),
        Click(640, 360, button="middle"),
        MouseMove(128, 72),
        Drag(128, 72, 256, 144),
        Drag(640, 360, 256, 144),
        Scroll("down", 5),
        Scroll("right", 3),
        TypeText("seed"), Key("Return"),
        Key("ctrl+c"),
        Key("Down", repeat=5),
        Wait(2.0),
        Drag(640, 360, 128, 72),
        Click(640, 360),
    ]
    assert decision.thought == "Strike the Nibbit." and decision.reasoning == ""


def test_venus_sequence_plays_a_card(frame):
    decision, _ = venus(act("Sequence(actions=[Hotkey(keys=['1']), Click(box=(742, 445))])", "Strike the Nibbit."), frame)
    assert decision.actions == [Key("1"), Click(950, 320)]
    assert decision.raw["tool_calls"] == [
        {"name": "Hotkey", "arguments": {"keys": ["1"]}},
        {"name": "Click", "arguments": {"box": (742, 445)}},
    ]
    assert json.dumps(decision.raw)


@pytest.mark.parametrize("box, expected", [
    ((0, 0), (0, 0)), ((999, 999), (1279, 719)), ((1200, -4), (1279, 0)), ((499.5, 499.5), (640, 360)),
])
def test_venus_coordinates_clamp(frame, box, expected):
    decision, _ = venus(act(f"Click(box={box})"), frame)
    assert decision.actions == [Click(*expected)]


def test_venus_finished_and_call_user(frame):
    decision, _ = venus(act("Finished(content='On the Act 1 map.')"), frame)
    assert decision.done and decision.stop_reason == "finished" and decision.actions == []
    assert decision.thought == "The map is open. Finished: On the Act 1 map."
    decision, _ = venus(act("Sequence(actions=[Hotkey(keys=['e']), CallUser(content='Stuck.')])"), frame)
    assert decision.done and decision.stop_reason == "call_user" and decision.actions == [Key("e")]


def test_venus_bare_action_without_tags(frame):
    decision, _ = venus("Click(box=(500, 500))", frame)
    assert decision.actions == [Click(640, 360)]


@pytest.mark.parametrize("action", [
    "os.system('ls')",
    "__import__('os').system('ls')",
    "Click(box=__import__('os'))",
    "Click(box=(1, 2)",
    "Fly(box=(1, 2))",
    "Click()",  # no cursor position yet
    "Click(box=(1, 2, 3))",
    "Swipe(amount=0, axis='vertical')",
    "Swipe(amount=3, axis='diagonal')",
    "Hotkey(keys=[])",
    "KeyDown(keys=['shift'])",
    "Sequence(actions=[Sequence(actions=[Wait(), Wait()]), Wait()])",
    "Sequence(actions=Wait())",
    "MouseUp()",
    "Type(content=5)",
])
def test_venus_malformed_gives_no_action(frame, action):
    decision, _ = venus(act(action), frame)
    assert decision.actions == [] and not decision.done


def test_venus_unparsed_counts(frame):
    decision, _ = venus(act("Click(box=(1, 2)"), frame)
    assert "[unparsed tool call x1]" in decision.thought and decision.raw["parse_errors"]


def test_venus_thinking_text(frame):
    # Thinking on: what the template's <think> held is reasoning, kept out of history.
    decision, _ = venus("I should open the map first.\n</think>\n<action>Hotkey(keys=['m'])</action>", frame,
                        thinking=True)
    assert decision.reasoning == "I should open the map first." and decision.thought == ""
    decision, _ = venus("<action>Hotkey(keys=['m'])</action>", frame, thinking=True,
                        message={"reasoning_content": "Open the map."})
    assert decision.reasoning == "Open the map." and decision.actions == [Key("m")]
    # Thinking off: the prompted <think> is the step's short thought, even if the server split it out.
    decision, _ = venus("<action>Hotkey(keys=['m'])</action>", frame, message={"reasoning_content": "Open the map."})
    assert decision.thought == "Open the map." and decision.reasoning == ""


def test_parse_action_rejects_code():
    with pytest.raises(ValueError):
        parse_action("Click(box=open('/etc/passwd'))")
    assert parse_action("Click((10, 20))") == [{"name": "Click", "arguments": {"box": (10, 20)}}]


# ---- over HTTP, like llama-server


class _Stub(BaseHTTPRequestHandler):
    requests: list = []
    reply: dict = {}

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).requests.append((self.path, dict(self.headers), body))
        data = json.dumps(self.reply).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


@pytest.fixture
def server():
    _Stub.requests = []
    httpd = HTTPServer(("127.0.0.1", 0), _Stub)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_port}/v1"
    httpd.shutdown()


@pytest.mark.parametrize("cls, config, content, message, expected", [
    (Qwen38Model, Qwen38Config(thinking=False), xml_call(action="left_click", coordinate=[500, 500]), {},
     [Click(640, 360)]),
    (Holo4Model, Holo4Config(thinking=True), "", {"tool_calls": [tool_call("hotkey_desktop", keys=["e"])],
                                                   "reasoning_content": "End turn."}, [Key("e")]),
    (VenusModel, VenusConfig(thinking=False), act("Hotkey(keys=['e'])"), {}, [Key("e")]),
])
def test_over_http(server, frame, cls, config, content, message, expected):
    _Stub.reply = completion(content, message=message)
    config.base_url = server
    decision = cls(config).decide(Observation(1, frame, []))
    assert decision.actions == expected
    assert decision.raw["timings"] == TIMINGS
    path, _, body = _Stub.requests[0]
    assert path == "/v1/chat/completions" and body["chat_template_kwargs"]["enable_thinking"] is config.thinking
    assert body["messages"][1]["content"]


def test_no_choices_raises(frame):
    model = VenusModel(transport=FakeTransport({"error": {"code": 500, "message": "failed to parse"}}))
    with pytest.raises(RuntimeError, match="no choices"):
        model.decide(Observation(1, frame, []))


def test_other_screen_sizes(frame):
    big = Image.new("RGB", (1920, 1080))
    decision, _ = holo(big, tool_call("click_desktop", element="x", x=500, y=1000))
    assert decision.actions == [Click(960, 1079)]
