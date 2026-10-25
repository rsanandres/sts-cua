import pytest
from anthropic.types import Message

from sts_cua.actions import Click, Drag, Key, Scroll, TypeText, Wait
from sts_cua.models.base import HistoryEntry, Observation
from sts_cua.models.claude import ClaudeConfig, ClaudeModel, fit_scale, thinking_params


def message(content, stop_reason="tool_use", usage=None):
    return Message.model_validate({
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": "claude-haiku-5-5",
        "content": content,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": usage or {"input_tokens": 1196, "output_tokens": 50, "cache_read_input_tokens": 4520, "cache_creation_input_tokens": 0},
    })


def tool(name, input, id="toolu_1"):
    return {"type": "tool_use", "id": id, "name": name, "toolset_name": "computer", "input": input}


class FakeClient:
    def __init__(self, response):
        self.response = response
        self.requests = []
        self.messages = self

    def create(self, **kwargs):
        self.requests.append(kwargs)
        return self.response


def test_thinking_params():
    assert thinking_params("claude-haiku-5-5", "off", "low")["thinking"] == {"type": "disabled"}
    assert thinking_params("claude-sonnet-5-5", "off", "low")["thinking"] == {"type": "between_tools"}
    on = thinking_params("claude-sonnet-5-5", "on", "medium")
    assert on["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert on["output_config"] == {"effort": "medium"}
    with pytest.raises(ValueError):
        thinking_params("claude-opus-5-5", "off", "low")
    with pytest.raises(ValueError):
        thinking_params("claude-haiku-5-5", "off", "max")


def test_fit_scale():
    assert fit_scale(1280, 720) == 1.0
    assert fit_scale(1920, 1080) == 1.0  # 69 x 39 = 2,691 visual tokens, inside the limit
    scale = fit_scale(3840, 2160)
    w, h = 3840 * scale, 2160 * scale
    assert max(w, h) <= 2576
    assert -(-int(w) // 28) * -(-int(h) // 28) <= 4784


def test_request_shape(frame):
    client = FakeClient(message([tool("wait", {"duration": 1})]))
    model = ClaudeModel(ClaudeConfig(), client=client)
    history = [HistoryEntry(1, "opened map", [Click(10, 20)])]
    model.decide(Observation(2, frame, history))
    req = client.requests[0]
    assert req["model"] == "claude-haiku-5-5"
    assert req["thinking"] == {"type": "disabled"}
    assert req["system"][0]["cache_control"] == {"type": "ephemeral"}
    toolset = req["tools"][0]
    assert toolset["type"] == "computer_toolset_20260801"
    assert toolset["configs"]["screenshot"] == {"enabled": False}
    content = req["messages"][0]["content"]
    assert content[0]["type"] == "image"
    assert "Step 1: click at (10, 20) (opened map)" in content[1]["text"]
    assert "action only" in content[1]["text"]


def test_parse_actions_and_cost(frame):
    response = message([
        {"type": "text", "text": "Strike the cultist."},
        tool("left_click", {"coordinate": [600, 650]}, "t1"),
        tool("left_click", {"coordinate": [900, 300]}, "t2"),
        tool("double_click", {"coordinate": [5000, -3]}, "t3"),
        tool("left_click_drag", {"start_coordinate": [1, 2], "coordinate": [3, 4]}, "t4"),
        tool("scroll", {"scroll_direction": "down", "scroll_amount": 2}, "t5"),
        tool("key", {"text": "Escape"}, "t6"),
        tool("type", {"text": "hi"}, "t7"),
        tool("wait", {"duration": 2}, "t8"),
        tool("left_click", {}, "t9"),
    ])
    model = ClaudeModel(ClaudeConfig(), client=FakeClient(response))
    decision = model.decide(Observation(1, frame, []))
    assert decision.actions == [
        Click(600, 650),
        Click(900, 300),
        Click(1279, 0, count=2),  # clamped to the screen
        Drag(1, 2, 3, 4),
        Scroll("down", 2),
        Key("Escape"),
        TypeText("hi"),
        Wait(2.0),
    ]
    assert decision.thought.startswith("Strike the cultist.")
    assert "skipped unsupported: left_click" in decision.thought
    # 1196 input at $0.10/M + 50 output at $0.50/M + 4520 cache reads at $0.01/M
    assert decision.usage.cost_usd == pytest.approx((1196 * 0.10 + 50 * 0.50 + 4520 * 0.01) / 1e6)


def test_coordinates_scale_back_to_screen():
    from PIL import Image

    big = Image.new("RGB", (3840, 2160))
    client = FakeClient(message([tool("left_click", {"coordinate": [100, 100]})]))
    model = ClaudeModel(ClaudeConfig(), client=client)
    decision = model.decide(Observation(1, big, []))
    sent = client.requests[0]["messages"][0]["content"][1]["text"]
    scale = fit_scale(3840, 2160)
    assert f"{round(3840 * scale)}x{round(2160 * scale)}" in sent
    assert decision.actions == [Click(round(100 / scale), round(100 / scale))]


def test_refusal_has_no_actions(frame):
    model = ClaudeModel(ClaudeConfig(), client=FakeClient(message([], stop_reason="refusal")))
    decision = model.decide(Observation(1, frame, []))
    assert decision.actions == []
    assert decision.stop_reason == "refusal"


def test_thinking_summary_is_kept(frame):
    response = message([
        {"type": "thinking", "thinking": "Enemy intends 12; block first.", "signature": "sig"},
        {"type": "text", "text": "Defend."},
        tool("left_click", {"coordinate": [500, 650]}),
    ])
    model = ClaudeModel(ClaudeConfig(model="claude-sonnet-5-5", thinking="on", effort="medium"), client=FakeClient(response))
    decision = model.decide(Observation(1, frame, []))
    assert decision.reasoning == "Enemy intends 12; block first."
    assert decision.thought == "Defend."


def test_send_width_shrinks_and_maps_back(frame):
    client = FakeClient(message([tool("left_click", {"coordinate": [480, 270]})]))
    model = ClaudeModel(ClaudeConfig(send_width=960), client=client)
    decision = model.decide(Observation(1, frame, []))
    assert "Screenshot is 960x540" in client.requests[0]["messages"][0]["content"][1]["text"]
    assert decision.actions == [Click(640, 360)]


def test_goal_and_shortcuts_reach_the_model(frame):
    client = FakeClient(message([tool("key", {"text": "1"})]))
    model = ClaudeModel(ClaudeConfig(goal="Start a singleplayer run"), client=client)
    decision = model.decide(Observation(1, frame, []))
    req = client.requests[0]
    assert req["messages"][0]["content"][1]["text"].startswith("Goal: Start a singleplayer run")
    assert "1 to 9, then 0" in req["system"][0]["text"]
    assert decision.actions == [Key("1")]
