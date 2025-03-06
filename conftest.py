import pytest
from aioresponses import aioresponses as aioresponses_


@pytest.fixture
def aioresponses():
    with aioresponses_() as m:
        yield m
