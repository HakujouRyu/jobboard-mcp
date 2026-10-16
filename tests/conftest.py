import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from jobboard_mcp.config import Settings
from jobboard_mcp.http import HttpClient

FIXTURES = Path(__file__).parent / "fixtures"


def fixture_json(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


@pytest.fixture
async def http() -> AsyncIterator[HttpClient]:
    async def no_sleep(_seconds: float) -> None:
        return None

    client = HttpClient(Settings(), sleep=no_sleep)
    yield client
    await client.aclose()
