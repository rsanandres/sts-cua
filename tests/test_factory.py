import argparse

import pytest

from sts_cua.actions import Wait
from sts_cua.models.base import ScriptedModel
from sts_cua.models.claude import ClaudeModel
from sts_cua.models.factory import add_model_args, build_model
from sts_cua.models.qwen import QwenModel


def parse(*argv, max_steps=True):
    parser = argparse.ArgumentParser()
    if max_steps:
        parser.add_argument("--max-steps", type=int, default=5)
    add_model_args(parser)
    return parser.parse_args(list(argv))


def test_scripted():
    model = build_model(parse("--model", "scripted"))
    assert isinstance(model, ScriptedModel)
    assert model._script == [[Wait(0.1)]] * 5
    assert isinstance(build_model(parse("--model", "scripted", max_steps=False)), ScriptedModel)


def test_claude(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    model = build_model(parse("--model", "claude-sonnet-5-5", "--thinking", "on", "--effort", "medium",
                              "--goal", "Win", "--send-width", "960", "--history-steps", "4"))
    assert isinstance(model, ClaudeModel)
    c = model.config
    assert (c.model, c.thinking, c.effort, c.goal, c.send_width, c.history_steps, c.max_tokens) == (
        "claude-sonnet-5-5", "on", "medium", "Win", 960, 4, None)


def test_claude_is_the_default(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    model = build_model(parse())
    assert isinstance(model, ClaudeModel) and model.config.model == "claude-haiku-5-5"


def test_local_model(monkeypatch):
    monkeypatch.setenv("VLLM_API_KEY", "k")
    model = build_model(parse("--model", "qwen3-vl-8b", "--base-url", "http://pc:8000/v1", "--temperature", "0",
                              "--goal", "Win", "--max-tokens", "256", "--seed", "3"))
    assert isinstance(model, QwenModel)
    c = model.config
    assert (c.base_url, c.served_model, c.temperature, c.goal, c.max_tokens, c.seed, c.api_key) == (
        "http://pc:8000/v1", "Qwen/Qwen3-VL-8B-Instruct-FP8", 0.0, "Win", 256, 3, "k")


def test_local_model_defaults_and_served_name(monkeypatch):
    monkeypatch.delenv("VLLM_API_KEY", raising=False)
    model = build_model(parse("--model", "qwen3-vl-8b", "--served-model", "sts-lora-v1"))
    c = model.config
    assert (c.base_url, c.served_model, c.temperature, c.max_tokens, c.api_key) == (
        "http://localhost:8000/v1", "sts-lora-v1", 0.7, 512, None)


@pytest.mark.parametrize("model", ["claude-haiku-5-5", "qwen3-vl-8b"])
def test_prompt_file(monkeypatch, tmp_path, model):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    path = tmp_path / "p.txt"
    path.write_text("Click it.\n\nCoordinates are pixels in the screenshot you were given, origin at the top left.\n")
    built = build_model(parse("--model", model, "--prompt-file", str(path)))
    prompt = built.config.system_prompt
    assert prompt.startswith("Click it.")
    if model == "qwen3-vl-8b":
        assert "0 to 1000 scale" in prompt
    else:
        assert "pixels in the screenshot" in prompt
