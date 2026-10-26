import pytest
from PIL import Image


@pytest.fixture
def frame():
    return Image.new("RGB", (1280, 720), (30, 30, 40))


@pytest.fixture
def app_server(tmp_path):
    from appfakes import start_server, stop_server

    running = start_server(tmp_path)
    yield running
    stop_server(running)


@pytest.fixture(autouse=True)
def model_dirs(tmp_path, monkeypatch):
    """Keep serve.py away from the real Hugging Face cache and models dir."""
    monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path / "hf-hub"))
    monkeypatch.delenv("STS_CUA_MODELS", raising=False)
    monkeypatch.delenv("LLAMA_SERVER", raising=False)
