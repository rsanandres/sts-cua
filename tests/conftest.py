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
