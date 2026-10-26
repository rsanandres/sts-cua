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


# ---- registry models


from sts_cua.models import registry
from sts_cua.models.factory import LOCAL_MODELS, adapter_class
from sts_cua.models.holo4 import Holo4Model
from sts_cua.models.qwen38 import Qwen38Model
from sts_cua.models.ui_venus import VenusModel

FAMILY_CLASSES = {"qwen3-vl": QwenModel, "qwen3.8": Qwen38Model, "holo4": Holo4Model, "ui-venus-2": VenusModel}


def test_local_models_come_from_the_registry():
    assert LOCAL_MODELS == {name: m.served_name for name, m in registry.MODELS.items()}
    assert LOCAL_MODELS["qwen3-vl-8b"] == "Qwen/Qwen3-VL-8B-Instruct-FP8"
    with pytest.raises(ValueError):
        adapter_class("nope")


def test_model_help_lists_registry_names():
    parser = argparse.ArgumentParser()
    add_model_args(parser)
    help_text = parser.format_help()
    for name in registry.MODELS:
        assert name in help_text


@pytest.mark.parametrize("name", list(registry.MODELS))
def test_builds_every_registry_model(monkeypatch, name):
    monkeypatch.delenv("VLLM_API_KEY", raising=False)
    monkeypatch.delenv("LLAMA_API_KEY", raising=False)
    entry = registry.MODELS[name]
    model = build_model(parse("--model", name))
    assert type(model) is FAMILY_CLASSES[entry.family]
    c = model.config
    assert (c.base_url, c.served_model, c.api_key) == ("http://localhost:8000/v1", entry.served_name, None)
    assert c.return_token_ids is (entry.server == "vllm")
    # Solo with no flag: the registry's thinking default; models without one send nothing.
    assert c.thinking is (True if entry.thinking else None)


@pytest.mark.parametrize("name", [n for n, m in registry.MODELS.items() if m.thinking])
def test_local_thinking_flag_and_coop_default(name):
    assert build_model(parse("--model", name, "--local-thinking", "off")).config.thinking is False
    on = build_model(parse("--model", name, "--local-thinking", "on", "--local-effort", "low"))
    assert on.config.thinking is True and on.config.reasoning_effort == "low"
    args = parse("--model", name)
    args.coop = True
    assert build_model(args).config.thinking is False  # co-op: off for speed unless asked
    args.local_thinking = "on"
    assert build_model(args).config.thinking is True


def test_local_thinking_on_without_a_thinking_mode():
    with pytest.raises(ValueError, match="no thinking mode"):
        build_model(parse("--model", "qwen3-vl-8b", "--local-thinking", "on"))
    assert build_model(parse("--model", "qwen3-vl-8b", "--local-thinking", "off")).config.thinking is None


def test_llama_model_flags(monkeypatch):
    monkeypatch.setenv("LLAMA_API_KEY", "k")
    monkeypatch.delenv("VLLM_API_KEY", raising=False)
    model = build_model(parse("--model", "holo4-27b", "--base-url", "http://pc:8080/v1", "--served-model", "holo",
                              "--temperature", "0.3", "--max-tokens", "2048", "--seed", "4", "--goal", "Win"))
    c = model.config
    assert (c.base_url, c.served_model, c.temperature, c.max_tokens, c.seed, c.goal, c.api_key) == (
        "http://pc:8080/v1", "holo", 0.3, 2048, 4, "Win", "k")
    assert model.request_params()["temperature"] == 0.3


@pytest.mark.parametrize("name, wording", [
    ("qwen3.8-27b", "0 to 1000 scale"), ("holo4-27b", "0-1000 grid"), ("ui-venus-2-9b", "(999, 999)"),
])
def test_prompt_file_for_registry_families(tmp_path, name, wording):
    path = tmp_path / "p.txt"
    path.write_text("Click it.\n\nCoordinates are pixels in the screenshot you were given, origin at the top left.\n")
    prompt = build_model(parse("--model", name, "--prompt-file", str(path))).config.system_prompt
    assert prompt.startswith("Click it.") and wording in prompt and "pixels in the screenshot" not in prompt
