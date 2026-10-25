import pytest
from PIL import Image


@pytest.fixture
def frame():
    return Image.new("RGB", (1280, 720), (30, 30, 40))
